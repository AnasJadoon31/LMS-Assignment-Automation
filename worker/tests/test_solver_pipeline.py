import dataclasses
import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from lms_worker import actions, files
from lms_worker.claude_login import LoginStatus
from lms_worker.db import Artifact, Assignment, Course, Event, Job, JobState
from lms_worker.kv import DbKV
from lms_worker.lms import models as lms
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.lms.errors import AlreadySubmitted, LMSError, SessionExpired
from lms_worker.notify import Outbox
from lms_worker.solver.claude import rate_limit
from lms_worker.solver.pipeline import CONTINUE, SIGNED_OUT_KEY, Solver, report_name
from lms_worker.solver.sandbox import SandboxRun
from lms_worker.solver.usage import UsageGuard
from lms_worker.watcher import POLL_NOW_KEY, Watcher

from .test_notify import Flaky
from .test_watcher import FakeLMS

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=LMS_TZ)
DUE = datetime(2026, 10, 11, 23, 55, tzinfo=LMS_TZ)
SUMMARY = {
    "status": "done", "kind": "assignment", "number": 1, "title": "Recurrence Relations", "course_code": "CSC-321",
    "course_name": "Design and Analysis of Algorithms",
    "exercises": [{"title": "Q1", "type": "theory", "result": "T(n) = Θ(n log n)"},
                  {"title": "Q2", "type": "theory", "result": "T(n) = Θ(n²)"}],
    "pages": 7, "checked": ["every page against the theme"],
}


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class FakeSandbox:
    """Plays a Claude run: writes the report files and feeds stream-json lines."""

    def __init__(self):
        self.runs = []
        self.script = None

    def work_path(self, work):
        return "/work"

    def run(self, work, env, on_line, timeout):
        self.runs.append({"job": json.loads((work / "job.json").read_text()), "env": env,
                          "files": sorted(p.name for p in (work / "in").iterdir() if p.is_file())})
        return self.script(work, on_line)


def stream(summary=None, *, session="sess-1", rate=None, error=None):
    lines = [{"type": "system", "subtype": "init", "session_id": session}]
    if rate:
        lines.append({"type": "rate_limit_event", "session_id": session, "rate_limit_info": rate})
    lines.append({"type": "assistant", "session_id": session, "message": {}})
    if error is not None:
        lines.append({"type": "result", "subtype": "success", "is_error": True, "result": error, "session_id": session})
    elif summary is not None:
        lines.append({"type": "result", "subtype": "success", "is_error": False, "num_turns": 14, "session_id": session,
                      "total_cost_usd": 1.5, "usage": {"input_tokens": 1000, "output_tokens": 400},
                      "structured_output": summary, "result": ""})
    return [json.dumps(x) for x in lines]


def play(lines, *, files=True):
    def script(work, on_line):
        if files:
            (work / "out" / "report.pdf").write_bytes(b"%PDF report")
            (work / "out" / "report.docx").write_bytes(b"PK report")
        for line in lines:
            if not on_line(line):
                return SandboxRun(exit_code=-9, stopped=True)
        return SandboxRun(exit_code=0)
    return script


def rate(status="allowed", util=0.3, overage=False, resets=None):
    resets = resets or int((NOW + timedelta(hours=2)).timestamp())
    return {"status": status, "resetsAt": resets, "rateLimitType": "five_hour", "isUsingOverage": overage,
            "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": resets}}}


class FakeDrive:
    def __init__(self, fail=None):
        self.saved = []
        self.fail = fail

    def save(self, folders, files):
        if self.fail:
            raise RuntimeError(self.fail)
        self.saved.append((folders, [f.name for f in files]))
        return "https://drive.google.com/drive/folders/abc"


class FakeLogin:
    def __init__(self, logged_in=True):
        self.logged_in = logged_in
        self.stamp_value = "1"

    def status(self):
        return LoginStatus(self.logged_in, method="claude.ai", email="student@example.com" if self.logged_in else None)

    def stamp(self):
        return self.stamp_value


@pytest.fixture
def channel():
    return Flaky()


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def sandbox():
    return FakeSandbox()


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
def solver(sessions, channel, sandbox, drive, clock, tmp_path):
    return Solver(sessions, DbKV(sessions), Outbox({"whatsapp": channel}), sandbox, tmp_path / "data", drive=drive,
                  now=clock, public_url="https://lms.example.com", env={"HTTPS_PROXY": "http://proxy.example:3128"},
                  route=False)  # routing has its own tests below


def add_job(sessions, tmp_path, *, mode="approve", title="Recurrence Relations", deadline=DUE, kind="assignment",
            state=JobState.DOWNLOADED, ref="155150:1", fingerprint="x"):
    q = tmp_path / f"q-{ref.replace(':', '-')}" / "DAA-Assignment-01.pdf"
    q.parent.mkdir(parents=True)
    q.write_bytes(b"%PDF question")
    with sessions.begin() as s:
        course = s.scalar(select(Course).where(Course.code == "155150"))
        if course is None:
            course = Course(code="155150", name="Design & Analysis of Algorithms", lms_ref="MTU1MTUw", semester="MjAyNjM=",
                            semester_name="Fall-2026", mode=mode)
            s.add(course)
        a = Assignment(course=course, lms_ref=ref, number=ref.split(":")[1], title=title, deadline=deadline,
                       fingerprint=fingerprint, attachment_name=q.name, kind=kind, question_path=str(q))
        job = Job(assignment=a, revision=1, fingerprint=fingerprint, state=state)
        s.add_all([a, job])
        s.flush()
        s.add(Artifact(job=job, kind="question", path=str(q), sha256="0" * 64, size=13))
        return job.id


def state(sessions, job_id):
    with sessions() as s:
        return s.get(Job, job_id).state


