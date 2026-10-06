from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from lms_worker.bot import Bot
from lms_worker.db import Artifact, Assignment, ChatSession, Course, Event, Job, JobState
from lms_worker.kv import DbKV
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.watcher import POLL_NOW_KEY, POLL_PAUSE_KEY

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=LMS_TZ)
CHAT = "923000000000"


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


@pytest.fixture
def semester(sessions, tmp_path):
    """Two courses: open, due-soon, submitted, missed and marked work, two papers, one job waiting for you."""
    q = tmp_path / "q.pdf"
    q.write_bytes(b"%PDF question")
    pdf = tmp_path / "solved.pdf"
    pdf.write_bytes(b"%PDF solved")
    with sessions.begin() as s:
        daa = Course(code="155150", name="DAA", lms_ref="MTU1MTUw", semester="MjAyNjM=", semester_name="Fall-2026")
        lab = Course(code="151704", name="CA Lab", lms_ref="MTUxNzA0", semester="MjAyNjM=", semester_name="Fall-2026")
        s.add_all([daa, lab])
        s.flush()

        def item(course, ref, title, deadline, **kw):
            a = Assignment(course=course, lms_ref=ref, number=ref.split(":")[1].lstrip("P"), title=title,
                           deadline=deadline, fingerprint="x", attachment_name=f"{title}.pdf", **kw)
            s.add(a)
            return a

        rec = item(daa, "155150:1", "Recurrence Relations", NOW + timedelta(hours=30), question_path=str(q))
        item(daa, "155150:2", "Sorting", NOW + timedelta(days=6))
        item(lab, "151704:5", "Lab 5", NOW - timedelta(days=5), deadline_passed=True, submitted_file="l5.pdf", marks="9/10")
        item(lab, "151704:6", "Lab 6", NOW + timedelta(days=2), submitted_file="l6.pdf")
        item(lab, "151704:4", "Lab 4", NOW - timedelta(days=9), deadline_passed=True)
        item(daa, "155150:P1", "Mid Term paper", NOW + timedelta(hours=20), kind="paper", term="Mid Term", question_path=str(q))
        item(lab, "151704:P1", "Mid Term paper", NOW - timedelta(days=1), kind="paper", term="Mid Term",
             deadline_passed=True, submitted_file="mid.pdf", marks="20/30")
        s.flush()
        job = Job(assignment=rec, revision=1, fingerprint="x", state=JobState.AWAITING_APPROVAL)
        s.add(job)
        s.flush()
        s.add(Artifact(job=job, kind="pdf", path=str(pdf), sha256="0" * 64, size=11))
    return {"daa": daa.id, "lab": lab.id, "rec": rec.id}


@pytest.fixture
def bot(sessions):
    return Bot(sessions, DbKV(sessions), now=Clock(NOW))


def say(bot, *messages):
    replies = []
    for m in messages:
        replies = bot.handle(CHAT, m)
    return replies


def text(replies):
    return "\n---\n".join(r.text for r in replies)


def test_main_menu_counts_everything(bot, semester):
    out = text(say(bot, "hi"))
    assert out.splitlines()[0] == "*LMS BOT*"
    assert "1. Assignments: 2 remaining, 1 due soon" in out
    assert "2. Papers: 1 remaining, 1 due soon" in out
    assert "3. Waiting for your approval (1)" in out
    assert "4. Due in the next 48 hours (2)" in out
    assert "7. Check the LMS now" in out and "8. System status" in out


def test_assignment_categories(bot, semester):
    out = text(say(bot, "menu", "1"))
    for line in [
        "1. Remaining (open, not submitted) (2)",
        "2. Due in the next 48 hours (1)",
        "3. Waiting for your approval (1)",
        "4. Queued or being solved (0)",
        "5. Stopped, needs you (0)",
        "6. Submitted (2)",
        "7. Missed (closed, no submission) (1)",
        "8. Marked (1)",
        "9. All (5)",
    ]:
        assert line in out


