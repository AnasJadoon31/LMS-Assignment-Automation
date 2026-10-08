"""Notes of the lectures you picked: asked for in the chat, written in the sandbox. One document
that explains what the slides only list, so the lectures can be learnt from it alone.

The lecture files are already on the server (the chat fetched them when you asked). One job:

1. Haiku skims the slides' text and decides which model writes the notes (Sonnet, or Opus for
   hard material), with what effort, and whether the notes need the slides' diagrams. Skipped
   when you fixed a model in Settings.
2. That model reads the text (and the figures only when they matter), writes the notes in
   Markdown and builds the PDF with lms-notes, which holds the one fixed layout.
3. The PDF comes to you, and goes to "University Assignments/<semester>/<course>/Lecture
   summaries" on Google Drive when it is connected.

It runs when no assignment is waiting to be solved, counts as a solver run for the daily limit,
waits like a solve when Claude is signed out or the plan's limit is reached, and is retried once.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import files
from ..actions import EFFORT_KEY
from ..db.models import Event, Summary, SummaryState
from .prompt import NOTES_SCHEMA, NOTES_TRIAGE_SCHEMA, notes_prompt, notes_system, notes_triage_prompt

log = logging.getLogger(__name__)

RUN_TIMEOUT = 45 * 60
MAX_TURNS = 120
MAX_ATTEMPTS = 2
RETRY_AFTER = timedelta(minutes=10)
CONTINUE = "Continue where you stopped: finish notes.md, build the PDF with lms-notes, look at the pages, and end with the summary."


class Summarizer:
    def __init__(self, solver) -> None:
        self.solver = solver  # the Solver: its sandbox, usage guard, outbox, Drive and Haiku runs

    @staticmethod
    def _ready(now: datetime):
        return (select(Summary).where(Summary.state == SummaryState.QUEUED,
                                      or_(Summary.not_before.is_(None), Summary.not_before <= now)).order_by(Summary.id))

    def waiting(self, s: Session, now: datetime) -> bool:
        return s.scalars(self._ready(now).limit(1)).first() is not None

    def recover(self) -> None:
        with self.solver.sessions.begin() as s:
            for sm in s.scalars(select(Summary).where(Summary.state == SummaryState.RUNNING)).all():
                sm.state, sm.attempts = SummaryState.QUEUED, max(0, sm.attempts - 1)

    def run_next(self, now: datetime):
        from .pipeline import DRIVE_ROOT, SIGNED_OUT, SolveResult, _pkt, _tail, _why

        v = self.solver
        with v.sessions() as s:
            sm = s.scalars(self._ready(now).limit(1).with_for_update(skip_locked=True)).first()
            if sm is None:
                return SolveResult("idle")
            course = sm.course
            lectures = json.loads(sm.lectures)
            sm.state, sm.attempts = SummaryState.RUNNING, sm.attempts + 1
            sm_id, session, triage = sm.id, sm.claude_session, json.loads(sm.triage) if sm.triage else None
            base = files.course_dir(v.data_dir, course)
            name, semester = course.name, course.semester_name or course.semester
            s.commit()
        work = base / "summaries" / str(sm_id) / "work"
        sources = [base / "lectures" / x["file"] for x in lectures]
        missing = [p.name for p in sources if not p.is_file()]
        if missing:
            return self._fail(sm_id, name, lectures, f"lecture files are no longer on the server: {', '.join(missing)}")
        resume = session if session and (work / "job.json").exists() and (work / "notes.md").exists() else None
        if not resume:
            shutil.rmtree(work, ignore_errors=True)
            (work / "in").mkdir(parents=True)
            for p in sources:
                shutil.copy2(p, work / "in" / p.name)
        where = v.sandbox.work_path(work)
        tokens = 0

        if triage is None and v.routed():  # Haiku skims the text; its run also makes in/text and in/pages
            answer, o, trouble, overage = v.ask_haiku(work, notes_triage_prompt(name, [p.name for p in sources], where),
                                                      NOTES_TRIAGE_SCHEMA, text=True)
            tokens = o.tokens
            if answer is None and (SIGNED_OUT.search(trouble) or o.usage_limited or overage):
                return self._wait(sm_id, now, tokens, signed_out=trouble if SIGNED_OUT.search(trouble) else None)
            triage = ({k: answer[k] for k in ("model", "effort", "reason", "diagrams")} if answer
                      else {"failed": trouble[:200]})
            with v.sessions.begin() as s:
                s.get(Summary, sm_id).triage = json.dumps(triage)
        triage = triage or {}

        spec = {"prompt": CONTINUE if resume else notes_prompt(name, lectures),
                "system": notes_system(where, diagrams=triage.get("diagrams")),
                "schema": NOTES_SCHEMA, "max_turns": MAX_TURNS, "text": True, "plain": True,
                "prepared": (work / "in" / "text").is_dir()}
        model = v.fixed_model() or triage.get("model")
        effort = (v.kv.get(EFFORT_KEY) or "").strip() or triage.get("effort")
        spec |= {k: val for k, val in (("model", model), ("effort", effort), ("resume", resume)) if val}
        (work / "out").mkdir(exist_ok=True)
        for stale in ("notes.pdf", "stderr.txt"):
            (work / "out" / stale).unlink(missing_ok=True)
        (work / "job.json").write_text(json.dumps(spec))
        v.guard.count_run(now)
        log.info("Writing lecture summary %s with %s", sm_id, model or "the default model")
        run, o, pauses, overage = v.run_claude(work, RUN_TIMEOUT)
        tokens += o.tokens

        stderr = _tail(work / "out" / "stderr.txt")
        trouble = " ".join(filter(None, [o.text if (o.is_error or not o.finished) else "", stderr, run.error]))
        result = o.structured if o.finished and not o.is_error else None
        pdf = work / "out" / "notes.pdf"
        with v.sessions.begin() as s:
            sm = s.get(Summary, sm_id)
            sm.claude_session = o.session_id or sm.claude_session
            for p in pauses:
                v.outbox.queue(s, "alert", p.describe())
        if result is None or result.get("status") == "blocked" or not pdf.is_file():
            if result is None and (SIGNED_OUT.search(trouble) or o.usage_limited or overage):
                return self._wait(sm_id, now, tokens, signed_out=trouble if SIGNED_OUT.search(trouble) else None)
            reason = ((result or {}).get("blocked_reason") if result and result.get("status") == "blocked"
                      else "the run ended without notes.pdf" if result else _why(run, o, stderr, RUN_TIMEOUT))
            with v.sessions.begin() as s:
                sm = s.get(Summary, sm_id)
                sm.cost_tokens += tokens
                if sm.attempts < MAX_ATTEMPTS and not (result and result.get("status") == "blocked"):
                    sm.state, sm.not_before, sm.error = SummaryState.QUEUED, now + RETRY_AFTER, reason
                    return SolveResult("retry", detail=reason)
            return self._fail(sm_id, name, lectures, reason or "Claude could not write these notes")

        title = _safe_name(str(result.get("title") or f"{name} - {_weeks(lectures)}"))
        dest = base / "summaries" / f"{title}.pdf"
        shutil.copy2(pdf, dest)
        saved, drive_error = v._save_to_drive([DRIVE_ROOT, semester, name, "Lecture summaries"], [dest])
        with v.sessions.begin() as s:
            sm = s.get(Summary, sm_id)
            sm.state, sm.path, sm.error, sm.cost_tokens = SummaryState.DONE, str(dest), None, sm.cost_tokens + tokens
            s.add(Event(detail=f"lecture summary {sm_id} written: {dest.name}"))
            lines = [f"Lecture notes summary: {name}", _weeks(lectures) + (f", {result.get('pages')} page(s)" if result.get("pages") else "")]
            if model:
                lines.append(f"Written by {model}" + (f", {effort} effort" if effort else "") + (f": {triage['reason']}" if triage.get("reason") else ""))
            if saved:
                lines.append(f"Saved to Google Drive: {DRIVE_ROOT} › {semester} › {name} › Lecture summaries")
            elif drive_error:
                lines.append(drive_error)
            v.outbox.queue(s, "summary", "\n".join(lines), file=dest)
        return SolveResult("solved", detail=f"summary {sm_id}")

    def _wait(self, sm_id: int, now: datetime, tokens: int, *, signed_out: str | None):
        """Signed out, or the plan's limit: back in the queue, this try not counted."""
        from .pipeline import SolveResult

        v = self.solver
        with v.sessions.begin() as s:
            sm = s.get(Summary, sm_id)
            sm.state, sm.attempts, sm.cost_tokens = SummaryState.QUEUED, max(0, sm.attempts - 1), sm.cost_tokens + tokens
            if not signed_out:
                pause = v.guard.paused(now)
                sm.not_before = pause.until if pause else now + timedelta(hours=1)
        if signed_out:
            v._signed_out(signed_out)
            return SolveResult("signed_out")
        return SolveResult("retry", detail="usage limit")

    def _fail(self, sm_id: int, course: str, lectures: list[dict], reason: str):
        from .pipeline import SolveResult

        v = self.solver
        with v.sessions.begin() as s:
            sm = s.get(Summary, sm_id)
            sm.state, sm.error = SummaryState.FAILED, reason
            s.add(Event(detail=f"lecture summary {sm_id} failed: {reason}"))
            v.outbox.queue(s, "summary", f"The lecture summary could not be written: {course}, {_weeks(lectures)}\n{reason}")
        return SolveResult("failed", detail=reason)


def _weeks(lectures: list[dict]) -> str:
    weeks = list(dict.fromkeys(str(x["week"]) for x in lectures))
    return ("week " if len(weeks) == 1 else "weeks ") + ", ".join(weeks)


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)
    return re.sub(r"\s+", " ", name).strip()[:120] or "Lecture summary"
