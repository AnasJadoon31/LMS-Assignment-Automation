import json
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
    """Two courses: open, due-soon, submitted, missed and marked work, four papers, one job waiting for you."""
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
        item(lab, "151704:P2", "Mid Term paper (60 Minutes)", NOW + timedelta(hours=21), kind="paper", term="Mid Term",
             starts_at=NOW + timedelta(hours=20), submission_hidden=True, paper_status="not_submitted")
        item(lab, "151704:P1", "Quiz paper", NOW - timedelta(days=3), kind="paper", term="Quiz",
             starts_at=NOW - timedelta(days=3, hours=1), deadline_passed=True, submission_hidden=True, marks="8/10",
             question_path=str(q), paper_status="submitted")
        item(lab, "151704:P3", "Retake paper", NOW - timedelta(days=10), kind="paper", term="Retake",
             starts_at=NOW - timedelta(days=10, hours=1), deadline_passed=True, submission_hidden=True,
             paper_status="missed")
        item(lab, "151704:P4", "Old paper", NOW - timedelta(days=20), kind="paper", term="Old",
             starts_at=NOW - timedelta(days=20, hours=1), deadline_passed=True, submission_hidden=True)
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
    assert "2. Lab papers: 1 upcoming, next Sun 11 Oct, 8:00 AM" in out
    assert "3. Waiting for your approval (1)" in out
    assert "4. Due or starting in the next 48 hours (2)" in out
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


def test_lab_paper_categories_lists_and_item(bot, semester):
    out = text(say(bot, "menu", "2"))
    assert out.startswith("*Lab papers · all labs*")
    for line in ["1. Upcoming (1)", "2. Open now (0)", "3. Starting in the next 48 hours (1)", "4. Finished (3)",
                 "5. Submitted (1)", "6. Missed (no submission) (1)", "7. Marked (1)", "8. All (4)"]:
        assert line in out
    assert "approval" not in out.lower()
    upcoming = text(say(bot, "1"))
    assert "1. Mid Term paper (60 Minutes) · CA Lab · Sun 11 Oct, 8:00 AM to 9:00 AM · starts in 20 h 0 min" in upcoming
    item = text(say(bot, "1"))
    assert "Sun 11 Oct, 8:00 AM to 9:00 AM (starts in 20 h 0 min)" in item
    assert "Status: not started" in item and "Lab papers are tracked only" in item
    # The LMS row colour: green submitted, red missed; a paper seen before colours were read is unknown
    finished = text(say(bot, "0", "0", "4"))
    assert "1. Quiz paper · CA Lab · Wed 7 Oct, 11:00 AM to 12:00 PM · 8/10" in finished
    assert "2. Retake paper · CA Lab · Wed 30 Sep, 11:00 AM to 12:00 PM · missed" in finished
    assert "3. Old paper · CA Lab · Sun 20 Sep, 11:00 AM to 12:00 PM · finished" in finished
    quiz = text(say(bot, "1"))
    assert "(ended)" in quiz and "Status: submitted" in quiz and "Marks: 8/10" in quiz
    assert "1. Send the paper" in quiz
    assert "Status: missed: the exam ended with no submission" in text(say(bot, "0", "2"))
    assert "Status: finished; the LMS does not show whether you submitted" in text(say(bot, "0", "3"))
    assert "Retake paper" in text(say(bot, "0", "0", "6")) and "Quiz paper" in text(say(bot, "0", "5"))


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


def test_a_typed_report_for_a_handwritten_sheet_can_be_approved(bot, semester, sessions):
    with sessions.begin() as s:
        s.scalar(select(Job)).summary = json.dumps({"status": "handwritten"})
    assert "3. Waiting for your approval (1)" in text(say(bot, "menu", "1"))
    item = text(say(bot, "3", "1"))
    assert "Service: solved; the sheet asks for handwritten work; waiting for your approval (revision 1)" in item
    assert "3. Approve and upload" in item
    confirm = text(say(bot, "3"))
    assert "Upload the solved PDF for Recurrence Relations to the LMS now?" in confirm
    assert "The sheet asks for handwritten work; this uploads the typed report as it is." in confirm
    assert text(say(bot, "1")).startswith("Approved: Recurrence Relations.")
    with sessions() as s:
        assert s.scalar(select(Job.state)) == "SUBMITTING"


def pick(replies, label):
    """The number the menu gives an option."""
    return next(line.split(".")[0] for line in text(replies).splitlines() if line.split(". ", 1)[-1].startswith(label))


