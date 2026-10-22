"""BahriaAdapter session handling. Kept apart from test_cms_login.py because the
adapter starts its own Playwright, which cannot run beside a module-scoped one."""

import dataclasses
from pathlib import Path

import pytest

from lms_worker.lms.bahria import BahriaAdapter, upload_name
from lms_worker.lms.errors import AlreadySubmitted, BadCredentials, LMSError, NothingSubmitted, ReloginPaused, SessionExpired
from lms_worker.lms.session import Throttle, session_is_valid

from .conftest import make_settings

NO_WAIT = Throttle(min_interval=0)


def adapter_for(urls, tmp_path: Path, **kw) -> BahriaAdapter:
    return BahriaAdapter(make_settings(urls, tmp_path, **kw), throttle=NO_WAIT)


def test_relogin_saves_a_session_that_passes_the_check(portal, urls, tmp_path):
    adapter = adapter_for(urls, tmp_path)
    adapter.ensure_session()
    path = adapter.store.kv.path("lms_session")
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert session_is_valid(adapter.store.load(), urls)

    # A second poll reuses the stored session: no new CMS login
    adapter.ensure_session()
    assert len(portal.posts) == 1


def test_wrong_password_counts_and_stops_after_two(portal, urls, tmp_path):
    adapter = adapter_for(urls, tmp_path, password="wrong")
    for attempt in (1, 2):
        with pytest.raises(BadCredentials, match="Invalid Enrollment or Password"):
            adapter.ensure_session()
        assert adapter.guard.state.failures == attempt

    with pytest.raises(ReloginPaused):
        adapter.ensure_session()
    assert len(portal.posts) == 2  # the third poll never reached CMS

    adapter.guard.reset()  # Retry
    with pytest.raises(BadCredentials):
        adapter.ensure_session()
    assert len(portal.posts) == 3


def test_failure_count_survives_restart_and_resets_on_success(portal, urls, tmp_path):
    with pytest.raises(BadCredentials):
        adapter_for(urls, tmp_path, password="wrong").ensure_session()

    restarted = adapter_for(urls, tmp_path)
    assert restarted.guard.state.failures == 1
    restarted.ensure_session()
    assert restarted.guard.state.failures == 0


def test_captcha_pauses_without_submitting(portal, urls, tmp_path):
    portal.captcha = True
    adapter = adapter_for(urls, tmp_path)
    with pytest.raises(ReloginPaused, match="CAPTCHA"):
        adapter.ensure_session()
    assert portal.posts == []
    assert "CAPTCHA" in adapter.guard.state.paused_reason

    with pytest.raises(ReloginPaused):
        adapter.ensure_session()
    assert portal.posts == []


def test_no_credentials_asks_for_a_pasted_session(portal, urls, tmp_path):
    settings = make_settings(urls, tmp_path)
    adapter = BahriaAdapter(settings.__class__(urls=urls, state_dir=tmp_path, credentials=None), throttle=NO_WAIT)
    with pytest.raises(SessionExpired, match="paste a session"):
        adapter.ensure_session()


def logged_in(urls, tmp_path: Path) -> BahriaAdapter:
    adapter = adapter_for(urls, tmp_path)
    adapter.store.save_pasted("good", urls)
    return adapter


def test_list_courses_and_assignments(portal, urls, tmp_path):
    with logged_in(urls, tmp_path) as adapter:
        courses = adapter.list_courses()
        assert [(c.code, c.name, c.semester_name) for c in courses] == [
            ("100002", "Computer Architecture Lab", "Fall-2026"),
            ("100003", "Design & Analysis of Algorithms", "Fall-2026"),
        ]
        lab, daa = courses
        assignments = adapter.list_assignments(lab)
        assert [(a.lms_ref, a.title, a.assignment_id) for a in assignments] == [
            ("100002:1", "Lab 1 Intro", None),
            ("100002:2", "Lab 2 – Arrays", "230173"),
            ("100002:3", "Recurrence Relations", "230368"),
        ]
        assert adapter.list_assignments(daa) == []


def test_every_assignment_gets_its_own_status(portal, urls, tmp_path):
    with logged_in(urls, tmp_path) as adapter:
        lab = adapter.list_courses()[0]
        statuses = [adapter.submission_status(a) for a in adapter.list_assignments(lab)]
    assert [(s.submitted, s.deadline_passed, s.marks) for s in statuses] == [
        (True, True, "8/10"),
        (True, False, None),
        (False, False, None),
    ]


def test_fingerprint_ignores_the_daily_download_key(portal, urls, tmp_path):
    with logged_in(urls, tmp_path) as adapter:
        lab = adapter.list_courses()[0]
        first = [a.fingerprint() for a in adapter.list_assignments(lab)]
        assert first == [a.fingerprint() for a in adapter.list_assignments(lab)]
    assert len(set(first)) == 3


def test_download_saves_the_question_file(portal, urls, tmp_path):
    with logged_in(urls, tmp_path) as adapter:
        a = adapter.list_assignments(adapter.list_courses()[0])[1]
        paths = adapter.download(a, tmp_path / "out")
    assert [p.name for p in paths] == ["Lab-2-Q-25092026-105052pm.pdf"]
    assert paths[0].read_bytes().startswith(b"%PDF")


