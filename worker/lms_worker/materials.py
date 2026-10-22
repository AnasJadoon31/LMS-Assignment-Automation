"""Lecture notes, course plan and course outline, fetched from the LMS when the chat asks.

Runs in the api, with the LMS session the worker keeps: it only reads pages and files. When
the session has ended it cannot log in itself (the worker does that), so it asks the worker
for a poll and tells you to try again. Files are kept under the course's folder, so a lecture
asked for twice is downloaded once. The course plan has no file on the LMS, only a table: it is
drawn as a page and a picture of it is sent. Nothing here uses Claude.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from . import files
from .bot import MaterialsError
from .db.models import Course, utcnow
from .kv import KeyValue
from .lms import models as lms
from .lms.errors import ContractBroken, LMSError, SessionExpired
from .lms.materials import Lecture, plan_html

log = logging.getLogger(__name__)

POLL_NOW_KEY = "poll_requested"  # the watcher's


def screenshot(page_html: str, path: Path) -> None:
    """A picture of the whole page, with the browser the worker image already has."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 600}, device_scale_factor=2)
            page.set_content(page_html)
            page.screenshot(path=str(path), full_page=True)
        finally:
            browser.close()


class Materials:
    def __init__(self, adapter, data_dir: Path, kv: KeyValue, *, now: Callable[[], datetime] = utcnow,
                 shoot: Callable[[str, Path], None] = screenshot, poll_key: str = POLL_NOW_KEY) -> None:
        self.adapter, self.data_dir, self.kv, self.now, self.shoot = adapter, data_dir, kv, now, shoot
        self.poll_key = poll_key  # the poller to wake when the session has ended

    def lectures(self, course: Course) -> list[Lecture]:
        return self._ask(lambda: self.adapter.lectures(_course(course)))

    def lecture(self, course: Course, week: str, filename: str) -> Path:
        dest = files.course_dir(self.data_dir, course) / "lectures"
        return self._ask(lambda: self.adapter.download_lecture(_course(course), week, filename, dest))

    def outline(self, course: Course) -> Path | None:
        dest = files.course_dir(self.data_dir, course) / "outline"
        return self._ask(lambda: self.adapter.download_outline(_course(course), dest))

    def plan(self, course: Course) -> Path | None:
        """A picture of the course plan's table, or None when the LMS has no plan."""
        weeks = self._ask(lambda: self.adapter.course_plan(_course(course)))
        if not weeks:
            return None
        page = plan_html(course.name, course.semester_name or "", weeks)
        folder = files.course_dir(self.data_dir, course) / "plan"
        path = folder / f"Course plan - {files.clean_name(course.name)} - {hashlib.sha256(page.encode()).hexdigest()[:8]}.png"
        if not path.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            try:
                self.shoot(page, path)
            except Exception as e:  # a browser that will not start must not end the chat
                log.exception("Could not draw the course plan")
                raise MaterialsError(f"The course plan is on the LMS ({len(weeks)} weeks) but its picture could not be made: {e}") from e
        return path

    def _ask(self, fetch):
        self.adapter.close()  # the worker may have logged in again since the last request
        try:
            return fetch()
        except SessionExpired as e:
            self.kv.set(self.poll_key, self.now().isoformat())
            raise MaterialsError("The LMS session has ended. A new login was asked for; try again in a minute.") from e
        except ContractBroken as e:
            raise MaterialsError(f"The LMS page is not as expected: {e}") from e
        except httpx.HTTPError as e:
            raise MaterialsError(f"The LMS did not answer ({type(e).__name__}); try again in a moment.") from e
        except LMSError as e:
            raise MaterialsError(str(e)) from e


def _course(c: Course) -> lms.Course:
    return lms.Course(code=c.code, name=c.name, lms_ref=c.lms_ref, semester=c.semester, semester_name=c.semester_name)
