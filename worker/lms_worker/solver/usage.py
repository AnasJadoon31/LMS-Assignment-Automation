"""Keeping the solver inside the Pro plan's limits.

Every run reports the plan's usage (see claude.py). Solving pauses until the window resets when:

- the plan rejects a request (the 5-hour or weekly limit is reached);
- a request is billed as paid extra usage: automation never spends money;
- a window passes PAUSE_AT (80% by default), so your own chats keep some room.

A cap on runs per day (Pakistan time) applies on top. Polling, downloads and messages never
pause; only solving does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..kv import KeyValue
from ..lms.bahria_pages import LMS_TZ
from .claude import RateLimit

USAGE_KEY = "claude_usage"
PAUSE_KEY = "claude_paused"
RUNS_KEY = "solver_runs"
MARGIN = timedelta(minutes=3)
WINDOW_NAMES = {"five_hour": "5-hour", "seven_day": "weekly", "seven_day_opus": "weekly Opus", "seven_day_sonnet": "weekly Sonnet"}


@dataclass(frozen=True)
class Pause:
    until: datetime
    reason: str  # limit, extra usage, headroom, daily cap

    def describe(self) -> str:
        when = self.until.astimezone(LMS_TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")
        why = {
            "limit": "the Claude Pro usage limit is reached",
            "extra usage": "Claude would be billed as paid extra usage",
            "headroom": "Claude usage is high; keeping the rest for your own chats",
            "daily cap": "today's solver runs are used up",
        }.get(self.reason, self.reason)
        return f"Solving paused: {why}. It resumes {when}."


class UsageGuard:
    def __init__(self, kv: KeyValue, *, pause_at: float = 0.8, daily_cap: int = 6):
        self.kv = kv
        self.pause_at = pause_at
        self.daily_cap = daily_cap

    # --- what the runs report ---

    def record(self, rl: RateLimit, now: datetime) -> Pause | None:
        """Store the latest usage; returns a new pause when this report calls for one."""
        self.kv.set(USAGE_KEY, json.dumps({**rl.to_json(), "seen_at": now.isoformat()}))
        pause = self._pause_for(rl, now)
        if pause:
            current = self.paused(now)
            if current is None or pause.until > current.until:
                self.kv.set(PAUSE_KEY, json.dumps({"until": pause.until.isoformat(), "reason": pause.reason}))
                return pause
        return None

    def _pause_for(self, rl: RateLimit, now: datetime) -> Pause | None:
        fallback = now + timedelta(hours=1)
        if rl.rejected:
            full = [r for _, (u, r) in rl.windows.items() if u >= 1 and r]
            until = max(full) if full else (rl.resets_at or fallback)
            return Pause(until + MARGIN, "limit")
        if rl.using_overage:
            return Pause((rl.resets_at or fallback) + MARGIN, "extra usage")
        high = [r for _, (u, r) in rl.windows.items() if u >= self.pause_at and r and r > now]
        if high:
            return Pause(max(high) + MARGIN, "headroom")
        return None

    def latest(self) -> RateLimit | None:
        raw = self.kv.get(USAGE_KEY)
        return RateLimit.from_json(json.loads(raw)) if raw else None

    # --- may a run start? ---

    def paused(self, now: datetime) -> Pause | None:
        raw = self.kv.get(PAUSE_KEY)
        if raw:
            d = json.loads(raw)
            until = datetime.fromisoformat(d["until"])
            if until > now:
                return Pause(until, d["reason"])
            self.kv.delete(PAUSE_KEY)
        if self.runs_today(now) >= self.daily_cap:
            midnight = (now.astimezone(LMS_TZ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return Pause(midnight.astimezone(timezone.utc), "daily cap")
        return None

    def runs_today(self, now: datetime) -> int:
        raw = self.kv.get(RUNS_KEY)
        if not raw:
            return 0
        d = json.loads(raw)
        return int(d["count"]) if d.get("day") == _day(now) else 0

    def count_run(self, now: datetime) -> None:
        self.kv.set(RUNS_KEY, json.dumps({"day": _day(now), "count": self.runs_today(now) + 1}))

    # --- for System status ---

    def status_line(self, now: datetime) -> str:
        rl = self.latest()
        parts = []
        if rl:
            for name, (util, resets) in rl.windows.items():
                label = WINDOW_NAMES.get(name, name)
                when = f", resets {resets.astimezone(LMS_TZ).strftime('%a %I:%M %p').replace(' 0', ' ')}" if resets and resets > now else ""
                parts.append(f"{label} {round(util * 100)}%{when}")
        line = "Claude usage: " + ("; ".join(parts) if parts else "not reported yet")
        line += f". Solver runs today: {self.runs_today(now)} of {self.daily_cap}."
        pause = self.paused(now)
        return line + (f"\n{pause.describe()}" if pause else "")


def _day(now: datetime) -> str:
    return now.astimezone(LMS_TZ).date().isoformat()
