import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from lms_worker.db import Artifact, Assignment, Course, Event, Job, JobState
from lms_worker.kv import DbKV, FileKV, SecretBox
from lms_worker.lms import models as lms
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.lms.errors import BadCredentials, ContractBroken, LMSError, ReloginPaused
from lms_worker.lms.session import SessionStore
from lms_worker.notify import Outbox
from lms_worker.schedule import next_delay
from lms_worker.schedule import run_forever
from lms_worker.watcher import HEARTBEAT_KEY, POLL_NOW_KEY, POLL_PAUSE_KEY, PollResult, Watcher

from .test_notify import Flaky

DAA = lms.Course(code="155150", name="Design & Analysis of Algorithms", lms_ref="MTU1MTUw", semester="MjAyNjM=", semester_name="Fall-2026")
LAB = lms.Course(code="151704", name="Computer Architecture Lab", lms_ref="MTUxNzA0", semester="MjAyNjM=", semester_name="Fall-2026")
DUE = datetime(2026, 10, 11, 23, 55, tzinfo=LMS_TZ)


def meta(course, number, title, **kw):
    defaults = dict(
        course=course,
        lms_ref=f"{course.code}:{'P' if kw.get('kind') == 'paper' else ''}{number}",
        number=str(number),
        title=title,
        deadline=DUE,
        attachment_name=f"{title.replace(' ', '-')}.pdf",
    )
    return lms.AssignmentMeta(**(defaults | kw))


class FakeLMS:
    def __init__(self):
        self.listing: dict[str, list[lms.AssignmentMeta]] = {}
        self.papers: dict[str, list[lms.AssignmentMeta]] = {}
        self.courses: list[lms.Course] = []
        self.session_error: Exception | None = None
        self.download_error: dict[str, Exception] = {}
        self.calls: list[str] = []

    def set(self, course, *assignments):
        if course not in self.courses:
            self.courses.append(course)
        self.listing[course.lms_ref] = list(assignments)

    def ensure_session(self):
        self.calls.append("ensure_session")
        if self.session_error:
            raise self.session_error

    def list_courses(self):
        self.calls.append("list_courses")
        return list(self.courses)

    def list_assignments(self, course):
        self.calls.append(f"list:{course.code}")
        return list(self.listing[course.lms_ref])

    def list_papers(self, course):
        self.calls.append(f"papers:{course.code}")
        return list(self.papers.get(course.lms_ref, []))

    def download(self, a, dest: Path):
        self.calls.append(f"download:{a.lms_ref}")
        if a.lms_ref in self.download_error:
            raise self.download_error[a.lms_ref]
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / a.attachment_name
        p.write_bytes(b"%PDF " + a.title.encode())
        return [p]


@pytest.fixture
def lmsfake():
    return FakeLMS()


@pytest.fixture
def channel():
    return Flaky()


@pytest.fixture
def watcher(lmsfake, sessions, channel, tmp_path):
    return Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data")


def jobs(sessions):
    with sessions() as s:
        return [(j.assignment.title, j.revision, j.state) for j in s.scalars(select(Job).order_by(Job.id))]


def test_first_poll_makes_jobs_only_for_open_unsubmitted_work(watcher, lmsfake, channel, sessions, tmp_path):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", remarks="Show all steps"))
    lmsfake.set(
        LAB,
        meta(LAB, 5, "Lab 5", deadline=DUE - timedelta(days=7), deadline_passed=True, submitted_file="mine.pdf"),
        meta(LAB, 6, "Lab 6", submitted_file="02-...-0930pm.pdf"),
    )

    r = watcher.poll_once()

    assert (r.status, r.courses, r.seen, r.new_jobs, r.skipped, r.downloaded) == ("ok", 2, 3, 1, 2, 1)
    assert jobs(sessions) == [("Recurrence Relations", 1, "DOWNLOADED")]
    with sessions() as s:
        reasons = dict(s.execute(select(Assignment.title, Assignment.skipped_reason)).all())
        assert reasons["Lab 6"] == "Already submitted on the LMS, not by the service"
        assert reasons["Lab 5"] == "Already submitted on the LMS, not by the service"
        assert reasons["Recurrence Relations"] is None
        assert s.scalar(select(func.count()).select_from(Course)) == 2
        art = s.scalar(select(Artifact))
        assert art.kind == "question" and Path(art.path).read_bytes().startswith(b"%PDF")
        assert Path(art.path).parent == tmp_path / "data" / "155150" / "1" / "1"

    courses_msg, assignment_msg = channel.sent
    assert courses_msg[0].startswith("New courses found on the LMS:") and "approve mode" in courses_msg[0]
    caption, filename = assignment_msg
    assert filename == "Recurrence-Relations.pdf"
    assert caption.splitlines() == [
        "New assignment",
        "Design & Analysis of Algorithms",
        "Recurrence Relations",
        "Due Sun 11 Oct, 11:55 PM",
        "Teacher's note: Show all steps",
    ]


