"""The WhatsApp menu: numbered screens, you reply with a digit.

Screens form a stack per chat: a digit opens an option, 0 goes back, "menu" goes to
the top. Lists keep the numbering they were shown with, so an item that appears
between two messages never shifts your choice. A chat idle for 10 minutes starts
again at the main menu. Approve and Reject ask 1. Yes / 2. No first.

The main menu is short: your work (assignments, lab papers, what needs you, what is due),
the courses (lecture notes, course plan, course outline and each course's settings) and the
service's own settings. The same menu answers on WhatsApp and on Discord.

Files: a lecture, an outline or a question file is sent as a document, the course plan as a
picture of its table. A file you send is used for what the open screen says: your own
submission, a reference for solving one assignment, or a reference for a course's theme.
Lecture notes, plan and outline are read from the LMS when you ask; none of that uses Claude.

Everything else reads the database the poller keeps current; "Check the LMS now" asks the
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

from . import actions, files
from .solver.prompt import THEME_ARTIFACT, system_prompt
from .db.models import (TERMINAL_STATES, Artifact, Assignment, ChatSession, Course, CourseMode, Event, Job, JobState,
                        Summary, SummaryState, utcnow)
from .kv import KeyValue
from .lms.bahria_pages import LMS_TZ, closes_at
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
        ("go", "Waiting for your go-ahead"),
        ("input", "Claude is waiting for your answer"),
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
ACTION = ("approval", "go", "input", "needs_you")  # what waits for you
MAX_SUMMARY_FILES = 12



@dataclass(frozen=True)
class Reply:
    text: str
    file: Path | None = None


@dataclass(frozen=True)
class Incoming:
    """A file you sent in the chat."""

    name: str
    data: bytes


class MaterialsError(Exception):
    """Lecture notes, a plan or an outline could not be fetched; the message is for you."""


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
        data_dir: Path | None = None,  # where files you send are kept; without it they are refused
        materials=None,  # materials.Materials: lecture notes, course plan and outline from the LMS
    ) -> None:
        self.sessions = sessions
        self.kv = kv
        self.now = now
        self.login = login
        self.setup_url = setup_url
        self.data_dir = data_dir
        self.materials = materials

    # --- entry point ---

    def handle(self, chat_id: str, text: str, file: Incoming | None = None) -> list[Reply]:
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
            replies = self._file(s, stack, file) if file is not None else self._dispatch(s, stack, text)
            chat.state = json.dumps({"stack": stack})
            chat.updated_at = self.now()
            return replies

    def _dispatch(self, s: Session, stack: list[dict], text: str) -> list[Reply]:
        low = text.lower()
        if low in ("menu", "hi", "hello", "start", "/start", "help"):
            del stack[1:]
            return [self._render(s, stack)]
        screen = stack[-1]

        if screen["name"] == "file":  # waiting for a file
            if text == "0":
                stack.pop()
                return [self._render(s, stack)]
            return [Reply("Send the file as a document, or 0 to cancel.")]

        if screen["name"] == "sum_text":  # free text: which weeks to summarize
            if text in ("", "0"):
                stack.pop()
                return [self._render(s, stack)]
            picked, problem = _pick_weeks(text, screen["items"])
            if problem:
                return [Reply(f"{problem} Reply like 1,3,5 or 2-4 or all. 0 cancels.")]
            stack[-1] = {"name": "sum_style", "course": screen["course"], "picked": picked}
            return [self._render(s, stack)]

        if screen["name"] == "answer_text":  # free text: your answer to Claude's questions
            stack.pop()
            if text in ("", "0"):
                return [self._render(s, stack)]
            job = s.get(Job, screen["job"])
            said = actions.answer(s, job, text, "the chat")[1] if job else "That job no longer exists."
            return [Reply(said), self._render(s, stack)]

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
            stack[-1] = {"name": "confirm", "action": "redo", "job": screen["job"], "note": text[:1000],
                         "strong": bool(screen.get("strong"))}
            return [self._render(s, stack)]

        if text == "0":
            if len(stack) > 1:
                stack.pop()
            return [self._render(s, stack)]
        if not text.isdigit() or text not in (screen.get("options") or {}):
            return [self._render(s, stack, hint="Reply with one of the numbers below.")]
        return self._choose(s, stack, screen["options"][text])

    # --- files you send ---

    def _file(self, s: Session, stack: list[dict], file: Incoming) -> list[Reply]:
        screen = stack[-1]
        if self.data_dir is None:
            return [Reply("Files cannot be kept on this server.")]
        if len(file.data) > files.MAX_BYTES:
            return [Reply(f"{file.name} is over {files.MAX_BYTES // 1048576} MB; nothing was saved.")]
        if not file.data:
            return [Reply("That file arrived empty; send it again as a document.")]
        if screen["name"] == "file":
            stack.pop()
            return self._use_file(s, stack, screen["purpose"], screen.get("assignment") or screen.get("course"), file)
        if screen["name"] == "theme":
            return self._use_file(s, stack, "theme", screen["course"], file)
        if screen["name"] == "item":
            a = s.get(Assignment, screen["id"])
            if a is not None and a.kind == ASSIGNMENT and not a.deadline_passed:
                held = files.keep(self.data_dir / "inbox", f"{a.id}-{file.name}", file.data)
                stack.append({"name": "file_what", "assignment": a.id, "path": str(held), "file": file.name})
                return [self._render(s, stack)]
        return [Reply("I did not know what that file is for. Open an assignment (to upload your own work or add a "
                      "reference for solving it) or a course's Report theme, then send it.")]

    def _use_file(self, s: Session, stack: list[dict], purpose: str, target: int, file: Incoming) -> list[Reply]:
        if purpose == "theme":
            course = s.get(Course, target)
            folder = files.theme_dir(self.data_dir, course)
            if len(files.listed(folder)) >= files.MAX_FILES:
                return [Reply(f"{course.name} already has {files.MAX_FILES} theme reference files; remove them first."), self._render(s, stack)]
            path = files.keep(folder, file.name, file.data)
            return [Reply(f"Saved {path.name} as a theme reference for {course.name}. It is used with the theme artifact, "
                          "from the next solve or redo."), self._render(s, stack)]
        a = s.get(Assignment, target)
        if a is None:
            return [Reply("That assignment is no longer listed."), self._render(s, stack)]
        if purpose == "ref":
            folder = files.refs_dir(self.data_dir, a)
            if len(files.listed(folder)) >= files.MAX_FILES:
                return [Reply(f"{a.title} already has {files.MAX_FILES} reference files; remove them first."), self._render(s, stack)]
            path = files.keep(folder, file.name, file.data)
            return [Reply(f"Saved {path.name} as a reference for {a.title}. It is used from the next solve or redo."),
                    self._render(s, stack)]
        suffix = Path(file.name).suffix.lower()
        if suffix not in actions.UPLOAD_TYPES:
            return [Reply(f"The LMS takes {', '.join(actions.UPLOAD_TYPES)}, not {suffix or 'a file without a type'}. Nothing was saved."),
                    self._render(s, stack)]
        if not actions.can_upload_own(a):
            return [Reply(f"{a.title} cannot take an upload now" + ("; delete the submission the LMS shows first." if a.submitted_file else ".")),
                    self._render(s, stack)]
        path = files.keep(files.own_dir(self.data_dir, a), file.name, file.data, only=True)
        stack.append({"name": "confirm", "action": "own", "assignment": a.id, "path": str(path)})
        return [self._render(s, stack)]

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
        if kind in ("plan", "outline", "lecture"):
            return self._material(s, stack, option)
        if kind == "sum_start":
            items = option["items"]
            stack.append({"name": "sum_text", "course": option["course"], "items": items})
            weeks = {}
            for week, title, _ in items:
                weeks.setdefault(week, [title, 0])[1] += 1
            lines = ["Which lectures should be summarized? Reply with the week numbers: 1,3,5 or 2-4, or all. 0 cancels."]
            lines += [f"Week {w}: {title}" + (f" ({n} files)" if n > 1 else "") for w, (title, n) in weeks.items()]
            return [Reply("\n".join(lines))]
        if kind == "summarize":
            return self._summarize(s, stack, option)
        if kind == "lectures_mode":
            course = s.get(Course, option["course"])
            said = actions.set_lectures(course, option["mode"])[1]
            stack.pop()
            return [Reply(said), self._render(s, stack)]
        if kind == "await_file":
            stack.append({"name": "file", **{k: v for k, v in option.items() if k != "do"}})
            what = {"own": f"your own work, as one file ({', '.join(actions.UPLOAD_TYPES)})",
                    "ref": "the reference file", "theme": "the theme reference file"}[option["purpose"]]
            return [Reply(f"Send {what} now, as a document. 0 cancels.")]
        if kind == "held_file":  # a file sent on an assignment's screen: now you said what it is for
            held = stack.pop()
            path = Path(held["path"])
            if not path.is_file():
                return [Reply("That file is gone; send it again."), self._render(s, stack)]
            file = Incoming(held["file"], path.read_bytes())
            path.unlink()
            return self._use_file(s, stack, option["purpose"], held["assignment"], file)
        if kind == "refs_clear":
            a = s.get(Assignment, option["assignment"])
            for p in files.listed(files.refs_dir(self.data_dir, a)) if a and self.data_dir else []:
                p.unlink()
            return [Reply("The reference files were removed."), self._render(s, stack)]
        if kind == "theme_files_clear":
            course = s.get(Course, option["course"])
            for p in files.listed(files.theme_dir(self.data_dir, course)) if self.data_dir else []:
                p.unlink()
            return [Reply(f"{course.name}'s theme reference files were removed."), self._render(s, stack)]
        if kind == "go":
            job = s.get(Job, option["job"])
            return [Reply(actions.go(s, job, "the chat")[1] if job else "That job no longer exists."), self._render(s, stack)]
        if kind == "answer_text":
            stack.append({"name": "answer_text", "job": option["job"]})
            return [Reply("Type your answer, in one message. To send a file as well, do that first with Add a reference file. 0 cancels.")]
        if kind == "answer_none":
            job = s.get(Job, option["job"])
            return [Reply(actions.answer(s, job, "", "the chat")[1] if job else "That job no longer exists."), self._render(s, stack)]
        if kind == "solve":
            course = s.get(Course, option["course"])
            said = actions.set_solve(course, option["solve"])[1]
            stack.pop()
            return [Reply(said), self._render(s, stack)]
        if kind == "safe":
            course = s.get(Course, option["course"])
            course.safe = bool(option["on"])
            said = (f"Safe mode is on for {course.name}: a report still waiting for your approval is uploaded when one hour is left."
                    if course.safe else f"Safe mode is off for {course.name}.")
            stack.pop()
            return [Reply(said), self._render(s, stack)]
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
                said = "The effort is picked per job again."
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
            stack.append({"name": "note", "job": option["job"], "strong": bool(option.get("strong"))})
            if option.get("strong"):
                return [Reply("Opus will solve it again from the sheet. Type what was wrong or what should change, in one message. 0 cancels.")]
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
            said = actions.set_mode(course, option["mode"])[1]
            stack.pop()
            return [Reply(said), self._render(s, stack)]
        raise ValueError(f"Unknown option {kind}")

    def _material(self, s: Session, stack: list[dict], option: dict) -> list[Reply]:
        """A course plan (as a picture), a course outline or a lecture file, fetched from the LMS now."""
        course = s.get(Course, option["course"])
        if self.materials is None:
            return [Reply("Course material is not available on this server.")]
        try:
            if option["do"] == "plan":
                path = self.materials.plan(course)
                return [Reply(f"Course plan · {course.name}", file=path) if path else
                        Reply(f"No course plan is on the LMS yet for {course.name}."), *([] if path else [self._render(s, stack)])]
            if option["do"] == "outline":
                path = self.materials.outline(course)
                return [Reply(f"Course outline · {course.name}", file=path) if path else
                        Reply(f"No course outline is on the LMS yet for {course.name}."), *([] if path else [self._render(s, stack)])]
            path = self.materials.lecture(course, option["week"], option["file"])
            return [Reply(f"{course.name} · week {option['week']} · {option['title']}", file=path)]
        except MaterialsError as e:
            return [Reply(str(e))]

    def _summarize(self, s: Session, stack: list[dict], option: dict) -> list[Reply]:
        """Fetch the picked lecture files (the worker reads them from the server) and queue the notes."""
        screen = stack.pop()
        course = s.get(Course, screen["course"])
        picked = screen["picked"]
        if self.materials is None:
            return [Reply("Course material is not available on this server."), self._render(s, stack)]
        try:
            lectures = [{"week": week, "title": title, "file": self.materials.lecture(course, week, name).name}
                        for week, title, name in picked]
        except MaterialsError as e:
            return [Reply(f"Nothing was queued: {e}"), self._render(s, stack)]
        actions.summarize(s, course, lectures, option["style"])
        weeks = ", ".join(dict.fromkeys(week for week, _, _ in picked))
        return [Reply(f"Queued: {option['style']} notes for {course.name}, week{'s' * (',' in weeks)} {weeks} ({len(lectures)} "
                      f"file{'s' * (len(lectures) != 1)}). Haiku first picks the model that writes them; the PDF comes here when "
                      "it is ready, after any assignment that is waiting to be solved."), self._render(s, stack)]

    def _set_model(self, model: str) -> str:
        model = model.strip()
        if not model:
            self.kv.delete(actions.MODEL_KEY)
            return "Haiku now picks the model for each job (Sonnet, or Opus for hard work)."
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
        if action == "own":
            a = s.get(Assignment, c["assignment"])
            if a is None:
                return "That assignment is no longer listed."
            return actions.upload_own(s, a, c["path"], self.kv, self.now(), "the chat")[1]
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
            return actions.redo(s, job, c.get("note") or "", "WhatsApp", strong=bool(c.get("strong")))[1]
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
            (f"Needs your action ({sum(_in(i, 'action', now) for i in a)})", _open({"name": "action"})),
            (f"Due or starting in the next 48 hours ({sum(_in(i, 'due_soon', now) for i in items)})",
             _open({"name": "list", "kind": None, "category": "due_soon"})),
            ("Courses: lecture notes, plan, outline, settings", _open({"name": "courses"})),
            ("Settings", _open({"name": "settings"})),
            ("Check the LMS now", {"do": "check"}),
            ("System status", {"do": "status"}),
        ]
        return "LMS BOT", [], options

    def _screen_action(self, s: Session, screen: dict):
        items = self._items(s, kind=ASSIGNMENT)
        now = self.now()
        options = [(f"{LABELS[key]} ({sum(_in(i, key, now) for i in items)})",
                    _open({"name": "list", "kind": ASSIGNMENT, "category": key})) for key in ACTION]
        return "Needs your action", [], options

    def _screen_settings(self, s: Session, screen: dict):
        options = [
            (f"Solver model: {self.kv.get(actions.MODEL_KEY) or 'auto, picked by Haiku'} (change)", _open({"name": "model"})),
            (f"Solver effort: {self.kv.get(actions.EFFORT_KEY) or 'auto'} (change)", _open({"name": "effort"})),
            (f"Daily solve limit: {'lifted for today' if self._cap_lifted() else 'on'} (change)", _open({"name": "cap"})),
            ("Course settings: solving, uploading, safe mode, prompt, theme", _open({"name": "modes"})),
            ("Reset the web page password", {"do": "confirm", "action": "password"}),
        ]
        return "Settings", [], options

    def _cap_lifted(self) -> bool:
        return self.kv.get(actions.CAP_LIFTED_KEY) == self.now().astimezone(LMS_TZ).date().isoformat()

    def _screen_model(self, s: Session, screen: dict):
        now = self.kv.get(actions.MODEL_KEY)
        lines = [f"Now: {now or 'auto'}.",
                 "Auto: Haiku reads each sheet first and sends it to Sonnet, or to Opus when it is hard, which saves usage. "
                 "A model picked here is used for everything instead. From the next solve or redo."]
        options = [("Auto: Haiku picks per job", {"do": "model", "model": ""})]
        options += [(f"{label} ({m})", {"do": "model", "model": m}) for label, m in actions.MODELS]
        options.append(("Type a model name", {"do": "model_text"}))
        return "Solver model", lines, options

    def _screen_effort(self, s: Session, screen: dict):
        now = self.kv.get(actions.EFFORT_KEY)
        lines = [f"Now: {now or 'auto'}.", "How hard the model thinks, from the next solve or redo. Higher uses more of your plan. "
                                           "Auto: Haiku picks low, medium or high per job."]
        options = [("Auto", {"do": "effort", "effort": ""})] + [(e, {"do": "effort", "effort": e}) for e in actions.EFFORTS]
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
            lines.append(f"Due {_when(a.deadline)}" + (f" ({_due_words(a, now)})" if not a.deadline_passed else ""))
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
                if job.state == JobState.NEEDS_INPUT:
                    lines += ["Claude asks:"] + [f"• {q}" for q in _questions(job)]
                lines += _triage_lines(job)
            elif a.skipped_reason:
                lines.append(f"Service: not handled; {a.skipped_reason.lower()}")
        else:
            lines.append("Lab papers are tracked only; the service never solves them.")
        refs = files.listed(files.refs_dir(self.data_dir, a)) if self.data_dir and a.kind == ASSIGNMENT else []
        if refs:
            lines.append("Your reference files: " + ", ".join(p.name for p in refs))
        if a.upload_requested_at:
            lines.append("Your own file is being uploaded now.")

        options = []
        if _question_file(a):
            options.append(("Send the paper" if a.kind == PAPER else "Send the question file", {"do": "file", "assignment": a.id}))
        if job:
            for art in sorted(job.artifacts, key=lambda x: x.id):
                if art.kind in ("pdf", "docx", "notebook", "receipt"):
                    label = {"pdf": "Send the solved PDF", "docx": "Send the DOCX", "notebook": "Send the notebook and code", "receipt": "Send the LMS receipt"}[art.kind]
                    options.append((label, {"do": "artifact", "artifact": art.id}))
            if job.state == JobState.WAITING_GO and not a.deadline_passed:
                options.append(("Solve it now", {"do": "go", "job": job.id}))
            if job.state == JobState.NEEDS_INPUT and not a.deadline_passed:
                options.append(("Answer Claude", {"do": "answer_text", "job": job.id}))
                options.append(("Solve it without an answer", {"do": "answer_none", "job": job.id}))
            if job.state == JobState.AWAITING_APPROVAL:
                label = "Approve and upload" + (" (replaces what is on the LMS)" if a.submitted_file else "")
                options.append((label, {"do": "confirm", "action": "approve", "job": job.id}))
                options.append(("Reject", {"do": "confirm", "action": "reject", "job": job.id}))
            if actions.can_redo(job):
                options.append(("Redo with a note", {"do": "note", "job": job.id}))
                options.append(("Redo with Opus", {"do": "note", "job": job.id, "strong": True}))
            options.append(("History", {"do": "history", "job": job.id}))
        if a.kind == ASSIGNMENT and not a.deadline_passed and self.data_dir:
            options.append(("Add a reference file for solving", {"do": "await_file", "purpose": "ref", "assignment": a.id}))
            if refs:
                options.append(("Remove the reference files", {"do": "refs_clear", "assignment": a.id}))
        if actions.can_upload_own(a) and self.data_dir:
            options.append(("Upload my own file", {"do": "await_file", "purpose": "own", "assignment": a.id}))
        if actions.can_delete(a):
            options.append(("Delete my submission from the LMS", {"do": "confirm", "action": "delete", "assignment": a.id}))
        return "", lines, options

    def _screen_file_what(self, s: Session, screen: dict):
        a = s.get(Assignment, screen["assignment"])
        options = []
        if a is not None and actions.can_upload_own(a):
            options.append(("Upload it to the LMS as my own work", {"do": "held_file", "purpose": "own"}))
        options.append(("Use it as a reference for solving this assignment", {"do": "held_file", "purpose": "ref"}))
        return "", [f"You sent {screen['file']}. What is it for?"], options

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
        options.append(("Lecture notes", _open({"name": "lectures", "course": c.id})))
        options.append(("Course plan", {"do": "plan", "course": c.id}))
        options.append(("Course outline", {"do": "outline", "course": c.id}))
        options.append((f"Settings: {_settings_words(c)}", _open({"name": "course_settings", "course": c.id})))
        return c.name, [], options

    def _screen_course_settings(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        options = [
            (f"Solving: {c.solve} (change)", _open({"name": "solve_pick", "course": c.id})),
            (f"Uploading: {c.mode} (change)", _open({"name": "mode_pick", "course": c.id})),
            (f"Safe mode: {'on' if c.safe else 'off'} (change)", _open({"name": "safe", "course": c.id})),
            (f"Lecture notes for solving: {c.lectures} (change)", _open({"name": "lectures_pick", "course": c.id})),
            (f"System prompt: {_prompt_words(c)} (change)", _open({"name": "prompt", "course": c.id})),
            (f"Report theme: {_theme_words(c, self.data_dir)} (change)", _open({"name": "theme", "course": c.id})),
        ]
        return f"{c.name} · settings", [], options

    def _screen_solve_pick(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        options = [(f"{mode}: {words}", {"do": "solve", "course": c.id, "solve": mode}) for mode, words in actions.SOLVE_WORDS.items()]
        return f"{c.name} · solving, now {c.solve}", ["When a new assignment appears in this course:"], options

    def _screen_lectures_pick(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        options = [(f"{mode}: {words}", {"do": "lectures_mode", "course": c.id, "mode": mode}) for mode, words in actions.LECTURE_WORDS.items()]
        lines = ["The lecture notes the teacher posted on the LMS, as a reference when this course's assignments are solved:"]
        return f"{c.name} · lecture notes for solving, now {c.lectures}", lines, options

    def _screen_sum_style(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        picked = screen["picked"]
        weeks = ", ".join(dict.fromkeys(week for week, _, _ in picked))
        options = [(words, {"do": "summarize", "style": style}) for style, words in actions.SUMMARY_STYLES.items()]
        return f"{c.name} · summary of week{'s' * (',' in weeks)} {weeks}", [f"{len(picked)} lecture file{'s' * (len(picked) != 1)}. Which kind of notes?"], options

    def _screen_safe(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        lines = [f"Now: {'on' if c.safe else 'off'}.",
                 "With safe mode on, a solved report that is still waiting for your approval is uploaded by itself when "
                 "one hour is left to the deadline and nothing is submitted. A report for handwritten work goes up too."]
        options = [("Turn safe mode off", {"do": "safe", "course": c.id, "on": False})] if c.safe else \
                  [("Turn safe mode on", {"do": "safe", "course": c.id, "on": True})]
        return f"{c.name} · safe mode", lines, options

    def _screen_lectures(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        page = screen.get("page", 0)
        if "items" not in screen:  # read from the LMS once, then keep the numbering
            if self.materials is None:
                return f"{c.name} · lecture notes", ["Course material is not available on this server."], []
            try:
                lectures = self.materials.lectures(c)
            except MaterialsError as e:
                return f"{c.name} · lecture notes", [str(e)], []
            screen["items"] = [[f.week, f.title, f.filename] for lec in lectures for f in lec.files]
            screen["notes"] = [f"Week {lec.week} video: {url}" for lec in lectures for url in lec.videos][:12]
        items = screen["items"]
        shown = items[page * PAGE:(page + 1) * PAGE]
        options = [(f"Week {week} · {title} · {_short(name)}",
                    {"do": "lecture", "course": c.id, "week": week, "title": title, "file": name}) for week, title, name in shown]
        if len(items) > (page + 1) * PAGE:
            options.append((f"More ({len(items) - (page + 1) * PAGE} left)",
                            _open({"name": "lectures", "course": c.id, "page": page + 1, "items": items, "notes": []})))
        if items and page == 0:
            options.append(("Summarize lectures into notes", {"do": "sum_start", "course": c.id, "items": items}))
        lines = (["No lecture files are on the LMS yet."] if not items else ["Pick a file to get it here."]) + screen.get("notes", [])
        return f"{c.name} · lecture notes", lines, options

    def _screen_theme(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        sent = files.listed(files.theme_dir(self.data_dir, c)) if self.data_dir else []
        lines = [f"Now: {c.theme or 'the default theme, ' + THEME_ARTIFACT}",
                 "The design system artifact this course's reports are built from. Courses without their own use the default."]
        if sent:
            lines.append("Your reference files, used with the artifact: " + ", ".join(p.name for p in sent))
        options = [("Set this course's theme link", {"do": "theme_text", "course": c.id})]
        if c.theme:
            options.append(("Use the default theme", {"do": "theme_clear", "course": c.id}))
        if self.data_dir:
            options.append(("Send a theme reference file", {"do": "await_file", "purpose": "theme", "course": c.id}))
        if sent:
            options.append(("Remove the reference files", {"do": "theme_files_clear", "course": c.id}))
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
        options = [(f"{c.name}: {_settings_words(c)}", _open({"name": "course_settings", "course": c.id})) for c in self._courses(s)]
        return "Course settings", ["Pick a course."], options

    def _screen_mode_pick(self, s: Session, screen: dict):
        c = s.get(Course, screen["course"])
        options = [
            ("approve: send me the PDF, upload when I say yes", {"do": "mode", "course": c.id, "mode": CourseMode.APPROVE}),
            ("auto: upload after the checks, without asking", {"do": "mode", "course": c.id, "mode": CourseMode.AUTO}),
        ]
        return f"{c.name} · uploading, now {c.mode}", ["When a report is solved in this course:"], options

    def _screen_confirm(self, s: Session, screen: dict):
        action = screen["action"]
        if action == "mode":
            c = s.get(Course, screen["course"])
            question = [f"Switch {c.name} to auto uploading?", "Work will be uploaded under your name without you reading it first."]
        elif action == "own":
            a = s.get(Assignment, screen["assignment"])
            if a is None:
                return "", ["That assignment is no longer listed."], [("OK", {"do": "no"})]
            question = [f"Upload {Path(screen['path']).name} to the LMS for {a.title} now?",
                        "It goes up as your submission, as it is. The service's own work on this assignment stops."]
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
                "approve": [f"Upload the solved PDF for {a.title} to the LMS now?", *typed,
                            *([f"The LMS shows {a.submitted_file}; it is replaced if the service uploaded it."] * bool(a.submitted_file))],
                "reject": [f"Reject the solution for {a.title}? Nothing will be uploaded."],
                "redo": [f"Redo {a.title} with this note?", f'"{screen.get("note", "")}"',
                         *(["Opus solves it again from the start, which uses more of your plan than a normal redo."] * bool(screen.get("strong")))],
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
        summaries = s.scalars(select(Summary.state).where(Summary.state.in_([SummaryState.QUEUED, SummaryState.RUNNING]))).all()
        if summaries:
            lines.append(f"Lecture summaries: {len(summaries)} being written or waiting")
        lines.append("Model: " + (f"fixed to {self.kv.get(actions.MODEL_KEY)}" if self.kv.get(actions.MODEL_KEY)
                                  else "picked per job by Haiku (Sonnet, or Opus for hard work)"))
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
    if category == "action":
        return any(_in(a, c, now) for c in ACTION)
    if category == "approval":  # a redo of an uploaded report waits here too: approving replaces the upload
        return state == JobState.AWAITING_APPROVAL and is_open
    if category == "go":
        return state == JobState.WAITING_GO and is_open and not a.submitted_file
    if category == "input":
        return state == JobState.NEEDS_INPUT and is_open and not a.submitted_file
    if category == "pipeline":
        return state in PIPELINE and is_open and not a.submitted_file
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
    if category in ("remaining", "due_soon", "approval", "go", "input", "pipeline", "needs_you", "upcoming", "open_now"):
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


def _settings_words(c: Course) -> str:
    return f"solving {c.solve}, uploading {c.mode}, safe mode {'on' if c.safe else 'off'}"


def _theme_words(c: Course, data_dir: Path | None) -> str:
    sent = len(files.listed(files.theme_dir(data_dir, c))) if data_dir else 0
    return ("its own" if c.theme else "default") + (f", {sent} reference file{'s' * (sent != 1)}" if sent else "")


def _triage_lines(job: Job) -> list[str]:
    """What Haiku decided for this job, for the item's screen."""
    try:
        triage = json.loads(job.triage or "{}")
    except ValueError:
        return []
    lines = []
    if triage.get("model"):
        lines.append(f"Sorted by Haiku: {triage['model']}, {triage.get('effort')} effort ({triage.get('reason')})")
    redo = triage.get("redo") or {}
    if redo.get("model") and redo.get("note") == job.note:
        lines.append(f"The redo: {redo['model']}, {redo.get('effort')} effort ({redo.get('reason')})")
    if triage.get("lectures"):
        lines.append("Lecture notes read: " + ", ".join(_short(n) for n in triage["lectures"]))
    return lines