def test_a_solved_assignment_comes_to_you_for_approval(solver, sandbox, drive, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY, rate=rate()))

    r = solver.run_once()

    assert (r.status, state(sessions, job_id)) == ("solved", "AWAITING_APPROVAL")
    run = sandbox.runs[0]
    assert run["files"] == ["DAA-Assignment-01.pdf"] and run["env"] == {"HTTPS_PROXY": "http://proxy.example:3128"}
    assert "Assignment on the LMS: Recurrence Relations (number 1)" in run["job"]["prompt"]
    assert "resume" not in run["job"] and run["job"]["schema"]["required"][0] == "status"
    assert "The work folder is /work" in run["job"]["system"]
    with sessions() as s:
        job = s.get(Job, job_id)
        arts = {a.kind: Path(a.path).name for a in job.artifacts}
        assert arts == {"question": "DAA-Assignment-01.pdf", "docx": "Assignment 01 - Recurrence Relations.docx",
                        "pdf": "Assignment 01 - Recurrence Relations.pdf"}
        assert (job.claude_session, job.cost_tokens, job.cost_usd, job.attempts) == ("sess-1", 1400, 1.5, 0)
        assert json.loads(job.summary)["pages"] == 7
    assert drive.saved == [(["University Assignments", "Fall-2026", "Design & Analysis of Algorithms",
                             "Assignment 01 - Recurrence Relations"],
                            ["DAA-Assignment-01.pdf", "Assignment 01 - Recurrence Relations.docx",
                             "Assignment 01 - Recurrence Relations.pdf"])]
    text, file = channel.sent[-1]
    assert file == "Assignment 01 - Recurrence Relations.pdf"
    assert text.startswith("Solved: Assignment 01 - Recurrence Relations\nDesign & Analysis of Algorithms\nDue Sun 11 Oct, 11:55 PM")
    assert "• Q1: T(n) = Θ(n log n)" in text
    assert "Saved to Google Drive: University Assignments › Fall-2026 › Design & Analysis of Algorithms › Assignment 01 - Recurrence Relations" in text
    assert text.endswith("To upload it, reply menu, then Waiting for your approval.")
    assert UsageGuard(DbKV(sessions)).runs_today(NOW) == 1
    assert solver.run_once().status == "idle"


DAA = lms.Course(code="155150", name="Design & Analysis of Algorithms", lms_ref="MTU1MTUw", semester="MjAyNjM=",
                 semester_name="Fall-2026")
LISTED = lms.AssignmentMeta(course=DAA, lms_ref="155150:1", number="1", title="Recurrence Relations", deadline=DUE,
                            attachment_name="DAA-Assignment-01.pdf")


def test_auto_mode_uploads_through_the_poller(solver, sandbox, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path, mode="auto", fingerprint=LISTED.fingerprint())
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    assert state(sessions, job_id) == "SUBMITTING"
    assert DbKV(sessions).get(POLL_NOW_KEY) is not None
    assert "Auto mode: uploading it now" in channel.sent[-1][0]

    class Uploading(FakeLMS):
        def submit(self, a, file):
            shot = tmp_path / "receipt.png"
            shot.write_bytes(b"png")
            self.calls.append(f"submit:{a.lms_ref}:{file.name}")
            return lms.Receipt(filename="Assignment-01.pdf", submitted_at=NOW, screenshot=shot)

    fake = Uploading()
    fake.set(DAA, LISTED)
    watcher = Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW))
    assert watcher.poll_once().uploaded == 1
    assert "submit:155150:1:Assignment 01 - Recurrence Relations.pdf" in fake.calls
    with sessions() as s:
        job = s.get(Job, job_id)
        assert (job.state, job.lms_submission_file) == ("CONFIRMED", "Assignment-01.pdf")
    assert channel.sent[-1] == ("Uploaded: Design & Analysis of Algorithms · Recurrence Relations\nThe LMS shows Assignment-01.pdf.",
                                "receipt.png")


def test_without_an_upload_the_pdf_comes_back_to_upload_by_hand(sessions, channel, tmp_path):
    job_id = add_job(sessions, tmp_path, state=JobState.SUBMITTING, fingerprint=LISTED.fingerprint())
    pdf = tmp_path / "Assignment 01 - Recurrence Relations.pdf"
    pdf.write_bytes(b"%PDF")
    with sessions.begin() as s:
        s.add(Artifact(job_id=job_id, kind="pdf", path=str(pdf), sha256="0" * 64, size=4))

    class Unmapped(FakeLMS):
        def submit(self, a, file):
            raise NotImplementedError("no upload")

    fake = Unmapped()
    fake.set(DAA, LISTED)
    Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW)).poll_once()

    assert state(sessions, job_id) == "NEEDS_HUMAN"
    text, file = channel.sent[-1]
    assert text.startswith("Upload this yourself: Design & Analysis of Algorithms · Recurrence Relations\nDue Sun 11 Oct, 11:55 PM")
    assert file == pdf.name


@pytest.mark.parametrize("replaces", [False, True])
def test_a_course_prompt_is_added_to_or_replaces_the_built_in_one(solver, sandbox, sessions, tmp_path, replaces):
    job_id = add_job(sessions, tmp_path)
    with sessions.begin() as s:
        course = s.get(Job, job_id).assignment.course
        course.prompt, course.prompt_replaces = "Execute every example too.", replaces
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    system = sandbox.runs[0]["job"]["system"]
    if replaces:
        assert system.startswith("Execute every example too.\n\nDo not ask the student anything")
        assert "You solve a university assignment" not in system
    else:
        assert system.startswith("You solve a university assignment") and "The work folder is /work" in system
        assert "The student's own instructions for this course; follow them too:\nExecute every example too." in system
        assert "Execute every example in the lab sheet as well" in system  # the built-in OS Lab rule


