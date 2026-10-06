"""What you can do to a job or a course: the same rules from WhatsApp and the web page.

Each action returns (done, message). Approve and Reject act only on a job waiting for
your approval; Redo only on one that is finished or stopped while the assignment is still
open. Approving asks the worker to poll at once, so the upload starts within seconds.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from .db.models import Course, CourseMode, Event, Job, JobState
from .kv import KeyValue

POLL_NOW_KEY = "poll_requested"  # the watcher's
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
    JobState.SUPERSEDED: "replaced by a newer revision",
    JobState.HANDWRITTEN: "solved; handwritten, sent to you to copy",
}
MODE_WORDS = {
    CourseMode.APPROVE: "send me the PDF, upload when I say yes",
    CourseMode.AUTO: "upload after the checks, without asking",
    CourseMode.NOTIFY: "only send me the question",
}


def move(s: Session, job: Job, to: JobState, detail: str) -> None:
    s.add(Event(job_id=job.id, from_state=job.state, to_state=to, detail=detail))
    job.state = to


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


def redo(s: Session, job: Job, note: str, via: str) -> tuple[bool, str]:
    a = job.assignment
    note = (note or "").strip()[:1000]
    if not note:
        return False, "Say what should change."
    if not can_redo(job):
        return False, f"Nothing changed: {a.title} is {JOB_WORDS.get(job.state, job.state)} now."
    job.note = note
    job.attempts = 0
    move(s, job, JobState.DOWNLOADED, f"redo requested on {via}: {note}")
    return True, f"Redo queued for {a.title}. The new PDF comes to you before anything is uploaded."


def set_mode(course: Course, mode: str) -> tuple[bool, str]:
    if mode not in MODE_WORDS:
        return False, f"Unknown mode {mode}"
    course.mode = mode
    return True, f"{course.name} is now in {mode} mode."
