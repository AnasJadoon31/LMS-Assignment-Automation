"""Numbered menus in a private chat with the class bot, for CRs (crbot.py) and for you (adminbot.py).

Reply with a number; 0 goes back, "menu" starts over. Some steps wait for typed text (an
announcement, a number, a class name to confirm); 0 cancels those. The place in the menu is kept
per person for 30 minutes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable

from ..bot import Reply
from ..db.models import ChatSession, utcnow
from ..lms.bahria_pages import LMS_TZ
from . import policy
from .store import Classes

START_WORDS = ("menu", "hi", "hello", "salam", "aoa", "start", "help", "")
IDLE_SECONDS = 1800


def when(dt: datetime | None) -> str:
    return dt.astimezone(LMS_TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ") if dt else "never"


class Stop(Exception):
    """A refused step: shown to the person as a note on the screen they were on."""


class ChatMenu:
    PREFIX = "menu:"

    def __init__(self, classes: Classes, *, now: Callable[[], datetime] = utcnow) -> None:
        self.classes, self.now, self.sessions = classes, now, classes.sessions

    # --- state ---

    def _key(self, who: Any) -> str:
        return self.PREFIX + hashlib.sha1(str(self._id(who)).encode()).hexdigest()[:40]

    def _id(self, who: Any) -> str:
        return str(who)

    def _load(self, who: Any) -> dict:
        with self.sessions() as s:
            row = s.get(ChatSession, self._key(who))
            if row is None or (row.updated_at and (self.now() - row.updated_at).total_seconds() > IDLE_SECONDS):
                return {}
            return json.loads(row.state or "{}")

    def _save(self, who: Any, state: dict) -> None:
        with self.sessions.begin() as s:
            row = s.get(ChatSession, self._key(who))
            if row is None:
                row = ChatSession(chat_id=self._key(who), state="{}")
                s.add(row)
            row.state, row.updated_at = json.dumps(state), self.now()

    # --- the conversation ---

    def handle(self, who: Any, text: str) -> list[Reply]:
        text = " ".join((text or "").split())
        state = self._load(who)
        try:
            replies, state = self._step(who, state, text)
        except (Stop, policy.PolicyError, PermissionError, ValueError, LookupError) as e:
            replies, state = self.show(who, "main", note=f"⚠️ {e}")  # e.g. the class was removed meanwhile
        self._save(who, state)
        return replies

    def _step(self, who: Any, state: dict, text: str) -> tuple[list[Reply], dict]:
        if text.lower() in START_WORDS + self.extra_start_words():
            return self.show(who, "main")
        if state.get("await"):
            if text == "0":
                return self.show(who, *self._back(state), note="Cancelled.")
            try:
                return self.answer(who, state, text)
            except (Stop, policy.PolicyError, PermissionError, ValueError, LookupError) as e:
                return self.show(who, *self._back(state), note=f"⚠️ {e}")
        if text == "0":
            return self.show(who, *self._back(state))
        option = (state.get("options") or {}).get(text)
        screen, ctx = state.get("screen", "main"), state.get("ctx") or {}
        if option is None:
            return self.show(who, screen, ctx, back=state.get("back"), hint="Reply with one of the numbers below.")
        try:
            return self.do(who, state, option)
        except (Stop, policy.PolicyError, PermissionError, ValueError, LookupError) as e:
            return self.show(who, screen, ctx, back=state.get("back"), note=f"⚠️ {e}")

    def extra_start_words(self) -> tuple[str, ...]:
        return ()

    @staticmethod
    def _back(state: dict) -> tuple[str, dict]:
        back = state.get("back") or ["main", {}]
        return back[0], back[1] or {}

    def show(self, who: Any, screen: str, ctx: dict | None = None, *, back: list | None = None, note: str | None = None,
             hint: str | None = None) -> tuple[list[Reply], dict]:
        ctx = ctx or {}
        title, lines, options = getattr(self, f"screen_{screen}")(who, ctx)
        body = ([note, ""] if note else []) + [title] + ([""] + lines if lines else [])
        if options:
            body += [""] + [f"{n}. {label}" for n, (label, _) in enumerate(options, start=1)]
        foot = "Reply with a number." if screen == "main" else "Reply with a number. 0 = back, menu = start."
        body += ["", hint or foot]
        state = {"screen": screen, "ctx": ctx, "back": back or self.default_back(screen, ctx),
                 "options": {str(n): opt for n, (_, opt) in enumerate(options, start=1)}}
        return [Reply("\n".join(body))], state

    def default_back(self, screen: str, ctx: dict) -> list:
        return ["main", {}]

    def ask(self, text: str, state: dict, waiting: str, **data) -> tuple[list[Reply], dict]:
        """Wait for typed text (or 1/2 for a yes/no); 0 cancels back to the current screen."""
        back = [state.get("screen", "main"), state.get("ctx") or {}]
        return [Reply(text)], {"await": waiting, "data": data, "back": back}

    def confirm(self, text: str, state: dict, action: dict) -> tuple[list[Reply], dict]:
        return self.ask(f"{text}\n\n1. Yes\n2. No", state, "confirm", action=action)

    # --- to fill in ---

    def do(self, who: Any, state: dict, option: dict) -> tuple[list[Reply], dict]:
        raise NotImplementedError

    def answer(self, who: Any, state: dict, text: str) -> tuple[list[Reply], dict]:
        raise NotImplementedError

    # --- shared: the rules screens, for a CR's settings and for your rules ---

    @staticmethod
    def describe(spec: policy.Spec, value: Any) -> str:
        if spec.kind == "switch":
            return "on" if value else "off"
        if spec.kind == "mode":
            return {"off": "Off", "notify": "Notices only", "menu": "Notices and menu"}[value]
        return str(value)
