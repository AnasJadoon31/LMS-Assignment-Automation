"""Parsers for LMS pages. Pure functions over HTML, so they test against saved pages.

Assignments.php?s=<semester>&oc=<course> is the one page that lists courses
and their assignments:

- `#semesterId` and `#courseId` selects; values are base64 ids, the selected
  option is the one in the URL (or the current semester by default)
- a `table.table-hover` with one row per assignment: number, title, question
  file link, your submission link, marks, returned submission, action, deadline

The action cell is the assignment's state: a Submit button (open, nothing
submitted), a Delete button (open, submitted; a correction is delete then
submit) or "Deadline Exceeded". The real assignment id shows only on open rows:
the Submit modal's hidden `assignmentID`, or the `a` parameter of the Delete
link. Closed rows carry no id, so rows are identified by course and number.

The Submit modal posts multipart to includes/studentprocess.php?s=assignmentSubmissionAdd
with assignmentID, offeredCourseID, ocID, studentID and assignmentSubmissionFile
(.pdf .doc .docx .xls .xlsx .zip, no special symbols in the name). Delete is a
plain GET link. Nothing in the poller follows either.

Download links are Download.php?k=<base64 of
"<student id>,<today's date>,<kind>,0,<filename>,<return url>">. The key
carries today's date, so it is never part of a fingerprint.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .errors import ContractBroken

LMS_TZ = ZoneInfo("Asia/Karachi")
NO_ROWS = ("please select a course", "no assignment uploaded")


@dataclass(frozen=True)
class Option:
    value: str  # base64, URL-decoded
    label: str
    selected: bool


@dataclass(frozen=True)
class DownloadKey:
    student_id: str
    kind: str  # "Assignment" or "AssignmentSubmissions"
    filename: str


@dataclass(frozen=True)
class AssignmentRow:
    number: str
    title: str
    question_file: str | None
    question_href: str | None
    remarks: str | None
    submission_file: str | None
    marks: str | None
    action: str  # "submit", "delete", "closed", or the cell text if unrecognised
    deadline: datetime | None
    assignment_id: str | None = None  # only on open rows

    @property
    def deadline_passed(self) -> bool:
        if self.action in ("submit", "delete"):
            return False
        if self.action == "closed":
            return True
        return self.deadline is not None and self.deadline < datetime.now(LMS_TZ)


@dataclass(frozen=True)
class AssignmentsPage:
    semesters: list[Option]
    courses: list[Option]
    rows: list[AssignmentRow]

    @property
    def current_semester(self) -> Option:
        return next((o for o in self.semesters if o.selected), self.semesters[0])


def b64_id(value: str) -> str:
    """'MTUxNjk0' -> '151694'."""
    try:
        return base64.b64decode(unquote(value)).decode()
    except (binascii.Error, UnicodeDecodeError):
        return value


def decode_download_key(href: str) -> DownloadKey | None:
    key = parse_qs(urlsplit(href).query).get("k", [None])[0]
    if not key:
        return None
    try:
        parts = base64.b64decode(key).decode().split(",")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if len(parts) < 5:
        return None
    return DownloadKey(student_id=parts[0], kind=parts[2], filename=parts[4])


def parse_deadline(text: str) -> datetime | None:
    """'1 October 2026-11:55 pm' -> 2026-10-01 23:55 PKT."""
    text = " ".join(text.split())
    for fmt in ("%d %B %Y-%I:%M %p", "%d %B %Y - %I:%M %p", "%d %b %Y-%I:%M %p"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LMS_TZ)
        except ValueError:
            continue
    return None


def _options(soup: BeautifulSoup, select_id: str) -> list[Option]:
    select = soup.find("select", id=select_id)
    if select is None:
        raise ContractBroken(f"Assignments page has no #{select_id} select")
    return [
        Option(value=unquote(o.get("value", "")), label=" ".join(o.get_text().split()), selected=o.has_attr("selected"))
        for o in select.find_all("option")
        if o.get("value")
    ]


def _text(cell: Tag) -> str:
    return " ".join(cell.get_text(" ").split())


def _action(cell: Tag) -> str:
    text = _text(cell).lower()
    if "deadline exceeded" in text:
        return "closed"
    if text in ("submit", "delete"):
        return text
    return text


def _assignment_id(soup: BeautifulSoup, action_cell: Tag) -> str | None:
    """From the Submit modal's hidden input, or the Delete link's `a` parameter."""
    trigger = action_cell.find("a", attrs={"data-target": True})
    modal = soup.find(id=trigger["data-target"].lstrip("#")) if trigger else None
    if modal is None:
        return None
    hidden = modal.find("input", attrs={"name": "assignmentID"})
    if hidden and hidden.get("value"):
        return b64_id(hidden["value"])
    delete = modal.find("a", href=re.compile(r"assignmentSubmissionDelete"))
    if delete:
        a = parse_qs(urlsplit(delete["href"]).query).get("a", [None])[0]
        return b64_id(a) if a else None
    return None


def _parse_row(soup: BeautifulSoup, cells: list[Tag]) -> AssignmentRow:
    number, title, question, submission, marks, _returned, status, deadline = cells[:8]

    q_link = question.find("a", href=re.compile(r"Download\.php"))
    q_key = decode_download_key(q_link["href"]) if q_link else None
    remarks_span = question.find("span", title=True)
    remarks = remarks_span["title"] if remarks_span else None

    s_link = submission.find("a", href=re.compile(r"Download\.php"))
    s_key = decode_download_key(s_link["href"]) if s_link else None

    labels = deadline.find_all(["small", "span"]) or [deadline]
    marks_text = _text(marks)

    return AssignmentRow(
        number=_text(number),
        title=_text(title),
        question_file=q_key.filename if q_key else None,
        question_href=q_link["href"] if q_link else None,
        remarks=None if remarks in (None, "No Remarks") else remarks,
        submission_file=s_key.filename if s_key else None,
        marks=None if marks_text.lower() in ("", "not marked yet", "---") else marks_text,
        action=_action(status),
        deadline=parse_deadline(_text(labels[-1])),
        assignment_id=_assignment_id(soup, status),
    )


def parse_assignments_page(html: str) -> AssignmentsPage:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-hover")
    if table is None:
        raise ContractBroken("Assignments page has no assignments table")
    headers = [_text(th).lower() for th in table.find_all("th")]
    if len(headers) < 8 or not headers[0].startswith("assign") or "deadline" not in headers[7]:
        raise ContractBroken(f"Assignments table columns changed: {headers}")

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) == 1 and any(m in _text(cells[0]).lower() for m in NO_ROWS):
            continue
        if len(cells) >= 8:
            rows.append(_parse_row(soup, cells))
        elif cells:
            raise ContractBroken(f"Assignments row has {len(cells)} cells, expected 8")

    return AssignmentsPage(
        semesters=_options(soup, "semesterId"),
        courses=_options(soup, "courseId"),
        rows=rows,
    )
