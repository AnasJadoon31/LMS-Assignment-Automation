import time

import httpx
import pytest
from fastapi.testclient import TestClient

from lms_worker.api import SECRET_HEADER, create_app, webhook_secret
from lms_worker.config import EvolutionConfig, PortalUrls, Settings
from lms_worker.kv import DbKV
from lms_worker.notify import WhatsAppNumber
from lms_worker.whatsapp import EvolutionAdmin

from .test_notify import Flaky
from .test_whatsapp import FakeEvolution

ME = "923001234567"
SECRET_KEY = "s" * 40
SETTINGS = Settings(
    urls=PortalUrls(),
    state_dir=None,
    credentials=None,
    app_secret_key=SECRET_KEY,
    evolution=EvolutionConfig(base_url="http://evolution-api:8080", api_key="k3y", instance="lms", number=ME),
)


def upsert(text="menu", *, jid=f"{ME}@s.whatsapp.net", msg_id="m1", from_me=False, **key_extra):
    return {
        "event": "messages.upsert",
        "instance": "lms",
        "data": {
            "key": {"remoteJid": jid, "fromMe": from_me, "id": msg_id, **key_extra},
            "pushName": "Me",
            "message": {"conversation": text},
            "messageType": "conversation",
        },
    }


@pytest.fixture
def phone():
    return Flaky()


@pytest.fixture
def client(sessions, phone):
    app = create_app(SETTINGS, sessions, DbKV(sessions), sender=phone, register_webhook=False)
    with TestClient(app) as c:
        yield c


def post(client, body, secret=webhook_secret(SECRET_KEY)):
    return client.post("/api/webhooks/evolution", json=body, headers={SECRET_HEADER: secret} if secret else {})


def test_your_message_gets_the_menu(client, phone):
    r = post(client, upsert("hi"))
    assert r.status_code == 200 and r.json() == {"handled": True, "replies": 1}
    assert phone.sent[0][0].startswith("*LMS BOT*")


def test_extended_text_messages_count_too(client, phone):
    body = upsert()
    body["data"]["message"] = {"extendedTextMessage": {"text": "menu"}}
    assert post(client, body).json()["handled"]
    assert phone.sent[0][0].startswith("*LMS BOT*")


def test_a_number_saved_on_the_setup_page_is_the_one_answered(sessions, phone):
    kv = DbKV(sessions)
    app = create_app(SETTINGS, sessions, kv, sender=phone, register_webhook=False)
    WhatsAppNumber(kv).save("923119999999")
    with TestClient(app) as c:
        assert post(c, upsert(jid="923119999999@s.whatsapp.net")).json()["handled"]
        assert post(c, upsert(msg_id="m2")).json()["reason"] == "sender"  # WHATSAPP_NUMBER no longer applies


def test_wrong_or_missing_secret_is_refused(client, phone):
    assert post(client, upsert(), secret="nope").status_code == 401
    assert post(client, upsert(), secret=None).status_code == 401
    assert phone.sent == []


def test_other_senders_groups_and_own_messages_are_ignored(client, phone):
    assert post(client, upsert(jid="923119999999@s.whatsapp.net")).json()["reason"] == "sender"
    assert post(client, upsert(jid="120363000000000000@g.us", msg_id="g")).json()["handled"] is False
    assert post(client, upsert(from_me=True, msg_id="me")).json()["handled"] is False
    assert post(client, {"event": "connection.update", "data": {}}).json()["handled"] is False
    assert phone.sent == []


def test_anonymous_ids_resolve_to_your_number(client, phone):
    body = upsert(jid="123456789012345@lid", msg_id="lid1", remoteJidAlt=f"{ME}@s.whatsapp.net")
    assert post(client, body).json()["handled"]


def test_a_message_delivered_twice_is_answered_once(client, phone):
    post(client, upsert(msg_id="dup"))
    assert post(client, upsert(msg_id="dup")).json()["reason"] == "duplicate"
    assert len(phone.sent) == 1


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True, "last_poll": None, "last_status": None}


def test_the_webhook_is_registered_with_the_secret(sessions):
    fake = FakeEvolution(exists=True, state="open")
    seen = []

    def evolution(req):
        if req.url.path == "/webhook/set/lms":
            seen.append(req)
            return httpx.Response(201, json={})
        return fake(req)

    admin = EvolutionAdmin(SETTINGS.evolution, transport=httpx.MockTransport(evolution), sleep=lambda s: None)
    app = create_app(SETTINGS, sessions, DbKV(sessions), sender=Flaky(), admin=admin, internal_url="http://api:8000")
    with TestClient(app):
        end = time.monotonic() + 5
        while not seen and time.monotonic() < end:
            time.sleep(0.02)
    import json

    body = json.loads(seen[0].content)["webhook"]
    assert body["url"] == "http://api:8000/api/webhooks/evolution"
    assert body["headers"] == {SECRET_HEADER: webhook_secret(SECRET_KEY)}
    assert body["events"] == ["MESSAGES_UPSERT"] and body["enabled"] is True
