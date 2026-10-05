"""Notifications: WhatsApp through Evolution API and/or a Discord webhook, behind an outbox.

Each change writes one row per configured channel in the same transaction; flush()
sends pending rows afterwards. Channels deliver independently: one being down never
holds the other back, and a retry never re-sends to a channel that already got it.

Discord webhooks are one-way, so approvals and the reply menu stay on WhatsApp and
the web UI; Discord gets the same alerts and files.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import EvolutionConfig
from .db.models import Notification

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 20


class NotifyError(Exception):
    pass


class Notifier(Protocol):
    def text(self, message: str) -> None: ...
    def document(self, path: Path, caption: str) -> None: ...


class EvolutionNotifier:
    """POST /message/sendText/{instance} and /message/sendMedia/{instance}, authenticated by the apikey header."""

    def __init__(self, cfg: EvolutionConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        if not cfg.number:
            raise ValueError("WhatsApp messages need WHATSAPP_NUMBER")
        self.cfg = cfg
        self.client = httpx.Client(
            base_url=cfg.base_url,
            headers={"apikey": cfg.api_key},
            timeout=60,
            transport=transport,
        )

    def text(self, message: str) -> None:
        self._post("sendText", {"number": self.cfg.number, "text": message})

    def document(self, path: Path, caption: str) -> None:
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._post(
            "sendMedia",
            {
                "number": self.cfg.number,
                "mediatype": "document",
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


class ConsoleNotifier:
    """Used when no channel is configured: messages go to the log."""

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
        for channel in self.channels:
            session.add(
                Notification(channel=channel, kind=kind, text=text, file_path=str(file) if file else None, job_id=job_id)
            )

    def flush(self, sessions: sessionmaker[Session]) -> tuple[int, int]:
        """Send pending messages oldest first on each channel. Returns (sent, failed)."""
        sent = failed = 0
        for channel, notifier in self.channels.items():
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
