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
    """Uploading: wait for your yes, or upload by itself."""

    APPROVE = "approve"
    AUTO = "auto"


class SolveMode(StrEnum):
    """Solving: always, only when you say so, or Claude asks you first when it needs something."""

    BYPASS = "bypass"
    MANUAL = "manual"
    AUTO = "auto"


class LectureMode(StrEnum):
    """The teacher's lecture notes as a reference for solving: Claude decides, always, or never."""

    AUTO = "auto"
    ALWAYS = "always"
    OFF = "off"


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
    WAITING_GO = "WAITING_GO"  # manual solving: the question is here, solving waits for your go-ahead
    NEEDS_INPUT = "NEEDS_INPUT"  # Claude asked you something before solving
    SUPERSEDED = "SUPERSEDED"  # the teacher changed the assignment; a newer revision took over
    HANDWRITTEN = "HANDWRITTEN"  # older jobs only; handwritten work now waits in AWAITING_APPROVAL


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
    solve: Mapped[str] = mapped_column(String(16), default=SolveMode.BYPASS, server_default="bypass")
    # Safe mode: a report still waiting for your approval is uploaded when one hour is left
    safe: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    lectures: Mapped[str] = mapped_column(String(16), default=LectureMode.OFF, server_default="off")
    sandbox_profile: Mapped[str] = mapped_column(String(64), default="python-jupyter")
    # Your own system prompt for this course's solver runs, set in the chat: added to the
    # built-in one, or used instead of it
    prompt: Mapped[str | None] = mapped_column(Text)
    # This course's own report theme: a design system artifact link, set in the chat. The default one without it
    theme: Mapped[str | None] = mapped_column(String(255))
    prompt_replaces: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
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
    # You asked for the LMS submission to be deleted; the worker does it at its next poll
    delete_requested_at: Mapped[datetime | None]
    # Your own file, sent in the chat, and when you asked for it to be uploaded
    own_file: Mapped[str | None] = mapped_column(String(1024))
    upload_requested_at: Mapped[datetime | None]
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
    # Notes for the solve (JSON): the lecture files put at hand, and a "Redo with Opus" you asked for
    triage: Mapped[str | None] = mapped_column(Text)
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


class SummaryState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class Summary(Base):
    """Summarized notes of the lectures you picked, asked for in the chat. Its own small queue."""

    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    lectures: Mapped[str] = mapped_column(Text)  # JSON: [{week, title, file}]
    style: Mapped[str] = mapped_column(String(16), default="notes")  # one kind now; older rows say short or detailed
    state: Mapped[str] = mapped_column(String(16), default=SummaryState.QUEUED, index=True)
    triage: Mapped[str | None] = mapped_column(Text)  # unused now; older rows hold a model choice
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    not_before: Mapped[datetime | None]
    claude_session: Mapped[str | None] = mapped_column(String(64))
    cost_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    path: Mapped[str | None] = mapped_column(String(1024))  # the finished PDF
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    course: Mapped[Course] = relationship()


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


