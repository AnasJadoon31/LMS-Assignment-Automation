"""BahriaAdapter session handling. Kept apart from test_cms_login.py because the
adapter starts its own Playwright, which cannot run beside a module-scoped one."""

from pathlib import Path

import pytest

from lms_worker.lms.bahria import BahriaAdapter
from lms_worker.lms.errors import BadCredentials, ReloginPaused, SessionExpired
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


def test_submit_is_not_built_yet(portal, urls, tmp_path):
    with logged_in(urls, tmp_path) as adapter:
        a = adapter.list_assignments(adapter.list_courses()[0])[2]
        with pytest.raises(NotImplementedError):
            adapter.submit(a, tmp_path / "x.pdf")


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
