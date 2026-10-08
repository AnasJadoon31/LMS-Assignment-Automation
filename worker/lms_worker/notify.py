"""Notifications: WhatsApp through Evolution API and/or a Discord webhook, behind an outbox.

Each change writes one row per configured channel in the same transaction; flush()
sends pending rows afterwards. Channels deliver independently: one being down never
holds the other back, and a retry never re-sends to a channel that already got it.

Discord webhooks are one-way, so approvals and the reply menu stay on WhatsApp and
the web UI; Discord gets the same alerts and files. The webhook can be set on the setup
page (kept encrypted) or with DISCORD_WEBHOOK_URL; the page's choice wins, and it is read
on every send, so a change needs no restart. The number WhatsApp messages go to works the
same way: saved on the setup page, or WHATSAPP_NUMBER.

A channel that is switched off (no Discord webhook, no WhatsApp number) gets no rows; its
pending rows wait.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import DISCORD_WEBHOOK, EvolutionConfig
from .db.models import Notification
from .kv import KeyValue, SecretBox

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 20
DISCORD_KEY = "discord_webhook"
WHATSAPP_KEY = "whatsapp_number"


class NotifyError(Exception):
    pass


class Notifier(Protocol):
    def text(self, message: str) -> None: ...
    def document(self, path: Path, caption: str) -> None: ...


def active(notifier: Notifier) -> bool:
    """Whether a channel is on now. Channels without an on/off switch are always on."""
    check = getattr(notifier, "active", None)
    return check() if check else True


class EvolutionNotifier:
    """POST /message/sendText/{instance} and /message/sendMedia/{instance}, authenticated by the apikey header.
    With `number`, where messages go is looked up on every send; without it, it is WHATSAPP_NUMBER."""

    def __init__(self, cfg: EvolutionConfig, *, number: Callable[[], str | None] | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        if number is None and not cfg.number:
            raise ValueError("WhatsApp messages need WHATSAPP_NUMBER")
        self.cfg = cfg
        self._number = number or (lambda: cfg.number)
        self.client = httpx.Client(
            base_url=cfg.base_url,
            headers={"apikey": cfg.api_key},
            timeout=60,
            transport=transport,
        )

    def active(self) -> bool:
        return bool(self._number())

    def _to(self) -> str:
        number = self._number()
        if not number:
            raise NotifyError("WhatsApp has no number to send to; save one on the setup page")
        return number

    def text(self, message: str) -> None:
        self._post("sendText", {"number": self._to(), "text": message})

    def document(self, path: Path, caption: str) -> None:
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._post(
            "sendMedia",
            {
                "number": self._to(),
                # a picture (the course plan) shows in the chat; everything else is a document
                "mediatype": "image" if mimetype in ("image/png", "image/jpeg") else "document",
                "mimetype": mimetype,
                "caption": caption,
                "fileName": path.name,
                "media": base64.b64encode(path.read_bytes()).decode(),
            },
        )

    def _post(self, endpoint: str, body: dict) -> None:
        try:
            r = self.client.post(f"/message/{endpoint}/{self.cfg.instance}", json=body)
        except httpx.HTTPError as e:
            raise NotifyError(f"Evolution API unreachable: {e}") from e
        if r.status_code >= 300:
            raise NotifyError(f"Evolution API {endpoint} returned {r.status_code}: {r.text[:300]}")


class DiscordNotifier:
    """Execute Webhook: POST {webhook url}?wait=true, JSON for text, multipart for a file."""

    MAX_CONTENT = 2000
    MAX_FILE_BYTES = 20 * 1024 * 1024  # Discord's default per-file limit
    MAX_RETRY_WAIT = 5.0

    def __init__(
        self,
        webhook_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = webhook_url
        self.client = httpx.Client(timeout=60, transport=transport)
        self.sleep = sleep

    def text(self, message: str) -> None:
        self._send(json_body=self._payload(message))

    def document(self, path: Path, caption: str) -> None:
        size = path.stat().st_size
        if size > self.MAX_FILE_BYTES:
            note = f"\n({path.name} is {size / 1048576:.1f} MB, over Discord's limit; get it from WhatsApp or the web UI)"
            self.text(caption + note)
            return
        payload = self._payload(caption) | {"attachments": [{"id": 0, "filename": path.name}]}
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (path.name, path.read_bytes(), mimetype)},
        )

    def _payload(self, content: str) -> dict:
        if len(content) > self.MAX_CONTENT:
            content = content[: self.MAX_CONTENT - 1] + "…"
        # Titles and notes come from the LMS: never let them ping anyone
        return {"content": content, "username": "LMS Bot", "allowed_mentions": {"parse": []}}

    def _send(self, *, json_body: dict | None = None, data: dict | None = None, files: dict | None = None) -> None:
        for attempt in (1, 2):
            try:
                r = self.client.post(self.url, params={"wait": "true"}, json=json_body, data=data, files=files)
            except httpx.HTTPError as e:
                raise NotifyError(f"Discord unreachable: {e}") from e
            if r.status_code == 429 and attempt == 1:
                wait = _retry_after(r)
                if wait is not None and wait <= self.MAX_RETRY_WAIT:
                    self.sleep(wait)
                    continue
            if r.status_code >= 300:
                raise NotifyError(f"Discord webhook returned {r.status_code}: {r.text[:300]}")
            return


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.json()["retry_after"])
    except (ValueError, KeyError, TypeError):
        header = r.headers.get("retry-after")
        return float(header) if header else None


class DiscordSetting:
    """Where Discord messages go: a webhook saved on the setup page (encrypted), else
    DISCORD_WEBHOOK_URL. Turning it off on the page overrides the variable too."""

    def __init__(self, kv: KeyValue | None, box: SecretBox | None, env_url: str | None = None, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.kv, self.box, self.env_url, self.transport = kv, box, env_url, transport

    def _stored(self) -> dict | None:
        raw = self.kv.get(DISCORD_KEY) if self.kv else None
        return json.loads(raw) if raw else None

    def url(self) -> str | None:
        stored = self._stored()
        if stored is None:
            return self.env_url
        if stored.get("off") or not self.box:
            return None
        try:
            return self.box.decrypt(stored["url"])
        except (ValueError, KeyError):
            return None

    def source(self) -> str | None:
        """page, env, or None when off."""
        stored = self._stored()
        if stored is None:
            return "env" if self.env_url else None
        return None if stored.get("off") else "page"

    @property
    def editable(self) -> bool:
        return self.kv is not None and self.box is not None

    def save(self, url: str) -> None:
        url = url.strip()
        if not DISCORD_WEBHOOK.fullmatch(url):
            raise ValueError("That is not a Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>)")
        if not self.editable:
            raise ValueError("Saving needs DATABASE_URL and APP_SECRET_KEY; set DISCORD_WEBHOOK_URL instead")
        self.kv.set(DISCORD_KEY, json.dumps({"url": self.box.encrypt(url)}))

    def turn_off(self) -> None:
        if not self.editable:
            raise ValueError("Turning Discord off here needs DATABASE_URL; unset DISCORD_WEBHOOK_URL instead")
        self.kv.set(DISCORD_KEY, json.dumps({"off": True}))

    def notifier(self) -> DiscordNotifier:
        url = self.url()
        if not url:
            raise NotifyError("Discord is off")
        return DiscordNotifier(url, transport=self.transport)


class WhatsAppNumber:
    """Where WhatsApp messages go, and the only number the bot answers: your own number,
    saved on the setup page, else WHATSAPP_NUMBER."""

    def __init__(self, kv: KeyValue | None, env_number: str | None = None) -> None:
        self.kv, self.env_number = kv, env_number

    def _stored(self) -> str | None:
        raw = self.kv.get(WHATSAPP_KEY) if self.kv else None
        return json.loads(raw).get("number") if raw else None

    def get(self) -> str | None:
        return self._stored() or self.env_number

    def source(self) -> str | None:
        """page, env, or None when there is no number."""
        if self._stored():
            return "page"
        return "env" if self.env_number else None

    @property
    def editable(self) -> bool:
        return self.kv is not None

    def save(self, number: str) -> str:
        digits = "".join(ch for ch in number if ch.isdigit())
        if not 8 <= len(digits) <= 15 or digits.startswith("0"):
            raise ValueError("Type the number with its country code and no leading 0, e.g. 923001234567")
        if not self.editable:
            raise ValueError("Saving a number here needs a store; set WHATSAPP_NUMBER instead")
        self.kv.set(WHATSAPP_KEY, json.dumps({"number": digits}))
        return digits

    def remove(self) -> None:
        """Forget the saved number; WHATSAPP_NUMBER applies again, if it is set."""
        if self.kv:
            self.kv.delete(WHATSAPP_KEY)


def masked(url: str | None) -> str | None:
    """'discord.com/api/webhooks/1234…/••••Ab3x': enough to recognise, not enough to post."""
    if not url:
        return None
    m = re.search(r"(discord(?:app)?\.com)/api/(?:v\d+/)?webhooks/(\d+)/([\w-]+)", url)
    if not m:
        return "set"
    return f"{m.group(1)}/api/webhooks/{m.group(2)[:4]}…/••••{m.group(3)[-4:]}"


class LiveDiscord:
    """The Discord channel as set at this moment: the webhook is looked up on every send."""

    def __init__(self, setting: DiscordSetting) -> None:
        self.setting = setting

    def active(self) -> bool:
        return self.setting.url() is not None

    def text(self, message: str) -> None:
        self.setting.notifier().text(message)

    def document(self, path: Path, caption: str) -> None:
        self.setting.notifier().document(path, caption)


class ConsoleNotifier:
    """Messages go to the log: when no other channel is on, or always without a switch."""

    def __init__(self, when: Callable[[], bool] | None = None) -> None:
        self._when = when

    def active(self) -> bool:
        return self._when() if self._when else True

    def text(self, message: str) -> None:
        log.info("notification: %s", message)
        print(f"[notify] {message}")

    def document(self, path: Path, caption: str) -> None:
        log.info("notification: %s [%s]", caption, path)
        print(f"[notify] {caption}\n[notify] attachment: {path}")


def send_test(channels: dict[str, Notifier], message: str) -> dict[str, str | None]:
    """Send one text straight to each channel, skipping the outbox. Returns the error per channel, or None."""
    results: dict[str, str | None] = {}
    for name, notifier in channels.items():
        if not active(notifier):
            continue
        try:
            notifier.text(message)
            results[name] = None
        except NotifyError as e:
            results[name] = str(e)
    return results


class Outbox:
    def __init__(self, channels: dict[str, Notifier]) -> None:
        if not channels:
            raise ValueError("Outbox needs at least one channel")
        self.channels = channels

    def queue(self, session: Session, kind: str, text: str, *, file: Path | None = None, job_id: int | None = None) -> None:
        for channel, notifier in self.channels.items():
            if not active(notifier):
                continue
            session.add(
                Notification(channel=channel, kind=kind, text=text, file_path=str(file) if file else None, job_id=job_id)
            )

    def flush(self, sessions: sessionmaker[Session]) -> tuple[int, int]:
        """Send pending messages oldest first on each channel. Returns (sent, failed)."""
        sent = failed = 0
        for channel, notifier in self.channels.items():
            if not active(notifier):  # switched off: its rows wait
                continue
            s_, f_ = self._flush_channel(sessions, channel, notifier)
            sent += s_
            failed += f_
        return sent, failed

    @staticmethod
    def _flush_channel(sessions: sessionmaker[Session], channel: str, notifier: Notifier) -> tuple[int, int]:
        sent = 0
        with sessions() as s:
            pending = s.scalars(
                select(Notification)
                .where(
                    Notification.channel == channel,
                    Notification.sent_at.is_(None),
                    Notification.attempts < MAX_ATTEMPTS,
                )
                .order_by(Notification.id)
            ).all()
            for n in pending:
                n.attempts += 1
                try:
                    if n.file_path and Path(n.file_path).exists():
                        notifier.document(Path(n.file_path), n.text)
                    else:
                        notifier.text(n.text)
                except (NotifyError, OSError) as e:
                    n.last_error = str(e)
                    s.commit()
                    log.warning("%s notification %s failed: %s", channel, n.id, e)
                    # Keep order on this channel: later messages wait for this one
                    return sent, 1
                n.sent_at = datetime.now(timezone.utc)
                n.last_error = None
                s.commit()
                sent += 1
        return sent, 0
