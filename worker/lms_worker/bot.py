"""The WhatsApp menu: numbered screens, you reply with a digit.

Screens form a stack per chat: a digit opens an option, 0 goes back, "menu" goes to
the top. Lists keep the numbering they were shown with, so an item that appears
between two messages never shifts your choice. A chat idle for 10 minutes starts
again at the main menu. Approve and Reject ask 1. Yes / 2. No first.

Everything reads the database the poller keeps current; "Check the LMS now" asks the
worker for an immediate poll. Lab papers are listed by their exam window and never solved;
their status is the LMS row colour: green submitted, white not submitted, red missed.
"""

from __future__ import annotations

import re

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import actions
from .solver.prompt import THEME_ARTIFACT, system_prompt
from .db.models import TERMINAL_STATES, Artifact, Assignment, ChatSession, Course, CourseMode, Event, Job, JobState, utcnow
from .kv import KeyValue
from .lms.bahria_pages import LMS_TZ
from .lms.models import PAPER_MISSED, PAPER_NOT_SUBMITTED, PAPER_SUBMITTED
from .solver.pipeline import SIGNED_OUT_KEY
from .solver.usage import UsageGuard
from .watcher import HEARTBEAT_KEY, POLL_NOW_KEY, POLL_PAUSE_KEY, is_lab

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
        ("upcoming", "Upcoming"),
        ("open_now", "Open now"),
        ("due_soon", "Starting in the next 48 hours"),
        ("finished", "Finished"),
        ("submitted", "Submitted"),
        ("missed", "Missed (no submission)"),
        ("marked", "Marked"),
        ("all", "All"),
    ],
}
LABELS = {k: v for cats in CATEGORIES.values() for k, v in cats}
LABELS["due_soon"] = "Due or starting in the next 48 hours"



@dataclass(frozen=True)
class Reply:
    text: str
    file: Path | None = None

THEME_LINK = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]{6,80}")