def test_a_quiet_second_poll_changes_nothing(watcher, lmsfake, channel, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    watcher.poll_once()
    sent = len(channel.sent)

    r = watcher.poll_once()

    assert (r.new_jobs, r.revisions, r.downloaded) == (0, 0, 0)
    assert len(channel.sent) == sent
    assert jobs(sessions) == [("Recurrence Relations", 1, "DOWNLOADED")]


def test_a_changed_assignment_gets_a_new_revision(watcher, lmsfake, channel, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    watcher.poll_once()

    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=DUE + timedelta(days=2), attachment_name="v2.pdf"))
    r = watcher.poll_once()

    assert r.revisions == 1
    assert jobs(sessions) == [
        ("Recurrence Relations", 1, "SUPERSEDED"),
        ("Recurrence Relations", 2, "DOWNLOADED"),
    ]
    caption, filename = channel.sent[-1]
    assert caption.startswith("Assignment changed (revision 2)")
    assert "Changed: deadline, question file" in caption
    assert filename == "v2.pdf"


def test_a_closed_assignment_that_reopens_gets_a_job(watcher, lmsfake, sessions):
    lmsfake.set(LAB, meta(LAB, 3, "Lab 3", deadline=DUE - timedelta(days=9), deadline_passed=True))
    watcher.poll_once()
    assert jobs(sessions) == []

    lmsfake.set(LAB, meta(LAB, 3, "Lab 3", deadline=DUE))
    r = watcher.poll_once()

    assert r.new_jobs == 1
    assert jobs(sessions) == [("Lab 3", 1, "DOWNLOADED")]
    with sessions() as s:
        assert s.scalar(select(Assignment.skipped_reason)) is None


def test_your_own_submission_stops_an_unfinished_job(watcher, lmsfake, channel, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    watcher.poll_once()
    sent = len(channel.sent)

    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", submitted_file="02-000000-001-x.pdf"))
    r = watcher.poll_once()

    assert r.stopped == 1
    assert jobs(sessions) == [("Recurrence Relations", 1, "NEEDS_HUMAN")]
    assert len(channel.sent) == sent  # nothing was solved yet, so no message


def test_a_stop_after_work_started_is_announced(watcher, lmsfake, channel, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    watcher.poll_once()
    with sessions.begin() as s:
        s.scalar(select(Job)).state = JobState.AWAITING_APPROVAL

    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline_passed=True))
    watcher.poll_once()

    assert jobs(sessions) == [("Recurrence Relations", 1, "NEEDS_HUMAN")]
    assert channel.sent[-1][0] == (
        "Stopped: Design & Analysis of Algorithms · Recurrence Relations\nThe deadline passed before the job finished."
    )


