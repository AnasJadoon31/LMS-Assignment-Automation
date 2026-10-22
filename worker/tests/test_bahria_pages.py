from datetime import datetime

import pytest

from lms_worker.lms.bahria_pages import (
    LMS_TZ,
    b64_id,
    decode_download_key,
    parse_assignments_page,
    parse_deadline,
    parse_papers_page,
)
from lms_worker.lms.errors import ContractBroken

from .conftest import ASSIGNMENTS_HTML, EMPTY_ASSIGNMENTS_HTML, EMPTY_PAPERS_HTML, PAPERS_HTML


def test_selects_give_semester_and_courses():
    page = parse_assignments_page(ASSIGNMENTS_HTML)
    assert page.current_semester.label == "Fall-2026"
    assert page.current_semester.value == "MjAyNjM="
    assert [(c.label, b64_id(c.value), c.selected) for c in page.courses] == [
        ("Computer Architecture Lab", "100002", True),
        ("Design & Analysis of Algorithms", "100003", False),
    ]


def test_rows_closed_submitted_open_submitted_and_open():
    closed, submitted, open_ = parse_assignments_page(ASSIGNMENTS_HTML).rows

    assert (closed.number, closed.title, closed.action) == ("1", "Lab 1 Intro", "closed")
    assert closed.deadline_passed and closed.assignment_id is None
    assert closed.marks == "8/10"
    assert closed.submission_file == "02-000000-001-100002100001-23092026-090000am.pdf"

    assert (submitted.action, submitted.assignment_id) == ("delete", "230173")
    assert not submitted.deadline_passed
    assert submitted.remarks == "Use MARS 4.5"
    assert submitted.marks is None
    assert submitted.question_file == "Lab-2-Q-25092026-105052pm.pdf"

    assert (open_.action, open_.assignment_id) == ("submit", "230368")
    assert open_.submission_file is None
    assert open_.deadline == datetime(2026, 10, 11, 23, 55, tzinfo=LMS_TZ)


def test_empty_course_has_no_rows():
    assert parse_assignments_page(EMPTY_ASSIGNMENTS_HTML).rows == []


def test_download_key_first_field_is_the_student_not_the_assignment():
    key = decode_download_key(parse_assignments_page(ASSIGNMENTS_HTML).rows[1].question_href)
    assert key.student_id == "100001"
    assert key.kind == "Assignment"
    assert key.filename == "Lab-2-Q-25092026-105052pm.pdf"
    assert decode_download_key("Download.php?k=%%%") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 October 2026-11:55 pm", datetime(2026, 10, 1, 23, 55, tzinfo=LMS_TZ)),
        ("12 October 2026-11:00 am", datetime(2026, 10, 12, 11, 0, tzinfo=LMS_TZ)),
        ("  23 September 2026-12:00 pm ", datetime(2026, 9, 23, 12, 0, tzinfo=LMS_TZ)),
        ("Extended: 25 Oct 2026 - 11:55 PM", datetime(2026, 10, 25, 23, 55, tzinfo=LMS_TZ)),
        ("3 November 2026 23:59", datetime(2026, 11, 3, 23, 59, tzinfo=LMS_TZ)),
        ("tomorrow", None),
    ],
)
def test_parse_deadline(text, expected):
    assert parse_deadline(text) == expected


@pytest.mark.parametrize("cell", [
    # the teacher extended it: the new label counts, before or after the Actual one
    "<small class='label label-info' title='Actual'>11 October 2026-11:55 pm</small><br>"
    "<small class='label label-danger' title='Extended'>Extended: 18 October 2026-11:55 pm</small>",
    "<small class='label label-danger' title='Extended'>18 October 2026-11:55 pm</small> "
    "<small class='label label-info' title='Actual'>11 October 2026-11:55 pm</small>",
    # untitled labels: the latest date
    "<small>18 October 2026-11:55 pm</small><small>11 October 2026-11:55 pm</small>",
])
def test_an_extended_deadline_is_read(cell):
    html = ASSIGNMENTS_HTML.replace("<small class='label label-info' title='Actual'>11 October 2026-11:55 pm</small>", cell)
    rows = parse_assignments_page(html).rows
    assert rows[-1].deadline == datetime(2026, 10, 18, 23, 55, tzinfo=LMS_TZ)


def test_changed_table_layout_breaks_the_contract():
    with pytest.raises(ContractBroken, match="columns changed"):
        parse_assignments_page(ASSIGNMENTS_HTML.replace("Deadline</th>", "Due</th>"))
    with pytest.raises(ContractBroken, match="no assignments table"):
        parse_assignments_page("<html><body>Maintenance</body></html>")
    with pytest.raises(ContractBroken, match="courseId"):
        parse_assignments_page(ASSIGNMENTS_HTML.replace('id="courseId"', 'id="course"'))


def test_lab_paper_rows_have_an_exam_window():
    mid, final, upcoming = parse_papers_page(PAPERS_HTML).rows
    # Real past papers of a lab course (Spring 2026)
    assert (mid.number, mid.title, mid.term, mid.action) == ("1", "Mid Term paper (60 Minutes)", "Mid Term", "closed")
    assert mid.starts_at == datetime(2026, 3, 17, 12, 55, tzinfo=LMS_TZ)
    assert mid.deadline == datetime(2026, 3, 17, 13, 55, tzinfo=LMS_TZ)
    assert mid.question_file == "DBMS-lab----2-17032026-123230pm.pdf"
    assert mid.remarks is None and mid.marks is None
    assert mid.submission_file is None and mid.submission_hidden  # Download.php?k=NA
    assert (mid.paper_status, final.paper_status, upcoming.paper_status) == ("submitted", "submitted", "not_submitted")
    assert final.title == "Final Term paper (Objective-2hours)" and final.remarks == "For BUMDC Only"
    assert (final.starts_at.hour, final.deadline.hour) == (12, 14)
    # An open paper (markup assumed from the assignments page)
    assert (upcoming.action, upcoming.assignment_id, upcoming.remarks) == ("submit", "88001", "Bring your own calculator")
    assert upcoming.starts_at == datetime(2026, 10, 20, 14, 0, tzinfo=LMS_TZ)
    assert not upcoming.deadline_passed
    assert parse_papers_page(EMPTY_PAPERS_HTML).rows == []


@pytest.mark.parametrize("style, status", [
    ('style="background: #90EE90"', "submitted"),  # what the LMS uses
    ('style="background-color:lightgreen;"', "submitted"),
    ('style="background: #FFFFFF"', "not_submitted"),
    ('style="background:#fff"', "not_submitted"),
    ('style="background: #FF7F7F"', "missed"),
    ('style="background: rgb(255, 204, 203)"', "missed"),
    ('bgcolor="red"', "missed"),
    ('style="background: #337ab7"', None),  # not one of the three
    ("", None),
])
def test_a_paper_row_colour_is_its_status(style, status):
    row = PAPERS_HTML.replace('<tr style="background: #FFFFFF">', f"<tr {style}>")
    assert parse_papers_page(row).rows[2].paper_status == status


def test_assignment_rows_have_no_paper_status():
    assert {r.paper_status for r in parse_assignments_page(ASSIGNMENTS_HTML).rows} == {None}


def test_layouts_do_not_mix():
    with pytest.raises(ContractBroken, match="Papers table columns changed"):
        parse_papers_page(ASSIGNMENTS_HTML)
    with pytest.raises(ContractBroken, match="Assignments table columns changed"):
        parse_assignments_page(PAPERS_HTML)
