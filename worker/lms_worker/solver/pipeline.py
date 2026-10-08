"""Solving downloaded assignments, one at a time, in the sandbox.

A DOWNLOADED job is picked when its course is in approve or auto mode, it is an assignment
(lab papers are never solved), its deadline has not passed and no pause holds it back; the
nearest deadline goes first. One Claude run reads the theme artifact, solves the sheet, builds
report.docx and report.pdf and checks every page (see prompt.py). Then, by Claude's summary:

- done: the PDF comes to you. In approve mode it waits for your approval; in auto mode it
  goes to the upload.
- handwritten: the PDF comes to you to copy by hand. It waits for your approval in every
  mode, auto too: it is uploaded only if you approve it as it is.
- blocked: the job stops with Claude's reason (NEEDS_HUMAN).

Both formats are kept next to the question file and, with Google Drive connected, saved to
"University Assignments/<semester>/<course>/<report name>" on your Drive with the question.

A run stopped by the plan's limit, or about to draw paid extra usage, waits for the reset and
then resumes the same Claude session. Any other failure is retried once, resuming too. When
Claude is signed out (the session expired, or it was never signed in), solving waits and one
message links the setup page; it resumes by itself after you sign in there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..actions import EFFORT_KEY, MODEL_KEY
from ..db.models import Artifact, Assignment, Course, CourseMode, Event, Job, JobState, utcnow
from ..kv import KeyValue
from ..lms.bahria_pages import LMS_TZ
from ..lms.models import PAPER
from ..notify import Outbox
from .claude import Outcome, StreamReader
from .prompt import SUMMARY_SCHEMA, THEME_ARTIFACT, Brief, system_prompt, task_prompt, course_prompt
from .sandbox import Sandbox, SandboxRun
from .usage import Pause, UsageGuard

log = logging.getLogger(__name__)

SIGNED_OUT_KEY = "claude_signed_out"
POLL_NOW_KEY = "poll_requested"  # the watcher's: an auto-mode upload should not wait for the next poll
RUN_TIMEOUT = 90 * 60
MAX_TURNS = 250
MAX_ATTEMPTS = 2  # a failed run is retried once
RETRY_AFTER = timedelta(minutes=10)
WARN_WITHIN = timedelta(hours=3)  # a pause ending this close to a deadline gets a warning
IDLE_SECONDS = 60
DRIVE_ROOT = "University Assignments"
SIGNED_OUT = re.compile(
    r"not logged in|please run /login|invalid api key|invalid bearer token|authentication_error"
    r"|oauth token (?:has )?(?:expired|been revoked)|\b401\b", re.IGNORECASE)
NO_SESSION = re.compile(r"no conversation found", re.IGNORECASE)
CONTINUE = "Continue where you stopped: finish the report, check every page of the PDF, and end with the summary."


class Login(Protocol):
    """ClaudeLogin, as far as solving needs it."""

    def status(self): ...
    def stamp(self) -> str | None: ...


class Drive(Protocol):
    def save(self, folders: list[str], files: list[Path]) -> str:
        """Upload files into the folder path, creating folders as needed; returns the folder's link."""


@dataclass
class SolveResult:
    status: str  # idle, paused, signed_out, solved, handwritten, blocked, retry, failed, stopped
    job_id: int | None = None
    detail: str = ""


