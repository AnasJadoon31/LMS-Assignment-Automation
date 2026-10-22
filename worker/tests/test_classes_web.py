"""The invite sign-up, your Classes page, the CR's page, and the class bot answering."""

import re
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lms_worker.api import SECRET_HEADER, class_webhook_secret, create_app
from lms_worker.classes.access import phone as phone_of
from lms_worker.classes.lms import SignedIn, SignInError
from lms_worker.db.models import ClassGroup, ClassLog, ClassMessage, ClassPerson, Course, SchoolClass
from lms_worker.kv import DbKV
from lms_worker.lms import models as lms
from lms_worker.whatsapp import LinkState

from .test_api import SECRET_KEY, SETTINGS

PASSWORD = "admin pass phrase"
BOT, BOT_LID = "923119999999", "99001122"
CR, ALI, ALI_LID = "923004444444", "923002222222", "55002222"
GROUP = "120363000000000999@g.us"
SEM = "MjAyNjM="
COURSES = [lms.Course(code=str(100 + i), name=f"Course {i}", lms_ref=f"REF{i}", semester=SEM, semester_name="Fall-2026")
           for i in range(4)]


class ClassAdmin:
    """The class bot's Evolution instance: linked, in one group where the CR is an admin."""

    def __init__(self):
        self.members = [{"id": f"{BOT_LID}@lid", "phoneNumber": f"{BOT}@s.whatsapp.net", "admin": None},
                        {"id": "77@lid", "phoneNumber": f"{CR}@s.whatsapp.net", "admin": "admin"},
                        {"id": f"{ALI_LID}@lid", "phoneNumber": f"{ALI}@s.whatsapp.net", "admin": None}]

    def state(self):
        return LinkState("open", number=BOT, name="Class Bot")

    def groups_with_members(self):
        return [{"id": GROUP, "name": "5-B official", "size": 3, "participants": self.members},
                {"id": "120363000000000111@g.us", "name": "Not mine", "size": 2,
                 "participants": [{"id": f"{CR}@s.whatsapp.net", "admin": None}]}]

    def participants(self, jid):
        return self.members if jid == GROUP else []


class Sender:
    def __init__(self):
        self.sent = []

    def text(self, to, message):
        self.sent.append((to, message))

    def document(self, to, path, caption):
        self.sent.append((to, caption))

    def code(self, to):
        return re.search(r"\*(\d{6})\*", [m for t, m in self.sent if t == to][-1]).group(1)


class Replies:
    def __init__(self):
        self.sent = []

    def send(self, m, replies):
        self.sent.append((m.chat, [r.text for r in replies], [r.file.name if r.file else None for r in replies]))


class Owner:
    def __init__(self):
        self.sent = []

    def text(self, message):
        self.sent.append(message)

    def document(self, path, caption):
        self.sent.append(caption)


SECTION_C = [lms.Course(code=str(100 + i), name=f"Course {i}", lms_ref=f"REF{i}", semester=SEM, semester_name="Fall-2026")
             for i in range(2, 6)]


def fake_sign_in(enrollment, password, institute):
    if password != "lms-pass":
        raise SignInError("The LMS did not accept that enrollment and password")
    return SignedIn(state={"cookies": [{"name": "PHPSESSID", "value": "abc", "domain": "lms.bahria.edu.pk"}]},
                    courses=SECTION_C if enrollment == "02-2" else COURSES)


@pytest.fixture
def env(sessions, tmp_path):
    settings = replace(SETTINGS, admin_password=PASSWORD, public_url="https://lms.example", data_dir=tmp_path / "data")
    sender, replies, owner = Sender(), Replies(), Owner()
    app = create_app(settings, sessions, DbKV(sessions), sender=owner, register_webhook=False, discord_chat=False,
                     class_admin=ClassAdmin(), class_sender=sender, class_public_sender=replies, class_sign_in=fake_sign_in)
    with TestClient(app, base_url="https://testserver") as c:
        yield {"c": c, "sender": sender, "replies": replies, "owner": owner, "sessions": sessions}


H = {"X-LMS": "1"}


def admin(c):
    r = c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    return c


def wait(c, url):
    for _ in range(100):
        j = c.get(url, headers=H).json()
        if j.get("state") != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("sign-in did not finish")


