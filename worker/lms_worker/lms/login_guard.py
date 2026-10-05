"""Failed-login counter for automatic CMS re-login.

CMS shows a CAPTCHA after 5 failed logins, so automatic re-login stops after 2
and waits for a pasted session or a manual Retry. The count survives restarts
and resets only on a successful login or a Retry. A CAPTCHA on the form
pauses re-login at once.

Kept in a JSON file for the spike; moves to the settings table with the database.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import ReloginPaused

MAX_FAILURES = 2


@dataclass
class GuardState:
    failures: int = 0
    paused_reason: str | None = None
    last_error: str | None = None
    updated_at: str | None = None


class LoginGuard:
    def __init__(self, path: Path, max_failures: int = MAX_FAILURES) -> None:
        self.path = path
        self.max_failures = max_failures

    @property
    def state(self) -> GuardState:
        if not self.path.exists():
            return GuardState()
        return GuardState(**json.loads(self.path.read_text()))

    def check(self) -> None:
        """Raise ReloginPaused when another automatic attempt is not allowed."""
        s = self.state
        if s.paused_reason:
            raise ReloginPaused(s.paused_reason)

    def record_failure(self, error: str) -> None:
        s = self.state
        s.failures += 1
        s.last_error = error
        if s.failures >= self.max_failures:
            s.paused_reason = (
                f"Automatic re-login stopped after {s.failures} failed attempts "
                f"(last: {error}). Paste a session or press Retry."
            )
        self._write(s)

    def pause(self, reason: str) -> None:
        s = self.state
        s.paused_reason = reason
        s.last_error = reason
        self._write(s)

    def record_success(self) -> None:
        self._write(GuardState())

    def reset(self) -> None:
        """The Retry button."""
        self._write(GuardState())

    def _write(self, s: GuardState) -> None:
        s.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), indent=2))
        os.replace(tmp, self.path)
