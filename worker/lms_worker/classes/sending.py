"""The class bot's WhatsApp: a second number, its own Evolution instance, messages spaced out.

Spacing every message a few seconds apart, in both the worker (notices) and the api (menu
replies), keeps the number's sending pattern calm, which is what keeps WhatsApp from banning it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from ..config import EvolutionConfig
from ..notify import EvolutionNotifier, NotifyError

GAP_SECONDS = 3.0


class Pacer:
    """At most one message per `gap` seconds from this process."""

    def __init__(self, gap: float = GAP_SECONDS, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.gap, self.clock, self.sleep = gap, clock, sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if self._last is not None and now - self._last < self.gap:
                self.sleep(self.gap - (now - self._last))
                now = self.clock()
            self._last = now


PACER = Pacer()


class ClassSender:
    """Sends to a group JID or a phone number through the class bot's instance."""

    def __init__(self, cfg: EvolutionConfig, *, pacer: Pacer = PACER, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg, self.pacer = cfg, pacer
        self._to: str | None = None
        self._notifier = EvolutionNotifier(cfg, number=lambda: self._to, transport=transport)

    def text(self, to: str, message: str) -> None:
        self.pacer.wait()
        self._to = to
        self._notifier.text(message)

    def document(self, to: str, path: Path, caption: str) -> None:
        self.pacer.wait()
        self._to = to
        self._notifier.document(path, caption)


__all__ = ["ClassSender", "NotifyError", "PACER", "Pacer"]