def _pick_weeks(text: str, items: list[list[str]]) -> tuple[list[list[str]], str | None]:
    """'1,3,5', '2-4' or 'all' -> the lecture files of those weeks, or what is wrong with the reply."""
    available = list(dict.fromkeys(week for week, _, _ in items))
    wanted: list[str] = []
    for token in re.split(r"[,\s]+", text.strip().lower()):
        if not token:
            continue
        if token == "all":
            wanted = list(available)
            break
        span = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if span:
            low, high = sorted((int(span[1]), int(span[2])))
            wanted += [w for w in available if w.isdigit() and low <= int(w) <= high]
        elif token in available:
            wanted.append(token)
        else:
            return [], f"There is no week {token} with lecture files."
    wanted = list(dict.fromkeys(wanted))
    picked = [item for item in items if item[0] in wanted]
    if not picked:
        return [], "No lecture was picked."
    if len(picked) > MAX_SUMMARY_FILES:
        return [], f"That is {len(picked)} files; one summary takes {MAX_SUMMARY_FILES} at most."
    return picked, None


def _questions(job: Job) -> list[str]:
    try:
        return [str(q) for q in json.loads(job.summary or "{}").get("questions") or []][:8]
    except ValueError:
        return []


def _short(name: str, limit: int = 38) -> str:
    """'Week-4-NFA-15092026-012828pm.pptx' -> 'Week-4-NFA.pptx': the LMS adds upload stamps to names."""
    stem, dot, ext = name.rpartition(".")
    stem = re.sub(r"(-+\d+-+)?(-\d{8}-\d{6}[ap]m)+$", "", stem if dot else name)
    stem = stem if len(stem) <= limit else stem[:limit - 1] + "…"
    return f"{stem}.{ext}" if dot else stem


def _due_words(a: Assignment, now: datetime) -> str:
    """Past the posted time the LMS still takes an upload until the end of that day."""
    if a.deadline > now:
        return _relative(a.deadline, now)
    return f"the posted time has passed; the LMS still takes it until {_when(closes_at(a.deadline))}"


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
    if job and not a.deadline_passed:
        tag = {JobState.AWAITING_APPROVAL: "new version waiting for you" if a.submitted_file else "waiting for you",
               JobState.WAITING_GO: "waiting for your go-ahead", JobState.NEEDS_INPUT: "Claude asked you"}.get(job.state)
        if tag:
            bits.append(tag)
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
