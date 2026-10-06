"""Reading Claude Code's stream-json output: the final result and the usage-limit events.

With `--output-format stream-json --verbose` the CLI prints one JSON object per line. Two kinds
matter here:

- `rate_limit_event`: after each model request, the plan's usage. `status` is allowed,
  allowed_warning or rejected; `resetsAt` is a Unix time; `rateLimitType` names the window
  (five_hour, seven_day, ...); `unifiedWindows` gives each window's utilization from 0 to 1;
  `isUsingOverage` is true when paid extra usage is being drawn.
- `result`: the end of the run, with `is_error`, `num_turns`, `session_id`, `total_cost_usd`,
  `usage` and, with --json-schema, `structured_output`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

LIMIT_TEXT = re.compile(r"hit your .*limit|usage limit|limit reached|rate limit", re.IGNORECASE)


@dataclass(frozen=True)
class RateLimit:
    status: str  # allowed, allowed_warning, rejected
    kind: str | None  # five_hour, seven_day, ...
    resets_at: datetime | None
    using_overage: bool = False
    windows: dict[str, tuple[float, datetime | None]] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return self.status == "rejected"

    def to_json(self) -> dict:
        return {
            "status": self.status, "kind": self.kind, "resets_at": _iso(self.resets_at), "using_overage": self.using_overage,
            "windows": {k: {"utilization": u, "resets_at": _iso(r)} for k, (u, r) in self.windows.items()},
        }

    @classmethod
    def from_json(cls, d: dict) -> RateLimit:
        return cls(status=d["status"], kind=d.get("kind"), resets_at=_parse(d.get("resets_at")),
                   using_overage=bool(d.get("using_overage")),
                   windows={k: (float(v["utilization"]), _parse(v.get("resets_at"))) for k, v in (d.get("windows") or {}).items()})


@dataclass
class Outcome:
    finished: bool = False  # a result line arrived
    is_error: bool = False
    text: str = ""
    structured: dict | None = None
    session_id: str | None = None
    turns: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    stop: str | None = None  # the CLI's terminal_reason or subtype
    rate_limit: RateLimit | None = None

    @property
    def usage_limited(self) -> bool:
        """The plan's limit stopped the run."""
        if self.rate_limit and self.rate_limit.rejected:
            return True
        return self.is_error and bool(LIMIT_TEXT.search(self.text or ""))

    @property
    def turn_capped(self) -> bool:
        return (self.stop or "") in ("error_max_turns", "max_turns")


def _ts(value) -> datetime | None:
    return datetime.fromtimestamp(value, tz=timezone.utc) if isinstance(value, (int, float)) and value > 0 else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def rate_limit(info: dict) -> RateLimit:
    windows = {
        name: (float(w.get("utilization") or 0), _ts(w.get("resetsAt")))
        for name, w in (info.get("unifiedWindows") or {}).items() if isinstance(w, dict)
    }
    return RateLimit(status=str(info.get("status") or "allowed"), kind=info.get("rateLimitType"),
                     resets_at=_ts(info.get("resetsAt")), using_overage=bool(info.get("isUsingOverage")), windows=windows)


class StreamReader:
    """Feed it the CLI's stdout one line at a time."""

    def __init__(self):
        self.outcome = Outcome()

    def feed(self, line: str) -> RateLimit | None:
        """Returns the usage info when the line carried some."""
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except ValueError:
            return None
        o = self.outcome
        kind = event.get("type")
        if event.get("session_id"):
            o.session_id = event["session_id"]
        if kind == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            o.rate_limit = rate_limit(event["rate_limit_info"])
            return o.rate_limit
        if kind == "assistant":
            o.turns += 1
        if kind == "result":
            o.finished = True
            o.is_error = bool(event.get("is_error")) or str(event.get("subtype", "")).startswith("error")
            o.text = str(event.get("result") or "")
            o.structured = event.get("structured_output") if isinstance(event.get("structured_output"), dict) else None
            o.turns = int(event.get("num_turns") or o.turns)
            o.cost_usd = float(event.get("total_cost_usd") or 0)
            usage = event.get("usage") or {}
            o.tokens = sum(int(v) for k, v in usage.items() if k.endswith("_tokens") and isinstance(v, (int, float)))
            o.stop = event.get("terminal_reason") or event.get("subtype")
        return None
