"""The WhatsApp menu: numbered screens, you reply with a digit.

Screens form a stack per chat: a digit opens an option, 0 goes back, "menu" goes to
the top. Lists keep the numbering they were shown with, so an item that appears
between two messages never shifts your choice. A chat idle for 10 minutes starts
again at the main menu. Approve and Reject ask 1. Yes / 2. No first.

Everything reads the database the poller keeps current; "Check the LMS now" asks the
worker for an immediate poll. Papers are listed like assignments but are never solved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .db.models import TERMINAL_STATES, Artifact, Assignment, ChatSession, Course, CourseMode, Event, Job, JobState, utcnow
from .kv import KeyValue
from .lms.bahria_pages import LMS_TZ
from .watcher import HEARTBEAT_KEY, POLL_NOW_KEY, POLL_PAUSE_KEY

IDLE = timedelta(minutes=10)
PAGE = 10
DUE_SOON = timedelta(hours=48)
ASSIGNMENT, PAPER = "assignment", "paper"
PIPELINE = {
    JobState.DETECTED, JobState.DOWNLOADED, JobState.TRIAGED, JobState.SOLVING,
    JobState.RENDERED, JobState.VERIFIED, JobState.SUBMITTING,
}

# key, label; the order is the menu order
CATEGORIES = {
    ASSIGNMENT: [
        ("remaining", "Remaining (open, not submitted)"),
        ("due_soon", "Due in the next 48 hours"),
        ("approval", "Waiting for your approval"),
        ("pipeline", "Queued or being solved"),
        ("needs_you", "Stopped, needs you"),
        ("submitted", "Submitted"),
        ("missed", "Missed (closed, no submission)"),
        ("marked", "Marked"),
        ("all", "All"),
    ],
    PAPER: [
        ("remaining", "Remaining (open, not submitted)"),
        ("due_soon", "Due in the next 48 hours"),
        ("submitted", "Submitted"),
        ("missed", "Missed (closed, no submission)"),
        ("marked", "Marked"),
        ("all", "All"),
    ],
}
LABELS = {k: v for cats in CATEGORIES.values() for k, v in cats}

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
}


@dataclass(frozen=True)
class Reply:
    text: str
    file: Path | None = None


class Bot:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        kv: KeyValue,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sessions = sessions
        self.kv = kv
        self.now = now

    # --- entry point ---

    def handle(self, chat_id: str, text: str) -> list[Reply]:
        text = (text or "").strip()
        with self.sessions.begin() as s:
            chat = s.get(ChatSession, chat_id)
            if chat is None:
                chat = ChatSession(chat_id=chat_id, state="{}")
                s.add(chat)
            stack = json.loads(chat.state or "{}").get("stack") or []
            idle = chat.updated_at is not None and self.now() - chat.updated_at > IDLE
            if not stack or idle:
                stack = [{"name": "main"}]
                self._render(s, stack)  # a digit is read against the main menu
            replies = self._dispatch(s, stack, text)
            chat.state = json.dumps({"stack": stack})
            chat.updated_at = self.now()
            return replies

    def _dispatch(self, s: Session, stack: list[dict], text: str) -> list[Reply]:
        low = text.lower()
        if low in ("menu", "hi", "hello", "start", "/start", "help"):
            del stack[1:]
            return [self._render(s, stack)]
        screen = stack[-1]

        if screen["name"] == "note":  # free text: the redo note
            if text in ("", "0"):
                stack.pop()
                return [self._render(s, stack)]
            stack[-1] = {"name": "confirm", "action": "redo", "job": screen["job"], "note": text[:1000]}
            return [self._render(s, stack)]

        if text == "0":
            if len(stack) > 1:
                stack.pop()
            return [self._render(s, stack)]
        if not text.isdigit() or text not in (screen.get("options") or {}):
            return [self._render(s, stack, hint="Reply with one of the numbers below.")]
        return self._choose(s, stack, screen["options"][text])

    # --- choices ---

    def _choose(self, s: Session, stack: list[dict], option: dict) -> list[Reply]:
        kind = option["do"]
        if kind == "open":
            stack.append(option["screen"])
            return [self._render(s, stack)]
        if kind == "check":
            paused = self.kv.get(POLL_PAUSE_KEY)
            if paused:
                return [Reply(f"Polling is paused: {paused}\nFix it, then run lms-worker resume on the server."), self._render(s, stack)]
            self.kv.set(POLL_NOW_KEY, self.now().isoformat())
            return [Reply("Checking the LMS now. Anything new arrives as its own message in a minute or two.")]
        if kind == "status":
            return [Reply(self._status(s))]
        if kind == "file":
            a = s.get(Assignment, option["assignment"])
            path = _question_file(a) if a else None
            if path is None:
                return [Reply("That file is not on the server yet. Try Check the LMS now."), self._render(s, stack)]
            return [Reply(f"{a.course.name} · {a.title}", file=path)]
        if kind == "artifact":
            art = s.get(Artifact, option["artifact"])
            if not art or not Path(art.path).exists():
                return [Reply("That file is no longer on the server."), self._render(s, stack)]
            return [Reply(f"{art.job.assignment.title} · {art.kind}", file=Path(art.path))]
        if kind == "history":
            return [Reply(self._history(s, option["job"]))]
        if kind == "confirm":
            stack.append({"name": "confirm", **{k: v for k, v in option.items() if k != "do"}})
            return [self._render(s, stack)]
        if kind == "note":
            stack.append({"name": "note", "job": option["job"]})
            return [Reply("Type what should change in the redo, in one message. 0 cancels.")]
        if kind == "yes":
            confirmed = stack.pop()
            done = self._execute(s, confirmed)
            if confirmed["action"] == "mode" and stack[-1]["name"] == "mode_pick":
                stack.pop()
            return [Reply(done), self._render(s, stack)]
        if kind == "no":
            stack.pop()
            return [Reply("Cancelled."), self._render(s, stack)]
        if kind == "mode":
            course = s.get(Course, option["course"])
            if option["mode"] == CourseMode.AUTO:
                stack.append({"name": "confirm", "action": "mode", "course": course.id, "mode": CourseMode.AUTO})
                return [self._render(s, stack)]
            course.mode = option["mode"]
            stack.pop()
            return [Reply(f"{course.name} is now in {course.mode} mode."), self._render(s, stack)]
        raise ValueError(f"Unknown option {kind}")

    def _execute(self, s: Session, c: dict) -> str:
        action = c["action"]
        if action == "mode":
            course = s.get(Course, c["course"])
            course.mode = c["mode"]
            return f"{course.name} is now in {course.mode} mode."
        job = s.get(Job, c["job"])
        if job is None:
            return "That job no longer exists."
        a = job.assignment
        if action in ("approve", "reject") and job.state != JobState.AWAITING_APPROVAL:
            return f"Nothing changed: {a.title} is {JOB_WORDS.get(job.state, job.state)} now."
        if action == "approve":
            self._move(s, job, JobState.SUBMITTING, "approved on WhatsApp")
            return f"Approved: {a.title}. Uploading now; the LMS receipt will follow."
        if action == "reject":
            self._move(s, job, JobState.REJECTED, "rejected on WhatsApp")
            return f"Rejected: {a.title}. Nothing will be uploaded."
        if action == "redo":
            job.note = c.get("note")
            job.attempts = 0
            self._move(s, job, JobState.DOWNLOADED, f"redo requested on WhatsApp: {job.note}")
            return f"Redo queued for {a.title}. The new PDF comes to you before anything is uploaded."
        raise ValueError(f"Unknown action {action}")

    @staticmethod
    def _move(s: Session, job: Job, to: JobState, detail: str) -> None:
        s.add(Event(job_id=job.id, from_state=job.state, to_state=to, detail=detail))
        job.state = to

    # --- rendering ---

    def _render(self, s: Session, stack: list[dict], hint: str | None = None) -> Reply:
        screen = stack[-1]
        title, lines, options = getattr(self, f"_screen_{screen['name']}")(s, screen)
        screen["options"] = {str(n): opt for n, (_, opt) in enumerate(options, start=1)}
        body = [f"*{title}*"] if title else []
        body += lines
        if lines and options:
            body.append("")
        body += [f"{n}. {label}" for n, (label, _) in enumerate(options, start=1)]
        footer = "Reply with a number." if len(stack) == 1 else "Reply with a number. 0 = back, menu = start."
        if screen["name"] == "confirm":
            footer = "Reply 1 or 2."
        body += ["", hint or footer]
        return Reply("\n".join(body))

    def _screen_main(self, s: Session, screen: dict):
        items = self._items(s)
        a = [i for i in items if i.kind == ASSIGNMENT]
        p = [i for i in items if i.kind == PAPER]
        now = self.now()
        options = [
            (f"Assignments: {_summary(a, now)}", _open({"name": "section", "kind": ASSIGNMENT})),
            (f"Papers: {_summary(p, now)}", _open({"name": "section", "kind": PAPER})),
            (f"Waiting for your approval ({sum(_in(i, 'approval', now) for i in a)})",
             _open({"name": "list", "kind": ASSIGNMENT, "category": "approval"})),
            (f"Due in the next 48 hours ({sum(_in(i, 'due_soon', now) for i in items)})",
             _open({"name": "list", "kind": None, "category": "due_soon"})),
            ("By course", _open({"name": "courses"})),
            ("Course modes (auto, approve, notify)", _open({"name": "modes"})),
            ("Check the LMS now", {"do": "check"}),
            ("System status", {"do": "status"}),
        ]
        return "LMS BOT", [], options

    def _screen_section(self, s: Session, screen: dict):
        kind, course_id = screen["kind"], screen.get("course")
        items = self._items(s, kind=kind, course_id=course_id)
        now = self.now()
        options = [
            (f"{label} ({sum(_in(i, key, now) for i in items)})",
             _open({"name": "list", "kind": kind, "course": course_id, "category": key}))
            for key, label in CATEGORIES[kind]
        ]
        where = s.get(Course, course_id).name if course_id else "all courses"
        return f"{'Assignments' if kind == ASSIGNMENT else 'Papers'} · {where}", [], options

    def _screen_list(self, s: Session, screen: dict):
        kind, course_id, category = screen.get("kind"), screen.get("course"), screen["category"]
        page = screen.get("page", 0)
        now = self.now()
        if "items" not in screen:  # freeze the list the first time it is shown
            items = [i for i in self._items(s, kind=kind, course_id=course_id) if _in(i, category, now)]
            items.sort(key=_order(category))
            screen["items"] = [i.id for i in items]
        ids = screen["items"]
        shown = ids[page * PAGE:(page + 1) * PAGE]
        rows = {a.id: a for a in s.scalars(select(Assignment).where(Assignment.id.in_(shown)).options(selectinload(Assignment.jobs)))}
        options = [(_line(rows[i], now, with_course=course_id is None), _open({"name": "item", "id": i})) for i in shown if i in rows]
        if len(ids) > (page + 1) * PAGE:
            options.append((f"More ({len(ids) - (page + 1) * PAGE} left)", _open({**_bare(screen), "page": page + 1, "items": ids})))
        what = {ASSIGNMENT: "Assignments", PAPER: "Papers", None: "Assignments and papers"}[kind]
        where = f" · {s.get(Course, course_id).name}" if course_id else ""
        lines = [] if ids else ["Nothing here."]
        return f"{what}: {LABELS[category].split(' (')[0].lower()}{where}", lines, options

    def _screen_item(self, s: Session, screen: dict):
        a = s.get(Assignment, screen["id"])
        if a is None:
            return "Gone", ["This item is no longer listed."], []
        now = self.now()
        job = a.jobs[-1] if a.jobs else None
        what = f"{a.term} paper {a.number}" if a.kind == PAPER else f"Assignment {a.number}"
        lines = [a.title, f"{a.course.name} · {what}"]
        lines.append(f"Due {_when(a.deadline)}" + (f" ({_relative(a.deadline, now)})" if a.deadline and not a.deadline_passed else "") if a.deadline else "No deadline shown")
        lines.append(f"Status: {_status(a, now)}")
        lines.append(f"Marks: {a.marks or 'not marked yet'}")
        if a.remarks:
            lines.append(f"Teacher's note: {a.remarks}")
        if a.kind == ASSIGNMENT:
            if job:
                state = JOB_WORDS.get(job.state, job.state) + (f": {job.last_error}" if job.state == JobState.NEEDS_HUMAN and job.last_error else "")
                lines.append(f"Service: {state} (revision {job.revision})")
            elif a.skipped_reason:
                lines.append(f"Service: not handled; {a.skipped_reason.lower()}")
        else:
            lines.append("Papers are tracked only; the service never solves them.")

        options = []
        if _question_file(a):
            options.append(("Send the paper" if a.kind == PAPER else "Send the question file", {"do": "file", "assignment": a.id}))
        if job:
            for art in sorted(job.artifacts, key=lambda x: x.id):
                if art.kind in ("pdf", "docx", "notebook", "receipt"):
                    label = {"pdf": "Send the solved PDF", "docx": "Send the DOCX", "notebook": "Send the notebook and code", "receipt": "Send the LMS receipt"}[art.kind]
                    options.append((label, {"do": "artifact", "artifact": art.id}))
            if job.state == JobState.AWAITING_APPROVAL:
                options.append(("Approve and upload", {"do": "confirm", "action": "approve", "job": job.id}))
                options.append(("Reject", {"do": "confirm", "action": "reject", "job": job.id}))
            if job.state in (JobState.AWAITING_APPROVAL, JobState.NEEDS_HUMAN, JobState.CONFIRMED) and not a.deadline_passed:
                options.append(("Redo with a note", {"do": "note", "job": job.id}))
            options.append(("History", {"do": "history", "job": job.id}))
        return "", lines, options

    def _screen_courses(self, s: Session, screen: dict):
        now = self.now()
        options = []
        for c in self._courses(s):
            items = self._items(s, course_id=c.id)
            a = sum(_in(i, "remaining", now) for i in items if i.kind == ASSIGNMENT)
            p = sum(_in(i, "remaining", now) for i in items if i.kind == PAPER)
            parts = [f"{a} assignment{'s' * (a != 1)}"] * bool(a) + [f"{p} paper{'s' * (p != 1)}"] * bool(p)
            label = f"{c.name} ({' + '.join(parts)} remaining)" if parts else c.name
            options.append((label, _open({"name": "course", "id": c.id})))
        return "Courses", [] if options else ["No courses yet: the first poll has not run."], options

    def _screen_course(self, s: Session, screen: dict):
        c = s.get(Course, screen["id"])
        now = self.now()
        items = self._items(s, course_id=c.id)
        a = [i for i in items if i.kind == ASSIGNMENT]
        p = [i for i in items if i.kind == PAPER]
        options = [
            (f"Assignments: {_summary(a, now)}", _open({"name": "section", "kind": ASSIGNMENT, "course": c.id})),
            (f"Papers: {_summary(p, now)}", _open({"name": "section", "kind": PAPER, "course": c.id})),
            (f"Mode: {c.mode} (change)", _open({"name": "mode_pick", "course": c.id})),
        ]
        return c.name, [], options

    def _screen_modes(self, s: Session, screen: dict):
        options = [(f"{c.name}: {c.mode}", _open({"name": "mode_pick", "course": c.id})) for c in self._courses(s)]
        lines = ["auto uploads after the checks, approve waits for your yes, notify only sends the question."]
        return "Course modes", lines, options

    def _screen_mode_pick(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        options = [
            ("approve: send me the PDF, upload when I say yes", {"do": "mode", "course": c.id, "mode": CourseMode.APPROVE}),
            ("auto: upload after the checks, without asking", {"do": "mode", "course": c.id, "mode": CourseMode.AUTO}),
            ("notify: only send me the question", {"do": "mode", "course": c.id, "mode": CourseMode.NOTIFY}),
        ]
        return f"{c.name} · now {c.mode}", [], options

    def _screen_confirm(self, s: Session, screen: dict):
        action = screen["action"]
        if action == "mode":
            c = s.get(Course, screen["course"])
            question = [f"Switch {c.name} to auto?", "Work will be uploaded under your name without you reading it first."]
        else:
            job = s.get(Job, screen["job"])
            if job is None:
                return "", ["That job no longer exists."], [("OK", {"do": "no"})]
            a = job.assignment
            question = {
                "approve": [f"Upload the solved PDF for {a.title} to the LMS now?"],
                "reject": [f"Reject the solution for {a.title}? Nothing will be uploaded."],
                "redo": [f"Redo {a.title} with this note?", f'"{screen.get("note", "")}"'],
            }[action]
        return "", question, [("Yes", {"do": "yes"}), ("No", {"do": "no"})]

    # --- data ---

    def _items(self, s: Session, kind: str | None = None, course_id: int | None = None) -> list[Assignment]:
        q = select(Assignment).join(Course).options(selectinload(Assignment.jobs))
        if kind:
            q = q.where(Assignment.kind == kind)
        if course_id:
            q = q.where(Assignment.course_id == course_id)
        else:
            q = q.where(Course.semester == self._semester(s))
        return list(s.scalars(q))

    def _courses(self, s: Session) -> list[Course]:
        return list(s.scalars(select(Course).where(Course.semester == self._semester(s)).order_by(Course.id)))

    @staticmethod
    def _semester(s: Session) -> str | None:
        return s.scalar(select(Course.semester).order_by(Course.last_seen_at.desc()).limit(1))

    def _status(self, s: Session) -> str:
        lines = ["*System status*"]
        beat = self.kv.get(HEARTBEAT_KEY)
        if beat:
            b = json.loads(beat)
            lines.append(f"Last check: {_when(datetime.fromisoformat(b['at']))} ({b['status']})")
        else:
            lines.append("Last check: never")
        paused = self.kv.get(POLL_PAUSE_KEY)
        lines.append(f"Polling: paused ({paused})" if paused else "Polling: running")
        guard = self.kv.get("login_guard")
        if guard and json.loads(guard).get("paused_reason"):
            lines.append(f"LMS login: paused ({json.loads(guard)['paused_reason']})")
        else:
            lines.append("LMS login: ok")
        jobs = s.scalars(select(Job)).all()
        active = [j for j in jobs if j.state not in TERMINAL_STATES]
        lines.append(f"Jobs: {len(active)} active, {sum(j.state == JobState.AWAITING_APPROVAL for j in jobs)} waiting for you")
        month = (self.now().year, self.now().month)
        lines.append(f"Tokens used this month: {sum(j.cost_tokens for j in jobs if (j.created_at.year, j.created_at.month) == month):,}")
        return "\n".join(lines)

    def _history(self, s: Session, job_id: int) -> str:
        events = s.scalars(select(Event).where(Event.job_id == job_id).order_by(Event.id.desc()).limit(8)).all()
        lines = ["*History*"] + [f"{_when(e.at)}: {e.detail}" for e in reversed(events)]
        return "\n".join(lines)


# --- helpers ---


def _open(screen: dict) -> dict:
    return {"do": "open", "screen": screen}


def _bare(screen: dict) -> dict:
    return {k: v for k, v in screen.items() if k not in ("options", "items", "page")}


def _question_file(a: Assignment) -> Path | None:
    """The downloaded question file or paper; older rows only have it as a job artifact."""
    candidates = [a.question_path] + [
        art.path for job in reversed(a.jobs) for art in sorted(job.artifacts, key=lambda x: -x.id) if art.kind == "question"
    ]
    return next((Path(p) for p in candidates if p and Path(p).exists()), None)


def _job(a: Assignment) -> Job | None:
    return a.jobs[-1] if a.jobs else None


def _in(a: Assignment, category: str, now: datetime) -> bool:
    is_open = not a.deadline_passed
    job = _job(a)
    state = job.state if job else None
    if category == "all":
        return True
    if category == "remaining":
        return is_open and not a.submitted_file
    if category == "due_soon":
        return is_open and not a.submitted_file and a.deadline is not None and a.deadline - now <= DUE_SOON
    if category == "approval":
        return state == JobState.AWAITING_APPROVAL
    if category == "pipeline":
        return state in PIPELINE
    if category == "needs_you":
        return state == JobState.NEEDS_HUMAN and is_open and not a.submitted_file
    if category == "submitted":
        return bool(a.submitted_file)
    if category == "missed":
        return a.deadline_passed and not a.submitted_file
    if category == "marked":
        return bool(a.marks)
    raise ValueError(category)


def _order(category: str):
    far = datetime.max.replace(tzinfo=LMS_TZ)
    if category in ("remaining", "due_soon", "approval", "pipeline", "needs_you"):
        return lambda a: (a.deadline or far, a.id)
    return lambda a: (-(a.deadline or far).timestamp(), -a.id)


def _summary(items: list[Assignment], now: datetime) -> str:
    left = sum(_in(i, "remaining", now) for i in items)
    soon = sum(_in(i, "due_soon", now) for i in items)
    if not items:
        return "none yet"
    if not left:
        return "nothing remaining"
    return f"{left} remaining" + (f", {soon} due soon" if soon else "")


def _status(a: Assignment, now: datetime) -> str:
    if a.submitted_file:
        return "submitted" + ("; still open, you can delete and resubmit on the LMS" if not a.deadline_passed else "")
    if a.deadline_passed:
        return "missed: closed with no submission"
    return "not submitted"


def _line(a: Assignment, now: datetime, *, with_course: bool) -> str:
    bits = [a.title]
    if with_course:
        bits.append(a.course.name)
    if a.deadline:
        bits.append(f"due {_when(a.deadline)}")
    if a.marks:
        bits.append(a.marks)
    elif a.submitted_file:
        bits.append("submitted")
    elif a.deadline_passed:
        bits.append("missed")
    job = _job(a)
    if job and job.state == JobState.AWAITING_APPROVAL:
        bits.append("waiting for you")
    return " · ".join(bits)


def _when(dt: datetime | None) -> str:
    if dt is None:
        return "no date"
    return dt.astimezone(LMS_TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _relative(dt: datetime, now: datetime) -> str:
    minutes = int((dt - now).total_seconds() // 60)
    if minutes < 0:
        return "past"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"in {days} d {hours} h"
    return f"in {hours} h {mins} min" if hours else f"in {mins} min"