def test_expired_session_mid_poll_is_reported(portal, urls, tmp_path):
    adapter = adapter_for(urls, tmp_path)
    adapter.store.save_pasted("stale", urls)
    with pytest.raises(SessionExpired, match="session ended"):
        adapter.list_courses()
    adapter.close()


def test_poller_never_requests_submit_or_delete_endpoints(portal, urls, tmp_path):
    with logged_in(urls, tmp_path) as adapter:
        for course in adapter.list_courses():
            for a in adapter.list_assignments(course):
                adapter.submission_status(a)
        adapter.download(adapter.list_assignments(adapter.list_courses()[0])[0], tmp_path / "out")
    assert not [p for p in portal.lms_requests if "studentprocess" in p]


def test_submit_uploads_through_the_form_and_reads_the_row_back(portal, urls, tmp_path):
    pdf = tmp_path / "Assignment 01 - Recurrence Relations.pdf"
    pdf.write_bytes(b"%PDF solved")
    with logged_in(urls, tmp_path) as adapter:
        lab = adapter.list_courses()[0]
        a = adapter.list_assignments(lab)[2]
        receipt = adapter.submit(a, pdf)
        assert receipt.filename == "02-000000-001-100002Assignment_01_Recurrence_Relations.pdf"
        assert adapter.submission_status(a).submitted
        with pytest.raises(AlreadySubmitted) as e:  # never a second upload over the first
            adapter.submit(a, pdf)
        assert e.value.filename == receipt.filename
        with pytest.raises(AlreadySubmitted):
            adapter.submit(adapter.list_assignments(lab)[1], pdf)  # submitted by you: left alone
        with pytest.raises(LMSError, match="not open"):
            adapter.submit(adapter.list_assignments(lab)[0], pdf)  # closed
    [upload] = portal.uploads
    assert upload["fields"] == {"assignmentID": "MjMwMzY4", "offeredCourseID": "MTAwMDAy", "ocID": "MTAwMDAy",
                                "studentID": "MTAwMDAx"}
    assert upload["file"] == {"field": "assignmentSubmissionFile", "filename": "Assignment_01_Recurrence_Relations.pdf",
                              "data": b"%PDF solved", "type": "application/pdf"}
    assert [p for p in portal.lms_requests if "studentprocess" in p] == [
        "/Student/includes/studentprocess.php?s=assignmentSubmissionAdd"]  # never Delete


def test_delete_follows_the_rows_own_link_and_reads_the_row_back(portal, urls, tmp_path):
    pdf = tmp_path / "Assignment 01 - Recurrence Relations.pdf"
    pdf.write_bytes(b"%PDF solved")
    with logged_in(urls, tmp_path) as adapter:
        lab = adapter.list_courses()[0]
        closed, yours, a = adapter.list_assignments(lab)
        with pytest.raises(NothingSubmitted):
            adapter.delete_submission(a)  # open, nothing uploaded yet
        receipt = adapter.submit(a, pdf)
        assert adapter.delete_submission(a) == receipt.filename
        assert not adapter.submission_status(a).submitted
        assert adapter.submit(a, pdf).filename == receipt.filename  # open again, so it can go up again
        with pytest.raises(LMSError, match="not open"):
            adapter.delete_submission(closed)
        with pytest.raises(LMSError, match="still shows the submission"):
            adapter.delete_submission(yours)  # this fake keeps that row; a delete that did not take is reported
    assert [d["a"] for d in portal.deletes] == ["MjMwMzY4", "MjMwMTcz"]
    assert portal.deletes[0] == {"s": "assignmentSubmissionDelete", "aSubId": "NTAwMDAwMg==", "a": "MjMwMzY4", "oc": "MTAwMDAy"}


def test_a_delete_link_for_another_course_is_not_followed(portal, urls, tmp_path, monkeypatch):
    import lms_worker.lms.bahria as bahria

    with logged_in(urls, tmp_path) as adapter:
        lab = adapter.list_courses()[0]
        yours = adapter.list_assignments(lab)[1]
        row = adapter._row(yours)
        moved = dataclasses.replace(row, delete_href=row.delete_href.replace("oc=MTAwMDAy", "oc=MTAwMDAz"))
        monkeypatch.setattr(bahria.BahriaAdapter, "_row", lambda self, a: moved)
        with pytest.raises(LMSError, match="not the one expected"):
            adapter.delete_submission(yours)
    assert portal.deletes == []


def test_upload_names_have_no_special_symbols():
    assert upload_name(Path("Lab 05 - Pipelining & Hazards (v2).PDF")) == "Lab_05_Pipelining_Hazards_v2.pdf"


def test_list_papers(portal, urls, tmp_path):
    with logged_in(urls, tmp_path) as adapter:
        lab, daa = adapter.list_courses()
        papers = adapter.list_papers(lab)
        assert [(p.lms_ref, p.term, p.deadline_passed, p.paper_status, p.submitted) for p in papers] == [
            ("100002:P1", "Mid Term", True, "submitted", True),
            ("100002:P2", "Final Term", True, "submitted", True),
            ("100002:P3", "Mid Term", False, "not_submitted", False),
        ]
        assert papers[2].starts_at.isoformat() == "2026-10-20T14:00:00+05:00"
        assert adapter.list_papers(daa) == []
        assert adapter.submission_status(papers[0]).submitted_file is None  # a paper's file is never shown
