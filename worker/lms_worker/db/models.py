"""PostgreSQL tables. The jobs table is also the work queue."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, false, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class CourseMode(StrEnum):
    NOTIFY = "notify"
    APPROVE = "approve"
    AUTO = "auto"


class JobState(StrEnum):
    DETECTED = "DETECTED"
    DOWNLOADED = "DOWNLOADED"
    TRIAGED = "TRIAGED"
    SOLVING = "SOLVING"
    RENDERED = "RENDERED"
    VERIFIED = "VERIFIED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUBMITTING = "SUBMITTING"
    CONFIRMED = "CONFIRMED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"  # the teacher changed the assignment; a newer revision took over
    HANDWRITTEN = "HANDWRITTEN"  # solved and sent to you to copy by hand; never uploaded


TERMINAL_STATES = {JobState.CONFIRMED, JobState.NEEDS_HUMAN, JobState.REJECTED, JobState.SUPERSEDED, JobState.HANDWRITTEN}


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("semester", "lms_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    lms_ref: Mapped[str] = mapped_column(String(64))
    semester: Mapped[str] = mapped_column(String(64))
    semester_name: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(16), default=CourseMode.APPROVE)
    sandbox_profile: Mapped[str] = mapped_column(String(64), default="python-jupyter")
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    assignments: Mapped[list[Assignment]] = relationship(back_populates="course")


class Assignment(Base):
    """An assignment or an exam paper (kind). Papers are tracked and announced, never solved."""

    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("course_id", "lms_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    kind: Mapped[str] = mapped_column(String(16), default="assignment", server_default="assignment", index=True)
    term: Mapped[str | None] = mapped_column(String(32))
    lms_ref: Mapped[str] = mapped_column(String(64))
    lms_assignment_id: Mapped[str | None] = mapped_column(String(64))
    number: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512))
    deadline: Mapped[datetime | None]
    attachment_name: Mapped[str | None] = mapped_column(String(512))
    remarks: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    # Why no job was made, e.g. closed or already submitted when first seen
    skipped_reason: Mapped[str | None] = mapped_column(Text)
    # Papers: the exam window opens at starts_at and closes at deadline
    starts_at: Mapped[datetime | None]
    opened_at: Mapped[datetime | None]  # when "the paper is open" was sent
    # What the LMS showed at the last poll
    submitted_file: Mapped[str | None] = mapped_column(String(512))
    submission_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    paper_status: Mapped[str | None] = mapped_column(String(16))  # lab papers: submitted, not_submitted, missed
    deadline_passed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    marks: Mapped[str | None] = mapped_column(String(64))
    status_seen_at: Mapped[datetime | None]
    # The latest downloaded question file or paper, and whether it was announced
    question_path: Mapped[str | None] = mapped_column(String(1024))
    announced_at: Mapped[datetime | None]
    # Deadline reminders already sent for the current deadline, e.g. "24h,2h"
    reminders: Mapped[str] = mapped_column(String(64), default="", server_default="")
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    course: Mapped[Course] = relationship(back_populates="assignments")
    jobs: Mapped[list[Job]] = relationship(back_populates="assignment", order_by="Job.revision")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("assignment_id", "revision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"))
    revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default=JobState.DETECTED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cost_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    # The file name the LMS shows after our upload; tells our submissions from yours
    lms_submission_file: Mapped[str | None] = mapped_column(String(512))
    # Your instruction when you ask for a redo
    note: Mapped[str | None] = mapped_column(Text)
    # Solving: not before this time (a usage pause or a retry), the Claude session to resume,
    # the plan cost the CLI reported, and Claude's summary of the finished report
    not_before: Mapped[datetime | None]
    claude_session: Mapped[str | None] = mapped_column(String(64))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    assignment: Mapped[Assignment] = relationship(back_populates="jobs")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="job")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # question, notebook, json, docx, pdf, receipt
    path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    job: Mapped[Job] = relationship(back_populates="artifacts")


class Event(Base):
    """Audit trail. job_id is empty for system events such as a paused login."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")


class Notification(Base):
    """Outbox: one row per message per channel, written with the change, sent after,
    retried until delivered. Channels deliver independently."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    channel: Mapped[str] = mapped_column(String(16))  # whatsapp, discord, console
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ChatSession(Base):
    """Where each WhatsApp chat is in the bot's menus. Resets after 10 minutes idle."""

    __tablename__ = "chat_sessions"

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)  # set by the bot on every message


class InboundMessage(Base):
    """Message ids already handled, so a webhook delivered twice is answered once."""

    __tablename__ = "inbound_messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