def test_a_service_upload_is_not_mistaken_for_yours(watcher, lmsfake, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    watcher.poll_once()
    with sessions.begin() as s:
        job = s.scalar(select(Job))
        job.state = JobState.AWAITING_APPROVAL  # e.g. a redo after the service's upload
        job.lms_submission_file = "ours.pdf"

    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", submitted_file="ours.pdf"))
    r = watcher.poll_once()

    assert r.stopped == 0
    assert jobs(sessions) == [("Recurrence Relations", 1, "AWAITING_APPROVAL")]


def test_downloads_retry_and_alert_on_the_third_failure(watcher, lmsfake, channel, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    lmsfake.download_error["155150:1"] = LMSError("404 from Download.php")

    for _ in range(4):
        watcher.poll_once()

    with sessions() as s:
        job = s.scalar(select(Job))
        assert (job.state, job.attempts, job.last_error) == ("DETECTED", 4, "404 from Download.php")
    alerts = [m for m, _ in channel.sent if m.startswith("Download failed")]
    assert alerts == ["Download failed 3 times: Design & Analysis of Algorithms · Recurrence Relations\n404 from Download.php"]

    del lmsfake.download_error["155150:1"]
    watcher.poll_once()
    assert jobs(sessions) == [("Recurrence Relations", 1, "DOWNLOADED")]


def test_a_changed_layout_pauses_polling_until_resume(watcher, lmsfake, channel, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    lmsfake.list_assignments = lambda course: (_ for _ in ()).throw(ContractBroken("Assignments table columns changed"))

    assert watcher.poll_once().status == "contract"
    calls = len(lmsfake.calls)
    second = watcher.poll_once()

    assert second.status == "paused"
    assert len(lmsfake.calls) == calls  # the LMS was not touched while paused
    assert [m for m, _ in channel.sent if "page changed" in m] == [
        "The LMS page changed: Assignments table columns changed\nPolling is paused. Check the page, then run lms-worker resume."
    ]
    DbKV(sessions).delete(POLL_PAUSE_KEY)
    del lmsfake.list_assignments
    assert watcher.poll_once().status == "ok"


def test_session_alerts_are_sent_once_and_reset_after_recovery(watcher, lmsfake, channel):
    lmsfake.set(DAA)
    lmsfake.session_error = ReloginPaused("CAPTCHA on CMS login")

    assert watcher.poll_once().status == "session"
    watcher.poll_once()
    assert [m for m, _ in channel.sent] == ["LMS login is paused: CAPTCHA on CMS login"]

    lmsfake.session_error = None
    watcher.poll_once()
    lmsfake.session_error = BadCredentials("Invalid Enrollment or Password.")
    watcher.poll_once()
    assert channel.sent[-1][0].startswith("CMS rejected the login: Invalid Enrollment or Password.")


def test_lms_down_backs_off_and_alerts_after_three_cycles(watcher, lmsfake, channel):
    lmsfake.set(DAA)
    lmsfake.session_error = httpx.ConnectError("timed out")
    results = [watcher.poll_once() for _ in range(4)]

    assert [r.status for r in results] == ["lms_down"] * 4
    alerts = [m for m, _ in channel.sent if "has not answered" in m]
    assert len(alerts) == 1 and alerts[0].endswith("ConnectError. Retrying with back-off.")

    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    rng = random.Random(1)
    assert [next_delay(now, results[0], n, rng) for n in (1, 2, 3, 4)] == [300, 900, 3600, 3600]


def test_messages_wait_in_the_outbox_while_whatsapp_is_down(lmsfake, sessions, tmp_path):
    down = Flaky(fail_times=2)
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": down}), tmp_path)
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))

    assert w.poll_once().sent == 0
    assert w.poll_once().sent == 0
    assert w.poll_once().sent == 2
    assert [m.splitlines()[0] for m, _ in down.sent] == ["New courses found on the LMS:", "New assignment"]


def test_heartbeat_records_the_last_poll(watcher, lmsfake, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    watcher.poll_once()
    beat = json.loads(DbKV(sessions).get(HEARTBEAT_KEY))
    assert beat["status"] == "ok" and beat["seen"] == 1 and "at" in beat


def test_every_transition_is_in_the_audit_trail(watcher, lmsfake, sessions):
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations"))
    watcher.poll_once()
    with sessions() as s:
        trail = s.execute(select(Event.from_state, Event.to_state, Event.detail).where(Event.job_id.is_not(None)).order_by(Event.id)).all()
    assert [tuple(t) for t in trail] == [
        (None, "DETECTED", "new assignment"),
        ("DETECTED", "DOWNLOADED", "downloaded 1 file(s)"),
    ]


@pytest.mark.parametrize(
    "pkt_hour,minutes",
    [(7, 30), (8, 10), (12, 10), (22, 10), (23, 30), (2, 30)],
)
def test_poll_every_10_minutes_by_day_and_30_overnight(pkt_hour, minutes):
    now = datetime(2026, 10, 6, pkt_hour, 30, tzinfo=LMS_TZ)
    delay = next_delay(now, PollResult(), 0, random.Random(7))
    assert minutes * 60 <= delay <= minutes * 60 + 20


def test_session_is_encrypted_in_the_database(sessions, urls):
    store = SessionStore(DbKV(sessions), SecretBox("x" * 40))
    store.save_pasted("secret-session-id", urls)
    raw = DbKV(sessions).get("lms_session")
    assert "secret-session-id" not in raw
    assert store.load()["cookies"][0]["value"] == "secret-session-id"
    # A new APP_SECRET_KEY cannot read it: treated as no session, so the worker logs in again
    assert SessionStore(DbKV(sessions), SecretBox("y" * 40)).load() is None
    with pytest.raises(ValueError, match="at least 32"):
        SecretBox("short")


def test_file_store_is_owner_only(tmp_path):
    kv = FileKV(tmp_path)
    kv.set("lms_session", "{}")
    assert oct(kv.path("lms_session").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        kv.path("../escape")


def paper(course, number, term, **kw):
    return meta(course, number, f"{term} paper", kind="paper", term=term, **kw)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


OSLAB = lms.Course(code="151869", name="Operating Systems Lab", lms_ref="MTUxODY5", semester="MjAyNjM=", semester_name="Fall-2026")


def lab_paper(start, minutes=90, number=1, term="Mid Term", **kw):
    kw.setdefault("paper_status", "not_submitted")  # a white row
    return paper(OSLAB, number, term, starts_at=start, deadline=start + timedelta(minutes=minutes),
                 submission_hidden=True, **kw)


def test_papers_are_read_only_for_lab_courses(watcher, lmsfake):
    lmsfake.set(DAA)
    lmsfake.set(OSLAB)
    watcher.poll_once()
    assert "papers:151869" in lmsfake.calls and "papers:155150" not in lmsfake.calls


def test_a_lab_paper_is_scheduled_reminded_and_sent_when_it_opens(lmsfake, sessions, channel, tmp_path):
    now = datetime(2026, 10, 18, 10, 0, tzinfo=LMS_TZ)
    clock = Clock(now)
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path, now=clock)
    start = datetime(2026, 10, 20, 14, 0, tzinfo=LMS_TZ)
    lmsfake.set(OSLAB)
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start, remarks="Bring your own calculator")]

    r = w.poll_once()
    assert (r.papers, r.new_papers, r.new_jobs) == (1, 1, 0)
    assert jobs(sessions) == []
    assert "download:151869:P1" not in lmsfake.calls  # never fetched before it opens
    msg, f = channel.sent[-1]
    assert msg.splitlines() == [
        "Lab paper scheduled", "Operating Systems Lab", "Mid Term paper", "Tue 20 Oct, 2:00 PM to 3:30 PM",
        "Note: Bring your own calculator", "The paper comes here as soon as it opens.",
    ] and f is None
    assert w.next_wake() == start + timedelta(seconds=15)

    clock.at = start - timedelta(hours=23)
    assert w.poll_once().reminders == 1
    clock.at = start - timedelta(minutes=50)
    assert w.poll_once().reminders == 1
    reminders = [m for m, _ in channel.sent if m.startswith("Lab paper starts in")]
    assert [m.splitlines()[0] for m in reminders] == ["Lab paper starts in 23 h 0 min", "Lab paper starts in 50 min"]

    clock.at = start + timedelta(seconds=15)
    w.poll_once()
    msg, f = channel.sent[-1]
    assert msg.splitlines()[:4] == ["Lab paper open now", "Operating Systems Lab", "Mid Term paper", "Ends 3:30 PM (1 h 29 min left)"]
    assert f == "Mid-Term-paper.pdf"
    assert w.next_wake() is None

    sent = len(channel.sent)
    clock.at = start + timedelta(minutes=5)
    w.poll_once()
    assert len(channel.sent) == sent  # sent once