def test_a_course_with_its_own_theme_is_solved_in_it_and_the_rest_use_the_default(solver, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    assert "design system artifact https://claude.ai/artifact/F2Je3LZvKN92ysXVL7jM2R." in sandbox.runs[0]["job"]["system"]

    with sessions.begin() as s:
        job = s.get(Job, job_id)
        job.assignment.course.theme = "https://claude.ai/artifact/OtherTheme42"
        actions.move(s, job, JobState.DOWNLOADED, "again")
    solver.run_once()
    system = sandbox.runs[1]["job"]["system"]
    assert "design system artifact https://claude.ai/artifact/OtherTheme42." in system and "F2Je3LZvKN92ysXVL7jM2R" not in system


def test_the_model_picked_in_the_chat_is_the_one_the_run_uses(solver, sandbox, sessions, tmp_path):
    add_job(sessions, tmp_path)
    DbKV(sessions).set("solver_model", "sonnet")
    DbKV(sessions).set("solver_effort", "xhigh")
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    assert (sandbox.runs[0]["job"]["model"], sandbox.runs[0]["job"]["effort"]) == ("sonnet", "xhigh")


def test_the_daily_limit_can_be_lifted_for_today():
    from lms_worker.kv import FileKV
    import tempfile

    kv = FileKV(Path(tempfile.mkdtemp()))
    guard = UsageGuard(kv, daily_cap=1)
    guard.count_run(NOW)
    assert guard.paused(NOW).reason == "daily cap"
    kv.set("solver_cap_lifted", NOW.astimezone(LMS_TZ).date().isoformat())
    assert guard.paused(NOW) is None
    assert guard.paused(NOW + timedelta(days=1)) is None  # a new day: the count starts again
    guard.count_run(NOW + timedelta(days=1))
    assert guard.paused(NOW + timedelta(days=1)).reason == "daily cap"  # and yesterday's lift is over


def test_handwritten_work_waits_for_approval_even_in_auto_mode(solver, sandbox, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path, mode="auto")
    sandbox.script = play(stream({**SUMMARY, "status": "handwritten"}))

    assert solver.run_once().status == "handwritten"
    assert state(sessions, job_id) == "AWAITING_APPROVAL"  # not SUBMITTING: a typed report is never uploaded unasked
    assert DbKV(sessions).get(POLL_NOW_KEY) is None
    text, file = channel.sent[-1]
    assert text.startswith("Solved, handwritten: Assignment 01 - Recurrence Relations")
    assert text.endswith("The sheet asks for handwritten work, so it was not uploaded: copy this by hand. "
                         "To upload this report as it is, reply menu, then Waiting for your approval.")
    assert file.endswith(".pdf")
    with sessions.begin() as s:
        job = s.get(Job, job_id)
        assert actions.handwritten(job) and actions.job_words(job) == actions.HANDWRITTEN_WORDS
        done, message = actions.approve(s, job, DbKV(sessions), NOW, "WhatsApp")
        assert done and message.startswith("Approved: Recurrence Relations.") and job.state == "SUBMITTING"


def test_older_handwritten_jobs_that_can_still_be_uploaded_become_approvable(engine, sessions, tmp_path):
    waiting = add_job(sessions, tmp_path, state=JobState.HANDWRITTEN)
    closed = add_job(sessions, tmp_path, state=JobState.HANDWRITTEN, ref="155150:2")
    submitted = add_job(sessions, tmp_path, state=JobState.HANDWRITTEN, ref="155150:3")
    with sessions.begin() as s:
        s.get(Job, closed).assignment.deadline_passed = True
        s.get(Job, submitted).assignment.submitted_file = "mine.pdf"

    migration = importlib.import_module("lms_worker.db.migrations.versions.0006_handwritten_waits_for_approval")
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()

    assert [state(sessions, j) for j in (waiting, closed, submitted)] == ["AWAITING_APPROVAL", "HANDWRITTEN", "HANDWRITTEN"]
    with sessions() as s:
        moved = s.scalars(select(Event).where(Event.to_state == "AWAITING_APPROVAL")).all()
        assert [(e.job_id, e.from_state, e.detail) for e in moved] == [
            (waiting, "HANDWRITTEN", "handwritten work now waits for your approval")]


def test_blocked_work_stops_with_claudes_reason(solver, sandbox, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream({**SUMMARY, "status": "blocked", "blocked_reason": "It needs an Arduino board."}), files=False)

    assert solver.run_once().status == "blocked"
    with sessions() as s:
        assert (s.get(Job, job_id).state, s.get(Job, job_id).last_error) == ("NEEDS_HUMAN", "It needs an Arduino board.")
    assert channel.sent[-1][0] == ("Can't be solved here: Design & Analysis of Algorithms · Recurrence Relations\n"
                                   "It needs an Arduino board.\nThe question file is in the menu; this one is yours to do.")


def test_the_usage_limit_pauses_then_the_same_session_resumes(solver, sandbox, channel, sessions, clock, tmp_path):
    job_id = add_job(sessions, tmp_path)
    resets = int((NOW + timedelta(hours=2)).timestamp())
    sandbox.script = play(stream(error="You've hit your limit · resets 2pm", rate=rate("rejected", 1.0, resets=resets)),
                          files=False)

    assert solver.run_once().status == "retry"
    with sessions() as s:
        job = s.get(Job, job_id)
        assert (job.state, job.attempts, job.claude_session) == ("DOWNLOADED", 0, "sess-1")
        assert job.not_before == NOW + timedelta(hours=2, minutes=3)
    assert channel.sent[-1][0].startswith("Solving paused: the Claude Pro usage limit is reached. It resumes Wed 7 Oct, 2:03 PM.")
    assert solver.run_once().status == "idle"  # held until the reset

    clock.at = NOW + timedelta(hours=2, minutes=4)
    sandbox.script = play(stream(SUMMARY))
    assert solver.run_once().status == "solved"
    assert sandbox.runs[-1]["job"]["resume"] == "sess-1" and sandbox.runs[-1]["job"]["prompt"] == CONTINUE
    assert state(sessions, job_id) == "AWAITING_APPROVAL"


def test_paid_extra_usage_stops_the_run_at_once(solver, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY, rate=rate("allowed", 0.5, overage=True)), files=False)

    assert solver.run_once().status == "retry"
    assert state(sessions, job_id) == "DOWNLOADED"
    assert UsageGuard(DbKV(sessions)).paused(NOW).reason == "extra usage"


def test_a_failed_run_is_retried_once_then_left_to_you(solver, sandbox, channel, sessions, clock, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = lambda work, on_line: SandboxRun(exit_code=1, timed_out=True)

    assert solver.run_once().status == "retry"
    assert solver.run_once().status == "idle"  # waits RETRY_AFTER
    clock.at = NOW + timedelta(minutes=11)
    assert solver.run_once().status == "failed"
    with sessions() as s:
        assert (s.get(Job, job_id).state, s.get(Job, job_id).attempts) == ("NEEDS_HUMAN", 2)
    assert channel.sent[-1][0].startswith("Solving failed: Design & Analysis of Algorithms · Recurrence Relations\nit ran past 90 minutes")


def test_signed_out_waits_and_links_the_setup_page_once(solver, sandbox, channel, sessions, tmp_path):
    login = FakeLogin(logged_in=False)
    solver.login = login
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY))

    assert solver.run_once().status == "signed_out"
    assert solver.run_once().status == "signed_out"
    alerts = [m for m, _ in channel.sent if "signed out" in m]
    assert alerts == ["Claude is signed out, so solving is waiting.\nSign in again at https://lms.example.com/setup: "
                      "Sign in to Claude. Solving resumes by itself."]
    assert not sandbox.runs

    login.logged_in, login.stamp_value = True, "2"  # signed in on the setup page
    assert solver.run_once().status == "solved"
    assert DbKV(sessions).get(SIGNED_OUT_KEY) is None
    assert "Claude is signed in again as student@example.com. Solving resumes." in [m for m, _ in channel.sent]
    assert state(sessions, job_id) == "AWAITING_APPROVAL"