class Bot:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        kv: KeyValue,
        *,
        now: Callable[[], datetime] = utcnow,
        login=None,  # ClaudeLogin: System status says whether Claude is signed in
        setup_url: str | None = None,
    ) -> None:
        self.sessions = sessions
        self.kv = kv
        self.now = now
        self.login = login
        self.setup_url = setup_url

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

        if screen["name"] == "prompt_text":  # free text: the course's own system prompt
            if text in ("", "0"):
                stack.pop()
                return [self._render(s, stack)]
            course = s.get(Course, screen["course"])
            course.prompt, course.prompt_replaces = text[:6000], bool(screen["replace"])
            stack.pop()
            how = "It replaces the built-in prompt" if course.prompt_replaces else "It is added to the built-in prompt"
            return [Reply(f"Saved for {course.name}. {how}, from the next solve or redo."), self._render(s, stack)]

        if screen["name"] == "theme_text":  # free text: the course's theme artifact link
            stack.pop()
            if text in ("", "0"):
                return [self._render(s, stack)]
            link = text.strip().split("?")[0].rstrip("/")
            if not THEME_LINK.fullmatch(link):
                return [Reply("That is not a Claude artifact link (https://claude.ai/artifact/…). Nothing changed."), self._render(s, stack)]
            course = s.get(Course, screen["course"])
            course.theme = link
            return [Reply(f"Saved. {course.name} reports use that theme, from the next solve or redo."), self._render(s, stack)]

        if screen["name"] == "model_text":  # free text: a model name
            stack.pop()
            if text in ("", "0"):
                return [self._render(s, stack)]
            return [Reply(self._set_model(text)), self._render(s, stack)]

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
        if kind == "prompt_text":
            stack.append({"name": "prompt_text", "course": option["course"], "replace": option["replace"]})
            what = ("the whole system prompt for this course; it replaces the built-in one" if option["replace"]
                    else "your instructions for this course; they are added to the built-in prompt")
            return [Reply(f"Type {what}, in one message. 0 cancels.")]
        if kind == "prompt_show":
            course = s.get(Course, option["course"])
            return [Reply(course.prompt or "No prompt of your own is set for this course.")]
        if kind == "prompt_clear":
            course = s.get(Course, option["course"])
            course.prompt, course.prompt_replaces = None, False
            return [Reply(f"Removed. {course.name} uses the built-in prompt only."), self._render(s, stack)]
        if kind == "effort":
            if option["effort"]:
                self.kv.set(actions.EFFORT_KEY, option["effort"])
                said = f"The solver now uses {option['effort']} effort, from the next solve or redo."
            else:
                self.kv.delete(actions.EFFORT_KEY)
                said = "The solver uses the default effort again."
            return [Reply(said), self._render(s, stack)]
        if kind == "cap":
            if option["lift"]:
                self.kv.set(actions.CAP_LIFTED_KEY, self.now().astimezone(LMS_TZ).date().isoformat())
                said = "The daily limit is lifted for today. Waiting work is picked up within a minute."
            else:
                self.kv.delete(actions.CAP_LIFTED_KEY)
                said = "The daily limit is back on."
            return [Reply(said), self._render(s, stack)]
        if kind == "prompt_builtin":
            return [Reply(system_prompt())]
        if kind == "theme_text":
            stack.append({"name": "theme_text", "course": option["course"]})
            return [Reply("Paste the theme's artifact link (https://claude.ai/artifact/…), in one message. 0 cancels.")]
        if kind == "theme_clear":
            course = s.get(Course, option["course"])
            course.theme = None
            return [Reply(f"{course.name} uses the default theme again."), self._render(s, stack)]
        if kind == "model":
            return [Reply(self._set_model(option["model"])), self._render(s, stack)]
        if kind == "model_text":
            stack.append({"name": "model_text"})
            return [Reply("Type the model name, e.g. claude-opus-5-5, in one message. 0 cancels.")]
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

    def _set_model(self, model: str) -> str:
        model = model.strip()
        if not model:
            self.kv.delete(actions.MODEL_KEY)
            return "The solver uses the default model again."
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,79}", model):
            return "That does not look like a model name. Nothing changed."
        self.kv.set(actions.MODEL_KEY, model)
        return f"The solver now uses {model}, from the next solve or redo."

    def _execute(self, s: Session, c: dict) -> str:
        action = c["action"]
        if action == "mode":
            return actions.set_mode(s.get(Course, c["course"]), c["mode"])[1]
        if action == "password":
            password = actions.reset_password(self.kv)
            where = self.setup_url.removesuffix("/setup") + "/login" if self.setup_url else "the web page"
            return f"The new web page password is:\n{password}\nSign in at {where}. Every browser was signed out; ADMIN_PASSWORD no longer works."
        if action == "delete":
            a = s.get(Assignment, c["assignment"])
            if a is None:
                return "That assignment is no longer listed."
            return actions.delete_submission(s, a, self.kv, self.now(), "WhatsApp")[1]
        job = s.get(Job, c["job"])
        if job is None:
            return "That job no longer exists."
        if action == "approve":
            return actions.approve(s, job, self.kv, self.now(), "WhatsApp")[1]
        if action == "reject":
            return actions.reject(s, job, "WhatsApp")[1]
        if action == "redo":
            return actions.redo(s, job, c.get("note") or "", "WhatsApp")[1]
        raise ValueError(f"Unknown action {action}")

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
            (f"Lab papers: {_paper_summary(p, now)}", _open({"name": "section", "kind": PAPER})),
            (f"Waiting for your approval ({sum(_in(i, 'approval', now) for i in a)})",
             _open({"name": "list", "kind": ASSIGNMENT, "category": "approval"})),
            (f"Due or starting in the next 48 hours ({sum(_in(i, 'due_soon', now) for i in items)})",
             _open({"name": "list", "kind": None, "category": "due_soon"})),
            ("By course", _open({"name": "courses"})),
            ("Course modes (auto, approve, notify)", _open({"name": "modes"})),
            ("Check the LMS now", {"do": "check"}),
            ("System status", {"do": "status"}),
            (f"Solver model: {self.kv.get(actions.MODEL_KEY) or 'default'} (change)", _open({"name": "model"})),
            (f"Solver effort: {self.kv.get(actions.EFFORT_KEY) or 'default'} (change)", _open({"name": "effort"})),
            (f"Daily solve limit: {'lifted for today' if self._cap_lifted() else 'on'} (change)", _open({"name": "cap"})),
            ("Reset the web page password", {"do": "confirm", "action": "password"}),
        ]
        return "LMS BOT", [], options

    def _cap_lifted(self) -> bool:
        return self.kv.get(actions.CAP_LIFTED_KEY) == self.now().astimezone(LMS_TZ).date().isoformat()

    def _screen_model(self, s: Session, screen: dict):
        now = self.kv.get(actions.MODEL_KEY)
        lines = [f"Now: {now or 'default (SOLVER_MODEL, else your plan’s)'}.", "The Claude model that solves your work, from the next solve or redo."]
        options = [("Default", {"do": "model", "model": ""})]
        options += [(f"{label} ({m})", {"do": "model", "model": m}) for label, m in actions.MODELS]
        options.append(("Type a model name", {"do": "model_text"}))
        return "Solver model", lines, options

    def _screen_effort(self, s: Session, screen: dict):
        now = self.kv.get(actions.EFFORT_KEY)
        lines = [f"Now: {now or 'default'}.", "How hard the model thinks, from the next solve or redo. Higher uses more of your plan."]
        options = [("Default", {"do": "effort", "effort": ""})] + [(e, {"do": "effort", "effort": e}) for e in actions.EFFORTS]
        return "Solver effort", lines, options

    def _screen_cap(self, s: Session, screen: dict):
        lifted = self._cap_lifted()
        lines = [f"Now: {'lifted for today' if lifted else 'on'}.",
                 "The limit on solver runs per day. Lifting it lets more be solved today; your Claude plan's own limits still apply.",
                 "It is back on by itself tomorrow."]
        options = [("Put the limit back", {"do": "cap", "lift": False})] if lifted else [("Lift the limit for today", {"do": "cap", "lift": True})]
        return "Daily solve limit", lines, options

    def _screen_section(self, s: Session, screen: dict):
        kind, course_id = screen["kind"], screen.get("course")
        items = self._items(s, kind=kind, course_id=course_id)
        now = self.now()
        options = [
            (f"{label} ({sum(_in(i, key, now) for i in items)})",
             _open({"name": "list", "kind": kind, "course": course_id, "category": key}))
            for key, label in CATEGORIES[kind]
        ]
        where = s.get(Course, course_id).name if course_id else ("all courses" if kind == ASSIGNMENT else "all labs")
        return f"{'Assignments' if kind == ASSIGNMENT else 'Lab papers'} · {where}", [], options

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
        what = {ASSIGNMENT: "Assignments", PAPER: "Lab papers", None: "Assignments and lab papers"}[kind]
        where = f" · {s.get(Course, course_id).name}" if course_id else ""
        lines = [] if ids else ["Nothing here."]
        return f"{what}: {LABELS[category].split(' (')[0].lower()}{where}", lines, options

    def _screen_item(self, s: Session, screen: dict):
        a = s.get(Assignment, screen["id"])
        if a is None:
            return "Gone", ["This item is no longer listed."], []
        now = self.now()
        job = a.jobs[-1] if a.jobs else None
        what = f"Paper {a.number}" if a.kind == PAPER else f"Assignment {a.number}"
        lines = [a.title, f"{a.course.name} · {what}"]
        if a.kind == PAPER and a.starts_at:
            lines.append(f"{_window(a)} ({_paper_timing(a, now)})")
        elif a.deadline:
            lines.append(f"Due {_when(a.deadline)}" + (f" ({_relative(a.deadline, now)})" if not a.deadline_passed else ""))
        else:
            lines.append("No deadline shown")
        lines.append(f"Status: {_status(a, now)}")
        lines.append(f"Marks: {a.marks or 'not marked yet'}")
        if a.remarks:
            lines.append(f"Teacher's note: {a.remarks}")
        if a.kind == ASSIGNMENT:
            if job:
                state = actions.job_words(job) + (f": {job.last_error}" if job.state == JobState.NEEDS_HUMAN and job.last_error else "")
                lines.append(f"Service: {state} (revision {job.revision})")
            elif a.skipped_reason:
                lines.append(f"Service: not handled; {a.skipped_reason.lower()}")
        else:
            lines.append("Lab papers are tracked only; the service never solves them.")

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
            if actions.can_redo(job):
                options.append(("Redo with a note", {"do": "note", "job": job.id}))
            options.append(("History", {"do": "history", "job": job.id}))
        if actions.can_delete(a):
            options.append(("Delete my submission from the LMS", {"do": "confirm", "action": "delete", "assignment": a.id}))
        return "", lines, options

    def _screen_courses(self, s: Session, screen: dict):
        now = self.now()
        options = []
        for c in self._courses(s):
            items = self._items(s, course_id=c.id)
            a = sum(_in(i, "remaining", now) for i in items if i.kind == ASSIGNMENT)
            p = sum(_in(i, "upcoming", now) or _in(i, "open_now", now) for i in items if i.kind == PAPER)
            parts = [f"{a} assignment{'s' * (a != 1)} remaining"] * bool(a) + [f"{p} paper{'s' * (p != 1)} coming"] * bool(p)
            label = f"{c.name} ({', '.join(parts)})" if parts else c.name
            options.append((label, _open({"name": "course", "id": c.id})))
        return "Courses", [] if options else ["No courses yet: the first poll has not run."], options

    def _screen_course(self, s: Session, screen: dict):
        c = s.get(Course, screen["id"])
        now = self.now()
        items = self._items(s, course_id=c.id)
        a = [i for i in items if i.kind == ASSIGNMENT]
        p = [i for i in items if i.kind == PAPER]
        options = [(f"Assignments: {_summary(a, now)}", _open({"name": "section", "kind": ASSIGNMENT, "course": c.id}))]
        if is_lab(c.name):
            options.append((f"Lab papers: {_paper_summary(p, now)}", _open({"name": "section", "kind": PAPER, "course": c.id})))
        options.append((f"Mode: {c.mode} (change)", _open({"name": "mode_pick", "course": c.id})))
        options.append((f"System prompt: {_prompt_words(c)} (change)", _open({"name": "prompt", "course": c.id})))
        options.append((f"Report theme: {'its own' if c.theme else 'default'} (change)", _open({"name": "theme", "course": c.id})))
        return c.name, [], options

    def _screen_theme(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        lines = [f"Now: {c.theme or 'the default theme, ' + THEME_ARTIFACT}",
                 "The design system artifact this course's reports are built from. Courses without their own use the default."]
        options = [("Set this course's theme link", {"do": "theme_text", "course": c.id})]
        if c.theme:
            options.append(("Use the default theme", {"do": "theme_clear", "course": c.id}))
        return f"{c.name} · report theme", lines, options

    def _screen_prompt(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        lines = [f"Now: {_prompt_words(c)}.", "What Claude is told when it solves this course's work."]
        options = [
            ("Add my instructions to the built-in prompt", {"do": "prompt_text", "course": c.id, "replace": False}),
            ("Replace the built-in prompt with mine", {"do": "prompt_text", "course": c.id, "replace": True}),
        ]
        options.append(("Show the built-in prompt", {"do": "prompt_builtin"}))
        if c.prompt:
            options += [("Show my prompt", {"do": "prompt_show", "course": c.id}),
                        ("Remove my prompt (built-in only)", {"do": "prompt_clear", "course": c.id})]
        return f"{c.name} · system prompt", lines, options

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
        elif action == "password":
            question = ["Reset the web page password?", "A new one is made and sent here; the old one stops working and every browser is signed out."]
        elif action == "delete":
            a = s.get(Assignment, screen["assignment"])
            if a is None:
                return "", ["That assignment is no longer listed."], [("OK", {"do": "no"})]
            question = [f"Delete your submission for {a.title} from the LMS?",
                        f"The LMS shows {a.submitted_file}. Nothing is uploaded in its place until you approve a report or upload one yourself."]
        else:
            job = s.get(Job, screen["job"])
            if job is None:
                return "", ["That job no longer exists."], [("OK", {"do": "no"})]
            a = job.assignment
            typed = ["The sheet asks for handwritten work; this uploads the typed report as it is."] * actions.handwritten(job)
            question = {
                "approve": [f"Upload the solved PDF for {a.title} to the LMS now?", *typed],
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
        lines.append(self._claude_line())
        lines.append(UsageGuard(self.kv).status_line(self.now()))
        jobs = s.scalars(select(Job)).all()
        active = [j for j in jobs if j.state not in TERMINAL_STATES]
        lines.append(f"Jobs: {len(active)} active, {sum(j.state == JobState.AWAITING_APPROVAL for j in jobs)} waiting for you")
        month = (self.now().year, self.now().month)
        lines.append(f"Tokens used this month: {sum(j.cost_tokens for j in jobs if (j.created_at.year, j.created_at.month) == month):,}")
        return "\n".join(lines)

    def _claude_line(self) -> str:
        where = self.setup_url or "the setup page"
        if self.login is not None:
            st = self.login.status()
            if st.logged_in:
                return "Claude: signed in" + (f" as {st.email}" if st.email else "")
            return f"Claude: not signed in; sign in at {where}"
        if self.kv.get(SIGNED_OUT_KEY):
            return f"Claude: signed out; sign in again at {where}"
        return "Claude: no sign-in problem seen"

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
    if a.kind == PAPER:
        return _paper_in(a, category, now)
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


def _paper_submitted(a: Assignment) -> bool:
    return bool(a.submitted_file) or a.paper_status == PAPER_SUBMITTED


def _paper_missed(a: Assignment, finished: bool) -> bool:
    """Red on the LMS, or over and still white. Without a colour, a hidden submission is unknown."""
    if a.paper_status == PAPER_MISSED:
        return True
    if not finished or _paper_submitted(a):
        return False
    return a.paper_status == PAPER_NOT_SUBMITTED or not a.submission_hidden


def _paper_in(a: Assignment, category: str, now: datetime) -> bool:
    finished = a.deadline_passed or a.paper_status == PAPER_MISSED or (a.deadline is not None and a.deadline <= now)
    started = a.starts_at is None or a.starts_at <= now
    submitted = _paper_submitted(a)
    if category == "all":
        return True
    if category in ("upcoming",):
        return not finished and not started
    if category == "open_now":
        return not finished and started
    if category == "remaining":
        return not finished and not submitted
    if category == "due_soon":
        return not finished and not submitted and a.starts_at is not None and a.starts_at - now <= DUE_SOON
    if category == "finished":
        return finished
    if category == "submitted":
        return submitted
    if category == "missed":
        return _paper_missed(a, finished)
    if category == "marked":
        return bool(a.marks)
    return False


def _paper_summary(items: list[Assignment], now: datetime) -> str:
    open_now = [i for i in items if _paper_in(i, "open_now", now)]
    upcoming = sorted((i for i in items if _paper_in(i, "upcoming", now)), key=lambda i: i.starts_at)
    if open_now:
        return f"{len(open_now)} open now"
    if upcoming:
        return f"{len(upcoming)} upcoming, next {_when(upcoming[0].starts_at)}"
    return "none scheduled" if not items else "none upcoming"


def _window(a: Assignment) -> str:
    end = a.deadline.astimezone(LMS_TZ).strftime("%I:%M %p").lstrip("0") if a.deadline else "?"
    return f"{_when(a.starts_at)} to {end}"


def _paper_timing(a: Assignment, now: datetime) -> str:
    if _paper_in(a, "finished", now):
        return "ended"
    if a.starts_at and a.starts_at > now:
        return f"starts {_relative(a.starts_at, now)}"
    return f"open now, {_relative(a.deadline, now).removeprefix('in ')} left" if a.deadline else "open now"


def _paper_phase(a: Assignment, now: datetime) -> str:
    if _paper_submitted(a):
        return "submitted"
    if _paper_in(a, "missed", now):
        return "missed"
    timing = _paper_timing(a, now)
    return "finished" if timing == "ended" else timing


def _order(category: str):
    far = datetime.max.replace(tzinfo=LMS_TZ)
    if category in ("remaining", "due_soon", "approval", "pipeline", "needs_you", "upcoming", "open_now"):
        return lambda a: (a.starts_at or a.deadline or far, a.id)
    return lambda a: (-(a.deadline or far).timestamp(), -a.id)


def _summary(items: list[Assignment], now: datetime) -> str:
    left = sum(_in(i, "remaining", now) for i in items)
    soon = sum(_in(i, "due_soon", now) for i in items)
    if not items:
        return "none yet"
    if not left:
        return "nothing remaining"
    return f"{left} remaining" + (f", {soon} due soon" if soon else "")


def _prompt_words(c: Course) -> str:
    if not c.prompt:
        return "built-in"
    return "yours, replacing the built-in one" if c.prompt_replaces else "built-in plus your instructions"


def _status(a: Assignment, now: datetime) -> str:
    if a.kind == PAPER:
        if _paper_submitted(a):
            return "submitted"
        if _paper_in(a, "missed", now):
            return "missed: the exam ended with no submission"
        if _paper_in(a, "finished", now):
            return "finished; the LMS does not show whether you submitted"
        return "not started" if _paper_in(a, "upcoming", now) else "open; nothing submitted yet"
    if a.submitted_file:
        if a.delete_requested_at:
            return "submitted; being deleted from the LMS, as you asked"
        return "submitted" + ("; still open, you can delete it and resubmit" if not a.deadline_passed else "")
    if a.deadline_passed:
        return "missed: closed with no submission"
    return "not submitted"


def _line(a: Assignment, now: datetime, *, with_course: bool) -> str:
    bits = [a.title]
    if with_course:
        bits.append(a.course.name)
    if a.kind == PAPER and a.starts_at:
        bits.append(_window(a))
        bits.append(a.marks or _paper_phase(a, now))
        return " · ".join(bits)
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