def test_an_open_paper_without_its_file_is_retried_every_minute(lmsfake, sessions, channel, tmp_path):
    start = datetime(2026, 10, 20, 14, 0, tzinfo=LMS_TZ)
    clock = Clock(start + timedelta(minutes=1))
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path, now=clock)
    lmsfake.set(OSLAB)
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start, attachment_name=None)]

    w.poll_once()
    assert channel.sent[-1][0].endswith("The paper file is not on the LMS yet; checking every minute.")
    assert w.next_wake() == clock.at + timedelta(seconds=60)

    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start, attachment_name="late.pdf")]
    clock.at = start + timedelta(minutes=2)
    w.poll_once()
    assert channel.sent[-1][1] == "late.pdf"
    assert len([m for m, _ in channel.sent if m.startswith("Lab paper open now")]) == 2


def test_past_lab_papers_are_recorded_quietly(watcher, lmsfake, channel, sessions):
    old = datetime(2026, 3, 17, 12, 55, tzinfo=LMS_TZ)
    lmsfake.set(OSLAB)
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(old, minutes=60, deadline_passed=True)]
    r = watcher.poll_once()
    assert r.skipped == 1
    assert not [m for m, _ in channel.sent if "Lab paper" in m]
    with sessions() as s:
        a = s.scalar(select(Assignment))
        assert (a.kind, a.submission_hidden, a.starts_at) == ("paper", True, old)


