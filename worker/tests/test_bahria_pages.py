from datetime import datetime

import pytest

from lms_worker.lms.bahria_pages import (
    LMS_TZ,
    b64_id,
    decode_download_key,
    parse_assignments_page,
    parse_deadline,
)
from lms_worker.lms.errors import ContractBroken

from .conftest import ASSIGNMENTS_HTML, EMPTY_ASSIGNMENTS_HTML


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
        ("tomorrow", None),
    ],
)
def test_parse_deadline(text, expected):
    assert parse_deadline(text) == expected


def test_changed_table_layout_breaks_the_contract():
    with pytest.raises(ContractBroken, match="columns changed"):
        parse_assignments_page(ASSIGNMENTS_HTML.replace("Deadline</th>", "Due</th>"))
    with pytest.raises(ContractBroken, match="no assignments table"):
        parse_assignments_page("<html><body>Maintenance</body></html>")
    with pytest.raises(ContractBroken, match="courseId"):
        parse_assignments_page(ASSIGNMENTS_HTML.replace('id="courseId"', 'id="course"'))