class BotUser(Base):
    """Someone in a group (or a private chat) who uses the public menu: how often, and the last
    thing they picked. One row per person per chat; chat_id is the group JID or "private"."""

    __tablename__ = "bot_users"
    __table_args__ = (UniqueConstraint("chat_id", "person"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    person: Mapped[str] = mapped_column(String(64))  # phone digits, or the WhatsApp LID when the phone is hidden
    name: Mapped[str] = mapped_column(String(128), default="")
    requests: Mapped[int] = mapped_column(Integer, default=0)
    last_option: Mapped[str] = mapped_column(String(256), default="")
    first_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_at: Mapped[datetime] = mapped_column(default=utcnow)


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


# --- classes: other sections' groups, run by their CR (lms_worker/classes) ---
#
# Kept apart from the tables above on purpose: nothing of a class can reach your courses,
# jobs or files, and the solver never sees a class assignment.


class ClassStatus(StrEnum):
    PENDING = "pending"  # signed up, waiting for your approval
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    REMOVED = "removed"  # the CR removed it, or you did; its login and files are gone


class SchoolClass(Base):
    """A section whose groups get notices (and, if allowed, the menu) from the class bot."""

    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=ClassStatus.PENDING, index=True)
    paused_by: Mapped[str | None] = mapped_column(String(16))  # admin or cr; a CR cannot undo yours
    muted_until: Mapped[datetime | None]  # the CR's "pause notices for N days"
    semester: Mapped[str] = mapped_column(String(64), default="")
    semester_name: Mapped[str] = mapped_column(String(64), default="")
    # The CR's LMS login: the enrollment always, the password only if they chose to keep it
    enrollment: Mapped[str] = mapped_column(String(64), default="")
    institute: Mapped[str] = mapped_column(String(64), default="Karachi Campus")
    password_enc: Mapped[str | None] = mapped_column(Text)
    login_state: Mapped[str] = mapped_column(String(16), default="ok")  # ok, needs_signin, error
    login_error: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[str] = mapped_column(Text, default="{}")  # your rules for this class (JSON)
    choices: Mapped[str] = mapped_column(Text, default="{}")  # the CR's choices (JSON)
    summary_sent: Mapped[str] = mapped_column(String(32), default="")
    next_poll_at: Mapped[datetime | None]
    last_poll_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    approved_at: Mapped[datetime | None]

    people: Mapped[list[ClassPerson]] = relationship(back_populates="klass", cascade="all, delete-orphan")
    groups: Mapped[list[ClassGroup]] = relationship(back_populates="klass", cascade="all, delete-orphan")
    courses: Mapped[list[ClassCourse]] = relationship(back_populates="klass", cascade="all, delete-orphan")


class ClassPerson(Base):
    """Who runs a class: its CR, and deputies you invited. They sign in with a WhatsApp code."""

    __tablename__ = "class_people"
    __table_args__ = (UniqueConstraint("class_id", "phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(16), default="cr")  # cr, deputy
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    klass: Mapped[SchoolClass] = relationship(back_populates="people")


class ClassInvite(Base):
    """A one-time sign-up link you made. Only its hash is kept."""

    __tablename__ = "class_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))  # the class, as you named it
    role: Mapped[str] = mapped_column(String(16), default="cr")
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"))  # deputies join an existing class
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    used_by: Mapped[str | None] = mapped_column(String(32))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class ClassGroup(Base):
    """A WhatsApp group a CR ticked. A group belongs to one class."""

    __tablename__ = "class_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    group_jid: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    added_by: Mapped[str] = mapped_column(String(32), default="")
    added_at: Mapped[datetime] = mapped_column(default=utcnow)

    klass: Mapped[SchoolClass] = relationship(back_populates="groups")


class ClassCourse(Base):
    __tablename__ = "class_courses"
    __table_args__ = (UniqueConstraint("class_id", "semester", "lms_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    lms_ref: Mapped[str] = mapped_column(String(64))
    semester: Mapped[str] = mapped_column(String(64))
    semester_name: Mapped[str] = mapped_column(String(64), default="")
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    klass: Mapped[SchoolClass] = relationship(back_populates="courses")
    assignments: Mapped[list[ClassAssignment]] = relationship(back_populates="course", cascade="all, delete-orphan")


class ClassAssignment(Base):
    """What the class's assignment list shows. Nothing about anyone's submission is kept."""

    __tablename__ = "class_assignments"
    __table_args__ = (UniqueConstraint("course_id", "lms_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("class_courses.id"), index=True)
    lms_ref: Mapped[str] = mapped_column(String(64))
    number: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512))
    deadline: Mapped[datetime | None]
    attachment_name: Mapped[str | None] = mapped_column(String(512))
    remarks: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    deadline_passed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    question_path: Mapped[str | None] = mapped_column(String(1024))
    download_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reminders: Mapped[str] = mapped_column(String(64), default="", server_default="")
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    course: Mapped[ClassCourse] = relationship(back_populates="assignments")


class ClassMessage(Base):
    """The class bot's outbox: one row per message per group (or CR), sent in order, paced."""

    __tablename__ = "class_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), index=True)
    to: Mapped[str] = mapped_column(String(64))  # a group JID or a phone number
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class ClassLog(Base):
    """Everything that happens around classes, kept 60 days: who did what, what the bot sent,
    who asked the menu for what (the option, never their chat text), and problems."""

    __tablename__ = "class_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), index=True)
    actor: Mapped[str] = mapped_column(String(64))  # admin, cr:<phone>, bot, system, student:<id>
    kind: Mapped[str] = mapped_column(String(32), index=True)  # cr, admin, sent, menu, limit, problem
    detail: Mapped[str] = mapped_column(Text, default="")
    person: Mapped[str | None] = mapped_column(String(64), index=True)


class ClassBlock(Base):
    """Someone the bot ignores: in one class (class_id), or everywhere (none)."""

    __tablename__ = "class_blocks"
    __table_args__ = (UniqueConstraint("class_id", "person"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), index=True)
    person: Mapped[str] = mapped_column(String(64), index=True)
    by: Mapped[str] = mapped_column(String(16))  # admin or cr
    at: Mapped[datetime] = mapped_column(default=utcnow)


class ClassHit(Base):
    """Usage counted against the limits: menu requests, files, group replies, limit notices."""

    __tablename__ = "class_hits"

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(Integer, index=True)
    who: Mapped[str] = mapped_column(String(64))  # a person, or a group JID
    kind: Mapped[str] = mapped_column(String(16))  # request, file, reply, limited, announce
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