def test_a_paper_that_turns_green_or_red_gets_one_message(lmsfake, sessions, channel, tmp_path):
    start = datetime(2026, 10, 20, 14, 0, tzinfo=LMS_TZ)
    clock = Clock(start + timedelta(minutes=1))
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path, now=clock)
    lmsfake.set(OSLAB)
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start), lab_paper(start, number=2, term="Final Term")]
    w.poll_once()
    assert len([m for m, _ in channel.sent if m.startswith("Lab paper open now")]) == 2

    clock.at = start + timedelta(minutes=40)
    lmsfake.papers[OSLAB.lms_ref] = [
        lab_paper(start, paper_status="submitted"),
        lab_paper(start, number=2, term="Final Term"),
    ]
    w.poll_once()
    assert channel.sent[-1][0].splitlines() == [
        "Lab paper submitted", "Operating Systems Lab", "Mid Term paper", "The LMS now shows your submission (green).",
    ]
    clock.at = start + timedelta(hours=2)
    lmsfake.papers[OSLAB.lms_ref] = [
        lab_paper(start, paper_status="submitted", deadline_passed=True),
        lab_paper(start, number=2, term="Final Term", paper_status="missed", deadline_passed=True),
    ]
    w.poll_once()
    assert channel.sent[-1][0].splitlines()[:3] == ["Lab paper missed", "Operating Systems Lab", "Final Term paper"]
    sent = len(channel.sent)
    w.poll_once()
    assert len(channel.sent) == sent  # each once
    with sessions() as s:
        assert [a.paper_status for a in s.scalars(select(Assignment).order_by(Assignment.number))] == ["submitted", "missed"]


def test_a_green_paper_gets_no_reminder_and_is_not_fetched(lmsfake, sessions, channel, tmp_path):
    start = datetime(2026, 10, 20, 14, 0, tzinfo=LMS_TZ)
    clock = Clock(start - timedelta(days=3))
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path, now=clock)
    lmsfake.set(OSLAB)
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start)]
    w.poll_once()
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start, paper_status="submitted")]  # e.g. submitted on campus
    w.poll_once()
    assert channel.sent[-1][0].startswith("Lab paper submitted")
    sent = len(channel.sent)
    clock.at = start - timedelta(minutes=30)
    assert w.poll_once().reminders == 0
    assert w.next_wake() is None
    clock.at = start + timedelta(seconds=15)
    w.poll_once()
    assert "download:151869:P1" not in lmsfake.calls
    assert len(channel.sent) == sent


def test_a_paper_first_seen_green_or_without_a_colour_is_quiet(watcher, lmsfake, channel, sessions):
    old = datetime(2026, 3, 17, 12, 55, tzinfo=LMS_TZ)
    lmsfake.set(OSLAB)
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(old, minutes=60, deadline_passed=True, paper_status=None)]
    watcher.poll_once()
    # Rows saved before colours were read have none; their first colour is not news
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(old, minutes=60, deadline_passed=True, paper_status="submitted")]
    watcher.poll_once()
    # Open now but already green: recorded, not fetched
    lmsfake.papers[OSLAB.lms_ref].append(
        lab_paper(datetime.now(LMS_TZ) - timedelta(minutes=5), number=2, paper_status="submitted"))
    assert watcher.poll_once().skipped == 1
    assert "download:151869:P2" not in lmsfake.calls
    assert not [m for m, _ in channel.sent if "Lab paper" in m]
    with sessions() as s:
        assert [(a.paper_status, a.skipped_reason) for a in s.scalars(select(Assignment).order_by(Assignment.number))] == [
            ("submitted", "Deadline passed"), ("submitted", "Already submitted on the LMS"),
        ]


