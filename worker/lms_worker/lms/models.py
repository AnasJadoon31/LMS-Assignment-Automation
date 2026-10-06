"""Types the rest of the system sees. Nothing outside lms/ touches portal HTML."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Course:
    code: str  # LMS offered-course id until a real code is set in the web UI
    name: str
    lms_ref: str  # base64 offered-course id, the `oc` query parameter
    semester: str  # base64 semester id, the `s` query parameter
    semester_name: str = ""


ASSIGNMENT = "assignment"
PAPER = "paper"

# A lab paper's row colour on the LMS: green, white, red
PAPER_SUBMITTED = "submitted"
PAPER_NOT_SUBMITTED = "not_submitted"
PAPER_MISSED = "missed"  # the exam ended with no submission


@dataclass(frozen=True)
class AssignmentMeta:
    """An assignment or an exam paper: both are listed the same way on the LMS."""

    course: Course
    lms_ref: str  # "<course code>:<row number>", papers "<course code>:P<row number>"
    number: str
    title: str
    deadline: datetime | None
    attachment_name: str | None
    attachment_size: int | None = None
    remarks: str | None = None
    assignment_id: str | None = None  # LMS id, shown only while the assignment is open
    kind: str = ASSIGNMENT
    term: str | None = None  # papers: "Mid Term", "Final Term"
    starts_at: datetime | None = None  # papers: when the exam window opens; deadline is when it closes
    # Snapshot from the listing, not part of the fingerprint
    submitted_file: str | None = None
    deadline_passed: bool = False
    marks: str | None = None
    submission_hidden: bool = False  # papers: the LMS shows a submission link with no file
    paper_status: str | None = None  # papers: PAPER_SUBMITTED, PAPER_NOT_SUBMITTED or PAPER_MISSED

    @property
    def submitted(self) -> bool:
        return self.submitted_file is not None or self.paper_status == PAPER_SUBMITTED

    def fingerprint(self) -> str:
        """Hash of course code, title, deadline and attachment name plus size.

        A new value on a known assignment means the teacher changed it.
        """
        parts = [
            self.course.code,
            self.title,
            self.deadline.isoformat() if self.deadline else "",
            self.attachment_name or "",
            str(self.attachment_size or ""),
        ]
        if self.starts_at is not None:  # papers only, so assignment fingerprints stay as they were
            parts.append(self.starts_at.isoformat())
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class Receipt:
    filename: str
    submitted_at: datetime
    screenshot: Path


@dataclass(frozen=True)
class SubmissionStatus:
    """What the LMS shows for an assignment.

    Whether a submitted file is the service's own is decided by the caller,
    against the filename in its receipt.
    """

    submitted_file: str | None
    deadline_passed: bool
    marks: str | None = None

    @property
    def submitted(self) -> bool:
        return self.submitted_file is not None