class Solver:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        kv: KeyValue,
        outbox: Outbox,
        sandbox: Sandbox,
        data_dir: Path,
        *,
        guard: UsageGuard | None = None,
        login: Login | None = None,
        drive: Drive | None = None,
        now: Callable[[], datetime] = utcnow,
        public_url: str | None = None,
        model: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = RUN_TIMEOUT,
        max_turns: int = MAX_TURNS,
    ) -> None:
        self.sessions, self.kv, self.outbox, self.sandbox = sessions, kv, outbox, sandbox
        self.data_dir = data_dir
        self.guard = guard or UsageGuard(kv)
        self.login, self.drive = login, drive
        self.now = now
        self.public_url = public_url
        self.model = model
        self.env = env or {}  # e.g. HTTPS_PROXY, the sandbox's way out
        self.timeout, self.max_turns = timeout, max_turns

    # --- the loop ---

    def recover(self) -> int:
        """Jobs left in SOLVING by a restart go back to the queue; their Claude session resumes."""
        with self.sessions.begin() as s:
            jobs = s.scalars(select(Job).where(Job.state == JobState.SOLVING)).all()
            for job in jobs:
                job.attempts = max(0, job.attempts - 1)
                _move(s, job, JobState.DOWNLOADED, "the worker restarted while solving; queued again")
            return len(jobs)

    def run_forever(self, stop: threading.Event) -> None:
        self.recover()
        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception:  # a bug in one run must not stop solving
                log.exception("Solver run crashed")
                result = SolveResult("failed")
            if result.status in ("idle", "paused", "signed_out", "failed"):
                stop.wait(IDLE_SECONDS)

    def run_once(self) -> SolveResult:
        result = self._run_once()
        if result.status != "idle":
            self.outbox.flush(self.sessions)  # the report, a pause or a sign-in message goes out now
        return result

    def _run_once(self) -> SolveResult:
        now = self.now()
        with self.sessions() as s:
            if s.scalars(self._ready(now).limit(1)).first() is None:
                return SolveResult("idle")
        if not self._signed_in():
            return SolveResult("signed_out")
        pause = self.guard.paused(now)
        if pause:
            self._warn_deadlines(pause)
            return SolveResult("paused", detail=pause.describe())
        claimed = self._claim(now)
        if claimed is None:
            return SolveResult("idle")
        job_id, work, prompt, resume = claimed
        with self.sessions() as s:
            course = s.get(Job, job_id).assignment.course
            own, theme = (course.prompt, course.prompt_replaces), course.theme
        self._write_job(work, prompt, resume, own, theme)
        self.guard.count_run(now)

        reader = StreamReader()
        pauses: list[Pause] = []
        overage = False

        def on_line(line: str) -> bool:
            nonlocal overage
            rl = reader.feed(line)
            if rl is not None:
                pause = self.guard.record(rl, self.now())
                if pause:
                    pauses.append(pause)
                if rl.using_overage:  # automation never spends money: stop now, resume after the reset
                    overage = True
                    return False
            return True

        log.info("Solving job %s%s", job_id, " (resuming)" if resume else "")
        run = self.sandbox.run(work, self.env, on_line, self.timeout)
        return self._finish(job_id, work, run, reader.outcome, pauses, overage)

    # --- picking a job ---

    @staticmethod
    def _ready(now: datetime):
        return (
            select(Job)
            .join(Job.assignment)
            .join(Assignment.course)
            .where(
                Job.state == JobState.DOWNLOADED,
                Assignment.kind != PAPER,
                Course.mode != CourseMode.NOTIFY,
                Assignment.deadline_passed.is_(False),
                or_(Assignment.deadline.is_(None), Assignment.deadline > now),
                or_(Job.not_before.is_(None), Job.not_before <= now),
            )
            .order_by(Assignment.deadline.asc().nulls_last(), Job.id)
        )

    def _claim(self, now: datetime) -> tuple[int, Path, str, str | None] | None:
        with self.sessions() as s:
            job = s.scalars(self._ready(now).limit(1).with_for_update(of=Job, skip_locked=True)).first()
            if job is None:
                return None
            a = job.assignment
            work = self.job_dir(a, job) / "work"
            redo = bool(job.note) and job.summary is not None
            # A run that ended blocked starts over: its session has already concluded it cannot go on
            blocked = job.summary is not None and json.loads(job.summary).get("status") == "blocked"
            resume = job.claude_session if job.claude_session and (work / "job.json").exists() and not blocked else None
            if not resume:
                if work.exists():
                    shutil.rmtree(work)
                (work / "in").mkdir(parents=True)
                for art in job.artifacts:
                    if art.kind == "question" and Path(art.path).is_file():
                        shutil.copy2(art.path, work / "in" / Path(art.path).name)
            if resume and redo:
                prompt = (f"The student asked for changes: {job.note}\nMake them, rebuild report.docx and report.pdf, "
                          "check every page again, and end with the summary.")
            elif resume:
                prompt = CONTINUE
            else:
                files = sorted(p.name for p in (work / "in").iterdir() if p.is_file())
                prompt = task_prompt(Brief(course=a.course.name, semester=a.course.semester_name or a.course.semester,
                                           title=a.title, number=a.number, deadline=a.deadline, remarks=a.remarks,
                                           files=files, on=now, note=job.note))
            if redo:
                job.summary = None  # a resumed redo continues as a plain run
            job.attempts += 1
            job.not_before = None
            _move(s, job, JobState.SOLVING, "solving" + (" (resuming the Claude session)" if resume else ""))
            s.commit()
            return job.id, work, prompt, resume

    def job_dir(self, a: Assignment, job: Job) -> Path:
        return self.data_dir / _safe(a.course.code) / str(a.id) / str(job.revision)

    def _write_job(self, work: Path, prompt: str, resume: str | None, own: tuple[str | None, bool] = (None, False),
                   theme: str | None = None) -> None:
        (work / "out").mkdir(parents=True, exist_ok=True)
        system = course_prompt(system_prompt(self.sandbox.work_path(work), theme or THEME_ARTIFACT), *own)
        spec = {"prompt": prompt, "system": system, "schema": SUMMARY_SCHEMA,
                "max_turns": self.max_turns}
        model = (self.kv.get(MODEL_KEY) or "").strip() or self.model  # the chat's choice, else SOLVER_MODEL
        if model:
            spec["model"] = model
        effort = (self.kv.get(EFFORT_KEY) or "").strip()
        if effort:
            spec["effort"] = effort
        if resume:
            spec["resume"] = resume
        (work / "job.json").write_text(json.dumps(spec))
        for stale in ("report.pdf", "report.docx", "stderr.txt"):
            (work / "out" / stale).unlink(missing_ok=True)

    # --- after the run ---

    def _finish(self, job_id: int, work: Path, run: SandboxRun, o: Outcome, pauses: list[Pause], overage: bool) -> SolveResult:
        now = self.now()
        stderr = _tail(work / "out" / "stderr.txt")
        trouble = " ".join(filter(None, [o.text if (o.is_error or not o.finished) else "", stderr, run.error]))
        summary = o.structured if o.finished and not o.is_error else None
        with self.sessions() as s:
            job = s.get(Job, job_id, with_for_update=True)
            a = job.assignment
            job.claude_session = o.session_id or job.claude_session
            job.cost_tokens += o.tokens
            job.cost_usd = (job.cost_usd or 0) + o.cost_usd
            for p in pauses:
                self.outbox.queue(s, "alert", p.describe() + _held(s, now, p))
            if job.state != JobState.SOLVING:  # stopped meanwhile: a submission appeared or the deadline passed
                s.add(Event(job_id=job.id, detail=f"the run ended after the job was {job.state}"))
                s.commit()
                return SolveResult("stopped", job.id)

            if summary is None:
                if NO_SESSION.search(trouble) and job.claude_session:
                    job.claude_session = None
                    return self._requeue(s, job, None, "the saved Claude session was gone; starting over", counts=False)
                if SIGNED_OUT.search(trouble):
                    self._requeue(s, job, None, "Claude is signed out", counts=False)
                    self._signed_out(trouble)
                    return SolveResult("signed_out", job_id)
                if o.usage_limited or overage:
                    pause = self.guard.paused(now)
                    until = pause.until if pause else now + timedelta(hours=1)
                    return self._requeue(s, job, until, f"usage limit; resumes {_pkt(until)}", counts=False)
                reason = _why(run, o, stderr, self.timeout)
                if job.attempts < MAX_ATTEMPTS:
                    return self._requeue(s, job, now + RETRY_AFTER, f"run failed ({reason}); retrying")
                return self._give_up(s, job, reason)

            job.summary = json.dumps(summary)
            if summary.get("status") == "blocked":
                reason = summary.get("blocked_reason") or "Claude could not do this one here"
                return self._give_up(s, job, reason, blocked=True)
            pdf, docx = work / "out" / "report.pdf", work / "out" / "report.docx"
            if not pdf.is_file():
                reason = "the run ended without report.pdf"
                if job.attempts < MAX_ATTEMPTS:
                    return self._requeue(s, job, now, f"{reason}; retrying")
                return self._give_up(s, job, reason)
            name = report_name(summary, a)
            files = self._store(s, job, work.parent, name, pdf, docx)
            job.last_error = None
            job.attempts = 0  # from here on, attempts count uploads
            handwritten = summary.get("status") == "handwritten"
            if a.course.mode == CourseMode.AUTO and not handwritten:
                to, status = JobState.SUBMITTING, "solved"
            else:  # a typed report for a sheet that asks for handwriting is never uploaded unasked
                to, status = JobState.AWAITING_APPROVAL, "handwritten" if handwritten else "solved"
            _move(s, job, to, f"solved: {len(summary.get('exercises') or [])} part(s), {summary.get('pages') or '?'} page(s)")
            questions = [Path(x.path) for x in job.artifacts if x.kind == "question" and Path(x.path).is_file()]
            folders = [DRIVE_ROOT, a.course.semester_name or a.course.semester, a.course.name, name]
            s.commit()
            course_name, deadline = a.course.name, a.deadline

        saved, drive_error = self._save_to_drive(folders, questions + files)
        with self.sessions.begin() as s:
            s.add(Event(job_id=job_id, detail=f"saved to Google Drive: {saved}" if saved else drive_error or "solved"))
            text = _solved_text(summary, name, course_name, deadline, to, folders if saved else None, drive_error)
            self.outbox.queue(s, "solved", text, file=files[-1], job_id=job_id)
        if to == JobState.SUBMITTING:
            self.kv.set(POLL_NOW_KEY, now.isoformat())
        return SolveResult(status, job_id)

    def _requeue(self, s: Session, job: Job, not_before: datetime | None, detail: str, *, counts: bool = True) -> SolveResult:
        if not counts:
            job.attempts = max(0, job.attempts - 1)
        job.not_before = not_before
        job.last_error = detail
        _move(s, job, JobState.DOWNLOADED, detail)
        s.commit()
        return SolveResult("retry", job.id, detail)

    def _give_up(self, s: Session, job: Job, reason: str, *, blocked: bool = False) -> SolveResult:
        a = job.assignment
        job.last_error = reason
        _move(s, job, JobState.NEEDS_HUMAN, ("cannot be solved here: " if blocked else "solving failed: ") + reason)
        head = "Can't be solved here" if blocked else "Solving failed"
        self.outbox.queue(s, "stopped", f"{head}: {a.course.name} · {a.title}\n{reason}\n"
                                        "The question file is in the menu; this one is yours to do.", job_id=job.id)
        s.commit()
        return SolveResult("blocked" if blocked else "failed", job.id, reason)

    def _store(self, s: Session, job: Job, job_dir: Path, name: str, pdf: Path, docx: Path) -> list[Path]:
        """Copy the report next to the question file as '<name>.docx' and '<name>.pdf' (PDF last)."""
        out = []
        for src, kind in ((docx, "docx"), (pdf, "pdf")):
            if not src.is_file():
                continue
            dest = job_dir / f"{name}{src.suffix}"
            shutil.copy2(src, dest)
            for old in [x for x in job.artifacts if x.kind == kind]:
                job.artifacts.remove(old)
                s.delete(old)
            data = dest.read_bytes()
            s.add(Artifact(job=job, kind=kind, path=str(dest), sha256=hashlib.sha256(data).hexdigest(), size=len(data)))
            out.append(dest)
        return out

    def _save_to_drive(self, folders: list[str], files: list[Path]) -> tuple[str | None, str | None]:
        """(the folder's link, None) when saved; (None, why) when it failed; (None, None) without Drive."""
        if self.drive is None:
            return None, None
        try:
            return self.drive.save(folders, files), None
        except Exception as e:  # Drive trouble never holds up the report
            log.warning("Saving to Google Drive failed: %s", e)
            return None, f"Not saved to Google Drive: {e}"

    # --- sign-in and pauses ---

    def _signed_in(self) -> bool:
        if self.login is None:
            return True
        raw = self.kv.get(SIGNED_OUT_KEY)
        if raw and json.loads(raw).get("stamp") == self.login.stamp():
            return False  # nothing changed since the sign-out was noticed
        status = self.login.status()
        if status.logged_in:
            if raw:
                self.kv.delete(SIGNED_OUT_KEY)
                with self.sessions.begin() as s:
                    who = f" as {status.email}" if status.email else ""
                    self.outbox.queue(s, "alert", f"Claude is signed in again{who}. Solving resumes.")
            return True
        self._signed_out(status.detail or "Claude Code is not signed in")
        return False

    def _signed_out(self, reason: str) -> None:
        first = self.kv.get(SIGNED_OUT_KEY) is None
        stamp = self.login.stamp() if self.login else None
        self.kv.set(SIGNED_OUT_KEY, json.dumps({"at": self.now().isoformat(), "reason": reason[:300], "stamp": stamp}))
        if first:
            where = f"{self.public_url}/setup" if self.public_url else "the setup page (/setup on the api's address)"
            with self.sessions.begin() as s:
                s.add(Event(detail=f"claude signed out: {reason[:300]}"))
                self.outbox.queue(s, "alert", f"Claude is signed out, so solving is waiting.\n"
                                              f"Sign in again at {where}: Sign in to Claude. Solving resumes by itself.")

    def _warn_deadlines(self, pause: Pause) -> None:
        """Once per job: its deadline comes before the pause ends, or soon after."""
        now = self.now()
        with self.sessions.begin() as s:
            for job in s.scalars(self._ready(now)).all():
                a = job.assignment
                key = f"warned:usage:{job.id}"
                if a.deadline is None or a.deadline > pause.until + WARN_WITHIN or self.kv.get(key):
                    continue
                self.kv.set(key, pause.until.isoformat())
                self.outbox.queue(s, "alert", f"Heads-up: {a.course.name} · {a.title} is due {_pkt(a.deadline)}.\n"
                                              f"{pause.describe()}\nIt may not be solved in time; do it yourself if it can't wait.",
                                  job_id=job.id)