def test_a_submission_on_an_open_assignment_can_be_deleted_after_a_yes(bot, semester, sessions):
    listed = say(bot, "menu", "1", "6")  # Assignments, Submitted
    item = say(bot, pick(listed, "Lab 6"))
    assert "Status: submitted; still open, you can delete it and resubmit" in text(item)
    confirm = text(say(bot, pick(item, "Delete my submission from the LMS")))
    assert "Delete your submission for Lab 6 from the LMS?" in confirm
    assert "The LMS shows l6.pdf. Nothing is uploaded in its place until you approve a report or upload one yourself." in confirm
    assert "1. Yes" in confirm and "2. No" in confirm

    assert text(say(bot, "2")).startswith("Cancelled.")
    with sessions() as s:
        assert s.scalar(select(Assignment).where(Assignment.title == "Lab 6")).delete_requested_at is None

    item = say(bot, "0", pick(listed, "Lab 6"))
    out = text(say(bot, pick(item, "Delete my submission from the LMS"), "1"))
    assert out.startswith("Deleting your submission for Lab 6 from the LMS now; a message will confirm it.")
    assert "Status: submitted; being deleted from the LMS, as you asked" in out
    assert "Delete my submission" not in out.split("---")[-1]  # asked once
    with sessions() as s:
        assert s.scalar(select(Assignment).where(Assignment.title == "Lab 6")).delete_requested_at is not None
        assert DbKV(sessions).get(POLL_NOW_KEY)

    closed = say(bot, "0", pick(listed, "Lab 5"))  # submitted and closed: the LMS offers no Delete
    assert "Delete my submission" not in text(closed)


def test_a_course_gets_its_own_system_prompt_from_the_chat(bot, semester, sessions):
    courses = say(bot, "menu", pick(say(bot, "menu"), "By course"))
    course = say(bot, pick(courses, "DAA"))
    assert "System prompt: built-in (change)" in text(course)
    screen = say(bot, pick(course, "System prompt"))
    assert "Now: built-in." in text(screen) and "Show my prompt" not in text(screen)

    asked = text(say(bot, pick(screen, "Add my instructions")))
    assert asked.startswith("Type your instructions for this course; they are added to the built-in prompt")
    out = text(say(bot, "Use C, never Python."))
    assert out.startswith("Saved for DAA. It is added to the built-in prompt, from the next solve or redo.")
    assert "Now: built-in plus your instructions." in out
    with sessions() as s:
        c = s.get(Course, semester["daa"])
        assert (c.prompt, c.prompt_replaces) == ("Use C, never Python.", False)

    screen = say(bot, "0", pick(course, "System prompt"))
    assert text(say(bot, pick(screen, "Show my prompt"))) == "Use C, never Python."
    say(bot, pick(screen, "Replace the built-in prompt"))
    assert "Now: yours, replacing the built-in one." in text(say(bot, "Only this."))
    with sessions() as s:
        c = s.get(Course, semester["daa"])
        assert (c.prompt, c.prompt_replaces) == ("Only this.", True)

    screen = say(bot, "0", pick(course, "System prompt"))
    assert text(say(bot, pick(screen, "Remove my prompt"))).startswith("Removed. DAA uses the built-in prompt only.")
    with sessions() as s:
        assert s.get(Course, semester["daa"]).prompt is None


def test_the_solver_model_is_picked_in_the_chat(bot, semester, sessions):
    menu = say(bot, "menu")
    assert "9. Solver model: default (change)" in text(menu)
    screen = say(bot, "9")
    assert "Now: default" in text(screen)
    for label in ("Fable 5.1", "Opus 5.5", "Opus 4.8", "Opus 4.5", "Sonnet 5.5", "Sonnet 4.5", "Haiku 4.5"):
        assert f". {label} (claude-" in text(screen), label
    out = text(say(bot, pick(screen, "Opus 4.8")))
    assert out.startswith("The solver now uses claude-opus-4-8, from the next solve or redo.") and "Now: claude-opus-4-8." in out
    assert DbKV(sessions).get("solver_model") == "claude-opus-4-8"
    say(bot, pick(screen, "Type a model name"))
    assert text(say(bot, "claude-opus-5-5")).startswith("The solver now uses claude-opus-5-5")
    say(bot, pick(screen, "Type a model name"))
    assert text(say(bot, "rm -rf /; x")).startswith("That does not look like a model name. Nothing changed.")
    assert DbKV(sessions).get("solver_model") == "claude-opus-5-5"
    assert text(say(bot, pick(screen, "Default"))).startswith("The solver uses the default model again.")
    assert DbKV(sessions).get("solver_model") is None


