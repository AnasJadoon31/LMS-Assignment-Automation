import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from lms_worker.config import EvolutionConfig
from lms_worker.db import Notification
from lms_worker.notify import DiscordNotifier, EvolutionNotifier, NotifyError, Outbox

WEBHOOK = "https://discord.com/api/webhooks/123/tok"


class Recorder:
    """httpx transport that records requests and answers from a list of responses."""

    def __init__(self, *responses: httpx.Response):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses) or [httpx.Response(200, json={"id": "1"})]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    @property
    def transport(self):
        return httpx.MockTransport(self)


def test_evolution_text_and_document(tmp_path):
    rec = Recorder(httpx.Response(201, json={}))
    cfg = EvolutionConfig(base_url="https://evo.example", api_key="k3y", instance="lms", number="923001234567")
    n = EvolutionNotifier(cfg, transport=rec.transport)
    n.text("hello")
    pdf = tmp_path / "Q.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    n.document(pdf, "New assignment")

    t, d = rec.requests
    assert str(t.url) == "https://evo.example/message/sendText/lms"
    assert t.headers["apikey"] == "k3y"
    assert json.loads(t.content) == {"number": "923001234567", "text": "hello"}
    body = json.loads(d.content)
    assert str(d.url) == "https://evo.example/message/sendMedia/lms"
    assert body["mediatype"] == "document" and body["mimetype"] == "application/pdf"
    assert body["fileName"] == "Q.pdf" and body["caption"] == "New assignment"
    assert body["media"] == "JVBERi0xLjc="


def test_evolution_error_raises():
    rec = Recorder(httpx.Response(401, text="bad key"))
    cfg = EvolutionConfig(base_url="https://evo.example", api_key="x", instance="lms", number="1")
    with pytest.raises(NotifyError, match="401"):
        EvolutionNotifier(cfg, transport=rec.transport).text("hi")


def test_discord_text_never_pings_and_is_trimmed():
    rec = Recorder()
    DiscordNotifier(WEBHOOK, transport=rec.transport).text("@everyone " + "x" * 3000)
    (r,) = rec.requests
    assert r.url.params["wait"] == "true"
    body = json.loads(r.content)
    assert body["allowed_mentions"] == {"parse": []}
    assert len(body["content"]) == 2000 and body["content"].endswith("…")


def test_discord_document_is_multipart(tmp_path):
    rec = Recorder()
    pdf = tmp_path / "Lab-6.pdf"
    pdf.write_bytes(b"%PDF-1.7 data")
    DiscordNotifier(WEBHOOK, transport=rec.transport).document(pdf, "New assignment")
    (r,) = rec.requests
    assert r.headers["content-type"].startswith("multipart/form-data")
    raw = r.content
    assert b'name="files[0]"; filename="Lab-6.pdf"' in raw
    assert b"%PDF-1.7 data" in raw
    payload = raw.split(b'name="payload_json"')[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n--")[0]
    assert json.loads(payload)["attachments"] == [{"id": 0, "filename": "Lab-6.pdf"}]
    assert json.loads(payload)["content"] == "New assignment"


def test_discord_file_over_the_limit_sends_a_note(tmp_path, monkeypatch):
    rec = Recorder()
    big = tmp_path / "big.zip"
    big.write_bytes(b"0" * 2048)
    monkeypatch.setattr(DiscordNotifier, "MAX_FILE_BYTES", 1024)
    DiscordNotifier(WEBHOOK, transport=rec.transport).document(big, "Solution ready")
    (r,) = rec.requests
    assert "over Discord's limit" in json.loads(r.content)["content"]


def test_discord_waits_once_on_a_short_rate_limit():
    slept = []
    rec = Recorder(httpx.Response(429, json={"retry_after": 0.4, "global": False}), httpx.Response(200, json={}))
    DiscordNotifier(WEBHOOK, transport=rec.transport, sleep=slept.append).text("hi")
    assert slept == [0.4] and len(rec.requests) == 2

    long_wait = Recorder(httpx.Response(429, json={"retry_after": 60}))
    with pytest.raises(NotifyError, match="429"):
        DiscordNotifier(WEBHOOK, transport=long_wait.transport, sleep=slept.append).text("hi")


class Flaky:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.sent: list[tuple[str, str | None]] = []

    def text(self, message):
        self._maybe_fail()
        self.sent.append((message, None))

    def document(self, path: Path, caption):
        self._maybe_fail()
        self.sent.append((caption, path.name))

    def _maybe_fail(self):
        if self.fail_times:
            self.fail_times -= 1
            raise NotifyError("down")


def test_outbox_channels_deliver_independently(sessions):
    whatsapp, discord = Flaky(), Flaky(fail_times=1)
    outbox = Outbox({"whatsapp": whatsapp, "discord": discord})
    with sessions.begin() as s:
        outbox.queue(s, "alert", "first")
        outbox.queue(s, "alert", "second")

    assert outbox.flush(sessions) == (2, 1)
    assert [m for m, _ in whatsapp.sent] == ["first", "second"]
    assert discord.sent == []  # its queue waits behind the failed message, in order

    assert outbox.flush(sessions) == (2, 0)
    assert [m for m, _ in discord.sent] == ["first", "second"]
    assert [m for m, _ in whatsapp.sent] == ["first", "second"]  # nothing re-sent

    with sessions() as s:
        rows = s.scalars(select(Notification).order_by(Notification.id)).all()
    assert {(r.channel, r.text, r.attempts) for r in rows} == {
        ("whatsapp", "first", 1),
        ("whatsapp", "second", 1),
        ("discord", "first", 2),
        ("discord", "second", 1),
    }


def test_outbox_sends_files_and_falls_back_to_text_when_missing(sessions, tmp_path):
    ch = Flaky()
    outbox = Outbox({"discord": ch})
    pdf = tmp_path / "Q.pdf"
    pdf.write_bytes(b"%PDF")
    with sessions.begin() as s:
        outbox.queue(s, "assignment", "with file", file=pdf)
        outbox.queue(s, "assignment", "file gone", file=tmp_path / "missing.pdf")
    outbox.flush(sessions)
    assert ch.sent == [("with file", "Q.pdf"), ("file gone", None)]
