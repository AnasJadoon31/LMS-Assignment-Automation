"""When to poll: every 10 minutes from 08:00 to 23:00 PKT, every 30 overnight, a few
seconds of jitter, and 5, 15, 60 minutes of back-off while the LMS is down."""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime
from typing import Callable

from .lms.bahria_pages import LMS_TZ
from .watcher import PollResult, Watcher

log = logging.getLogger(__name__)

DAY_START, DAY_END = 8, 23
DAY_MINUTES, NIGHT_MINUTES = 10, 30
BACKOFF_MINUTES = (5, 15, 60)
JITTER_SECONDS = 20
WAKE_CHECK_SECONDS = 10


def next_delay(now: datetime, result: PollResult, down_streak: int, rng: random.Random) -> float:
    """Seconds until the next poll."""
    if result.status == "lms_down":
        return BACKOFF_MINUTES[min(down_streak, len(BACKOFF_MINUTES)) - 1] * 60
    hour = now.astimezone(LMS_TZ).hour
    minutes = DAY_MINUTES if DAY_START <= hour < DAY_END else NIGHT_MINUTES
    return minutes * 60 + rng.uniform(0, JITTER_SECONDS)


def run_forever(
    watcher: Watcher,
    stop: threading.Event,
    *,
    clock: Callable[[], datetime],
    rng: random.Random | None = None,
    on_result: Callable[[PollResult, float], None] | None = None,
    poll_requested: Callable[[], bool] | None = None,
) -> None:
    """Poll, wait, repeat. A requested check (from the WhatsApp menu) ends the wait early."""
    rng = rng or random.Random()
    down_streak = 0
    while not stop.is_set():
        try:
            result = watcher.poll_once()
        except Exception:  # a bug in one cycle must not stop the poller
            log.exception("Poll cycle crashed")
            result = PollResult(status="error")
        down_streak = down_streak + 1 if result.status == "lms_down" else 0
        delay = next_delay(clock(), result, down_streak, rng)
        if on_result:
            on_result(result, delay)
        waited = 0.0
        while waited < delay and not stop.is_set():
            step = min(WAKE_CHECK_SECONDS, delay - waited)
            if stop.wait(step):
                break
            waited += step
            if poll_requested and poll_requested():
                break