# --- helpers ---


def _move(s: Session, job: Job, to: JobState, detail: str) -> None:
    s.add(Event(job_id=job.id, from_state=job.state, to_state=to, detail=detail))
    job.state = to


def _held(s: Session, now: datetime, pause: Pause) -> str:
    waiting = s.scalars(Solver._ready(now)).all()
    return f"\n{len(waiting)} assignment(s) waiting to be solved." if waiting else ""


def _why(run: SandboxRun, o: Outcome, stderr: str, timeout: float) -> str:
    if run.error:
        return f"the sandbox failed: {run.error}"
    if run.timed_out:
        return f"it ran past {int(timeout // 60)} minutes"
    if o.turn_capped:
        return "it used up its turns"
    if not o.finished:
        return "Claude Code stopped without an answer" + (f": {stderr[-300:]}" if stderr else "")
    if o.is_error:
        return f"Claude Code reported an error: {o.text[:300]}"
    return "Claude did not return the summary"


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(errors="replace")[-limit:].strip()
    except OSError:
        return ""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "course"


def report_name(summary: dict, a: Assignment) -> str:
    """'Lab 05 - Pipelining' or 'Assignment 01 - Recurrence Relations'."""
    kind = "Lab" if summary.get("kind") == "lab" else "Assignment"
    number = summary.get("number")
    if not isinstance(number, int):
        digits = re.sub(r"\D", "", a.number or "")
        number = int(digits) if digits else 0
    title = str(summary.get("title") or a.title)
    name = f"{kind} {number:02d} - {title}"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)
    return re.sub(r"\s+", " ", name).strip()[:120]