def test_a_run_that_finds_claude_signed_out_does_not_count(solver, sandbox, channel, sessions, tmp_path):
    solver.login = FakeLogin(logged_in=True)
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream(error="Invalid API key · Please run /login"), files=False)

    assert solver.run_once().status == "signed_out"
    with sessions() as s:
        assert (s.get(Job, job_id).state, s.get(Job, job_id).attempts) == ("DOWNLOADED", 0)
    assert solver.run_once().status == "signed_out"  # status still says signed in, but nothing changed since
    assert len(sandbox.runs) == 1


def test_only_open_assignments_are_solved_until_the_end_of_the_deadlines_day(solver, sandbox, sessions, tmp_path):
    add_job(sessions, tmp_path, kind="paper", ref="155150:P1", title="Mid Term paper")
    add_job(sessions, tmp_path, ref="155150:2", title="Yesterday", deadline=NOW - timedelta(days=1))
    waiting = add_job(sessions, tmp_path, ref="155150:4", title="Manual")
    with sessions.begin() as s:
        s.get(Job, waiting).state = JobState.WAITING_GO  # manual solving: not before your go-ahead
    assert solver.run_once().status == "idle" and not sandbox.runs
    # The posted time passed a minute ago, but the LMS takes uploads until 11:59 pm that day
    late = add_job(sessions, tmp_path, ref="155150:3", title="Sorting", deadline=NOW - timedelta(minutes=1))
    sandbox.script = play(stream(SUMMARY))
    assert solver.run_once().status == "solved" and state(sessions, late) == "AWAITING_APPROVAL"


def test_a_redo_starts_fresh_when_the_prompt_or_theme_changed(solver, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    with sessions.begin() as s:
        actions.redo(s, s.get(Job, job_id), "tighter margins", "WhatsApp")
    solver.run_once()
    same = sandbox.runs[1]["job"]
    assert same["resume"] == "sess-1" and same["prompt"].startswith("The student asked for changes: tighter margins")

    with sessions.begin() as s:
        job = s.get(Job, job_id)
        job.assignment.course.theme = "https://claude.ai/artifact/NewTheme42"
        job.assignment.course.prompt = "Use C."
        actions.redo(s, job, "new theme please", "WhatsApp")
    solver.run_once()
    fresh = sandbox.runs[2]["job"]
    assert "resume" not in fresh  # a resumed session would keep working from the old theme
    assert "NewTheme42" in fresh["system"] and "Use C." in fresh["system"]
    assert "The student asked for a redo with this note: new theme please" in fresh["prompt"]
    with sessions() as s:
        assert "a fresh run: the course's prompt, theme or files changed" in s.scalars(
            select(Event.detail).where(Event.job_id == job_id, Event.to_state == "SOLVING").order_by(Event.id.desc())).first()


def test_auto_solving_lets_claude_ask_first_and_your_answer_resumes_it(solver, sandbox, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    with sessions.begin() as s:
        a = s.get(Job, job_id).assignment
        a.course.solve = "auto"
        files.keep(files.refs_dir(solver.data_dir, a), "notes.pdf", b"%PDF notes")
        files.keep(files.theme_dir(solver.data_dir, a.course), "format.docx", b"PK")
    asked = {**SUMMARY, "status": "needs_input", "questions": ["Which dataset should Q2 use?"]}
    sandbox.script = play(stream(asked), files=False)
    assert solver.run_once().status == "asked" and state(sessions, job_id) == "NEEDS_INPUT"
    first = sandbox.runs[0]["job"]["system"]
    assert "Before you solve, decide whether you can do all of it well on your own" in first
    assert "/work/in/theme-reference: format.docx" in first and "/work/in/reference: notes.pdf" in first
    assert channel.sent[-1][0].startswith("Claude needs something before solving: Design & Analysis of Algorithms · Recurrence Relations\n"
                                          "1. Which dataset should Q2 use?")

    with sessions.begin() as s:
        actions.answer(s, s.get(Job, job_id), "Use iris.", "the chat")
    sandbox.script = play(stream(SUMMARY))
    assert solver.run_once().status == "solved"
    again = sandbox.runs[1]["job"]
    assert again["resume"] == "sess-1" and again["prompt"].startswith("The student answered: Use iris.")
    with sessions() as s:
        job = s.get(Job, job_id)
        work = solver.job_dir(job.assignment, job) / "work" / "in"
    assert (work / "reference" / "notes.pdf").read_bytes() == b"%PDF notes" and (work / "theme-reference" / "format.docx").is_file()


def test_bypass_solving_tells_claude_never_to_ask(solver, sandbox, sessions, tmp_path):
    add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    system = sandbox.runs[0]["job"]["system"]
    assert "Do not ask the student anything" in system and "Before you solve, decide" not in system


def test_nearest_deadline_first_and_a_deadline_warning_while_paused(solver, sandbox, channel, sessions, tmp_path):
    later = add_job(sessions, tmp_path, ref="155150:2", title="Sorting", deadline=DUE + timedelta(days=3))
    soon = add_job(sessions, tmp_path, ref="155150:1", title="Recurrence Relations", deadline=NOW + timedelta(hours=4))
    guard = UsageGuard(DbKV(sessions))
    guard.record(rate_limit(rate("allowed", 0.85, resets=int((NOW + timedelta(hours=3)).timestamp()))), NOW)

    assert solver.run_once().status == "paused"
    solver.run_once()
    warnings = [m for m, _ in channel.sent if m.startswith("Heads-up")]
    assert len(warnings) == 1 and warnings[0].startswith(
        "Heads-up: Design & Analysis of Algorithms · Recurrence Relations is due Wed 7 Oct, 4:00 PM.")

    DbKV(sessions).delete("claude_paused")
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    assert (state(sessions, soon), state(sessions, later)) == ("AWAITING_APPROVAL", "DOWNLOADED")


def test_a_job_stopped_during_the_run_stays_stopped(solver, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)

    def script(work, on_line):
        with sessions.begin() as s:  # the poller sees your own submission meanwhile
            s.get(Job, job_id).state = JobState.NEEDS_HUMAN
        return play(stream(SUMMARY))(work, on_line)

    sandbox.script = script
    assert solver.run_once().status == "stopped"
    assert state(sessions, job_id) == "NEEDS_HUMAN"


def test_a_restart_requeues_a_job_left_solving(solver, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path, state=JobState.SOLVING)
    assert solver.recover() == 1
    assert state(sessions, job_id) == "DOWNLOADED"


def test_a_drive_failure_does_not_hold_up_the_report(solver, sandbox, channel, sessions, tmp_path):
    solver.drive = FakeDrive(fail="invalid_grant")
    add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY))
    assert solver.run_once().status == "solved"
    assert "Not saved to Google Drive: invalid_grant" in channel.sent[-1][0]