def test_paper_categories_and_lists(bot, semester):
    out = text(say(bot, "menu", "2"))
    assert "1. Remaining (open, not submitted) (1)" in out and "5. Marked (1)" in out
    assert "approval" not in out.lower()
    marked = text(say(bot, "5"))
    assert "1. Mid Term paper · CA Lab · due" in marked and "20/30" in marked


def test_remaining_list_is_sorted_by_deadline_and_opens_an_item(bot, semester):
    out = text(say(bot, "menu", "1", "1"))
    assert out.index("Recurrence Relations") < out.index("Sorting")
    item = text(say(bot, "1"))
    assert "Recurrence Relations\nDAA · Assignment 1" in item
    assert "(in 1 d 6 h)" in item
    assert "Status: not submitted" in item
    assert "Service: solved; waiting for your approval (revision 1)" in item
    assert "1. Send the question file" in item and "2. Send the solved PDF" in item
    assert "3. Approve and upload" in item and "4. Reject" in item and "5. Redo with a note" in item


def test_files_are_sent_as_documents(bot, semester):
    say(bot, "menu", "1", "1", "1")
    (q,) = say(bot, "1")
    assert q.file.read_bytes() == b"%PDF question"
    (pdf,) = say(bot, "2")
    assert pdf.file.read_bytes() == b"%PDF solved"


def test_approve_needs_a_yes(bot, semester, sessions):
    say(bot, "menu", "3", "1")
    confirm = text(say(bot, "3"))
    assert "Upload the solved PDF for Recurrence Relations to the LMS now?" in confirm
    assert "1. Yes" in confirm and "2. No" in confirm

    out = text(say(bot, "2"))
    assert out.startswith("Cancelled.")
    with sessions() as s:
        assert s.scalar(select(Job.state)) == "AWAITING_APPROVAL"

    say(bot, "3")
    out = text(say(bot, "1"))
    assert "Approved: Recurrence Relations. Uploading now" in out
    with sessions() as s:
        assert s.scalar(select(Job.state)) == "SUBMITTING"
        assert s.scalar(select(Event.detail).order_by(Event.id.desc())) == "approved on WhatsApp"


def test_a_wrong_digit_cannot_approve(bot, semester, sessions):
    say(bot, "menu", "3", "1", "3")
    out = text(say(bot, "7"))
    assert "Reply with one of the numbers below." in out
    with sessions() as s:
        assert s.scalar(select(Job.state)) == "AWAITING_APPROVAL"


def test_redo_takes_a_note_then_a_yes(bot, semester, sessions):
    say(bot, "menu", "3", "1")
    assert "Type what should change" in text(say(bot, "5"))
    confirm = text(say(bot, "Use the master theorem for part b"))
    assert 'Redo Recurrence Relations with this note?\n"Use the master theorem for part b"' in confirm
    say(bot, "1")
    with sessions() as s:
        job = s.scalar(select(Job))
        assert (job.state, job.note) == ("DOWNLOADED", "Use the master theorem for part b")


def test_reject(bot, semester, sessions):
    say(bot, "menu", "3", "1", "4")
    assert "Rejected: Recurrence Relations" in text(say(bot, "1"))
    with sessions() as s:
        assert s.scalar(select(Job.state)) == "REJECTED"


def test_by_course_has_assignments_papers_and_mode(bot, semester):
    out = text(say(bot, "menu", "5"))
    assert "1. DAA (2 assignments + 1 paper remaining)" in out and "2. CA Lab" in out
    course = text(say(bot, "2"))
    assert "*CA Lab*" in course
    assert "1. Assignments: nothing remaining" in course
    assert "2. Papers: nothing remaining" in course
    assert "3. Mode: approve (change)" in course
    missed = text(say(bot, "1", "7"))
    assert "Lab 4 · due" in missed and "CA Lab ·" not in missed  # course name left out inside a course


def test_switching_to_auto_asks_first(bot, semester, sessions):
    out = text(say(bot, "menu", "6", "1"))
    assert "*DAA · now approve*" in out
    warn = text(say(bot, "2"))
    assert "Switch DAA to auto?" in warn and "without you reading it first" in warn
    done = text(say(bot, "1"))
    assert done.startswith("DAA is now in auto mode.")
    assert "*Course modes*" in done and "1. DAA: auto" in done
    say(bot, "1", "3")
    with sessions() as s:
        assert s.scalar(select(Course.mode).where(Course.name == "DAA")) == "notify"


