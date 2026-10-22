"""Classes: other sections' groups run by their CR, within your rules."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from lms_worker.classes import policy
from lms_worker.classes.menu import ClassMenu
from lms_worker.classes.poller import ClassPoller
from lms_worker.classes.store import Classes, class_dir, session_key
from lms_worker.db.models import (Assignment, ClassAssignment, ClassGroup, ClassLog, ClassMessage, ClassPerson, ClassStatus,
                                  Course, SchoolClass)
from lms_worker.kv import DbKV
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.lms.errors import SessionExpired
from lms_worker.notify import NotifyError

from .test_watcher import DAA, FakeLMS, meta

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LMS_TZ)
GROUP = "120363000000000777@g.us"


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class Sender:
    def __init__(self):
        self.sent, self.fail = [], False

    def text(self, to, message):
        if self.fail:
            raise NotifyError("down")
        self.sent.append((to, message, None))

    def document(self, to, path, caption):
        if self.fail:
            raise NotifyError("down")
        self.sent.append((to, caption, path.name))


def make_class(sessions, name="BSCS 5-B", status=ClassStatus.ACTIVE, groups=(GROUP,), cr="923001234567", rules=None):
    with sessions.begin() as s:
        k = SchoolClass(name=name, status=status, enrollment="02-1", rules=json.dumps(rules or {}))
        k.people.append(ClassPerson(phone=cr, name="Sara", role="cr"))
        for g in groups:
            k.groups.append(ClassGroup(group_jid=g, name="5-B official"))
        s.add(k)
        s.flush()
        return k.id


@pytest.fixture
def setup(sessions, tmp_path):
    clock = Clock(NOW)
    kv = DbKV(sessions)
    classes = Classes(sessions, kv, tmp_path / "data", now=clock)
    lms_ = FakeLMS()
    sender, alerts = Sender(), []
    poller = ClassPoller(classes, lambda k: lms_, sender=sender, alert=alerts.append, now=clock, public_url="https://lms.example")
    return {"clock": clock, "classes": classes, "lms": lms_, "sender": sender, "alerts": alerts, "poller": poller, "kv": kv}


# --- your rules and the CR's choices ---


def test_rules_lock_override_the_crs_choices():
    defaults = {**policy.DEFAULT_RULES, "remind_2h": {"lock": "on"}}
    p = policy.Policy(defaults, {}, {"remind_2h": False, "remind_24h": False, "summary_morning": False})
    assert p.on("remind_2h") is True            # always on: their "off" does not count
    assert p.on("remind_24h") is False          # theirs to decide
    with pytest.raises(policy.PolicyError, match="set by the admin"):
        p.choose("remind_2h", False)
    # this class only: the morning summary locked off and hidden from the CR
    p = policy.Policy(defaults, {"summary_morning": {"lock": "off", "hide": True}}, {"summary_morning": True})
    assert p.on("summary_morning") is False
    assert "summary_morning" not in [i["key"] for i in p.for_cr()]
    assert p.source("summary_morning") == "class" and p.source("remind_2h") == "default"


def test_limits_can_only_go_lower_and_the_mode_no_higher_than_you_allow():
    p = policy.Policy(policy.DEFAULT_RULES, {}, {"limit_requests": 50, "mode": "menu"})
    assert p.value("limit_requests") == 10    # their 50 is capped at your 10
    assert p.value("mode") == "notify"        # you allow notices only
    assert p.choose("limit_requests", 5)["limit_requests"] == 5
    with pytest.raises(policy.PolicyError):
        p.choose("limit_requests", 11)
    with pytest.raises(policy.PolicyError, match="allows up to notify"):
        p.choose("mode", "menu")
    fixed = policy.Policy({**policy.DEFAULT_RULES, "limit_files": {"max": 2, "cr": False}}, {"mode": {"max": "menu", "cr": True}}, {"limit_files": 1})
    assert fixed.value("limit_files") == 2 and not fixed.editable("limit_files")
    assert fixed.value("mode") == "menu" and fixed.menu


def test_bad_rules_are_refused():
    with pytest.raises(policy.PolicyError):
        policy.clean_rules({"remind_2h": {"lock": "sometimes"}})
    with pytest.raises(policy.PolicyError):
        policy.clean_rules({"nope": {"lock": "on"}})
    assert policy.clean_rules({"announce": {"lock": "off", "hide": True}, "limit_files": {"max": "4"}}) == {
        "announce": {"lock": "off", "hide": True}, "limit_files": {"max": 4, "cr": False}}


# --- the class poller ---


def test_first_check_is_quiet_then_new_and_changed_work_is_announced(setup, sessions):
    class_id = make_class(sessions)
    lms_, poller, sender = setup["lms"], setup["poller"], setup["sender"]
    lms_.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=3)))
    assert poller.poll(class_id) == "ok"
    poller.flush()
    assert sender.sent == []                                   # what was already there is not announced
    with sessions() as s:
        a = s.scalar(select(ClassAssignment))
        assert Path(a.question_path).is_file() and str(class_dir(setup["classes"].data_dir, class_id)) in a.question_path

    lms_.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=3)),
             meta(DAA, 2, "Sorting", deadline=NOW + timedelta(days=5)))
    poller.poll(class_id)
    poller.flush()
    to, text, file = sender.sent[-1]
    assert to == GROUP and text.startswith("📢 *New assignment*") and "Sorting" in text and file == "Sorting.pdf"

    lms_.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=4)),
             meta(DAA, 2, "Sorting", deadline=NOW + timedelta(days=5)))
    poller.poll(class_id)
    poller.flush()
    assert sender.sent[-1][1].startswith("🗓️ *Deadline changed*")
    with sessions() as s:
        logs = [r.detail for r in s.scalars(select(ClassLog).where(ClassLog.kind == "sent")).all()]
    assert len(logs) == 2 and logs[0].startswith("new → 5-B official")


def test_rules_decide_what_goes_out(setup, sessions):
    class_id = make_class(sessions, rules={"notice_file": {"lock": "off"}})
    lms_, poller, sender = setup["lms"], setup["poller"], setup["sender"]
    lms_.set(DAA)
    poller.poll(class_id)
    lms_.set(DAA, meta(DAA, 2, "Sorting", deadline=NOW + timedelta(days=5)))
    poller.poll(class_id)
    poller.flush()
    assert sender.sent[-1][2] is None                          # the file is locked off for this class
    with sessions.begin() as s:
        s.get(SchoolClass, class_id).choices = json.dumps({"mode": "off"})
    lms_.set(DAA, meta(DAA, 2, "Sorting", deadline=NOW + timedelta(days=5)), meta(DAA, 3, "Graphs", deadline=NOW + timedelta(days=6)))
    poller.poll(class_id)
    poller.flush()
    assert len(sender.sent) == 1                               # the CR turned the bot off


def test_reminders_and_summaries_from_the_database(setup, sessions):
    class_id = make_class(sessions)
    lms_, poller, sender, clock = setup["lms"], setup["poller"], setup["sender"], setup["clock"]
    lms_.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(hours=30)))
    poller.poll(class_id)
    clock.at = NOW + timedelta(hours=7)                         # 23 h left
    poller.remind()
    poller.remind()
    clock.at = NOW + timedelta(hours=28, minutes=30)            # 1.5 h left
    poller.remind()
    poller.flush()
    assert [t.split("\n")[0] for _, t, _ in sender.sent] == ["⏳ *Due in 24 hours*", "🚨 *Due in 2 hours*"]
    clock.at = datetime(2026, 10, 21, 8, 10, tzinfo=LMS_TZ)
    poller.summaries()
    poller.summaries()
    poller.flush()
    assert sender.sent[-1][1].startswith("☀️ *Good morning!*") and len(sender.sent) == 3


def test_a_paused_or_muted_class_holds_its_messages(setup, sessions):
    class_id = make_class(sessions)
    classes, lms_, poller, sender, clock = setup["classes"], setup["lms"], setup["poller"], setup["sender"], setup["clock"]
    lms_.set(DAA)
    poller.poll(class_id)
    lms_.set(DAA, meta(DAA, 2, "Sorting", deadline=NOW + timedelta(days=5)))
    poller.poll(class_id)
    classes.set_status(class_id, ClassStatus.PAUSED, "admin")
    poller.flush()
    assert [m for m in sender.sent if m[0] == GROUP] == []      # waits while paused
    with pytest.raises(PermissionError, match="only the admin"):
        classes.set_status(class_id, ClassStatus.ACTIVE, "cr:923001234567")
    classes.set_status(class_id, ClassStatus.ACTIVE, "admin")
    poller.flush()
    assert [t.split("\n")[0] for to, t, _ in sender.sent if to == GROUP] == ["📢 *New assignment*"]
    assert any(to == "923001234567" and "paused by the admin" in t for to, t, _ in sender.sent)


def test_a_signed_out_class_asks_its_cr_once_and_tells_you(setup, sessions):
    class_id = make_class(sessions)
    lms_, poller, sender, alerts = setup["lms"], setup["poller"], setup["sender"], setup["alerts"]
    lms_.session_error = SessionExpired("no password kept")
    assert poller.poll(class_id) == "signin"
    assert poller.poll(class_id) == "signin"
    poller.flush()
    assert [to for to, _, _ in sender.sent] == ["923001234567"]
    assert "sign in again at https://lms.example/cr" in sender.sent[0][1]
    assert len(alerts) == 1 and "BSCS 5-B" in alerts[0]
    lms_.session_error = None
    assert poller.poll(class_id) == "ok"
    with sessions() as s:
        assert s.get(SchoolClass, class_id).login_state == "ok"


def test_a_failing_group_waits_and_the_others_go_on(setup, sessions):
    class_id = make_class(sessions, groups=(GROUP, "120363000000000888@g.us"))
    sender, classes = setup["sender"], setup["classes"]
    with sessions.begin() as s:
        classes.queue(s, s.get(SchoolClass, class_id), "test", "one")
    sender.fail = True
    setup["poller"].flush()
    sender.fail = False
    setup["poller"].flush()
    assert sorted(to for to, _, _ in sender.sent) == [GROUP, "120363000000000888@g.us"]
    with sessions() as s:
        assert {m.attempts for m in s.scalars(select(ClassMessage)).all()} == {2}


def test_removing_a_class_deletes_its_login_and_files(setup, sessions):
    class_id = make_class(sessions)
    lms_, poller, classes, kv = setup["lms"], setup["poller"], setup["classes"], setup["kv"]
    lms_.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=3)))
    poller.poll(class_id)
    kv.set(session_key(class_id), "secret")
    with sessions.begin() as s:
        s.get(SchoolClass, class_id).password_enc = "encrypted"
    classes.remove(class_id, "cr:923001234567")
    assert kv.get(session_key(class_id)) is None
    assert not class_dir(classes.data_dir, class_id).exists()
    with sessions() as s:
        k = s.get(SchoolClass, class_id)
        assert (k.status, k.password_enc, k.groups, k.courses, k.people) == ("removed", None, [], [], [])
        assert s.scalar(select(ClassLog).where(ClassLog.detail.like("Class removed%"))) is not None


# --- the class menu ---


def test_the_class_menu_shows_only_the_class_own_files(setup, sessions, tmp_path):
    class_id = make_class(sessions, rules={"mode": {"max": "menu"}})
    lms_, poller, classes = setup["lms"], setup["poller"], setup["classes"]
    lms_.set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=3)))
    poller.poll(class_id)
    # your own course and question file, which must never show up
    mine = tmp_path / "data" / "155150" / "mine.pdf"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"%PDF mine")
    with sessions.begin() as s:
        c = Course(code="155150", name="My own course", lms_ref="X", semester=DAA.semester)
        s.add(c)
        s.flush()
        s.add(Assignment(course_id=c.id, lms_ref="X:1", number="1", title="Mine", fingerprint="f", question_path=str(mine)))
    with sessions() as s:
        k = s.get(SchoolClass, class_id)
        menu = ClassMenu(sessions, classes.data_dir, k, classes.policy(k), now=setup["clock"])
    first = menu.handle(GROUP, "923009", "Ali", "menu", group=True)[0].text
    assert first.startswith("📚 *BSCS 5-B*") and "1. 📝 Assignment question files" in first
    courses = menu.handle(GROUP, "923009", "Ali", "1", group=True)[0].text
    assert "Design & Analysis of Algorithms" in courses and "My own course" not in courses
    menu.handle(GROUP, "923009", "Ali", "1", group=True)
    reply = menu.handle(GROUP, "923009", "Ali", "1", group=True)[0]
    assert reply.file.name == "Recurrence-Relations.pdf"
    assert str(class_dir(classes.data_dir, class_id)) in str(reply.file)


def test_menu_sections_follow_the_rules(setup, sessions):
    class_id = make_class(sessions, rules={"mode": {"max": "menu"}, "menu_lectures": {"lock": "off"},
                                           "menu_leaderboard": {"lock": "off"}})
    classes = setup["classes"]
    with sessions() as s:
        k = s.get(SchoolClass, class_id)
        menu = ClassMenu(sessions, classes.data_dir, k, classes.policy(k), now=setup["clock"])
    text = menu.handle(GROUP, "923009", "Ali", "menu", group=True)[0].text
    assert "Lectures" not in text and "Leaderboard" not in text and "Deadlines" in text


# --- the CR's LMS sign-in, against the fake portal ---


def test_sign_in_reads_the_course_list_and_keeps_nothing(portal, urls, tmp_path):
    from lms_worker.classes.lms import SignInError, sign_in

    from .conftest import ENROLLMENT, PASSWORD, make_settings

    settings = make_settings(urls, tmp_path)
    done = sign_in(settings, ENROLLMENT, PASSWORD, "Karachi Campus")
    assert [c.name for c in done.courses] and any(c["name"] == "PHPSESSID" for c in done.state["cookies"])
    assert not any(tmp_path.iterdir())                        # nothing written to disk
    with pytest.raises(SignInError, match="did not accept"):
        sign_in(settings, ENROLLMENT, "wrong", "Karachi Campus")
    portal.captcha = True
    with pytest.raises(SignInError, match="CAPTCHA"):
        sign_in(settings, ENROLLMENT, PASSWORD, "Karachi Campus")


def test_a_class_adapter_logs_in_again_only_with_a_kept_password(portal, urls, tmp_path, sessions):
    from lms_worker.classes.lms import adapter
    from lms_worker.kv import SecretBox

    from .conftest import ENROLLMENT, PASSWORD, make_settings

    settings, kv, box = make_settings(urls, tmp_path), DbKV(sessions), SecretBox("k" * 40)
    klass = SchoolClass(id=7, name="X", enrollment=ENROLLMENT, institute="Karachi Campus")
    with pytest.raises(SessionExpired):
        adapter(settings, kv, box, klass).ensure_session()     # no password kept: the CR must sign in
    klass.password_enc = box.encrypt(PASSWORD)
    a = adapter(settings, kv, box, klass)
    a.ensure_session()
    assert a.list_courses() and kv.get(session_key(7)) is not None and kv.get("lms_session") is None  # its own key, not yours
