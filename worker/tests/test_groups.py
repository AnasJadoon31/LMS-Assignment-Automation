import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lms_worker.config import EvolutionConfig
from lms_worker.groups import Group, GroupNotices, GroupNotifier, WhatsAppGroups, summary_text
from lms_worker.kv import DbKV
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.notify import NotifyError, Outbox
from lms_worker.watcher import Watcher
from lms_worker.web import setup_router

from .test_notify import Flaky
from .test_watcher import DAA, FakeLMS, meta
from .test_web import KEY, PASSWORD, StubAdmin, signed_in

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=LMS_TZ)
GROUP = Group("120363000000000001@g.us", "BSCS 5-A")


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


@pytest.fixture
def setup(sessions, tmp_path):
    clock = Clock(NOW)
    kv = DbKV(sessions)
    groups = WhatsAppGroups(kv)
    groups.save([GROUP])
    owner, group = Flaky(), Flaky()
    outbox = Outbox({"whatsapp": owner, "group": group})
    notices = GroupNotices(sessions, kv, outbox, groups, now=clock)
    lms = FakeLMS()
    watcher = Watcher(lms, sessions, kv, outbox, tmp_path / "data", now=clock, groups=notices)
    return {"clock": clock, "owner": owner, "group": group, "lms": lms, "watcher": watcher, "notices": notices, "groups": groups}


def test_only_addressed_messages_reach_the_group(sessions):
    owner, group = Flaky(), Flaky()
    outbox = Outbox({"whatsapp": owner, "group": group})
    with sessions.begin() as s:
        outbox.queue(s, "alert", "private")
        outbox.queue(s, "group-summary", "for the group", to=("group",))
    outbox.flush(sessions)
    assert [m for m, _ in owner.sent] == ["private"] and [m for m, _ in group.sent] == ["for the group"]


def test_new_assignments_go_to_the_group_with_the_file(setup):
    setup["lms"].set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=3)))
    setup["watcher"].poll_once()
    text, file = setup["group"].sent[0]
    assert text.startswith("📢 *New assignment*\n\n📘 Design & Analysis of Algorithms\n📝 *Recurrence Relations*\n⏰ Due Sun 11 Oct")
    assert file == "Recurrence-Relations.pdf"
    assert any(m.startswith("New assignment") for m, _ in setup["owner"].sent)  # your own message is unchanged
    assert len(setup["group"].sent) == 1


def test_reminders_go_whether_submitted_or_not_and_only_once(setup):
    setup["lms"].set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=3)),
                     meta(DAA, 2, "Sorting", deadline=NOW + timedelta(days=3), submitted_file="mine.pdf"))
    setup["watcher"].poll_once()
    setup["group"].sent.clear()
    setup["clock"].at = NOW + timedelta(days=2, hours=1)  # 23 h left
    setup["watcher"].poll_once()
    setup["watcher"].poll_once()
    assert sorted(m.split("\n")[3] for m, _ in setup["group"].sent) == ["📝 *Recurrence Relations*", "📝 *Sorting*"]
    assert all(m.startswith("⏳ *Due in 24 hours*") for m, _ in setup["group"].sent)
    setup["clock"].at = NOW + timedelta(days=2, hours=22, minutes=30)  # 1.5 h left
    setup["watcher"].poll_once()
    later = [m.split("\n")[0] for m, _ in setup["group"].sent[2:] if "Assignments due" not in m]
    assert later == ["🚨 *Due in 2 hours*"] * 2


def test_summaries_at_8_am_and_9_pm_once_each(setup):
    setup["lms"].set(DAA, meta(DAA, 1, "Recurrence Relations", deadline=NOW + timedelta(days=1, hours=4)),
                     meta(DAA, 2, "Sorting", deadline=NOW + timedelta(days=6), submitted_file="mine.pdf"))
    setup["watcher"].poll_once()
    setup["group"].sent.clear()
    for at, expect in [(datetime(2026, 10, 8, 12, 0), 0), (datetime(2026, 10, 8, 21, 5), 1), (datetime(2026, 10, 8, 21, 40), 1),
                       (datetime(2026, 10, 9, 7, 59), 1), (datetime(2026, 10, 9, 8, 20), 2)]:
        setup["clock"].at = at.replace(tzinfo=LMS_TZ)
        setup["watcher"].poll_once()
        summaries = [m for m, _ in setup["group"].sent if "Assignments due" in m]
        assert len(summaries) == expect, at
    evening = summaries[0]
    assert evening.startswith("🌙 *Good evening!* Assignments due · Thu 8 Oct")
    assert "🔴 *Recurrence Relations*" in evening and "🟢 *Sorting*" in evening  # submitted ones too
    assert evening.index("Recurrence") < evening.index("Sorting")
    assert summaries[1].startswith("☀️ *Good morning!*")


def test_an_empty_summary_says_so():
    assert summary_text([], NOW).endswith("✅ No assignment deadlines coming up.")


def test_group_notifier_sends_to_every_group_and_tolerates_one_failing(sessions):
    groups = WhatsAppGroups(DbKV(sessions))
    groups.save([GROUP, Group("120363000000000002@g.us", "Lab group")])
    sent, fail = [], set()

    def handler(request):
        body = json.loads(request.content)
        if body["number"] in fail:
            return httpx.Response(500, text="down")
        sent.append(body["number"])
        return httpx.Response(201, json={})

    cfg = EvolutionConfig(base_url="http://evo", api_key="k", instance="lms")
    n = GroupNotifier(cfg, groups, transport=httpx.MockTransport(handler))
    n.text("hello")
    assert sent == ["120363000000000001@g.us", "120363000000000002@g.us"]
    fail.add("120363000000000001@g.us")
    n.text("again")  # one group down: the other still gets it, no retry storm
    fail.add("120363000000000002@g.us")
    with pytest.raises(NotifyError):
        n.text("all down")


def test_picking_groups_on_the_setup_page(sessions):
    class Admin(StubAdmin):
        def groups(self):
            return [{"id": GROUP.id, "name": GROUP.name, "size": 42}]

    groups = WhatsAppGroups(DbKV(sessions))
    sender = Flaky()
    app = FastAPI()
    app.include_router(setup_router(password=PASSWORD, secret_key=KEY, login=None, admin=Admin(), groups=groups,
                                    group_sender=sender, group_summary=lambda: "SUMMARY"))
    c = signed_in(TestClient(app, base_url="https://lms.example.com"))
    h = {"X-LMS": "1"}
    assert c.get("/api/setup/whatsapp/groups", headers=h).json() == {"groups": [{"id": GROUP.id, "name": GROUP.name, "size": 42, "chosen": False}]}
    assert c.post("/api/setup/whatsapp/groups/test", headers=h, json={}).status_code == 400  # none chosen yet
    assert c.post("/api/setup/whatsapp/groups", headers=h, json={"groups": [{"id": "923001112223@s.whatsapp.net"}]}).status_code == 400
    assert c.post("/api/setup/whatsapp/groups", headers=h, json={"groups": [{"id": GROUP.id, "name": GROUP.name}]}).json()["count"] == 1
    assert groups.get() == [GROUP]
    assert c.post("/api/setup/whatsapp/groups/test", headers=h, json={}).status_code == 200
    assert c.post("/api/setup/whatsapp/groups/summary", headers=h, json={}).status_code == 200
    assert [m for m, _ in sender.sent][-1] == "SUMMARY"
    assert "SetupPage" not in c.get("/setup").text and "Show the bot's groups" in c.get("/setup").text