def test_back_and_menu(bot, semester):
    say(bot, "menu", "1", "1")
    assert text(say(bot, "0")).startswith("*Assignments · all courses*")
    assert text(say(bot, "0")).startswith("*LMS BOT*")
    say(bot, "1", "6")
    assert text(say(bot, "menu")).startswith("*LMS BOT*")


def test_anything_else_reprints_the_menu(bot, semester):
    say(bot, "menu", "1")
    out = text(say(bot, "what's due?"))
    assert out.startswith("*Assignments · all courses*") and out.endswith("Reply with one of the numbers below.")


def test_idle_chat_starts_again_at_the_main_menu(sessions, semester):
    clock = Clock(NOW)
    bot = Bot(sessions, DbKV(sessions), now=clock)
    say(bot, "menu", "1", "1")
    clock.at = NOW + timedelta(minutes=11)
    out = text(say(bot, "1"))  # read against the main menu, not the old list
    assert out.startswith("*Assignments · all courses*")


def test_list_numbers_stay_put_when_new_items_arrive(bot, semester, sessions):
    say(bot, "menu", "1", "1")
    with sessions.begin() as s:
        daa = s.scalar(select(Course).where(Course.name == "DAA"))
        s.add(Assignment(course=daa, lms_ref="155150:3", number="3", title="Greedy", deadline=NOW + timedelta(hours=1), fingerprint="x"))
    out = text(say(bot, "2"))
    assert out.startswith("Sorting")


def test_long_lists_are_paged(bot, semester, sessions):
    with sessions.begin() as s:
        daa = s.scalar(select(Course).where(Course.name == "DAA"))
        for n in range(10, 22):
            s.add(Assignment(course=daa, lms_ref=f"155150:{n}", number=str(n), title=f"Old {n}",
                             deadline=NOW - timedelta(days=n), fingerprint="x", deadline_passed=True))
    out = text(say(bot, "menu", "1", "9"))
    assert "11. More (7 left)" in out
    page2 = text(say(bot, "11"))
    assert page2.count(" · due ") == 7


def test_check_now_asks_the_worker(bot, semester, sessions):
    out = text(say(bot, "menu", "7"))
    assert out.startswith("Checking the LMS now")
    assert DbKV(sessions).get(POLL_NOW_KEY) is not None

    DbKV(sessions).set(POLL_PAUSE_KEY, "Assignments table columns changed")
    out = text(say(bot, "7"))
    assert "Polling is paused: Assignments table columns changed" in out


def test_status(bot, semester):
    out = text(say(bot, "menu", "8"))
    assert "*System status*" in out and "Last check: never" in out
    assert "Jobs: 1 active, 1 waiting for you" in out


def test_state_is_kept_per_chat(bot, semester, sessions):
    say(bot, "menu", "1")
    other = text(bot.handle("923111111111", "1"))
    assert other.startswith("*Assignments · all courses*")  # a new chat starts at the main menu
    with sessions() as s:
        assert {c.chat_id for c in s.scalars(select(ChatSession))} == {CHAT, "923111111111"}


def test_question_file_falls_back_to_the_download_record(bot, sessions, tmp_path):
    q = tmp_path / "old.pdf"
    q.write_bytes(b"%PDF old")
    with sessions.begin() as s:
        c = Course(code="1", name="OS", lms_ref="x", semester="MjAyNjM=", semester_name="Fall-2026")
        a = Assignment(course=c, lms_ref="1:1", number="1", title="Lab 1", deadline=NOW + timedelta(days=3), fingerprint="x")
        job = Job(assignment=a, revision=1, fingerprint="x", state=JobState.DOWNLOADED)
        s.add_all([c, a, job])
        s.flush()
        s.add(Artifact(job=job, kind="question", path=str(q), sha256="0" * 64, size=8))
    item = text(say(bot, "menu", "1", "1", "1"))
    assert "1. Send the question file" in item
    (reply,) = say(bot, "1")
    assert reply.file.read_bytes() == b"%PDF old"
