from lms_worker.lms.session import SessionStore, Throttle, lms_cookies, session_is_valid


def test_pasted_session_is_valid_only_with_the_right_cookie(portal, urls, tmp_path):
    store = SessionStore(tmp_path / "session.json")
    assert session_is_valid(store.save_pasted("PHPSESSID=good", urls), urls)
    assert not session_is_valid(store.save_pasted("stale", urls), urls)
    assert lms_cookies(store.load(), urls) == {"PHPSESSID": "stale"}


def test_expired_session_costs_one_request_and_never_touches_cms(portal, urls, tmp_path):
    state = SessionStore(tmp_path / "s.json").save_pasted("stale", urls)
    assert not session_is_valid(state, urls)
    assert portal.lms_requests == ["/Student/Dashboard.php"]


def test_redirects_inside_the_lms_are_followed(portal, urls, tmp_path):
    hop = urls.__class__(cms_login=urls.cms_login, lms_dashboard=urls.lms_dashboard.replace("Dashboard", "Hop"))
    state = SessionStore(tmp_path / "s.json").save_pasted("good", hop)
    assert session_is_valid(state, hop)


def test_no_lms_cookie_means_no_request(portal, urls):
    assert not session_is_valid({"cookies": [{"name": "cms", "value": "x", "domain": "127.0.0.1"}]}, urls)
    assert not session_is_valid(None, urls)
    assert portal.lms_requests == []


def test_throttle_spaces_requests():
    now = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    t = Throttle(1.0, clock=lambda: now[0], sleep=sleep)
    t.wait()
    now[0] += 0.25
    t.wait()
    now[0] += 5
    t.wait()
    assert slept == [0.75]
