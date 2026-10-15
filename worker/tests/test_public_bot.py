import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from lms_worker.bot import Reply
from lms_worker.config import EvolutionConfig
from lms_worker.db import Artifact, Assignment, Course, Job, JobState
from lms_worker.db.models import BotUser
from lms_worker.groups import Group, WhatsAppGroups
from lms_worker.kv import DbKV
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.lms.materials import Lecture, LectureFile
from lms_worker.public_bot import Directory, PublicBot, PublicSender, addressed, allowed, parse
from lms_worker.whatsapp import LinkState

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=LMS_TZ)
BOT, BOT_LID = "923009999999", "88001122"
GROUP = "120363000000000001@g.us"
ALI, ALI_LID = "923001111111", "77001111"


def group_msg(text, *, mentions=(), sender_lid=ALI_LID, sender_phone=ALI, quoted_participant=None, msg_id="g1", chat=GROUP):
    ctx = {}
    if mentions:
        ctx["mentionedJid"] = list(mentions)
    if quoted_participant:
        ctx["participant"] = quoted_participant
    key = {"remoteJid": chat, "fromMe": False, "id": msg_id, "participant": f"{sender_lid}@lid"}
    if sender_phone:
        key["participantAlt"] = f"{sender_phone}@s.whatsapp.net"
    return {"event": "messages.upsert", "data": {"key": key, "pushName": "Ali",
            "message": {"extendedTextMessage": {"text": text, "contextInfo": ctx}}}}


class Admin:
    def __init__(self):
        self.calls = 0

    def state(self):
        return LinkState("open", number=BOT, name="LMS Bot")

    def participants(self, jid):
        self.calls += 1
        return [{"id": f"{BOT_LID}@lid", "phoneNumber": f"{BOT}@s.whatsapp.net"},
                {"id": f"{ALI_LID}@lid", "phoneNumber": f"{ALI}@s.whatsapp.net"}]


@pytest.fixture
def directory(sessions):
    groups = WhatsAppGroups(DbKV(sessions))
    groups.save([Group(GROUP, "BSCS 5-A")])
    return Directory(Admin(), groups)


