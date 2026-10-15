"""LMSAdapter for the Bahria University LMS, reached through CMS login."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from ..config import Settings
from ..kv import FileKV
from .bahria_pages import AssignmentRow, b64_id, parse_assignments_page, parse_papers_page
from .materials import Lecture, LectureFile, PlanWeek, parse_lectures, parse_outlines, parse_plan, plan_html
from .cms_login import login
from .errors import (
    AlreadySubmitted,
    BadCredentials,
    CaptchaRequired,
    ContractBroken,
    LMSError,
    LoginError,
    NothingSubmitted,
    ReloginPaused,
    SessionExpired,
)
from .login_guard import LoginGuard
from .models import ASSIGNMENT, PAPER, AssignmentMeta, Course, Receipt, SubmissionStatus
from .session import USER_AGENT, SessionStore, Throttle, lms_cookies, session_is_valid

ASSIGNMENTS = "Assignments.php"
PAPERS = "Papers.php"
PAGES = {ASSIGNMENT: (ASSIGNMENTS, parse_assignments_page), PAPER: (PAPERS, parse_papers_page)}
SUBMIT_ACTION = re.compile(r"includes/studentprocess\.php\?s=assignmentSubmissionAdd")
DELETE_ACTION = re.compile(r"includes/studentprocess\.php\?s=assignmentSubmissionDelete&[\w%=&+/.-]+")
UPLOAD_TYPES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"}


class BahriaAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        store: SessionStore | None = None,
        guard: LoginGuard | None = None,
        throttle: Throttle | None = None,
        headless: bool = True,
    ) -> None:
        self.settings = settings
        self.store = store or SessionStore(FileKV(settings.state_dir))
        self.guard = guard or LoginGuard(self.store.kv)
        self.throttle = throttle or Throttle()
        self.headless = headless
        self._http: httpx.Client | None = None

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> BahriaAdapter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def ensure_session(self) -> None:
        if session_is_valid(self.store.load(), self.settings.urls, throttle=self.throttle):
            return
        self.relogin()

    def relogin(self) -> None:
        """Log in through CMS and save the new LMS session.

        Only a rejected login counts toward the failure limit. A CAPTCHA or a
        changed login page pauses re-login without submitting anything.
        """
        creds = self.settings.credentials
        if creds is None:
            raise SessionExpired("LMS session expired and no CMS credentials are set; paste a session")
        self.guard.check()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                context = browser.new_context()
                page = context.new_page()
                try:
                    login(page, creds, self.settings.urls)
                except BadCredentials as e:
                    self.guard.record_failure(str(e))
                    raise
                except CaptchaRequired as e:
                    self.guard.pause(f"CAPTCHA on CMS login ({e}). Paste a session or press Retry.")
                    raise ReloginPaused(str(e)) from e
                except ContractBroken as e:
                    self.guard.pause(f"CMS login page changed ({e}). Check it, then press Retry.")
                    raise
                except PlaywrightError as e:
                    raise LoginError(f"Browser error during CMS login: {e}") from e
                state = context.storage_state()
            finally:
                browser.close()

        self.store.save(state)
        self.guard.record_success()
        self.close()  # the next request picks up the new cookie

    def list_courses(self) -> list[Course]:
        """Courses of the current semester, as the LMS lists them."""
        page = parse_assignments_page(self._get(ASSIGNMENTS).text)
        sem = page.current_semester
        return [
            Course(code=b64_id(o.value), name=o.label, lms_ref=o.value, semester=sem.value, semester_name=sem.label)
            for o in page.courses
        ]

    def list_assignments(self, course: Course) -> list[AssignmentMeta]:
        return [self._meta(course, row, ASSIGNMENT) for row in self._rows(course, ASSIGNMENT)]

    def list_papers(self, course: Course) -> list[AssignmentMeta]:
        """Online exam papers (mid and final terms), from Papers.php."""
        return [self._meta(course, row, PAPER) for row in self._rows(course, PAPER)]

    def download(self, a: AssignmentMeta, dest: Path) -> list[Path]:
        """Save the question file. Re-reads the list first: download keys expire daily."""
        row = self._row(a)
        if not row.question_href:
            return []
        r = self._get(row.question_href)
        name = _attachment_name(r) or row.question_file or f"{a.lms_ref}.bin"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / Path(name).name
        path.write_bytes(r.content)
        return [path]

    def submit(self, a: AssignmentMeta, file: Path) -> Receipt:
        """Upload file through the row's Submit form, then read the row back to confirm.

        Only an open row with nothing submitted: a row showing Delete raises AlreadySubmitted,
        since the service never deletes a submission to upload over it. Papers are never uploaded."""
        if a.kind != ASSIGNMENT:
            raise LMSError("Only assignments are uploaded; lab papers never are")
        if file.suffix.lower() not in UPLOAD_TYPES:
            raise LMSError(f"The LMS takes {', '.join(sorted(UPLOAD_TYPES))}, not {file.suffix}")
        row = self._row(a)
        if row.action == "delete":
            raise AlreadySubmitted(row.submission_file)
        if row.action != "submit":
            raise LMSError(f"{a.title} is not open for submission ({row.action})")
        form = row.submit_form
        if form is None or not SUBMIT_ACTION.fullmatch(form.action):
            raise ContractBroken(f"The Submit form for {a.title} changed: {form.action if form else 'missing'}")
        self.throttle.wait()
        r = self._client().post(form.action, data=dict(form.fields),
                                files={form.file_field: (upload_name(file), file.read_bytes(), _mime(file))})
        if r.is_redirect:
            target = urljoin(str(r.url), r.headers.get("location", ""))
            if urlsplit(target).hostname != self.settings.urls.lms_host:
                raise SessionExpired(f"The upload was redirected to {target}; the session ended")
        elif r.status_code >= 400:
            raise LMSError(f"The LMS answered the upload with HTTP {r.status_code}")
        after = self._row(a)
        if not after.submission_file:
            raise LMSError("The LMS does not show the file after the upload")
        return Receipt(filename=after.submission_file, submitted_at=datetime.now(timezone.utc))

    def delete_submission(self, a: AssignmentMeta) -> str:
        """Follow the row's Delete link, then read the row back to confirm; the LMS offers it
        while the assignment is open. Returns the name of the file that was deleted.

        Only on your request, and only the link the row itself carries, for this course."""
        if a.kind != ASSIGNMENT:
            raise LMSError("Only an assignment's submission can be deleted")
        row = self._row(a)
        if row.action == "submit":
            raise NothingSubmitted(f"The LMS shows no submission for {a.title}")
        if row.action != "delete":
            raise LMSError(f"{a.title} is not open, so its submission cannot be deleted ({row.action})")
        href = row.delete_href
        query = parse_qs(urlsplit(href).query) if href else {}
        if not href or not DELETE_ACTION.fullmatch(href) or not query.get("aSubId") or query.get("oc") != [a.course.lms_ref]:
            raise LMSError(f"The Delete link for {a.title} is not the one expected ({href or 'missing'}); delete it on the LMS")
        self.throttle.wait()
        r = self._client().get(href)
        if r.is_redirect:
            target = urljoin(str(r.url), r.headers.get("location", ""))
            if urlsplit(target).hostname != self.settings.urls.lms_host:
                raise SessionExpired(f"The delete was redirected to {target}; the session ended")
        elif r.status_code >= 400:
            raise LMSError(f"The LMS answered the delete with HTTP {r.status_code}")
        after = self._row(a)
        if after.action != "submit" or after.submission_file:
            raise LMSError("The LMS still shows the submission after the delete")
        return row.submission_file or "the submission"

    # --- course material, read on request; nothing here uses Claude ---

    def course_plan(self, course: Course) -> list[PlanWeek]:
        return parse_plan(self._get("CoursePlan.php", {"s": course.semester, "oc": course.lms_ref}).text)

    def lectures(self, course: Course) -> list[Lecture]:
        return parse_lectures(self._get("LectureNotes.php", {"s": course.semester, "oc": course.lms_ref}).text)

    def download_lecture(self, course: Course, week: str, filename: str, dest: Path) -> Path:
        """Reads the list again for today's link. A file already saved is not fetched twice."""
        path = dest / Path(filename).name
        if path.is_file() and path.stat().st_size:
            return path
        for lecture in self.lectures(course):
            for f in lecture.files:
                if f.filename == filename and f.week == week:
                    return self._save(f.href, path)
        raise LMSError(f"{filename} is no longer in {course.name}'s lecture notes")

    def download_outline(self, course: Course, dest: Path) -> Path | None:
        """The course outline file, or None when the teacher posted none."""
        page = self._get("CourseOutline.php", {"s": course.semester, "oc": course.lms_ref}).text
        found = parse_outlines(page).get(course.name.lower())
        if found is None:
            return None
        filename, href = found
        path = dest / Path(filename).name
        return path if path.is_file() and path.stat().st_size else self._save(href, path)

    def _save(self, href: str, path: Path) -> Path:
        r = self._get(href)
        if "text/html" in r.headers.get("content-type", "") and b"<html" in r.content[:2000].lower():
            raise LMSError("The LMS answered with a page instead of the file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        return path

    def submission_status(self, a: AssignmentMeta) -> SubmissionStatus:
        row = self._row(a)
        return SubmissionStatus(submitted_file=row.submission_file, deadline_passed=row.deadline_passed, marks=row.marks)

    # --- helpers ---

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=urljoin(self.settings.urls.lms_dashboard, "."),
                cookies=lms_cookies(self.store.load(), self.settings.urls),
                headers={"User-Agent": USER_AGENT},
                timeout=60,
                follow_redirects=False,
            )
        return self._http

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        self.throttle.wait()
        r = self._client().get(path, params=params)
        if r.is_redirect:
            target = urljoin(str(r.url), r.headers.get("location", ""))
            if urlsplit(target).hostname != self.settings.urls.lms_host:
                raise SessionExpired(f"LMS redirected {path} to {target}; the session ended")
            raise LMSError(f"Unexpected redirect from {path} to {target}")
        r.raise_for_status()
        return r

    def _rows(self, course: Course, kind: str = ASSIGNMENT) -> list[AssignmentRow]:
        path, parse = PAGES[kind]
        page = parse(self._get(path, {"s": course.semester, "oc": course.lms_ref}).text)
        selected = next((o for o in page.courses if o.selected), None)
        if selected is None or selected.value != course.lms_ref:
            raise ContractBroken(f"Asked for {course.name} but the LMS showed {selected.label if selected else 'no course'}")
        return page.rows

    def _row(self, a: AssignmentMeta) -> AssignmentRow:
        for row in self._rows(a.course, a.kind):
            if self._meta(a.course, row, a.kind).lms_ref == a.lms_ref:
                if row.title != a.title:
                    raise LMSError(f"Row {a.lms_ref} was {a.title!r}, now {row.title!r}; re-list before acting")
                return row
        raise LMSError(f"Assignment {a.title} ({a.lms_ref}) is no longer listed in {a.course.name}")

    @staticmethod
    def _meta(course: Course, row: AssignmentRow, kind: str = ASSIGNMENT) -> AssignmentMeta:
        return AssignmentMeta(
            course=course,
            lms_ref=f"{course.code}:{'P' if kind == PAPER else ''}{row.number}",
            kind=kind,
            term=row.term,
            starts_at=row.starts_at,
            marks=row.marks,
            submission_hidden=row.submission_hidden,
            paper_status=row.paper_status,
            number=row.number,
            title=row.title,
            deadline=row.deadline,
            attachment_name=row.question_file,
            remarks=row.remarks,
            assignment_id=row.assignment_id,
            submitted_file=row.submission_file,
            deadline_passed=row.deadline_passed,
        )


def _attachment_name(r: httpx.Response) -> str | None:
    m = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition", ""))
    return m.group(1) if m else None


def upload_name(file: Path) -> str:
    """The LMS refuses special symbols in file names: 'Assignment 01 - Sorting.pdf' -> 'Assignment_01_Sorting.pdf'."""
    stem = re.sub(r"[^A-Za-z0-9]+", "_", file.stem).strip("_")[:80] or "submission"
    return stem + file.suffix.lower()


def _mime(file: Path) -> str:
    return {".pdf": "application/pdf", ".zip": "application/zip", ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[file.suffix.lower()]