def _pkt(dt: datetime) -> str:
    return dt.astimezone(LMS_TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _solved_text(summary: dict, name: str, course: str, deadline: datetime | None, to: JobState,
                 folders: list[str] | None, drive_error: str | None) -> str:
    handwritten = summary.get("status") == "handwritten"
    head = "Solved, handwritten" if handwritten else "Solved"
    lines = [f"{head}: {name}", course, f"Due {_pkt(deadline)}" if deadline else "No deadline shown"]
    parts = summary.get("exercises") or []
    pages = summary.get("pages")
    lines.append(f"{len(parts)} part(s)" + (f", {pages} page(s)" if pages else ""))
    for p in parts[:8]:
        lines.append(f"• {p.get('title', '')}: {p.get('result', '')}")
    if summary.get("notes_for_student"):
        lines.append(f"Note: {summary['notes_for_student']}")
    if folders:
        lines.append("Saved to Google Drive: " + " › ".join(folders))
    elif drive_error:
        lines.append(drive_error)
    if to == JobState.SUBMITTING:
        lines.append("Auto mode: uploading it now; the LMS receipt will follow.")
    elif handwritten:
        lines.append("The sheet asks for handwritten work, so it was not uploaded: copy this by hand. "
                     "To upload this report as it is, reply menu, then Waiting for your approval.")
    else:
        lines.append("To upload it, reply menu, then Waiting for your approval.")
    return "\n".join(lines)
