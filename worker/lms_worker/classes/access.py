"""Who may use the CR pages: numbers proved with a code the class bot sends on WhatsApp, signed
cookies, one-time invites, and LMS sign-ins that run in the background."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from ..kv import KeyValue
from .lms import SignInError

log = logging.getLogger(__name__)

CODE_MINUTES = 10
CODE_TRIES = 5
CODES_PER_HOUR = 3
CR_COOKIE = "lms_cr"
JOIN_COOKIE = "lms_join"
CR_SECONDS = 30 * 24 * 3600
JOIN_SECONDS = 2 * 3600
INVITE_HOURS = 48


def phone(raw: str | None) -> str:
    """'0300-1234567' or '+92 300 1234567' -> '923001234567'. Raises ValueError."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = "92" + digits[1:]
    if not 10 <= len(digits) <= 15:
        raise ValueError("Give your WhatsApp number with its country code, e.g. 923001234567")
    return digits


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(24)


class Codes:
    """Six-digit codes sent on WhatsApp. Only their hash is kept, for ten minutes; five wrong
    tries end a code; three codes an hour per number at most."""

    def __init__(self, kv: KeyValue, send: Callable[[str, str], None], *, clock: Callable[[], float] = time.time) -> None:
        self.kv, self.send, self.clock = kv, send, clock

    def _key(self, purpose: str, number: str) -> str:
        return f"code:{purpose}:{number}"

    def issue(self, purpose: str, number: str, what: str) -> None:
        key = self._key(purpose, number)
        old = json.loads(self.kv.get(key) or "{}")
        sent = [t for t in old.get("sent", []) if t > self.clock() - 3600]
        if len(sent) >= CODES_PER_HOUR:
            raise ValueError("Too many codes for this number; try again in an hour")
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.send(number, f"Your code for {what}: *{code}*\n\nIt works for {CODE_MINUTES} minutes. "
                          "If you did not ask for it, ignore this message.")
        self.kv.set(key, json.dumps({"hash": token_hash(f"{number}:{code}"), "until": self.clock() + CODE_MINUTES * 60,
                                     "tries": 0, "sent": sent + [self.clock()]}))

    def check(self, purpose: str, number: str, code: str) -> bool:
        key = self._key(purpose, number)
        data = json.loads(self.kv.get(key) or "{}")
        if not data.get("hash") or data.get("until", 0) < self.clock() or data.get("tries", 0) >= CODE_TRIES:
            return False
        ok = hmac.compare_digest(data["hash"], token_hash(f"{number}:{''.join(ch for ch in code if ch.isdigit())}"))
        if ok:
            self.kv.set(key, json.dumps({"sent": data.get("sent", [])}))
        else:
            data["tries"] = data.get("tries", 0) + 1
            self.kv.set(key, json.dumps(data))
        return ok


class Signer:
    def __init__(self, secret: str, purpose: str) -> None:
        self.key, self.purpose = secret.encode(), purpose

    def _sig(self, value: str) -> str:
        return hmac.new(self.key, f"{self.purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    def make(self, value: str, seconds: int, now: float) -> str:
        body = f"{value}|{int(now) + seconds}"
        return f"{body}|{self._sig(body)}"

    def read(self, cookie: str | None, now: float) -> str | None:
        if not cookie or cookie.count("|") < 2:
            return None
        body, sig = cookie.rsplit("|", 1)
        value, _, expires = body.rpartition("|")
        if not expires.isdigit() or int(expires) < now or not hmac.compare_digest(sig, self._sig(body)):
            return None
        return value


class SignInJobs:
    """LMS sign-ins run in a thread (a browser takes ten seconds or more), one browser at a time."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.browser = threading.Semaphore(1)

    def start(self, key: str, fn: Callable[[], object]) -> None:
        with self.lock:
            if self.jobs.get(key, {}).get("state") == "running":
                raise ValueError("A sign-in is already running; wait for it")
            self.jobs[key] = {"state": "running", "at": time.time()}

        def run() -> None:
            with self.browser:
                try:
                    result = {"state": "done", "result": fn()}
                except SignInError as e:
                    result = {"state": "error", "error": str(e)}
                except Exception as e:
                    log.exception("Class sign-in failed")
                    result = {"state": "error", "error": f"Something went wrong: {type(e).__name__}"}
            with self.lock:
                self.jobs[key] = {**result, "at": time.time()}

        threading.Thread(target=run, name=f"signin-{key[:12]}", daemon=True).start()

    def get(self, key: str) -> dict:
        with self.lock:
            return dict(self.jobs.get(key) or {"state": "none"})

    def pop(self, key: str) -> None:
        with self.lock:
            self.jobs.pop(key, None)


def invite_expiry(now: datetime) -> datetime:
    return now + timedelta(hours=INVITE_HOURS)
