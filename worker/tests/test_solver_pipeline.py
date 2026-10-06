import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from lms_worker.claude_login import LoginStatus
from lms_worker.db import Artifact, Assignment, Course, Event, Job, JobState
from lms_worker.kv import DbKV
from lms_worker.lms import models as lms
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.lms.errors import AlreadySubmitted
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
                  now=clock, public_url="https://lms.example.com", env={"HTTPS_PROXY": "http://egress:3128"})


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
    assert run["files"] == ["DAA-Assignment-01.pdf"] and run["env"] == {"HTTPS_PROXY": "http://egress:3128"}
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


def test_handwritten_work_is_sent_to_copy_and_never_uploaded(solver, sandbox, channel, sessions, tmp_path):
    job_id = add_job(sessions, tmp_path, mode="auto")
    sandbox.script = play(stream({**SUMMARY, "status": "handwritten"}))

    assert solver.run_once().status == "handwritten"
    assert state(sessions, job_id) == "HANDWRITTEN"
    text, file = channel.sent[-1]
    assert text.startswith("Solved, handwritten: Assignment 01 - Recurrence Relations")
    assert text.endswith("The sheet asks for handwritten work: copy this by hand. Nothing will be uploaded.")
    assert file.endswith(".pdf")


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


def test_only_open_assignments_in_approve_or_auto_mode_are_solved(solver, sandbox, sessions, tmp_path):
    add_job(sessions, tmp_path, kind="paper", ref="155150:P1", title="Mid Term paper")
    add_job(sessions, tmp_path, ref="155150:2", title="Closed", deadline=NOW - timedelta(minutes=1))
    assert solver.run_once().status == "idle"
    job_id = add_job(sessions, tmp_path, ref="155150:3", title="Sorting")
    with sessions.begin() as s:
        s.get(Job, job_id).assignment.course.mode = "notify"
    assert solver.run_once().status == "idle" and not sandbox.runs


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