def test_parse_a_tagged_group_message():
    m = parse(group_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"]))
    assert (m.chat, m.group, m.ids, m.name, m.mentions) == (GROUP, True, [ALI, ALI_LID], "Ali", [BOT_LID])
    assert m.jids[0] == f"{ALI}@s.whatsapp.net" and m.key["participant"] == f"{ALI_LID}@lid"


def test_who_gets_answered(directory):
    assert addressed(parse(group_msg("menu", mentions=[f"{BOT_LID}@lid"])), directory)          # tagged by LID
    assert addressed(parse(group_msg("menu", mentions=[f"{BOT}@s.whatsapp.net"])), directory)   # or by phone
    assert addressed(parse(group_msg("2", quoted_participant=f"{BOT_LID}@lid")), directory)      # a reply to the bot
    assert not addressed(parse(group_msg("2")), directory)                                       # plain chatter
    assert not addressed(parse(group_msg("menu", mentions=[f"{BOT_LID}@lid"], chat="999@g.us")), directory)  # other group
    private = {"event": "messages.upsert", "data": {"key": {"remoteJid": f"{ALI}@s.whatsapp.net", "id": "p1"},
                                                     "message": {"conversation": "hi"}}}
    assert addressed(parse(private), directory)                                                  # a member, privately
    private["data"]["key"]["remoteJid"] = "923005550000@s.whatsapp.net"
    assert not addressed(parse(private), directory)                                              # a stranger


@pytest.fixture
def course(sessions, tmp_path):
    data = tmp_path / "data"
    qdir = data / "155150" / "1" / "1"
    qdir.mkdir(parents=True)
    q = qdir / "Assignment-01.pdf"
    q.write_bytes(b"%PDF question")
    report = qdir / "Assignment 01 - Recurrence Relations.pdf"
    report.write_bytes(b"%PDF my solved report")
    with sessions.begin() as s:
        c = Course(code="155150", name="DAA", lms_ref="x", semester="S", semester_name="Fall-2026")
        a = Assignment(course=c, lms_ref="155150:1", number="1", title="Recurrence Relations", deadline=NOW + timedelta(days=2),
                       fingerprint="x", question_path=str(q))
        j = Job(assignment=a, revision=1, fingerprint="x", state=JobState.AWAITING_APPROVAL)
        s.add_all([c, a, j])
        s.flush()
        s.add_all([Artifact(job=j, kind="question", path=str(q), sha256="0" * 64, size=1),
                   Artifact(job=j, kind="pdf", path=str(report), sha256="0" * 64, size=1)])
    return {"data": data, "question": q, "report": report}


class FakeMaterials:
    def __init__(self, data):
        self.data = data

    def lectures(self, course):
        return [Lecture("1", "Asymptotics", (LectureFile("1", "Asymptotics", "L1.pdf", "x"),), (), None)]

    def lecture(self, course, week, filename):
        p = self.data / "155150" / "lectures" / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF lecture")
        return p

    def outline(self, course):
        return None

    def plan(self, course):
        return None


def test_the_menu_sends_question_files_and_lectures_and_counts(sessions, course):
    bot = PublicBot(sessions, course["data"], materials=FakeMaterials(course["data"]), now=lambda: NOW)
    say = lambda t, who=ALI: bot.handle(GROUP, who, "Ali", t, group=True)
    main = say(f"@{BOT_LID}")[0].text
    assert main.startswith("📚 *LMS Bot*") and "🏆 Leaderboard" in main and "tag me or reply" in main
    assert "pick a course" in say("1")[0].text
    assert "Recurrence Relations" in say("1")[0].text          # DAA -> its assignments
    sent = say("1")[0]
    assert sent.file == course["question"] and sent.text.startswith("📝 Recurrence Relations")
    say("menu"); say("2"); say("1")
    lec = say("1")[0]
    assert lec.file.name == "L1.pdf"
    # someone else has their own place in the menu
    assert say("3", who="923002222222")[0].text.startswith("📚 *LMS Bot*")  # "3" means nothing before the menu
    with sessions() as s:
        ali = s.scalar(select(BotUser).where(BotUser.person == ALI))
        assert ali.requests == 8 and ali.last_option == "DAA · Week 1: Asymptotics" and ali.chat_id == GROUP
    say("menu")
    board = say("5")[0].text  # counted after the reply: Ali has 9 so far
    assert board.startswith("🏆 *Leaderboard*") and "🥇 Ali · 9" in board and "🥈 Ali · 1" in board


def test_deadlines_list(sessions, course):
    bot = PublicBot(sessions, course["data"], now=lambda: NOW)
    bot.handle("private", ALI, "Ali", "hi", group=False)
    text = bot.handle("private", ALI, "Ali", "3", group=False)[0].text
    assert text.startswith("⏰ *Deadlines*") and "🟠 *Recurrence Relations*" in text


def test_only_question_and_lms_files_can_ever_be_sent(sessions, course):
    data = course["data"]
    with sessions() as s:
        questions = PublicBot._question_paths(s)
    assert allowed(course["question"], data, questions)
    assert not allowed(course["report"], data, questions)               # your solved report, same folder
    lecture = data / "155150" / "lectures" / "L1.pdf"
    lecture.parent.mkdir(parents=True)
    lecture.write_bytes(b"x")
    assert allowed(lecture, data, questions)
    own = data / "155150" / "1" / "own" / "lectures" / "mine.pdf"       # a lookalike folder deeper down
    own.parent.mkdir(parents=True)
    own.write_bytes(b"x")
    assert not allowed(own, data, questions)
    assert not allowed(Path("/etc/passwd"), data, questions)


def test_group_replies_tag_and_quote_the_person():
    bodies = []
    t = httpx.MockTransport(lambda r: bodies.append(json.loads(r.content)) or httpx.Response(201, json={}))
    m = parse(group_msg(f"@{BOT_LID} menu", mentions=[f"{BOT_LID}@lid"]))
    PublicSender(EvolutionConfig(base_url="http://evo", api_key="k", instance="lms"), transport=t).send(m, [Reply("📚 *LMS Bot*")])
    assert bodies[0]["number"] == GROUP and bodies[0]["text"] == f"@{ALI} 📚 *LMS Bot*"
    assert bodies[0]["mentioned"] == [f"{ALI}@s.whatsapp.net"] and bodies[0]["quoted"]["key"]["id"] == "g1"


def test_the_webhook_routes_you_and_classmates_apart(sessions):
    from fastapi.testclient import TestClient

    from lms_worker.api import SECRET_HEADER, create_app, webhook_secret

    from .test_api import ME, SECRET_KEY, SETTINGS, upsert
    from .test_notify import Flaky

    WhatsAppGroups(DbKV(sessions)).save([Group(GROUP, "BSCS 5-A")])
    phone = Flaky()
    app = create_app(SETTINGS, sessions, DbKV(sessions), sender=phone, admin=Admin(), register_webhook=False)
    h = {SECRET_HEADER: webhook_secret(SECRET_KEY)}
    with TestClient(app) as c:
        assert c.post("/api/webhooks/evolution", json=upsert("hi"), headers=h).json() == {"handled": True, "replies": 1}
        assert phone.sent[0][0].startswith("*LMS BOT*")                       # you: your own menu
        r = c.post("/api/webhooks/evolution", json=group_msg("menu", mentions=[f"{BOT_LID}@lid"]), headers=h).json()
        assert r == {"handled": True, "replies": 1, "public": True}            # tagged in the group: the public menu
        r = c.post("/api/webhooks/evolution", json=group_msg("anyone?", msg_id="g2"), headers=h).json()
        assert r["handled"] is False                                          # group chatter is left alone
    assert len(phone.sent) == 1                                               # nothing of theirs reached your chat


def test_tags_lifted_to_the_top_of_the_data_count_too(directory):
    """Evolution API v2 sends contextInfo next to the message, not inside it."""
    body = group_msg("menu")
    body["data"]["message"] = {"conversation": f"@{BOT_LID} menu"}
    body["data"]["contextInfo"] = {"mentionedJid": [f"{BOT_LID}@lid"]}
    m = parse(body)
    assert m.mentions == [BOT_LID] and addressed(m, directory)


def test_evolution_is_told_to_pass_group_messages_on():
    import httpx

    from lms_worker.config import EvolutionConfig
    from lms_worker.whatsapp import EvolutionAdmin

    sent = []

    def handler(request):
        if request.url.path.startswith("/settings/find"):
            return httpx.Response(200, json={"rejectCall": True, "msgCall": "busy", "groupsIgnore": True, "alwaysOnline": False,
                                             "readMessages": False, "readStatus": False, "syncFullHistory": False})
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={})

    admin = EvolutionAdmin(EvolutionConfig(base_url="http://evo", api_key="k", instance="lms"), transport=httpx.MockTransport(handler))
    assert admin.hear_groups() is True
    assert sent == [{"rejectCall": True, "msgCall": "busy", "alwaysOnline": False, "readMessages": False, "readStatus": False,
                     "syncFullHistory": False, "groupsIgnore": False}]