def test_effort_the_daily_limit_and_the_built_in_prompt_from_the_chat(bot, semester, sessions):
    kv = DbKV(sessions)
    menu = say(bot, "menu")
    assert "Solver effort: default (change)" in text(menu) and "Daily solve limit: on (change)" in text(menu)
    screen = say(bot, pick(menu, "Solver effort"))
    assert [l.split(". ")[1] for l in text(screen).splitlines() if l[:1].isdigit()] == ["Default", "low", "medium", "high", "xhigh", "max"]
    assert text(say(bot, pick(screen, "xhigh"))).startswith("The solver now uses xhigh effort")
    assert kv.get("solver_effort") == "xhigh"
    assert text(say(bot, pick(screen, "Default"))).startswith("The solver uses the default effort again.")
    assert kv.get("solver_effort") is None

    menu = say(bot, "menu")
    out = text(say(bot, pick(menu, "Daily solve limit"), "1"))
    assert out.startswith("The daily limit is lifted for today.") and "Now: lifted for today." in out
    assert kv.get("solver_cap_lifted") == "2026-10-10"
    assert text(say(bot, "1")).startswith("The daily limit is back on.") and kv.get("solver_cap_lifted") is None

    courses = say(bot, "menu", pick(say(bot, "menu"), "By course"))
    course = say(bot, pick(courses, "DAA"))
    screen = say(bot, pick(course, "System prompt"))
    shown = text(say(bot, pick(screen, "Show the built-in prompt")))
    assert shown.startswith("You solve a university assignment") and "anas@server" in shown


def test_the_web_page_password_is_reset_from_the_chat(bot, semester, sessions):
    from lms_worker import actions

    kv = DbKV(sessions)
    assert actions.password_matches(kv, "from-env", "from-env")
    menu = say(bot, "menu")
    confirm = text(say(bot, pick(menu, "Reset the web page password")))
    assert "Reset the web page password?" in confirm and "1. Yes" in confirm
    out = text(say(bot, "1"))
    new = out.splitlines()[1]
    assert out.startswith("The new web page password is:") and len(new) >= 12
    assert actions.password_matches(kv, "from-env", new)
    assert not actions.password_matches(kv, "from-env", "from-env")  # the variable no longer opens the page
    assert new not in kv.get("admin_password")  # only a hash is kept


def test_a_course_gets_its_own_report_theme_from_the_chat(bot, semester, sessions):
    courses = say(bot, "menu", pick(say(bot, "menu"), "By course"))
    course = say(bot, pick(courses, "DAA"))
    assert "Report theme: default (change)" in text(course)
    screen = say(bot, pick(course, "Report theme"))
    assert "Now: the default theme, https://claude.ai/artifact/" in text(screen) and "Use the default theme" not in text(screen)

    say(bot, pick(screen, "Set this course's theme link"))
    assert text(say(bot, "https://example.com/x")).startswith("That is not a Claude artifact link")
    say(bot, pick(screen, "Set this course's theme link"))
    out = text(say(bot, "https://claude.ai/artifact/AbC123xyz_-9?utm=1"))
    assert out.startswith("Saved. DAA reports use that theme") and "Now: https://claude.ai/artifact/AbC123xyz_-9" in out
    with sessions() as s:
        assert s.get(Course, semester["daa"]).theme == "https://claude.ai/artifact/AbC123xyz_-9"
        assert s.get(Course, semester["lab"]).theme is None  # the rest keep the default

    screen = say(bot, "0", pick(course, "Report theme"))
    assert text(say(bot, pick(screen, "Use the default theme"))).startswith("DAA uses the default theme again.")
    with sessions() as s:
        assert s.get(Course, semester["daa"]).theme is None


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
    assert "1. DAA (2 assignments remaining)" in out and "2. CA Lab (1 paper coming)" in out
    daa = text(say(bot, "1"))
    assert "Lab papers" not in daa and "2. Mode: approve (change)" in daa  # papers are for labs only
    course = text(say(bot, "0", "2"))
    assert "*CA Lab*" in course
    assert "1. Assignments: nothing remaining" in course
    assert "2. Lab papers: 1 upcoming, next Sun 11 Oct, 8:00 AM" in course
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