def join(env, *, phone="0300-4444444", password="lms-pass", enrollment="02-1"):
    """Make an invite as you, then sign up through it as the CR. Returns the invite link."""
    c, sender = env["c"], env["sender"]
    admin(c)
    link = c.post("/api/classes/invites", json={"name": "BSCS 5-B"}, headers=H).json()["link"]
    token = link.rsplit("/", 1)[1]
    c.cookies.clear()
    assert "Join the class bot: BSCS 5-B" in c.get(f"/join/{token}").text
    assert c.post(f"/api/join/{token}/code", json={"phone": phone, "name": "Sara"}, headers=H).json() == {"sent": True}
    code = sender.code(phone_of(phone))
    assert c.post(f"/api/join/{token}/verify", json={"phone": phone, "name": "Sara", "code": code}, headers=H).json() == {"verified": True}
    c.post(f"/api/join/{token}/lms", json={"enrollment": enrollment, "password": password, "remember": False}, headers=H)
    return token, wait(c, f"/api/join/{token}/lms")


def test_a_cr_joins_through_an_invite_and_waits_for_approval(env):
    c, owner, sessions = env["c"], env["owner"], env["sessions"]
    token, result = join(env)
    assert result == {"state": "done"}
    assert "lms_cr" in c.cookies                                    # signed in to their page
    state = c.get("/api/cr/state", headers=H).json()
    assert state["class"]["status"] == "pending" and state["me"]["name"] == "Sara"
    assert state["class"]["remembers_password"] is False
    assert len(state["class"]["courses"]) == 4
    time.sleep(0.1)
    assert any("waiting for your approval" in m for m in owner.sent)
    # the link is used up
    c.cookies.clear()
    assert c.get(f"/join/{token}").status_code == 404
    with sessions() as s:
        k = s.scalar(select(SchoolClass))
        assert (k.name, k.enrollment, k.password_enc) == ("BSCS 5-B", "02-1", None)
        assert s.scalar(select(ClassPerson)).phone == CR


def test_a_wrong_code_or_password_gets_nowhere(env):
    c, sender = env["c"], env["sender"]
    admin(c)
    token = c.post("/api/classes/invites", json={"name": "BSCS 5-B"}, headers=H).json()["link"].rsplit("/", 1)[1]
    c.cookies.clear()
    c.post(f"/api/join/{token}/code", json={"phone": CR, "name": "Sara"}, headers=H)
    assert c.post(f"/api/join/{token}/lms", json={"enrollment": "02-1", "password": "lms-pass"}, headers=H).status_code == 401
    for _ in range(5):
        assert c.post(f"/api/join/{token}/verify", json={"phone": CR, "name": "Sara", "code": "000000"}, headers=H).status_code == 400
    good = sender.code(CR)
    assert c.post(f"/api/join/{token}/verify", json={"phone": CR, "name": "Sara", "code": good}, headers=H).status_code == 400  # 5 tries used
    c.post(f"/api/join/{token}/code", json={"phone": CR, "name": "Sara"}, headers=H)
    assert c.post(f"/api/join/{token}/verify", json={"phone": CR, "name": "Sara", "code": sender.code(CR)}, headers=H).status_code == 200
    c.post(f"/api/join/{token}/lms", json={"enrollment": "02-1", "password": "wrong"}, headers=H)
    assert "did not accept" in wait(c, f"/api/join/{token}/lms")["error"]
    assert c.post(f"/api/join/{token}/code", json={"phone": CR, "name": "Sara"}, headers=H).status_code == 200
    assert c.post(f"/api/join/{token}/code", json={"phone": CR, "name": "Sara"}, headers=H).status_code == 400  # 3 codes an hour


