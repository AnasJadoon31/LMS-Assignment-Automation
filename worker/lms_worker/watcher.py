"""One poll of the LMS: sync courses, detect new and changed assignments, download, notify.

Rules (from the spec):
- A new fingerprint creates a job. A changed fingerprint on a known assignment
  creates a new revision; an unfinished older revision is superseded.
- Anything already closed, or already showing a submission the service did not
  make, is recorded but gets no job. Your manual work is never touched.
- If a submission the service did not make appears while a job is unfinished,
  the job stops (NEEDS_HUMAN). So does a job whose deadline passes.
- A changed LMS layout pauses polling until `lms-worker resume`. A session that
  cannot be renewed, or a login that fails, alerts once and retries next poll.
- Exam papers (Papers.php) are tracked and announced with their file, never solved.
- Every poll records what the LMS shows for each item (submitted file, open or
  closed, marks), sends "marks posted" when marks appear, and reminds you 24 hours
  and 2 hours before a deadline that has nothing submitted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import notify
from .db.models import (
    TERMINAL_STATES,
    Artifact,
    Assignment,
    Course,
    CourseMode,
    Event,
    Job,
    JobState,
    utcnow,
)
from .kv import KeyValue
from .lms import models as lms
from .lms.models import PAPER
from .lms.adapter import LMSAdapter
from .lms.bahria_pages import LMS_TZ
from .lms.errors import BadCredentials, ContractBroken, LMSError, LoginError, ReloginPaused, SessionExpired

log = logging.getLogger(__name__)

POLL_PAUSE_KEY = "polling_paused"
POLL_NOW_KEY = "poll_requested"
HEARTBEAT_KEY = "last_poll"
DOWN_SINCE_KEY = "lms_down_since"
DOWNLOAD_ALERT_AFTER = 3
LMS_DOWN_ALERT_AFTER = 3
# Stopping a job the service has not worked on yet is not worth a message
QUIET_STATES = {JobState.DETECTED, JobState.DOWNLOADED}
REMINDER_HOURS = (24, 2)


@dataclass
class PollResult:
    status: str = "ok"  # ok, paused, session, contract, lms_down
    courses: int = 0
    seen: int = 0
    new_jobs: int = 0
    revisions: int = 0
    skipped: int = 0
    stopped: int = 0
    downloaded: int = 0
    papers: int = 0
    new_papers: int = 0
    marks_posted: int = 0
    reminders: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)


class Watcher:
    def __init__(
        self,
        adapter: LMSAdapter,
        sessions: sessionmaker[Session],
        kv: KeyValue,
        outbox: notify.Outbox,
        data_dir: Path,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.adapter = adapter
        self.sessions = sessions
        self.kv = kv
        self.outbox = outbox
        self.data_dir = data_dir
        self.now = now
        self.lms_down_count = 0

    # --- the cycle ---

    def poll_once(self) -> PollResult:
        result = PollResult()
        self.kv.delete(POLL_NOW_KEY)
        paused = self.kv.get(POLL_PAUSE_KEY)
        if paused:
            result.status = "paused"
            result.errors.append(paused)
        else:
            try:
                self._poll(result)
                self.lms_down_count = 0
                self.kv.delete(DOWN_SINCE_KEY)
                self._clear_alert("lms-down")
            except ContractBroken as e:
                result.status = "contract"
                result.errors.append(str(e))
                self.kv.set(POLL_PAUSE_KEY, str(e))
                self._alert("contract", f"The LMS page changed: {e}\nPolling is paused. Check the page, then run lms-worker resume.")
            except ReloginPaused as e:
                result.status = "session"
                result.errors.append(str(e))
                self._alert("session", f"LMS login is paused: {e}")
            except BadCredentials as e:
                result.status = "session"
                result.errors.append(str(e))
                self._alert("session", f"CMS rejected the login: {e}. Automatic login stops after 2 failures.")
            except SessionExpired as e:
                result.status = "session"
                result.errors.append(str(e))
                self._alert("session", f"The LMS session ended: {e}")
            except LoginError as e:
                result.status = "session"
                result.errors.append(str(e))
                self._alert("session", f"CMS login failed: {e}")
            except httpx.HTTPError as e:
                result.status = "lms_down"
                result.errors.append(f"{type(e).__name__}: {e}")
                self._lms_down(e)
        result.sent, _ = self.outbox.flush(self.sessions)
        self.kv.set(HEARTBEAT_KEY, json.dumps({"at": self.now().isoformat(), **asdict(result)}))
        return result

    def _poll(self, result: PollResult) -> None:
        self.poll_started = self.now()
        self.adapter.ensure_session()
        self._clear_alert("session")
        courses = self.adapter.list_courses()
        result.courses = len(courses)
        new_courses: list[str] = []
        for c in courses:
            listed = self._list(c, self.adapter.list_assignments, "assignments", result)
            papers = self._list(c, self.adapter.list_papers, "papers", result)
            if listed is None and papers is None:
                continue
            with self.sessions.begin() as s:
                row, created = self._sync_course(s, c)
                if created:
                    new_courses.append(row.name)
                for meta in (listed or []) + (papers or []):
                    if meta.kind == PAPER:
                        result.papers += 1
                    else:
                        result.seen += 1
                    self._sync_item(s, row, meta, result)
        if new_courses:
            with self.sessions.begin() as s:
                self.outbox.queue(
                    s,
                    "courses",
                    "New courses found on the LMS:\n"
                    + "\n".join(f"- {n}" for n in new_courses)
                    + "\nEach starts in approve mode.",
                )
        result.downloaded = self._download_pending(result)
        self._announce_papers(result)
        self._remind(result)

    @staticmethod
    def _list(course: lms.Course, fetch: Callable, what: str, result: PollResult) -> list | None:
        try:
            return fetch(course)
        except (ContractBroken, SessionExpired, httpx.HTTPError):
            raise
        except LMSError as e:
            result.errors.append(f"{course.name} {what}: {e}")
            return None

    # --- courses and assignments ---

    def _sync_course(self, s: Session, c: lms.Course) -> tuple[Course, bool]:
        row = s.scalar(select(Course).where(Course.semester == c.semester, Course.lms_ref == c.lms_ref))
        if row is None:
            row = Course(
                code=c.code,
                name=c.name,
                lms_ref=c.lms_ref,
                semester=c.semester,
                semester_name=c.semester_name,
                mode=CourseMode.APPROVE,
            )
            s.add(row)
            s.flush()
            return row, True
        row.name = c.name
        row.last_seen_at = self.now()
        return row, False

    def _sync_item(self, s: Session, course: Course, meta: lms.AssignmentMeta, result: PollResult) -> None:
        outcome = self._sync_assignment(s, course, meta, result)
        if outcome:
            setattr(result, outcome, getattr(result, outcome) + 1)

    def _sync_assignment(
        self, s: Session, course: Course, meta: lms.AssignmentMeta, result: PollResult
    ) -> str | None:
        """Returns the PollResult counter to bump: new_jobs, revisions, skipped, stopped, new_papers, or None."""
        fp = meta.fingerprint()
        row = s.scalar(select(Assignment).where(Assignment.course_id == course.id, Assignment.lms_ref == meta.lms_ref))

        if row is None:
            row = Assignment(course=course, kind=meta.kind, lms_ref=meta.lms_ref, fingerprint=fp)
            self._copy(row, meta)
            self._snapshot(row, meta)
            row.reminders = ",".join(f"{h}h" for h in self._crossed(row))  # the first message covers these
            s.add(row)
            s.flush()
            reason = _skip_reason(meta, own=set())
            if reason:
                row.skipped_reason = reason
                return "skipped"
            if meta.kind == PAPER:
                return "new_papers"
            self._new_job(s, row, 1, fp, "new assignment")
            return "new_jobs"

        changes = _changes(row, meta)
        if meta.marks and meta.marks != row.marks:
            self.outbox.queue(s, "marks", f"Marks posted\n{course.name}\n{row.title}: {meta.marks}")
            result.marks_posted += 1
        if "deadline" in changes:
            row.reminders = ""
        self._copy(row, meta)
        self._snapshot(row, meta)
        row.last_seen_at = self.now()

        if meta.kind == PAPER:
            if fp == row.fingerprint:
                return None
            row.fingerprint = fp
            row.skipped_reason = _skip_reason(meta, own=set())
            row.announced_at = None  # announce the change
            row.question_path = None
            return None

        latest = row.jobs[-1] if row.jobs else None
        own = {j.lms_submission_file for j in row.jobs if j.lms_submission_file}

        if latest and latest.state not in TERMINAL_STATES:
            if meta.submitted_file and meta.submitted_file not in own:
                self._stop(s, latest, "The LMS shows a submission the service did not make")
                return "stopped"
            if meta.deadline_passed:
                self._stop(s, latest, "The deadline passed before the job finished")
                return "stopped"

        if fp == row.fingerprint:
            return None
        row.fingerprint = fp

        reason = _skip_reason(meta, own)
        if reason:
            row.skipped_reason = reason
            if latest:
                s.add(Event(job_id=latest.id, detail=f"Assignment changed ({', '.join(changes)}) but {reason.lower()}"))
            return None
        row.skipped_reason = None

        if latest is None:
            self._new_job(s, row, 1, fp, f"reopened: {', '.join(changes)}")
            return "new_jobs"
        if latest.state not in TERMINAL_STATES:
            self._transition(s, latest, JobState.SUPERSEDED, f"superseded by revision {latest.revision + 1}")
        self._new_job(s, row, latest.revision + 1, fp, f"changed: {', '.join(changes)}")
        return "revisions"

    def _snapshot(self, row: Assignment, meta: lms.AssignmentMeta) -> None:
        row.submitted_file = meta.submitted_file
        row.deadline_passed = meta.deadline_passed
        row.marks = meta.marks
        row.term = meta.term
        row.status_seen_at = self.now()

    def _crossed(self, row: Assignment) -> list[int]:
        """Reminder thresholds (hours) the deadline is already within."""
        if row.deadline is None or row.deadline_passed or row.submitted_file:
            return []
        left = (row.deadline - self.now()).total_seconds() / 3600
        return [h for h in REMINDER_HOURS if left <= h]

    @staticmethod
    def _copy(row: Assignment, meta: lms.AssignmentMeta) -> None:
        row.number = meta.number
        row.title = meta.title
        row.deadline = meta.deadline
        row.attachment_name = meta.attachment_name
        row.remarks = meta.remarks
        row.lms_assignment_id = meta.assignment_id or row.lms_assignment_id

    def _new_job(self, s: Session, a: Assignment, revision: int, fp: str, detail: str) -> Job:
        job = Job(assignment=a, revision=revision, fingerprint=fp, state=JobState.DETECTED)
        s.add(job)
        s.flush()
        s.add(Event(job_id=job.id, from_state=None, to_state=JobState.DETECTED, detail=detail))
        return job

    def _transition(self, s: Session, job: Job, to: JobState, detail: str) -> None:
        s.add(Event(job_id=job.id, from_state=job.state, to_state=to, detail=detail))
        job.state = to

    def _stop(self, s: Session, job: Job, reason: str) -> None:
        quiet = job.state in QUIET_STATES
        job.last_error = reason
        self._transition(s, job, JobState.NEEDS_HUMAN, reason)
        if not quiet:
            a = job.assignment
            self.outbox.queue(s, "stopped", f"Stopped: {a.course.name} · {a.title}\n{reason}.", job_id=job.id)

    # --- downloads ---

    def _download_pending(self, result: PollResult) -> int:
        done = 0
        with self.sessions() as s:
            jobs = s.scalars(
                select(Job).where(Job.state == JobState.DETECTED).order_by(Job.id).with_for_update(skip_locked=True)
            ).all()
            for job in jobs:
                a = job.assignment
                dest = self.data_dir / _safe(a.course.code) / str(a.id) / str(job.revision)
                try:
                    paths = self.adapter.download(_meta(a), dest)
                except (ContractBroken, SessionExpired, httpx.HTTPError):
                    s.commit()
                    raise
                except LMSError as e:
                    job.attempts += 1
                    job.last_error = str(e)
                    result.errors.append(f"{a.title}: {e}")
                    if job.attempts == DOWNLOAD_ALERT_AFTER:
                        self.outbox.queue(s, "alert", f"Download failed {job.attempts} times: {a.course.name} · {a.title}\n{e}", job_id=job.id)
                    s.commit()
                    continue
                for p in paths:
                    data = p.read_bytes()
                    s.add(Artifact(job=job, kind="question", path=str(p), sha256=hashlib.sha256(data).hexdigest(), size=len(data)))
                job.attempts = 0
                job.last_error = None
                a.question_path = str(paths[0]) if paths else None
                a.announced_at = self.now()
                self._transition(s, job, JobState.DOWNLOADED, f"downloaded {len(paths)} file(s)")
                text = _announce(a, job, self._detected_detail(s, job))
                self.outbox.queue(s, "assignment", text, file=paths[0] if paths else None, job_id=job.id)
                s.commit()
                done += 1
        return done

    def _announce_papers(self, result: PollResult) -> None:
        """Download each new or changed open paper and send it."""
        with self.sessions() as s:
            papers = s.scalars(
                select(Assignment).where(
                    Assignment.kind == PAPER,
                    Assignment.announced_at.is_(None),
                    Assignment.skipped_reason.is_(None),
                    Assignment.deadline_passed.is_(False),
                )
            ).all()
            for a in papers:
                path = None
                if a.attachment_name:
                    dest = self.data_dir / _safe(a.course.code) / "papers" / str(a.id)
                    try:
                        paths = self.adapter.download(_meta(a), dest)
                    except (ContractBroken, SessionExpired, httpx.HTTPError):
                        s.commit()
                        raise
                    except LMSError as e:
                        result.errors.append(f"{a.title}: {e}")
                        continue
                    path = paths[0] if paths else None
                changed = a.first_seen_at < self.poll_started
                a.question_path = str(path) if path else None
                a.announced_at = self.now()
                self.outbox.queue(s, "paper", _announce_paper(a, changed), file=path)
                s.commit()

    def _remind(self, result: PollResult) -> None:
        """24 h and 2 h before a deadline with nothing submitted: one message each."""
        horizon = self.now() + timedelta(hours=max(REMINDER_HOURS))
        with self.sessions() as s:
            due = s.scalars(
                select(Assignment).where(
                    Assignment.deadline.is_not(None),
                    Assignment.deadline <= horizon,
                    Assignment.deadline_passed.is_(False),
                    Assignment.submitted_file.is_(None),
                    Assignment.skipped_reason.is_(None),
                )
            ).all()
            for a in due:
                sent = set(filter(None, a.reminders.split(",")))
                crossed = self._crossed(a)
                fresh = [h for h in crossed if f"{h}h" not in sent]
                if not fresh:
                    continue
                a.reminders = ",".join(sorted(sent | {f"{h}h" for h in crossed}))
                self.outbox.queue(s, "reminder", _reminder(a, self.now()))
                result.reminders += 1
            s.commit()

    @staticmethod
    def _detected_detail(s: Session, job: Job) -> str:
        return s.scalar(
            select(Event.detail).where(Event.job_id == job.id, Event.to_state == JobState.DETECTED).order_by(Event.id.desc())
        ) or ""

    # --- alerts ---

    def _alert(self, code: str, text: str) -> None:
        """Send once per distinct message; repeats of the same alert stay quiet."""
        key = f"alert:{code}"
        if self.kv.get(key) == text:
            return
        self.kv.set(key, text)
        with self.sessions.begin() as s:
            s.add(Event(detail=f"{code}: {text}"))
            self.outbox.queue(s, "alert", text)

    def _clear_alert(self, code: str) -> None:
        self.kv.delete(f"alert:{code}")

    def _lms_down(self, e: Exception) -> None:
        self.lms_down_count += 1
        if self.kv.get(DOWN_SINCE_KEY) is None:
            self.kv.set(DOWN_SINCE_KEY, self.now().isoformat())
        if self.lms_down_count >= LMS_DOWN_ALERT_AFTER:
            since = datetime.fromisoformat(self.kv.get(DOWN_SINCE_KEY) or self.now().isoformat())
            self._alert("lms-down", f"The LMS has not answered since {_pkt(since)}: {type(e).__name__}. Retrying with back-off.")


def _skip_reason(meta: lms.AssignmentMeta, own: set[str]) -> str | None:
    if meta.submitted_file and meta.submitted_file not in own:
        return "Already submitted on the LMS, not by the service"
    if meta.deadline_passed:
        return "Deadline passed"
    return None


def _changes(row: Assignment, meta: lms.AssignmentMeta) -> list[str]:
    changes = []
    if row.title != meta.title:
        changes.append("title")
    if row.deadline != meta.deadline:
        changes.append("deadline")
    if row.attachment_name != meta.attachment_name:
        changes.append("question file")
    return changes or ["details"]


def _meta(a: Assignment) -> lms.AssignmentMeta:
    c = a.course
    return lms.AssignmentMeta(
        course=lms.Course(code=c.code, name=c.name, lms_ref=c.lms_ref, semester=c.semester, semester_name=c.semester_name),
        lms_ref=a.lms_ref,
        number=a.number,
        title=a.title,
        deadline=a.deadline,
        attachment_name=a.attachment_name,
        remarks=a.remarks,
        assignment_id=a.lms_assignment_id,
        kind=a.kind,
        term=a.term,
    )


def _pkt(dt: datetime) -> str:
    return dt.astimezone(LMS_TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _announce(a: Assignment, job: Job, detail: str) -> str:
    head = "New assignment" if job.revision == 1 else f"Assignment changed (revision {job.revision})"
    lines = [head, a.course.name, a.title, f"Due {_pkt(a.deadline)}" if a.deadline else "No deadline shown"]
    if detail.startswith(("changed: ", "reopened: ")):
        lines.append(detail[0].upper() + detail[1:])
    if a.remarks:
        lines.append(f"Teacher's note: {a.remarks}")
    return "\n".join(lines)


def _announce_paper(a: Assignment, changed: bool) -> str:
    lines = ["Paper changed" if changed else "New paper posted", a.course.name, a.title]
    lines.append(f"Due {_pkt(a.deadline)}" if a.deadline else "No deadline shown")
    if a.remarks:
        lines.append(f"Note: {a.remarks}")
    if not a.attachment_name:
        lines.append("No paper file yet; it appears on the LMS at the time on your date sheet.")
    return "\n".join(lines)


def _left(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _reminder(a: Assignment, now: datetime) -> str:
    what = "Paper" if a.kind == PAPER else "Assignment"
    lines = [f"{what} due in {_left(a.deadline - now)}, nothing submitted", a.course.name, a.title, f"Due {_pkt(a.deadline)}"]
    latest = a.jobs[-1] if a.jobs else None
    if latest and latest.state == JobState.AWAITING_APPROVAL:
        lines.append("The solved PDF is waiting for your approval.")
    return "\n".join(lines)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "course"