def test_report_names():
    a = Assignment(number="3", title="Lab 3: Pipelining")
    assert report_name({"kind": "lab", "number": 5, "title": "Pipelining / Hazards"}, a) == "Lab 05 - Pipelining Hazards"
    assert report_name({"kind": "assignment", "title": ""}, a) == "Assignment 03 - Lab 3 Pipelining"


@pytest.mark.parametrize("attempts, expected", [(1, "CONFIRMED"), (0, "NEEDS_HUMAN")])
def test_a_submission_already_there_counts_as_ours_only_after_our_own_try(sessions, channel, tmp_path, attempts, expected):
    job_id = add_job(sessions, tmp_path, state=JobState.SUBMITTING, fingerprint=LISTED.fingerprint())
    pdf = tmp_path / "Assignment 01 - Recurrence Relations.pdf"
    pdf.write_bytes(b"%PDF")
    with sessions.begin() as s:
        s.add(Artifact(job_id=job_id, kind="pdf", path=str(pdf), sha256="0" * 64, size=4))
        s.get(Job, job_id).attempts = attempts  # 1: an earlier upload timed out before the LMS was read back

    class Lagging(FakeLMS):
        def submit(self, a, file):
            raise AlreadySubmitted("Assignment_01.pdf")

    fake = Lagging()
    fake.set(DAA, LISTED)
    Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW)).poll_once()
    with sessions() as s:
        job = s.get(Job, job_id)
        assert job.state == expected
        assert job.lms_submission_file == ("Assignment_01.pdf" if expected == "CONFIRMED" else None)


UPLOADED = "Assignment_01.pdf"


def uploaded_job(sessions, tmp_path, *, state=JobState.CONFIRMED):
    """A job whose report the service uploaded; the LMS shows the file."""
    job_id = add_job(sessions, tmp_path, state=state, fingerprint=LISTED.fingerprint())
    with sessions.begin() as s:
        job = s.get(Job, job_id)
        job.lms_submission_file = UPLOADED if state == JobState.CONFIRMED else None
        job.assignment.submitted_file = UPLOADED
    return job_id


def ask_to_delete(sessions, job_id):
    with sessions.begin() as s:
        return actions.delete_submission(s, s.get(Job, job_id).assignment, DbKV(sessions), NOW, "WhatsApp")


def test_a_submission_you_ask_to_delete_is_deleted_and_the_report_waits_again(sessions, channel, tmp_path):
    job_id = uploaded_job(sessions, tmp_path)
    done, message = ask_to_delete(sessions, job_id)
    assert done and message == "Deleting your submission for Recurrence Relations from the LMS now; a message will confirm it."
    assert DbKV(sessions).get(POLL_NOW_KEY)  # the worker is asked to poll at once
    assert ask_to_delete(sessions, job_id) == (False, "Nothing changed: the submission for Recurrence Relations is already being deleted.")

    class Deleting(FakeLMS):
        def delete_submission(self, a):
            self.calls.append(f"delete:{a.lms_ref}")
            self.listing[DAA.lms_ref] = [LISTED]  # the row is open again, nothing submitted
            return UPLOADED

    fake = Deleting()
    fake.set(DAA, dataclasses.replace(LISTED, submitted_file=UPLOADED))
    watcher = Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW))
    assert watcher.poll_once().deleted == 1
    assert fake.calls.count("delete:155150:1") == 1
    with sessions() as s:
        job = s.get(Job, job_id)
        a = job.assignment
        assert (job.state, job.lms_submission_file) == ("AWAITING_APPROVAL", None)
        assert (a.submitted_file, a.delete_requested_at) == (None, None)
        assert s.scalars(select(Event.detail).where(Event.job_id == job_id).order_by(Event.id)).all()[-2:] == [
            "delete of the LMS submission requested on WhatsApp: Recurrence Relations",
            "Assignment_01.pdf deleted from the LMS at your request; waiting for your approval again"]
    assert channel.sent[-1][0] == (
        "Deleted from the LMS: Design & Analysis of Algorithms · Recurrence Relations\n"
        "Assignment_01.pdf is no longer submitted.\n"
        "The solved report waits for your approval again: reply menu, then Waiting for your approval.")

    assert watcher.poll_once().deleted == 0  # asked once, done once
    assert fake.calls.count("delete:155150:1") == 1
    assert state(sessions, job_id) == "AWAITING_APPROVAL"


def test_a_report_that_stopped_on_your_old_submission_can_go_up_once_you_delete_it(sessions, channel, tmp_path):
    job_id = uploaded_job(sessions, tmp_path, state=JobState.NEEDS_HUMAN)
    pdf = tmp_path / "Assignment 01 - Recurrence Relations.pdf"
    pdf.write_bytes(b"%PDF solved")
    with sessions.begin() as s:
        job = s.get(Job, job_id)
        job.last_error = "The LMS already shows a submission; the service never replaces one"
        s.add(Artifact(job_id=job_id, kind="pdf", path=str(pdf), sha256="0" * 64, size=11))
    ask_to_delete(sessions, job_id)

    class Deleting(FakeLMS):
        def delete_submission(self, a):
            self.listing[DAA.lms_ref] = [LISTED]
            return UPLOADED

    fake = Deleting()
    fake.set(DAA, dataclasses.replace(LISTED, submitted_file=UPLOADED))
    Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW)).poll_once()
    with sessions() as s:
        job = s.get(Job, job_id)
        assert (job.state, job.last_error) == ("AWAITING_APPROVAL", None)
    assert channel.sent[-1][0].endswith("The solved report waits for your approval again: reply menu, then Waiting for your approval.")


