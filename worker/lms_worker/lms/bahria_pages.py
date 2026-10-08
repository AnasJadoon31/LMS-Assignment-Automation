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
plain GET link to includes/studentprocess.php?s=assignmentSubmissionDelete with aSubId, a and
oc. The poller follows neither; the upload step posts the Submit form after your approval,
and the Delete link is followed only when you ask for a submission to be deleted.

Papers.php lists lab exam papers (only lab courses get them), with ten columns: number,
term (with its duration, e.g. "Mid Term (60 Minutes)"), paper file, remarks, your
submission, marks, returned comments, returned submission, action, and the exam window:
"Date: 17 March 2026", "Start Time: 12:55 pm", "End Time: 01:55 pm". The submission link
reads Download.php?k=NA, so the file is never shown; the row colour says what happened:
green (#90EE90) submitted, white not submitted, red the exam ended with no submission.
No open paper has been seen yet; its Submit button is assumed to match the assignments'.

Download links are Download.php?k=<base64 of
"<student id>,<today's date>,<kind>,0,<filename>,<return url>">. The key
carries today's date, so it is never part of a fingerprint.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .errors import ContractBroken
from .models import PAPER_MISSED, PAPER_NOT_SUBMITTED, PAPER_SUBMITTED

LMS_TZ = ZoneInfo("Asia/Karachi")
NO_ROWS = ("please select a course", "no assignment uploaded", "no paper uploaded", "no paper")


@dataclass(frozen=True)
class Layout:
    """Where each field sits in a row of a listing table."""

    name: str
    first_header: str
    columns: int
    number: int
    title: int | None
    term: int | None
    question: int
    remarks: int
    submission: int
    marks: int
    action: int
    deadline: int


ASSIGNMENTS = Layout("assignments", "assign", 8, number=0, title=1, term=None, question=2, remarks=2,
                     submission=3, marks=4, action=6, deadline=7)
PAPERS = Layout("papers", "paper", 10, number=0, title=None, term=1, question=2, remarks=3,
                submission=4, marks=5, action=8, deadline=9)


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
class SubmitForm:
    """The Submit modal's form on an open row."""

    action: str  # as written in the page, e.g. includes/studentprocess.php?s=assignmentSubmissionAdd
    fields: tuple[tuple[str, str], ...]  # the hidden inputs
    file_field: str


@dataclass(frozen=True)
class AssignmentRow:
    """One row of the assignments or papers table."""

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
    term: str | None = None  # papers: "Mid Term", "Final Term"
    starts_at: datetime | None = None  # papers: when the exam window opens (deadline is when it closes)
    submission_hidden: bool = False  # papers: a submission link with no file (k=NA)
    paper_status: str | None = None  # papers: from the row colour, None if it is not one of the three
    submit_form: SubmitForm | None = None  # open rows with nothing submitted
    delete_href: str | None = None  # open rows with a submission: the Delete link

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


def closes_at(deadline: datetime) -> datetime:
    """When the LMS really stops taking uploads: the end of the deadline's day, whatever time
    the teacher set. The posted time is still the deadline; this is only how long the Submit
    button stays."""
    return deadline.astimezone(LMS_TZ).replace(hour=23, minute=59, second=59, microsecond=0)


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
        raise ContractBroken(f"Listing page has no #{select_id} select")
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
    """From the Submit modal's hidden id input, or the Delete link's id parameter."""
    trigger = action_cell.find("a", attrs={"data-target": True})
    modal = soup.find(id=trigger["data-target"].lstrip("#")) if trigger else None
    if modal is None:
        return None
    hidden = modal.find("input", attrs={"name": re.compile(r"^(assignment|paper)ID$", re.I)})
    if hidden and hidden.get("value"):
        return b64_id(hidden["value"])
    delete = modal.find("a", href=re.compile(r"(assignment|paper)SubmissionDelete", re.I))
    if delete:
        query = parse_qs(urlsplit(delete["href"]).query)
        value = (query.get("a") or query.get("p") or [None])[0]
        return b64_id(value) if value else None
    return None


def _delete_href(soup: BeautifulSoup, action_cell: Tag) -> str | None:
    """The link in the row's Delete modal."""
    trigger = action_cell.find("a", attrs={"data-target": True})
    modal = soup.find(id=trigger["data-target"].lstrip("#")) if trigger else None
    link = modal.find("a", href=re.compile(r"assignmentSubmissionDelete", re.I)) if modal else None
    return link["href"] if link else None


def _submit_form(soup: BeautifulSoup, action_cell: Tag) -> SubmitForm | None:
    trigger = action_cell.find("a", attrs={"data-target": True})
    modal = soup.find(id=trigger["data-target"].lstrip("#")) if trigger else None
    form = modal.find("form", action=re.compile(r"SubmissionAdd", re.I)) if modal else None
    file_input = form.find("input", attrs={"type": "file"}) if form else None
    if form is None or file_input is None or not file_input.get("name"):
        return None
    fields = tuple((i["name"], i.get("value", "")) for i in form.find_all("input", attrs={"type": "hidden"})
                   if i.get("name") and not i.has_attr("disabled"))
    return SubmitForm(action=form["action"], fields=fields, file_field=file_input["name"])


TERM = re.compile(r"^(?P<main>[^({]+?)\s*(?P<extra>\([^)]*\))?\s*(?P<note>\{[^}]*\})?\s*$")


def _term(cell: Tag) -> tuple[str | None, str | None, str | None]:
    """'Mid Term (60 Minutes)' -> ('Mid Term', '(60 Minutes)', None);
    'Final Term (Objective-2hours) { For BUMDC Only }' -> (..., 'For BUMDC Only')."""
    text = _text(cell)
    m = TERM.match(text)
    if not m:
        return text or None, None, None
    note = m["note"].strip("{} ") if m["note"] else None
    return m["main"].strip(), m["extra"], note


def _window(cell: Tag) -> tuple[datetime | None, datetime | None]:
    """Date, Start Time and End Time labels -> (start, end) in PKT."""
    labels = {(small.get("title") or "").lower(): _text(small).split(":", 1)[-1].strip() for small in cell.find_all("small")}
    day, start, end = labels.get("date"), labels.get("start time"), labels.get("end time")
    if not (day and start and end):
        return None, None
    try:
        date = datetime.strptime(" ".join(day.split()), "%d %B %Y")
        t0 = datetime.strptime(start.lower(), "%I:%M %p").time()
        t1 = datetime.strptime(end.lower(), "%I:%M %p").time()
    except ValueError:
        return None, None
    begins = datetime.combine(date, t0, tzinfo=LMS_TZ)
    ends = datetime.combine(date, t1, tzinfo=LMS_TZ)
    if ends <= begins:  # an exam that runs past midnight
        ends += timedelta(days=1)
    return begins, ends


def _remarks(cell: Tag, layout: Layout) -> str | None:
    span = cell.find("span", title=True)
    text = span["title"] if span else (_text(cell) if layout.remarks != layout.question else None)
    return None if not text or text.strip().lower() in ("no remarks", "---", "-") else text.strip()


COLOURS = {
    "white": "ffffff", "green": "008000", "lightgreen": "90ee90", "palegreen": "98fb98", "lime": "00ff00",
    "red": "ff0000", "lightcoral": "f08080", "salmon": "fa8072", "tomato": "ff6347", "pink": "ffc0cb",
}
BACKGROUND = re.compile(r"background(?:-color)?\s*:\s*(#[0-9a-f]{3,6}\b|rgba?\([^)]*\)|[a-z]+)", re.I)


def _rgb(value: str) -> tuple[int, int, int] | None:
    value = value.strip().lower()
    value = "#" + COLOURS[value] if value in COLOURS else value
    if m := re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value):
        digits = m[1] if len(m[1]) == 6 else "".join(c * 2 for c in m[1])
        return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
    if m := re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+).*\)", value):
        return int(m[1]), int(m[2]), int(m[3])
    return None


