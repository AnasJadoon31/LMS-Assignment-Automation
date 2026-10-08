"""What you can do to a job or a course: the same rules from WhatsApp and the web page.

Each action returns (done, message). Approve and Reject act only on a job waiting for
your approval; Redo only on one that is finished or stopped while the assignment is still
open. Approving asks the worker to poll at once, so the upload starts within seconds.
Deleting a submission is the LMS's own Delete button: only while the assignment is open and
the LMS shows a file; the worker follows the link at once and reports what happened.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from .db.models import Assignment, Course, CourseMode, Event, Job, JobState, LectureMode, SolveMode, Summary
from .kv import KeyValue

POLL_NOW_KEY = "poll_requested"  # the watcher's
MODEL_KEY = "solver_model"  # a model fixed in the chat; wins over SOLVER_MODEL. Without either, Haiku routes each job
EFFORT_KEY = "solver_effort"  # the effort picked in the chat; the CLI's own default without it
CAP_LIFTED_KEY = "solver_cap_lifted"  # the day (Pakistan time) the daily run limit is lifted for
PASSWORD_KEY = "admin_password"  # the page password as reset from the chat (salted hash); wins over ADMIN_PASSWORD
# What Claude Code 2.1.290 (the pinned CLI) knows; whether your plan offers each one is the plan's business
MODELS = (
    ("Fable 5.1", "claude-fable-5-1"), ("Opus 5.5", "claude-opus-5-5"), ("Opus 5", "claude-opus-5"),
    ("Opus 4.8", "claude-opus-4-8"), ("Opus 4.7", "claude-opus-4-7"), ("Opus 4.6", "claude-opus-4-6"),
    ("Opus 4.5", "claude-opus-4-5"), ("Opus 4.1", "claude-opus-4-1"),
    ("Sonnet 5.5", "claude-sonnet-5-5"), ("Sonnet 5", "claude-sonnet-5"), ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Sonnet 4.5", "claude-sonnet-4-5"), ("Haiku 4.5", "claude-haiku-4-5"),
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
REDO_STATES = {JobState.AWAITING_APPROVAL, JobState.NEEDS_HUMAN, JobState.CONFIRMED, JobState.HANDWRITTEN}

JOB_WORDS = {
    JobState.DETECTED: "found; downloading the question file",
    JobState.DOWNLOADED: "question file saved; waiting for the solver",
    JobState.TRIAGED: "checked; waiting to be solved",
    JobState.SOLVING: "being solved",
    JobState.RENDERED: "solved; PDF built, being checked",
    JobState.VERIFIED: "solved and checked",
    JobState.AWAITING_APPROVAL: "solved; waiting for your approval",
    JobState.SUBMITTING: "approved; uploading",
    JobState.CONFIRMED: "uploaded by the service",
    JobState.NEEDS_HUMAN: "stopped",
    JobState.REJECTED: "rejected by you",
    JobState.WAITING_GO: "not solved; waiting for your go-ahead",
    JobState.NEEDS_INPUT: "not solved; Claude is waiting for your answer",
    JobState.SUPERSEDED: "replaced by a newer revision",
    JobState.HANDWRITTEN: "solved; handwritten, sent to you to copy",  # older jobs only
}
HANDWRITTEN_WORDS = "solved; the sheet asks for handwritten work; waiting for your approval"
MODE_WORDS = {
    CourseMode.APPROVE: "send me the PDF, upload when I say yes",
    CourseMode.AUTO: "upload after the checks, without asking",
}
SOLVE_WORDS = {
    SolveMode.BYPASS: "always solve, never ask me",
    SolveMode.MANUAL: "ask me each time before solving",
    SolveMode.AUTO: "Claude solves, or asks me first when it needs something",
}
LECTURE_WORDS = {
    LectureMode.AUTO: "Haiku decides per assignment whether the lecture notes are needed, and which",
    LectureMode.ALWAYS: "every assignment is solved with the lecture notes at hand",
    LectureMode.OFF: "the lecture notes are not used",
}
UPLOAD_TYPES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")  # what the LMS takes


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def reset_password(kv: KeyValue) -> str:
    """A new page password, kept as a salted hash. Returns it, to be shown once."""
    password, salt = secrets.token_urlsafe(12), secrets.token_hex(16)
    kv.set(PASSWORD_KEY, json.dumps({"salt": salt, "hash": _hash(password, salt)}))
    return password


def stored_password(kv: KeyValue | None) -> dict | None:
    raw = kv.get(PASSWORD_KEY) if kv is not None else None
    return json.loads(raw) if raw else None


def password_matches(kv: KeyValue | None, env_password: str | None, given: str) -> bool:
    """Against the password reset from the chat when there is one, else ADMIN_PASSWORD."""
    stored = stored_password(kv)
    if stored:
        return hmac.compare_digest(_hash(given, stored["salt"]), stored["hash"])
    return bool(env_password) and hmac.compare_digest(given.encode(), env_password.encode())


def move(s: Session, job: Job, to: JobState, detail: str) -> None:
    s.add(Event(job_id=job.id, from_state=job.state, to_state=to, detail=detail))
    job.state = to


def handwritten(job: Job) -> bool:
    """The sheet asks for handwritten work. The report is still yours to approve: copy it by
    hand, or have it uploaded as it is."""
    try:
        return json.loads(job.summary or "{}").get("status") == "handwritten"
    except ValueError:
        return False


def job_words(job: Job) -> str:
    if job.state == JobState.AWAITING_APPROVAL and handwritten(job):
        return HANDWRITTEN_WORDS
    return JOB_WORDS.get(job.state, job.state)


def can_redo(job: Job) -> bool:
    return job.state in REDO_STATES and not job.assignment.deadline_passed


def approve(s: Session, job: Job, kv: KeyValue, now: datetime, via: str) -> tuple[bool, str]:
    a = job.assignment
    if job.state != JobState.AWAITING_APPROVAL:
        return False, f"Nothing changed: {a.title} is {JOB_WORDS.get(job.state, job.state)} now."
    job.attempts = 0  # counts upload tries from here
    move(s, job, JobState.SUBMITTING, f"approved on {via}")
    kv.set(POLL_NOW_KEY, now.isoformat())
    return True, f"Approved: {a.title}. Uploading now; the LMS receipt will follow."


def reject(s: Session, job: Job, via: str) -> tuple[bool, str]:
    a = job.assignment
    if job.state != JobState.AWAITING_APPROVAL:
        return False, f"Nothing changed: {a.title} is {JOB_WORDS.get(job.state, job.state)} now."
    move(s, job, JobState.REJECTED, f"rejected on {via}")
    return True, f"Rejected: {a.title}. Nothing will be uploaded."


def redo(s: Session, job: Job, note: str, via: str, *, strong: bool = False) -> tuple[bool, str]:
    """strong: solve it again from the sheet with Opus, whatever model Haiku or Settings would pick."""
    a = job.assignment
    note = (note or "").strip()[:1000]
    if not note:
        return False, "Say what should change."
    if not can_redo(job):
        return False, f"Nothing changed: {a.title} is {JOB_WORDS.get(job.state, job.state)} now."
    job.note = note
    job.attempts = 0
    if strong:  # Haiku's place in the triage is taken: this redo is already sorted
        triage = json.loads(job.triage) if job.triage else {}
        triage["redo"] = {"note": note, "model": "opus", "effort": "high", "reason": "you asked for Opus", "forced": True}
        job.triage = json.dumps(triage)
    move(s, job, JobState.DOWNLOADED, f"redo{' with Opus' * strong} requested on {via}: {note}")
    return True, (f"Redo queued for {a.title}{', solved again from the start with Opus' * strong}. "
                  "The new PDF comes to you before anything is uploaded.")


def go(s: Session, job: Job, via: str) -> tuple[bool, str]:
    """Manual solving: your go-ahead."""
    a = job.assignment
    if job.state != JobState.WAITING_GO:
        return False, f"Nothing changed: {a.title} is {JOB_WORDS.get(job.state, job.state)} now."
    job.attempts = 0
    move(s, job, JobState.DOWNLOADED, f"go-ahead on {via}")
    return True, f"Queued: {a.title} will be solved. The PDF comes to you when it is ready."


def answer(s: Session, job: Job, text: str, via: str) -> tuple[bool, str]:
    """Your answer to what Claude asked; empty means solve it without one."""
    a = job.assignment
    if job.state != JobState.NEEDS_INPUT:
        return False, f"Nothing changed: {a.title} is {JOB_WORDS.get(job.state, job.state)} now."
    job.note = (text or "").strip()[:2000] or None
    job.attempts = 0
    move(s, job, JobState.DOWNLOADED, f"answered on {via}: {job.note}" if job.note else f"told to go on without an answer, on {via}")
    return True, f"Thanks. {a.title} is queued again; the PDF comes to you when it is ready."


def can_upload_own(a: Assignment) -> bool:
    """Open, and the LMS shows nothing submitted: its Submit button is there."""
    return a.kind == "assignment" and not a.deadline_passed and not a.submitted_file and a.upload_requested_at is None


def upload_own(s: Session, a: Assignment, path: str, kv: KeyValue, now: datetime, via: str) -> tuple[bool, str]:
    if not can_upload_own(a):
        return False, f"Nothing changed: {a.title} cannot take an upload now."
    a.own_file, a.upload_requested_at = path, now
    job = a.jobs[-1] if a.jobs else None
    s.add(Event(job_id=job.id if job else None, detail=f"upload of your own file requested on {via}: {path.rsplit('/', 1)[-1]}"))
    kv.set(POLL_NOW_KEY, now.isoformat())
    return True, f"Uploading your file for {a.title} now; a message will confirm it."


def can_delete(a: Assignment) -> bool:
    """The LMS shows a submission and the assignment is still open, so the LMS would offer Delete."""
    return a.kind == "assignment" and bool(a.submitted_file) and not a.deadline_passed and a.delete_requested_at is None


def delete_submission(s: Session, a: Assignment, kv: KeyValue, now: datetime, via: str) -> tuple[bool, str]:
    if a.delete_requested_at is not None:
        return False, f"Nothing changed: the submission for {a.title} is already being deleted."
    if not can_delete(a):
        return False, f"Nothing changed: {a.title} has no submission on the LMS that can be deleted now."
    a.delete_requested_at = now
    job = a.jobs[-1] if a.jobs else None
    s.add(Event(job_id=job.id if job else None, detail=f"delete of the LMS submission requested on {via}: {a.title}"))
    kv.set(POLL_NOW_KEY, now.isoformat())
    return True, f"Deleting your submission for {a.title} from the LMS now; a message will confirm it."


def set_mode(course: Course, mode: str) -> tuple[bool, str]:
    if mode not in MODE_WORDS:
        return False, f"Unknown mode {mode}"
    course.mode = mode
    return True, f"{course.name}: uploading is now {mode} ({MODE_WORDS[mode]})."


def set_solve(course: Course, solve: str) -> tuple[bool, str]:
    if solve not in SOLVE_WORDS:
        return False, f"Unknown solving mode {solve}"
    course.solve = solve
    return True, f"{course.name}: solving is now {solve} ({SOLVE_WORDS[solve]}), from the next new assignment."


def set_lectures(course: Course, mode: str) -> tuple[bool, str]:
    if mode not in LECTURE_WORDS:
        return False, f"Unknown lecture notes mode {mode}"
    course.lectures = mode
    return True, f"{course.name}: lecture notes for solving are now {mode} ({LECTURE_WORDS[mode]}), from the next new assignment."


def summarize(s: Session, course: Course, lectures: list[dict]) -> Summary:
    """Queue explained notes of these lecture files ([{week, title, file}]), which are on the server."""
    summary = Summary(course=course, lectures=json.dumps(lectures), style="notes")
    s.add(summary)
    s.flush()
    return summary