def test_sections_sign_up_separately_and_an_overlap_is_shown_not_refused(env):
    """Sections of one program share a semester and maybe some courses: each is its own class. A big
    overlap is shown to you for the approval; only an LMS account that already runs a class is refused."""
    c, sessions, owner = env["c"], env["sessions"], env["owner"]
    join(env)                                                               # 5-B: REF0..REF3
    _, again = join(env, phone="03007654321")                               # same account again
    assert again["state"] == "error" and "already runs a class" in again["error"]
    _, other = join(env, phone="03001112223", enrollment="02-2")            # 5-C: REF2..REF5, two shared
    assert other == {"state": "done"}
    _, twin = join(env, phone="03001112224", enrollment="02-3")             # someone else of 5-B: all four shared
    assert twin == {"state": "done"}
    c.cookies.clear()
    admin(c)
    notes = {k["enrollment"]: k["overlap"] for k in c.get("/api/classes", headers=H).json()["classes"]}
    assert notes["02-2"] == "Shares 2 of 4 courses with BSCS 5-B"                # another section: just a note
    assert notes["02-3"].startswith("⚠️ Shares 4 of 4 courses with BSCS 5-B: maybe the same section")
    time.sleep(0.1)
    assert any("maybe the same section" in m for m in owner.sent)            # in the approval alert too
    with sessions.begin() as s:
        for i in range(4):
            s.add(Course(code="x", name="mine", lms_ref=f"REF{i}", semester=SEM))
    notes = {k["enrollment"]: k["overlap"] for k in c.get("/api/classes", headers=H).json()["classes"]}
    assert notes["02-1"].startswith("⚠️ Shares 4 of 4 courses with your own class")


def test_you_approve_and_rule_and_the_cr_works_within_your_rules(env):
    c, sender, sessions = env["c"], env["sender"], env["sessions"]
    join(env)
    cr_cookie = c.cookies.get("lms_cr")
    c.cookies.clear()
    admin(c)
    page = c.get("/api/classes", headers=H).json()
    k = page["classes"][0]
    assert k["status"] == "pending" and page["bot"]["linked"] is True
    c.post(f"/api/classes/{k['id']}/status", json={"status": "active"}, headers=H)
    # your rules for this class: the menu allowed, 2-hour reminders always on, announcements allowed (2 a day)
    r = c.post(f"/api/classes/{k['id']}/rules", json={"rules": {
        "mode": {"max": "menu", "cr": True}, "remind_2h": {"lock": "on"}, "announce": {"lock": "cr", "default": True},
        "limit_announce": {"max": 2, "cr": True}}}, headers=H).json()
    assert any("Reminder 2 hours" in n and "always on" in n for n in r["told_cr"]), r
    with sessions() as s:  # queued for the worker, which sends the class bot's messages
        assert s.scalar(select(ClassMessage).where(ClassMessage.to == CR, ClassMessage.text.like("The admin changed%")))

    c.cookies.clear()
    c.cookies.set("lms_cr", cr_cookie)
    st = c.get("/api/cr/state", headers=H).json()
    assert st["class"]["status"] == "active"
    settings = {s["key"]: s for s in st["settings"]}
    assert settings["remind_2h"]["editable"] is False and settings["mode"]["choices"] == ["off", "notify", "menu"]
    assert c.post("/api/cr/settings", json={"key": "remind_2h", "value": False}, headers=H).status_code == 400
    assert c.post("/api/cr/settings", json={"key": "mode", "value": "menu"}, headers=H).status_code == 200
    assert c.post("/api/cr/settings", json={"key": "limit_requests", "value": 50}, headers=H).status_code == 400
    # groups: only where the CR is an admin
    groups = c.get("/api/cr/groups", headers=H).json()["groups"]
    assert [g["name"] for g in groups] == ["5-B official"]
    assert c.post("/api/cr/groups", json={"groups": [{"id": "120363000000000111@g.us"}]}, headers=H).status_code == 403
    assert c.post("/api/cr/groups", json={"groups": [{"id": GROUP}]}, headers=H).status_code == 200
    # announcements, two a day
    for _ in range(2):
        assert c.post("/api/cr/announce", json={"text": "Class cancelled tomorrow"}, headers=H).json() == {"groups": 1}
    assert "2 announcements a day" in c.post("/api/cr/announce", json={"text": "again"}, headers=H).json()["detail"]
    # the CR pauses and resumes; your pause they cannot undo
    assert c.post("/api/cr/pause", json={"on": True}, headers=H).status_code == 200
    assert c.post("/api/cr/pause", json={"on": False}, headers=H).status_code == 200
    with sessions() as s:
        logs = [l.detail for l in s.scalars(select(ClassLog).order_by(ClassLog.id)).all()]
    assert "Group ticked: 5-B official" in logs and any(l.startswith("Announcement to 1 groups") for l in logs)