def test_a_rescheduled_paper_is_announced_again(lmsfake, sessions, channel, tmp_path):
    now = datetime(2026, 10, 18, 10, 0, tzinfo=LMS_TZ)
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path, now=Clock(now))
    start = datetime(2026, 10, 20, 14, 0, tzinfo=LMS_TZ)
    lmsfake.set(OSLAB)
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start)]
    w.poll_once()
    lmsfake.papers[OSLAB.lms_ref] = [lab_paper(start + timedelta(days=1))]
    w.poll_once()
    assert channel.sent[-1][0].splitlines()[:4] == ["Lab paper changed", "Operating Systems Lab", "Mid Term paper", "Wed 21 Oct, 2:00 PM to 3:30 PM"]


def test_marks_posted_is_sent_once_when_marks_appear(watcher, lmsfake, channel):
    lmsfake.set(LAB, meta(LAB, 4, "Lab 4", deadline_passed=True, submitted_file="mine.pdf", marks=None))
    watcher.poll_once()
    lmsfake.set(LAB, meta(LAB, 4, "Lab 4", deadline_passed=True, submitted_file="mine.pdf", marks="9/10"))
    r = watcher.poll_once()
    watcher.poll_once()

    assert r.marks_posted == 1
    assert [m for m, _ in channel.sent if m.startswith("Marks posted")] == ["Marks posted\nComputer Architecture Lab\nLab 4: 9/10"]


def test_assignment_reminders_24h_and_2h_before_the_deadline(lmsfake, sessions, channel, tmp_path):
    now = datetime(2026, 10, 10, 9, 0, tzinfo=LMS_TZ)
    clock = Clock(now)
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path, now=clock)
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=now + timedelta(hours=40)))
    w.poll_once()
    assert not [m for m, _ in channel.sent if " due in " in m]

    clock.at = now + timedelta(hours=17)  # 23 h left
    assert w.poll_once().reminders == 1
    clock.at = now + timedelta(hours=17, minutes=10)
    assert w.poll_once().reminders == 0
    clock.at = now + timedelta(hours=38, minutes=30)  # 1 h 30 min left
    assert w.poll_once().reminders == 1

    reminders = [m for m, _ in channel.sent if " due in " in m]
    assert reminders[0].startswith("Assignment due in 23 h 0 min, nothing submitted")
    assert reminders[1].startswith("Assignment due in 1 h 30 min, nothing submitted")


def test_no_reminder_once_submitted(lmsfake, sessions, channel, tmp_path):
    now = datetime(2026, 10, 10, 9, 0, tzinfo=LMS_TZ)
    clock = Clock(now)
    w = Watcher(lmsfake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path, now=clock)
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=now + timedelta(hours=30)))
    w.poll_once()
    lmsfake.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=now + timedelta(hours=30), submitted_file="mine.pdf"))
    clock.at = now + timedelta(hours=8)
    assert w.poll_once().reminders == 0


def test_status_snapshot_is_kept_for_every_item(watcher, lmsfake, sessions):
    lmsfake.set(LAB, meta(LAB, 6, "Lab 6", submitted_file="mine.pdf", marks=None))
    watcher.poll_once()
    with sessions() as s:
        a = s.scalar(select(Assignment))
        assert (a.submitted_file, a.deadline_passed, a.marks, a.kind) == ("mine.pdf", False, None, "assignment")
        assert a.status_seen_at is not None


def test_a_requested_check_ends_the_wait_early(watcher, lmsfake, sessions):
    import threading

    lmsfake.set(DAA)
    kv = DbKV(sessions)
    stop = threading.Event()
    polls = []

    def on_result(result, delay):
        polls.append(result.status)
        if len(polls) == 1:
            kv.set(POLL_NOW_KEY, "whatsapp")
        else:
            stop.set()

    import lms_worker.schedule as schedule

    old = schedule.WAKE_CHECK_SECONDS
    schedule.WAKE_CHECK_SECONDS = 0.05
    try:
        t = threading.Thread(
            target=run_forever,
            args=(watcher, stop),
            kwargs=dict(clock=lambda: datetime.now(timezone.utc), on_result=on_result,
                        poll_requested=lambda: kv.get(POLL_NOW_KEY) is not None),
        )
        t.start()
        t.join(timeout=10)
    finally:
        schedule.WAKE_CHECK_SECONDS = old
        stop.set()
    assert polls == ["ok", "ok"]  # the second poll came long before the 10-minute interval
    assert kv.get(POLL_NOW_KEY) is None
