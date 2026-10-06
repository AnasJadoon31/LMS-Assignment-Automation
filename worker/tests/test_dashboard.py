import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from lms_worker.dashboard import dashboard_router
from lms_worker.db import Course, Event, Job, Notification
from lms_worker.kv import DbKV, SecretBox
from lms_worker.notify import DiscordSetting, LiveDiscord, Outbox
from lms_worker.watcher import POLL_NOW_KEY
from lms_worker.web import Auth, setup_router

from .test_bot import NOW, Clock, semester  # noqa: F401  (semester is a fixture)
from .test_notify import Flaky
from .test_web import KEY, PASSWORD, StubAdmin, signed_in

H = {"X-LMS": "1"}
WEBHOOK = "https://discord.com/api/webhooks/1234567890/abcdefTOKENxyz9"


class DiscordHook:
    def __init__(self):
        self.posts = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.posts.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "1"})


@pytest.fixture
def hook():
    return DiscordHook()


@pytest.fixture
def discord(sessions, hook):
    return DiscordSetting(DbKV(sessions), SecretBox(KEY), None, transport=hook.transport)


@pytest.fixture
def client(sessions, discord):
    auth = Auth(PASSWORD, KEY)
    app = FastAPI()
    app.include_router(setup_router(password=PASSWORD, secret_key=KEY, login=None, admin=StubAdmin(), discord=discord, auth=auth))
    app.include_router(dashboard_router(auth, sessions, DbKV(sessions), now=Clock(NOW)))
    return TestClient(app, base_url="https://lms.example.com")


def test_the_courses_pages_need_the_password(client, semester):
    assert client.get("/courses", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/courses", headers=H).status_code == 401
    assert client.get(f"/files/question/{semester['rec']}").status_code == 401
    signed_in(client)
    assert "All courses" not in client.get("/courses").text and "Courses" in client.get("/courses").text
    assert client.get("/api/courses").status_code == 403  # no X-LMS header


def test_course_list_with_modes_and_counts(client, semester):
    signed_in(client)
    d = client.get("/api/courses", headers=H).json()
    assert d["semester"] == "Fall-2026"
    by_name = {c["name"]: c for c in d["courses"]}
    assert by_name["DAA"]["counts"] == {"remaining": 2, "due_soon": 1, "approval": 1, "needs_you": 0, "papers_coming": 0}
    assert by_name["CA Lab"]["counts"]["papers_coming"] == 1 and by_name["CA Lab"]["lab"] is True
    assert by_name["DAA"]["mode"] == "approve" and set(d["modes"]) == {"approve", "auto", "notify"}

    assert client.post(f"/api/courses/{semester['daa']}/mode", headers=H, json={"mode": "auto"}).json() == {
        "message": "DAA is now in auto mode."}
    assert client.post(f"/api/courses/{semester['daa']}/mode", headers=H, json={"mode": "sometimes"}).status_code == 400


def test_a_course_shows_its_work_files_and_approval(client, semester, sessions):
    signed_in(client)
    d = client.get(f"/api/courses/{semester['daa']}", headers=H).json()
    assert [i["title"] for i in d["assignments"]] == ["Recurrence Relations", "Sorting"]  # soonest first
    rec = d["assignments"][0]
    assert rec["when"].startswith("Due Sun 11 Oct, 6:00 PM (in 1 d 6 h)") and rec["status"] == "not submitted"
    assert rec["job"]["words"] == "solved; waiting for your approval" and rec["job"]["can_approve"]
    assert [f["label"] for f in rec["files"]] == ["Question file", "Solved PDF"]
    assert client.get(rec["files"][0]["url"]).content == b"%PDF question"
    pdf = client.get(rec["files"][1]["url"])
    assert pdf.content == b"%PDF solved" and pdf.headers["content-disposition"].startswith("inline")

    job = rec["job"]["id"]
    assert client.post(f"/api/jobs/{job}/approve", headers=H).json()["message"].startswith("Approved: Recurrence Relations.")
    assert client.post(f"/api/jobs/{job}/approve", headers=H).status_code == 409  # already approved
    with sessions() as s:
        assert s.get(Job, job).state == "SUBMITTING"
        assert s.scalar(select(Event.detail).order_by(Event.id.desc())) == "approved on the web page"
    assert DbKV(sessions).get(POLL_NOW_KEY)


def test_redo_needs_a_note_and_queues_the_job(client, semester, sessions):
    signed_in(client)
    job = client.get(f"/api/courses/{semester['daa']}", headers=H).json()["assignments"][0]["job"]["id"]
    assert client.post(f"/api/jobs/{job}/redo", headers=H, json={"note": " "}).status_code == 409
    assert client.post(f"/api/jobs/{job}/redo", headers=H, json={"note": "Use merge sort"}).status_code == 200
    with sessions() as s:
        assert (s.get(Job, job).state, s.get(Job, job).note) == ("DOWNLOADED", "Use merge sort")


def test_lab_papers_are_listed_but_never_have_actions(client, semester):
    signed_in(client)
    d = client.get(f"/api/courses/{semester['lab']}", headers=H).json()
    assert d["course"]["lab"] and d["papers"][0]["title"] == "Mid Term paper (60 Minutes)"
    assert all(p["job"] is None for p in d["papers"])


def test_discord_from_the_setup_page(client, discord, hook, sessions):
    signed_in(client)
    assert client.get("/api/setup/status", headers=H).json()["discord"] == {
        "available": True, "on": False, "source": None, "masked": None, "editable": True}
    assert client.post("/api/setup/discord", headers=H, json={"url": "https://example.com/hook"}).status_code == 400
    assert client.post("/api/setup/discord", headers=H, json={"url": WEBHOOK}).json() == {"ok": True}
    assert client.post("/api/setup/discord/test", headers=H).json() == {"ok": True}
    assert hook.posts[-1]["content"].startswith("Test message from LMS Assignment Automation")
    status = client.get("/api/setup/status", headers=H).json()["discord"]
    assert status["on"] and status["source"] == "page" and status["masked"] == "discord.com/api/webhooks/1234…/••••xyz9"
    assert WEBHOOK not in json.dumps(DbKV(sessions).get("discord_webhook"))  # encrypted at rest

    # The worker's outbox reads the setting on every message: no restart needed
    whatsapp = Flaky()
    outbox = Outbox({"whatsapp": whatsapp, "discord": LiveDiscord(discord)})
    with sessions.begin() as s:
        outbox.queue(s, "alert", "one")
    outbox.flush(sessions)
    assert hook.posts[-1]["content"] == "one"
    assert client.post("/api/setup/discord/off", headers=H).json() == {"ok": True}
    with sessions.begin() as s:
        outbox.queue(s, "alert", "two")
    with sessions() as s:
        assert [n.channel for n in s.scalars(select(Notification).where(Notification.text == "two"))] == ["whatsapp"]


def test_a_webhook_from_the_environment_shows_and_can_be_turned_off(sessions, hook):
    setting = DiscordSetting(DbKV(sessions), SecretBox(KEY), WEBHOOK, transport=hook.transport)
    assert (setting.url(), setting.source()) == (WEBHOOK, "env")
    setting.turn_off()
    assert (setting.url(), setting.source()) == (None, None)