def test_a_submission_deleted_on_the_lms_itself_puts_the_report_back_for_approval(sessions, channel, tmp_path):
    job_id = uploaded_job(sessions, tmp_path)
    fake = FakeLMS()
    fake.set(DAA, dataclasses.replace(LISTED, submitted_file=UPLOADED))
    watcher = Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW))
    watcher.poll_once()
    assert state(sessions, job_id) == "CONFIRMED"  # still on the LMS: nothing changes

    fake.set(DAA, LISTED)  # you deleted it on the LMS website
    watcher.poll_once()
    with sessions() as s:
        job = s.get(Job, job_id)
        assert (job.state, job.lms_submission_file) == ("AWAITING_APPROVAL", None)
    assert channel.sent[-1][0].startswith("No longer submitted: Design & Analysis of Algorithms · Recurrence Relations")
    sent = len(channel.sent)
    watcher.poll_once()
    assert len(channel.sent) == sent and state(sessions, job_id) == "AWAITING_APPROVAL"  # said once


def test_a_delete_the_lms_refuses_is_reported_and_not_repeated(sessions, channel, tmp_path):
    job_id = uploaded_job(sessions, tmp_path)
    ask_to_delete(sessions, job_id)

    class Refusing(FakeLMS):
        def delete_submission(self, a):
            self.calls.append("delete")
            raise LMSError("The LMS still shows the submission after the delete")

    fake = Refusing()
    fake.set(DAA, dataclasses.replace(LISTED, submitted_file=UPLOADED))
    watcher = Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW))
    assert watcher.poll_once().deleted == 0 and watcher.poll_once().deleted == 0
    assert fake.calls.count("delete") == 1
    with sessions() as s:
        job = s.get(Job, job_id)
        assert (job.state, job.lms_submission_file, job.assignment.submitted_file) == ("CONFIRMED", UPLOADED, UPLOADED)
        assert job.assignment.delete_requested_at is None
    assert channel.sent[-1][0] == ("Not deleted: Design & Analysis of Algorithms · Recurrence Relations\n"
                                   "The LMS still shows the submission after the delete")


def test_delete_is_offered_only_for_an_open_assignment_the_lms_shows_a_file_for(sessions, tmp_path):
    job_id = add_job(sessions, tmp_path, state=JobState.AWAITING_APPROVAL)
    nothing = "Nothing changed: Recurrence Relations has no submission on the LMS that can be deleted now."
    assert ask_to_delete(sessions, job_id) == (False, nothing)  # nothing submitted
    with sessions.begin() as s:
        a = s.get(Job, job_id).assignment
        a.submitted_file, a.deadline_passed = UPLOADED, True
    assert ask_to_delete(sessions, job_id) == (False, nothing)  # closed: the LMS no longer offers Delete
    with sessions() as s:
        assert s.get(Job, job_id).assignment.delete_requested_at is None


def test_a_session_that_ends_keeps_the_delete_for_the_next_poll(sessions, channel, tmp_path):
    job_id = uploaded_job(sessions, tmp_path)
    ask_to_delete(sessions, job_id)

    class Expired(FakeLMS):
        def delete_submission(self, a):
            raise SessionExpired("the session ended")

    fake = Expired()
    fake.set(DAA, dataclasses.replace(LISTED, submitted_file=UPLOADED))
    result = Watcher(fake, sessions, DbKV(sessions), Outbox({"whatsapp": channel}), tmp_path / "data", now=Clock(NOW)).poll_once()
    assert result.status == "session"
    with sessions() as s:
        assert s.get(Job, job_id).assignment.delete_requested_at is not None
    assert state(sessions, job_id) == "CONFIRMED"


def test_a_redo_after_a_blocked_run_starts_a_new_session(solver, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream({**SUMMARY, "status": "blocked", "blocked_reason": "No Artifact tool."}), files=False)
    assert solver.run_once().status == "blocked"
    with sessions.begin() as s:
        from lms_worker import actions, files
        assert actions.redo(s, s.get(Job, job_id), "Try again", "the web page")[0]

    sandbox.script = play(stream(SUMMARY, session="sess-2"))
    assert solver.run_once().status == "solved"
    job = sandbox.runs[-1]["job"]
    assert "resume" not in job and "The student asked for a redo with this note: Try again" in job["prompt"]


# --- routing: Haiku looks first ---

TRIAGE = {"model": "sonnet", "effort": "medium", "reason": "routine recurrences", "handwritten": False, "lectures": [], "questions": []}


def routed_script(answers, summary=SUMMARY):
    """Haiku runs get the next of answers (a dict, or None for a failed run); other runs solve."""
    answers = list(answers)

    def script(work, on_line):
        job = json.loads((work / "job.json").read_text())
        if job.get("model") == "haiku":
            answer = answers.pop(0)
            lines = stream(answer, session="triage") if answer else stream(error="API Error: overloaded", session="triage")
            return play(lines, files=False)(work, on_line)
        return play(stream(summary))(work, on_line)
    return script


@pytest.fixture
def router(sessions, channel, sandbox, drive, clock, tmp_path):
    return Solver(sessions, DbKV(sessions), Outbox({"whatsapp": channel}), sandbox, tmp_path / "data", drive=drive, now=clock)


def details(sessions, job_id):
    with sessions() as s:
        return list(s.scalars(select(Event.detail).where(Event.job_id == job_id).order_by(Event.id)))