def test_your_pause_and_block_beat_the_crs(env):
    c, sessions = env["c"], env["sessions"]
    join(env)
    cr_cookie = c.cookies.get("lms_cr")
    c.cookies.clear()
    admin(c)
    cid = c.get("/api/classes", headers=H).json()["classes"][0]["id"]
    c.post(f"/api/classes/{cid}/status", json={"status": "active"}, headers=H)
    c.post(f"/api/classes/{cid}/status", json={"status": "paused"}, headers=H)
    c.post("/api/classes/blocks", json={"person": ALI, "class_id": cid}, headers=H)
    c.cookies.clear()
    c.cookies.set("lms_cr", cr_cookie)
    assert "only the admin can resume" in c.post("/api/cr/pause", json={"on": False}, headers=H).json()["detail"]
    assert "only the admin can unblock" in c.post("/api/cr/unblock", json={"person": ALI}, headers=H).json()["detail"]
    c.cookies.clear()
    admin(c)
    c.post(f"/api/classes/{cid}/status", json={"status": "blocked"}, headers=H)
    c.cookies.clear()
    c.cookies.set("lms_cr", cr_cookie)
    assert c.post("/api/cr/settings", json={"key": "remind_24h", "value": False}, headers=H).status_code == 403


def test_the_cr_removes_the_class(env):
    c, sessions, owner = env["c"], env["sessions"], env["owner"]
    join(env)
    assert c.post("/api/cr/remove", json={"confirm": "nope"}, headers=H).status_code == 400
    assert c.post("/api/cr/remove", json={"confirm": "bscs 5-b"}, headers=H).status_code == 200
    assert c.get("/api/cr/state", headers=H).status_code == 401
    with sessions() as s:
        assert s.scalar(select(SchoolClass)).status == "removed"
    time.sleep(0.1)
    assert any("removed the class BSCS 5-B" in m for m in owner.sent)


def test_cr_sign_in_by_whatsapp_code_tells_strangers_nothing(env):
    c, sender = env["c"], env["sender"]
    join(env)
    c.cookies.clear()
    assert c.post("/api/cr/code", json={"phone": "923009999999"}, headers=H).json() == {"sent": True}
    assert all(t != "923009999999" for t, _ in sender.sent)       # a stranger gets no code
    assert c.post("/api/cr/code", json={"phone": CR}, headers=H).json() == {"sent": True}
    assert c.post("/api/cr/verify", json={"phone": CR, "code": sender.code(CR)}, headers=H).status_code == 200
    assert c.get("/api/cr/state", headers=H).status_code == 200
    assert c.get("/api/classes", headers=H).status_code == 401    # a CR is not you


# --- the class bot answering ---


def class_msg(text, *, mentions=(), msg_id="c1", chat=GROUP, sender=(ALI, ALI_LID)):
    phone, lid = sender
    key = {"remoteJid": chat, "fromMe": False, "id": msg_id}
    if chat.endswith("@g.us"):
        key |= {"participant": f"{lid}@lid", "participantAlt": f"{phone}@s.whatsapp.net"}
    else:
        key |= {"remoteJid": f"{lid}@lid", "remoteJidAlt": f"{phone}@s.whatsapp.net"}
    return {"event": "messages.upsert", "instance": "lms-classes", "data": {
        "key": key, "pushName": "Ali", "message": {"conversation": text},
        "contextInfo": {"mentionedJid": list(mentions)} if mentions else {}}}


@pytest.fixture
def live(env):
    """An approved class with the menu on, its group ticked, one assignment with its file."""
    c, sessions = env["c"], env["sessions"]
    join(env)
    c.cookies.clear()
    admin(c)
    cid = c.get("/api/classes", headers=H).json()["classes"][0]["id"]
    c.post(f"/api/classes/{cid}/status", json={"status": "active"}, headers=H)
    c.post(f"/api/classes/{cid}/rules", json={"rules": {"mode": {"max": "menu"}, "limit_requests": {"max": 3},
                                                        "limit_files": {"max": 1}}}, headers=H)
    with sessions.begin() as s:
        s.add(ClassGroup(class_id=cid, group_jid=GROUP, name="5-B official"))
    return {**env, "cid": cid}


def post(c, body):
    return c.post("/api/webhooks/evolution/classes", json=body, headers={SECRET_HEADER: class_webhook_secret(SECRET_KEY)}).json()