def _paper_status(tr: Tag) -> str | None:
    """Green row: submitted. White: not submitted. Red: the exam ended with no submission."""
    m = BACKGROUND.search(tr.get("style") or "")
    rgb = _rgb(m[1] if m else tr.get("bgcolor") or "")
    if rgb is None:
        return None
    r, g, b = rgb
    if min(rgb) >= 0xD0 and max(rgb) - min(rgb) <= 0x18:  # white or a light grey
        return PAPER_NOT_SUBMITTED
    if g > r and g >= b:
        return PAPER_SUBMITTED
    if r > g and r > b:
        return PAPER_MISSED
    return None


def _parse_row(soup: BeautifulSoup, cells: list[Tag], layout: Layout, tr: Tag | None = None) -> AssignmentRow:
    question, submission, status, deadline = (
        cells[layout.question], cells[layout.submission], cells[layout.action], cells[layout.deadline]
    )
    q_link = question.find("a", href=re.compile(r"Download\.php"))
    q_key = decode_download_key(q_link["href"]) if q_link else None
    s_link = submission.find("a", href=re.compile(r"Download\.php"))
    s_key = decode_download_key(s_link["href"]) if s_link else None
    marks_text = _text(cells[layout.marks])
    number = _text(cells[layout.number])
    remarks = _remarks(cells[layout.remarks], layout)
    term = starts_at = None
    if layout.term is not None:
        term, extra, note = _term(cells[layout.term])
        title = " ".join(filter(None, [f"{term} paper" if term else f"Paper {number}", extra]))
        if note:
            remarks = f"{remarks}; {note}" if remarks else note
        starts_at, ends_at = _window(deadline)
    else:
        title = _text(cells[layout.title])
    if starts_at is None:
        labels = deadline.find_all(["small", "span"]) or [deadline]
        ends_at = parse_deadline(_text(labels[-1]))
    hidden = bool(s_link) and s_key is None and "k=NA" in (s_link.get("href") or "")

    return AssignmentRow(
        number=number,
        title=title,
        question_file=q_key.filename if q_key else None,
        question_href=q_link["href"] if q_link else None,
        remarks=remarks,
        submission_file=s_key.filename if s_key else None,
        marks=None if marks_text.lower() in ("", "not marked yet", "---") else marks_text,
        action=_action(status),
        deadline=ends_at,
        assignment_id=_assignment_id(soup, status),
        term=term or None,
        starts_at=starts_at,
        submission_hidden=hidden,
        paper_status=_paper_status(tr) if tr is not None and layout.term is not None else None,
        submit_form=_submit_form(soup, status) if _action(status) == "submit" else None,
        delete_href=_delete_href(soup, status) if _action(status) == "delete" else None,
    )


def parse_assignments_page(html: str) -> AssignmentsPage:
    return parse_listing(html, ASSIGNMENTS)


def parse_papers_page(html: str) -> AssignmentsPage:
    return parse_listing(html, PAPERS)


def parse_listing(html: str, layout: Layout) -> AssignmentsPage:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-hover")
    if table is None:
        raise ContractBroken(f"The {layout.name} page has no {layout.name} table")
    headers = [_text(th).lower() for th in table.find_all("th")]
    if (
        len(headers) < layout.columns
        or not headers[0].startswith(layout.first_header)
        or "deadline" not in headers[layout.deadline]
    ):
        raise ContractBroken(f"{layout.name.capitalize()} table columns changed: {headers}")

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) == 1 and any(m in _text(cells[0]).lower() for m in NO_ROWS):
            continue
        if len(cells) >= layout.columns:
            rows.append(_parse_row(soup, cells, layout, tr))
        elif cells:
            raise ContractBroken(f"{layout.name.capitalize()} row has {len(cells)} cells, expected {layout.columns}")

    return AssignmentsPage(
        semesters=_options(soup, "semesterId"),
        courses=_options(soup, "courseId"),
        rows=rows,
    )