def test_haiku_reads_the_sheet_and_routes_the_solve(router, sandbox, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = routed_script([{**TRIAGE, "handwritten": True}])
    assert router.run_once().status == "solved"
    look, solve = sandbox.runs
    assert look["job"]["model"] == "haiku" and look["files"] == ["DAA-Assignment-01.pdf"] and "resume" not in look["job"]
    assert look["job"]["lean"] is True and "lean" not in solve["job"]  # only Haiku's look runs without connectors and skills
    assert "Do not solve anything" in look["job"]["prompt"] and "lectures: leave empty." in look["job"]["prompt"]
    assert (solve["job"]["model"], solve["job"]["effort"]) == ("sonnet", "medium")
    log = details(sessions, job_id)
    assert "Haiku sorted it: sonnet, medium effort; routine recurrences" in log
    assert "solving with sonnet, medium effort" in log
    assert any(t.startswith("Handwritten work: Design & Analysis of Algorithms · Recurrence Relations") for t, _ in channel.sent)
    assert UsageGuard(DbKV(sessions)).runs_today(NOW) == 1  # Haiku's look is not a solver run
    with sessions() as s:
        assert s.get(Job, job_id).cost_tokens == 2800  # both runs are counted in the job's tokens


def test_a_fixed_model_or_effort_switches_routing_off(router, sandbox, sessions, tmp_path):
    add_job(sessions, tmp_path)
    DbKV(sessions).set("solver_model", "claude-opus-5-5")
    sandbox.script = routed_script([])
    assert router.run_once().status == "solved"
    (solve,) = sandbox.runs
    assert solve["job"]["model"] == "claude-opus-5-5" and "effort" not in solve["job"]

    DbKV(sessions).delete("solver_model")
    DbKV(sessions).set("solver_effort", "max")  # your effort wins over Haiku's, the model is still routed
    add_job(sessions, tmp_path, ref="155150:2", title="Sorting")
    sandbox.script = routed_script([{**TRIAGE, "model": "opus", "effort": "high"}])
    router.run_once()
    assert (sandbox.runs[-1]["job"]["model"], sandbox.runs[-1]["job"]["effort"]) == ("opus", "max")


def test_when_haiku_fails_the_solve_goes_on_with_the_default_model(router, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = routed_script([None])
    assert router.run_once().status == "solved"
    assert "model" not in sandbox.runs[1]["job"]
    assert any(d.startswith("Haiku could not sort it") for d in details(sessions, job_id))


def test_a_redo_is_sorted_again_by_its_note(router, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = routed_script([{**TRIAGE, "model": "opus", "effort": "high"},
                                    {"model": "sonnet", "effort": "low", "reason": "a formatting change"}])
    router.run_once()
    with sessions.begin() as s:
        actions.redo(s, s.get(Job, job_id), "make the headings bigger", "WhatsApp")
    assert router.run_once().status == "solved"
    look, solve = sandbox.runs[2:]
    assert look["job"]["model"] == "haiku" and "The student now asks for this change: make the headings bigger" in look["job"]["prompt"]
    assert look["files"] == []  # the note is enough: the sheet is not read again
    assert (solve["job"]["model"], solve["job"]["effort"], solve["job"]["resume"]) == ("sonnet", "low", "sess-1")
    assert "Haiku sorted the redo: sonnet, low effort; a formatting change" in details(sessions, job_id)


class FakeLectures:
    def __init__(self, data_dir, fail=False):
        self.data_dir, self.fail, self.fetched = data_dir, fail, []

    def lectures(self, course):
        from lms_worker.lms.materials import Lecture, LectureFile

        if self.fail:
            raise RuntimeError("the LMS session has ended")
        return [Lecture(str(n), f"Topic {n}", (LectureFile(str(n), f"Topic {n}", f"week-{n}.pptx", "x"),), (), None) for n in (1, 2, 3)]

    def lecture(self, course, week, filename):
        self.fetched.append(filename)
        return files.keep(files.course_dir(self.data_dir, course) / "lectures", filename, b"slides")


def lecture_files(solver, sessions, job_id):
    with sessions() as s:
        job = s.get(Job, job_id)
        folder = solver.job_dir(job.assignment, job) / "work" / "in" / "lectures"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def test_lecture_notes_are_a_reference_when_haiku_says_so(router, sandbox, sessions, tmp_path):
    router.materials = FakeLectures(router.data_dir)
    job_id = add_job(sessions, tmp_path)
    with sessions.begin() as s:
        s.get(Job, job_id).assignment.course.lectures = "auto"
    sandbox.script = routed_script([{**TRIAGE, "lectures": ["week-2.pptx", "made-up.pptx"]}])
    router.run_once()
    look, solve = sandbox.runs
    assert "- week 2: Topic 2 [week-2.pptx]" in look["job"]["prompt"] and "at most 4" in look["job"]["prompt"]
    assert lecture_files(router, sessions, job_id) == ["week-2.pptx"] and router.materials.fetched == ["week-2.pptx"]
    assert "/work/in/lectures: week-2.pptx. They were picked as the ones this assignment builds on" in solve["job"]["system"]
    assert "lecture notes to read: week-2.pptx" in details(sessions, job_id)


def test_always_gives_every_lecture_and_off_gives_none(solver, sandbox, sessions, tmp_path):
    solver.materials = FakeLectures(solver.data_dir)  # this solver does not route: no Haiku run is needed for "always"
    job_id = add_job(sessions, tmp_path)
    sandbox.script = play(stream(SUMMARY))
    solver.run_once()
    assert lecture_files(solver, sessions, job_id) == [] and "in/lectures" not in sandbox.runs[0]["job"]["system"]

    other = add_job(sessions, tmp_path, ref="155150:2", title="Sorting")
    with sessions.begin() as s:
        s.get(Job, other).assignment.course.lectures = "always"
    solver.run_once()
    assert len(sandbox.runs) == 2 and lecture_files(solver, sessions, other) == ["week-1.pptx", "week-2.pptx", "week-3.pptx"]
    assert "Read the ones this assignment builds on before solving" in sandbox.runs[1]["job"]["system"]

    solver.materials.fail = True  # lecture notes that cannot be read never hold a solve up
    third = add_job(sessions, tmp_path, ref="155150:3", title="Graphs")
    assert solver.run_once().status == "solved"
    assert any(d.startswith("the lecture notes could not be read") for d in details(sessions, third))


def test_in_auto_solving_haiku_asks_before_a_stronger_model_is_started(router, sandbox, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    with sessions.begin() as s:
        s.get(Job, job_id).assignment.course.solve = "auto"
    sandbox.script = routed_script([{**TRIAGE, "questions": ["Which dataset does Q2 mean?"]}])
    assert router.run_once().status == "asked" and state(sessions, job_id) == "NEEDS_INPUT"
    assert len(sandbox.runs) == 1 and "list what to ask" in sandbox.runs[0]["job"]["prompt"]
    assert channel.sent[-1][0].startswith("Something is needed before solving: Design & Analysis of Algorithms · Recurrence Relations\n"
                                          "1. Which dataset does Q2 mean?")
    with sessions.begin() as s:
        actions.answer(s, s.get(Job, job_id), "The iris one.", "the chat")
    assert router.run_once().status == "solved"
    solve = sandbox.runs[1]["job"]  # no second look by Haiku
    assert solve["model"] == "sonnet" and "resume" not in solve
    assert "Before solving, the student was asked: Which dataset does Q2 mean?\nThe student answered: The iris one." in solve["prompt"]
    assert "Do not ask the student anything" in solve["system"]


# --- lecture summaries ---

NOTES = {"status": "done", "title": "DAA: weeks 1 to 2", "pages": 3}
NOTES_TRIAGE = {"model": "sonnet", "effort": "low", "reason": "plain definitions", "diagrams": True}


def notes_script(answers, result=NOTES, pdf=True):
    answers = list(answers)

    def script(work, on_line):
        job = json.loads((work / "job.json").read_text())
        if job.get("model") == "haiku":
            return play(stream(answers.pop(0), session="triage"), files=False)(work, on_line)
        if pdf:
            (work / "out" / "notes.pdf").write_bytes(b"%PDF notes")
            (work / "notes.md").write_text("# notes")
        return play(stream(result, session="notes-1"), files=False)(work, on_line)
    return script


def queue_summary(solver, sessions):
    from lms_worker.db import Summary

    with sessions.begin() as s:
        course = s.scalar(select(Course).where(Course.code == "155150"))
        if course is None:
            course = Course(code="155150", name="Design & Analysis of Algorithms", lms_ref="MTU1MTUw", semester="MjAyNjM=",
                            semester_name="Fall-2026")
            s.add(course)
            s.flush()
        for name in ("week-1.pptx", "week-2.pdf"):
            files.keep(files.course_dir(solver.data_dir, course) / "lectures", name, b"slides")
        lectures = [{"week": "1", "title": "Intro", "file": "week-1.pptx"}, {"week": "2", "title": "Recurrences", "file": "week-2.pdf"}]
        return actions.summarize(s, course, lectures).id


def test_a_lecture_summary_is_routed_written_and_sent(router, sandbox, drive, channel, sessions, tmp_path):
    from lms_worker.db import Summary

    sm_id = queue_summary(router, sessions)
    sandbox.script = notes_script([NOTES_TRIAGE])
    assert router.run_once().status == "solved"
    look, write = sandbox.runs
    assert look["job"]["model"] == "haiku" and look["job"]["text"] is True and look["files"] == ["week-1.pptx", "week-2.pdf"]
    assert "Read the text only" in look["job"]["prompt"]
    assert (write["job"]["model"], write["job"]["effort"], write["job"]["text"]) == ("sonnet", "low", True)
    assert write["job"]["plain"] is True and "lean" not in write["job"]
    assert "one document from which the lectures can be learnt without opening the slides" in write["job"]["system"]
    assert "Leave nothing out to save space" in write["job"]["system"] and "The slides lean on their figures" in write["job"]["system"]
    assert "lms-notes /work/notes.md /work/out/notes.pdf" in write["job"]["system"]
    assert "week 1: Intro (week-1.pptx); week 2: Recurrences (week-2.pdf)" in write["job"]["prompt"]
    with sessions() as s:
        sm = s.get(Summary, sm_id)
        assert sm.state == "DONE" and Path(sm.path).name == "DAA weeks 1 to 2.pdf" and Path(sm.path).read_bytes() == b"%PDF notes"
    text, file = channel.sent[-1]
    assert file == "DAA weeks 1 to 2.pdf"
    assert text.startswith("Lecture notes summary: Design & Analysis of Algorithms\nweeks 1, 2, 3 page(s)\nWritten by sonnet, low effort: plain definitions")
    assert drive.saved[-1] == (["University Assignments", "Fall-2026", "Design & Analysis of Algorithms", "Lecture summaries"],
                               ["DAA weeks 1 to 2.pdf"])
    assert router.run_once().status == "idle"


def test_assignments_go_before_summaries_and_a_failed_summary_is_retried_once(router, sandbox, channel, sessions, tmp_path, clock):
    from lms_worker.db import Summary

    sm_id = queue_summary(router, sessions)
    job_id = add_job(sessions, tmp_path)
    sandbox.script = routed_script([TRIAGE])
    assert router.run_once().job_id == job_id  # the assignment first
    with sessions() as s:
        assert s.get(Summary, sm_id).state == "QUEUED"

    sandbox.script = notes_script([{**NOTES_TRIAGE, "diagrams": False}], pdf=False)
    assert router.run_once().status == "retry"
    assert "do not open the page images" in sandbox.runs[-1]["job"]["system"]
    assert router.run_once().status == "idle"  # not before ten minutes have passed
    clock.at = NOW + timedelta(minutes=11)
    assert router.run_once().status == "failed"
    assert sandbox.runs[-1]["job"]["model"] == "sonnet" and len([r for r in sandbox.runs if r["job"].get("model") == "haiku"]) == 2
    with sessions() as s:
        assert s.get(Summary, sm_id).state == "FAILED"
    assert channel.sent[-1][0].startswith("The lecture summary could not be written: Design & Analysis of Algorithms, weeks 1, 2\n"
                                          "the run ended without notes.pdf")


def test_redo_with_opus_starts_again_on_opus_whatever_else_is_set(router, sandbox, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path)
    sandbox.script = routed_script([TRIAGE])
    router.run_once()
    DbKV(sessions).set("solver_model", "claude-sonnet-5-5")  # even a model fixed in Settings
    with sessions.begin() as s:
        actions.redo(s, s.get(Job, job_id), "Q2 is wrong", "WhatsApp", strong=True)
    assert router.run_once().status == "solved"
    again = sandbox.runs[-1]["job"]
    assert len(sandbox.runs) == 3  # Haiku is not asked: you already chose
    assert (again["model"], again["effort"]) == ("opus", "high") and "resume" not in again
    assert "The student asked for a redo with this note: Q2 is wrong" in again["prompt"]
    assert "solving (a fresh run, as you asked) with opus, high effort" in details(sessions, job_id)

    with sessions.begin() as s:  # a normal redo afterwards goes back to the fixed model
        actions.redo(s, s.get(Job, job_id), "bigger headings", "WhatsApp")
    router.run_once()
    assert sandbox.runs[-1]["job"]["model"] == "claude-sonnet-5-5" and sandbox.runs[-1]["job"]["resume"] == "sess-1"