def test_the_class_bot_answers_tags_in_class_groups_within_the_limits(live):
    c, replies, sessions, cid = live["c"], live["replies"], live["sessions"], live["cid"]
    c.cookies.clear()
    assert c.post("/api/webhooks/evolution/classes", json=class_msg("menu"), headers={SECRET_HEADER: "wrong"}).status_code == 401
    assert post(c, class_msg("anyone?", msg_id="x0"))["reason"] == "not for the bot"
    r = post(c, class_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"], msg_id="x1"))
    assert r == {"handled": True, "reason": "ok", "replies": 1}
    assert replies.sent[-1][1][0].startswith("📚 *BSCS 5-B*")
    for n in (2, 3):
        post(c, class_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"], msg_id=f"x{n}"))
    limited = post(c, class_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"], msg_id="x4"))
    assert limited["reason"] == "limited" and "limit of 3 menu requests an hour" in replies.sent[-1][1][0]
    assert post(c, class_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"], msg_id="x5"))["reason"] == "over the limit"
    assert len(replies.sent) == 4                                   # one polite note, then silence
    with sessions() as s:
        kinds = [l.kind for l in s.scalars(select(ClassLog).where(ClassLog.person == ALI)).all()]
    assert kinds.count("menu") == 3 and kinds.count("limit") == 1


def test_private_chat_needs_a_class_group_and_blocks_are_silent(live):
    c, replies, cid = live["c"], live["replies"], live["cid"]
    stranger = class_msg("hi", chat="private", msg_id="p0", sender=("923005555555", "123"))
    assert post(c, stranger)["reason"] == "not in a class group"
    assert post(c, class_msg("hi", chat="private", msg_id="p1"))["reason"] == "ok"
    assert replies.sent[-1][0] == f"{ALI_LID}@lid" and replies.sent[-1][1][0].startswith("📚 *BSCS 5-B*")
    admin(c)
    c.post("/api/classes/blocks", json={"person": ALI}, headers=H)  # everywhere
    assert post(c, class_msg("hi", chat="private", msg_id="p2"))["reason"] == "blocked"
    c.post("/api/classes/pause-all", json={"on": True}, headers=H)
    assert post(c, class_msg("hi", chat="private", msg_id="p3"))["reason"] == "all classes are paused"


def test_a_notices_only_class_does_not_answer(live):
    c, cid = live["c"], live["cid"]
    admin(c)
    c.post(f"/api/classes/{cid}/rules", json={"rules": {"mode": {"max": "notify"}}}, headers=H)
    r = post(c, class_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"], msg_id="n1"))
    assert r["reason"] == "the menu is off for this class"


def test_your_own_webhook_never_answers_with_a_class(live):
    from lms_worker.api import webhook_secret

    c = live["c"]
    body = class_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"], msg_id="o1")
    r = c.post("/api/webhooks/evolution", json=body, headers={SECRET_HEADER: webhook_secret(SECRET_KEY)}).json()
    assert r["handled"] is False and live["replies"].sent == []


def test_logs_and_csv_for_you_only(live):
    c = live["c"]
    admin(c)
    logs = c.get("/api/classes/logs?days=7", headers=H).json()["logs"]
    assert any("signed up as CR" in l["detail"] for l in logs)
    csv = c.get("/classes/logs.csv?days=7")
    assert csv.status_code == 200 and csv.text.startswith("time,class,who,kind,person,detail")
    c.cookies.clear()
    assert c.get("/classes/logs.csv").status_code == 401


def test_files_over_the_hourly_limit_are_replaced_by_a_note(live, tmp_path):
    from lms_worker.classes.store import class_dir
    from lms_worker.db.models import ClassAssignment, ClassCourse

    c, replies, sessions, cid = live["c"], live["replies"], live["sessions"], live["cid"]
    admin(c)
    c.post(f"/api/classes/{cid}/rules", json={"rules": {"mode": {"max": "menu"}, "limit_files": {"max": 1}}}, headers=H)
    data_dir = c.app.state.classes_data_dir
    with sessions.begin() as s:
        course = s.scalar(select(ClassCourse).where(ClassCourse.class_id == cid, ClassCourse.lms_ref == "REF0"))
        path = class_dir(data_dir, cid) / "100" / "questions" / "1" / "Lab-1.pdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"%PDF lab 1")
        s.add(ClassAssignment(course_id=course.id, lms_ref="100:1", number="1", title="Lab 1", fingerprint="f",
                              question_path=str(path), deadline=None))
    tag = dict(mentions=[f"{BOT_LID}@lid"])
    for n, text in enumerate(["menu", "1", "1", "1"]):
        post(c, class_msg(f"@{BOT_LID} {text}", msg_id=f"f{n}", **tag))
    assert replies.sent[-1][2] == ["Lab-1.pdf"]
    post(c, class_msg(f"@{BOT_LID} 1", msg_id="f9", **tag))
    assert replies.sent[-1][2] == [None] and "limit of 1 files an hour" in replies.sent[-1][1][0]


def test_the_pages_render(live):
    c = live["c"]
    admin(c)
    page = c.get("/classes")
    assert page.status_code == 200 and 'id="r-list"' in page.text and ">Classes</a>" in page.text
    c.cookies.clear()
    assert 'id="send"' in c.get("/cr").text                         # the sign-in form
    assert c.get("/classes", follow_redirects=False).status_code == 303
    assert c.get("/join/not-a-token").status_code == 404


def test_the_worker_runs_the_class_poller(sessions, tmp_path):
    from lms_worker.runtime import Runtime

    rt = Runtime(settings=replace(SETTINGS, data_dir=tmp_path), kv=DbKV(sessions), adapter=None, sessions=sessions)
    poller = rt.class_poller()
    assert poller is not None and poller.sender is not None and poller.sender.cfg.instance == "lms-classes"
    poller.tick()                                                   # no classes yet: nothing to do, no error


def test_rules_changed_on_the_page_are_saved_for_one_class(env):
    """In a browser: changing a row of one class's rules makes it that class's own rule, and saves."""
    import socket
    import threading

    import uvicorn
    from playwright.sync_api import sync_playwright

    join(env)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(env["c"].app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/login")
            page.fill("#p", PASSWORD)
            page.click("button")
            page.goto(f"http://127.0.0.1:{port}/classes")
            page.wait_for_selector('#r-which option:nth-child(2)', state="attached")
            page.select_option("#r-which", page.eval_on_selector("#r-which option:nth-child(2)", "o => o.value"))
            page.select_option('[data-mode="mode"]', "menu")
            assert not page.is_checked('[data-inherit="mode"]')       # no longer following the default
            page.click("#r-save")
            page.wait_for_function("document.getElementById('r-err').textContent.startsWith('Saved')")
            browser.close()
        assert errors == []
    finally:
        server.should_exit = True
    with env["sessions"]() as s:
        assert '"mode": {"max": "menu"' in s.scalar(select(SchoolClass)).rules


# --- the controls on WhatsApp ---


def chat(c, text, *, who, n=[0]):
    """A private message to the class bot from `who` (a phone number); returns the bot's reply text."""
    n[0] += 1
    before = len(c.replies.sent)
    body = class_msg(text, chat="private", msg_id=f"w{n[0]}", sender=(who, "1" + who[-6:]))
    r = post(c.client, body)
    new = c.replies.sent[before:]
    return "\n---\n".join(t for _, texts, _ in new for t in texts), r


class Chat:
    def __init__(self, env):
        self.client, self.replies = env["c"], env["replies"]

    def __call__(self, text, who):
        return chat(self, text, who=who)[0]


def test_you_run_classes_from_whatsapp(env):
    from .test_api import ME

    c, sessions = env["c"], env["sessions"]
    say = Chat(env)
    main = say("menu", ME)
    assert main.startswith("🏫 *Classes* · your controls") and "3. ➕ Invite a CR" in main
    assert "Send the class's name" in say("3", ME)
    reply = say("BSCS 5-D", ME)
    link = re.search(r"/join/(\S+)", reply)
    assert "Invite for the CR of *BSCS 5-D*" in reply and link
    join(env)                                                   # a CR signs up through the page
    c.cookies.clear()
    waiting = say("menu", ME)
    assert "Waiting for approval (1)" in waiting
    assert "BSCS 5-B · ⏳ waiting" in say("1", ME)
    klass = say("1", ME)
    assert klass.startswith("🏫 *BSCS 5-B*") and "1. ✅ Approve" in klass
    assert "✅ Done. The CR was told." in say("1", ME)
    # rules for this class: the menu allowed
    rules = say("3", ME)
    assert "⚖️ *Rules for BSCS 5-B*" in rules
    mode = say("1", ME)
    assert "Up to: Notices and menu" in mode
    assert "✅ Saved. The CR was told." in say("3", ME)
    with sessions() as s:
        assert '"mode": {"max": "menu"' in s.scalar(select(SchoolClass)).rules
    # a number per class: limits
    say("0", ME)
    limits = say("16", ME)                                      # Menu requests per person per hour
    assert "Menu requests per person per hour" in limits
    say("1", ME)
    assert "✅ Saved" in say("4", ME)
    with sessions() as s:
        assert '"limit_requests": {"max": 4' in s.scalar(select(SchoolClass)).rules
    # pause everything from WhatsApp, and back
    say("menu", ME)
    assert "Pause every class?" in say("6", ME)
    assert "Every class is paused" in say("1", ME)
    assert "Every class is back on" in say("6", ME)


def test_a_cr_runs_their_class_from_whatsapp(env):
    from .test_api import ME

    c, sessions = env["c"], env["sessions"]
    say = Chat(env)
    join(env)
    c.cookies.clear()
    admin(c)
    cid = c.get("/api/classes", headers=H).json()["classes"][0]["id"]
    c.post(f"/api/classes/{cid}/status", json={"status": "active"}, headers=H)
    c.post(f"/api/classes/{cid}/rules", json={"rules": {"mode": {"max": "menu", "cr": True}, "remind_2h": {"lock": "on"},
                                                        "announce": {"lock": "cr", "default": True}}}, headers=H)
    main = say("hi", CR)
    assert main.startswith("🛠️ *BSCS 5-B* · CR controls") and "📣 Announce (3 left today)" in main
    # settings: a locked one stays, a free one flips
    settings = say("2", CR)
    lines = {l.split(". ", 1)[1].split(":")[0]: l.split(". ", 1)[0] for l in settings.splitlines() if re.match(r"\d+\. ", l)}
    assert "🔒 Reminder 2 hours before the deadline is set by the admin" in say(lines["Reminder 2 hours before the deadline"], CR)
    assert "✅ Reminder 24 hours before the deadline: off." in say(lines["Reminder 24 hours before the deadline"], CR)
    # groups: tick the one where the CR is an admin
    say("menu", CR)
    groups = say("3", CR)
    assert "⬜ 5-B official" in groups and "Not mine" not in groups
    assert "✅ 5-B official" in say("1", CR)
    # announce, with a preview and a yes
    say("menu", CR)
    assert "Send the announcement" in say("1", CR)
    preview = say("Quiz moved to Friday", CR)
    assert "This goes to 1 group" in preview and "📣 *From your CR* (Sara)" in preview
    assert "✅ Announcement queued for 1 group." in say("1", CR)
    # pause notices, then an announcement is refused without counting
    say("menu", CR)
    say("5", CR)
    assert "Notices paused for 3 days" in say("3", CR)
    say("1", CR)
    say("Another one", CR)
    assert "Notices are paused for your class" in say("1", CR)
    # the student menu and back
    say("menu", CR)
    student_option = [l for l in say("menu", CR).splitlines() if "Student menu" in l][0].split(".")[0]
    assert "send *menu*" in say(student_option, CR)
    assert say("menu", CR).startswith("📚 *BSCS 5-B*")
    assert say("cr", CR).startswith("🛠️ *BSCS 5-B*")
    with sessions() as s:
        details = [l.detail for l in s.scalars(select(ClassLog)).all()]
        assert sum(d.startswith("Announcement to 1 groups") for d in details) == 1
    # the CR cannot reach your controls, and you are not mistaken for a CR
    assert "your controls" not in say("admin", CR)
    assert say("menu", ME).startswith("🏫 *Classes*")


def test_a_cr_removes_their_class_from_whatsapp(env):
    c, sessions = env["c"], env["sessions"]
    say = Chat(env)
    join(env)
    remove = [l for l in say("menu", CR).splitlines() if "Remove my class" in l][0].split(".")[0]
    assert "send the class name exactly" in say(remove, CR)
    assert "not the class name" in say("BSCS 5-X", CR)
    assert "is removed" in say("bscs 5-b", CR)
    with sessions() as s:
        assert s.scalar(select(SchoolClass)).status == "removed"
