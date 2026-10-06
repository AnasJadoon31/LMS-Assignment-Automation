"""The api service: receives WhatsApp messages from Evolution API and answers through the bot.

POST /api/webhooks/evolution   Evolution posts every incoming message here. The request
                               must carry the shared secret header; only your own number
                               (saved on the setup page, or WHATSAPP_NUMBER) gets answers; a message delivered twice
                               is answered once. Replies go out after the response.
GET  /api/health               last poll, for Coolify's health check
/setup                         the setup page: Claude sign-in and WhatsApp linking (web.py)

On start it points the Evolution instance's webhook at itself (API_INTERNAL_URL) with
the secret, retrying until Evolution is up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import shutil
import threading
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .bot import Bot, Reply
from .claude_login import ClaudeLogin
from .gdrive import GoogleDrive
from .config import Settings
from .db.models import InboundMessage
from .kv import KeyValue, SecretBox
from .notify import DiscordSetting, EvolutionNotifier, Notifier, NotifyError, WhatsAppNumber
from .watcher import HEARTBEAT_KEY
from .dashboard import dashboard_router
from .web import Auth, setup_router
from .whatsapp import EvolutionAdmin, WhatsAppError

log = logging.getLogger(__name__)

SECRET_HEADER = "x-webhook-secret"
DEFAULT_INTERNAL_URL = "http://api:8000"
REGISTER_RETRY_SECONDS = 30


def webhook_secret(app_secret_key: str) -> str:
    """Derived from APP_SECRET_KEY, so there is no extra secret to manage."""
    return hmac.new(app_secret_key.encode(), b"evolution-webhook", hashlib.sha256).hexdigest()


def create_app(
    settings: Settings,
    sessions: sessionmaker[Session],
    kv: KeyValue,
    *,
    sender: Notifier | None = None,
    admin: EvolutionAdmin | None = None,
    internal_url: str = DEFAULT_INTERNAL_URL,
    register_webhook: bool = True,
    login: ClaudeLogin | None = None,
    drive: GoogleDrive | None = None,
    discord: DiscordSetting | None = None,
    whatsapp: WhatsAppNumber | None = None,
) -> FastAPI:
    secret = webhook_secret(settings.app_secret_key or "")
    if whatsapp is None:
        whatsapp = WhatsAppNumber(kv, settings.evolution.number if settings.evolution else None)
    if sender is None and settings.evolution:
        sender = EvolutionNotifier(settings.evolution, number=whatsapp.get)
    if admin is None and settings.evolution:
        admin = EvolutionAdmin(settings.evolution)
    if login is None and shutil.which("claude"):
        login = ClaudeLogin(settings.claude_home)
    if drive is None and settings.google_client_id and settings.google_client_secret and settings.app_secret_key:
        drive = GoogleDrive(settings.google_client_id, settings.google_client_secret, kv, SecretBox(settings.app_secret_key))
    if discord is None:
        box = SecretBox(settings.app_secret_key) if settings.app_secret_key else None
        discord = DiscordSetting(kv if box else None, box, settings.discord_webhook_url)
    bot = Bot(sessions, kv, login=login, setup_url=f"{settings.public_url}/setup" if settings.public_url else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        if register_webhook and admin is not None:
            url = f"{internal_url.rstrip('/')}/api/webhooks/evolution"
            threading.Thread(target=_register_until_done, args=(admin, url, secret, stop), daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="LMS Assignment Automation", lifespan=lifespan, docs_url=None, redoc_url=None)
    auth = Auth(settings.admin_password, settings.app_secret_key or "")
    app.include_router(setup_router(password=settings.admin_password, secret_key=settings.app_secret_key or "",
                                    login=login, admin=admin, drive=drive, discord=discord,
                                    whatsapp=whatsapp, sender=sender,
                                    public_url=settings.public_url, auth=auth))
    app.include_router(dashboard_router(auth, sessions, kv))

    @app.get("/api/health")
    def health() -> dict:
        beat = kv.get(HEARTBEAT_KEY)
        last = json.loads(beat) if beat else {}
        return {"ok": True, "last_poll": last.get("at"), "last_status": last.get("status")}

    @app.post("/api/webhooks/evolution")
    async def evolution(
        request: Request,
        background: BackgroundTasks,
        x_webhook_secret: str | None = Header(default=None),
    ) -> dict:
        if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, secret):
            raise HTTPException(status_code=401, detail="bad secret")
        body = await request.json()
        message = _incoming(body)
        if message is None:
            return {"handled": False, "reason": "not a text message to the bot"}
        number, message_id, text = message
        if number != whatsapp.get():
            log.info("Ignored a WhatsApp message from a number that is not the one messages go to")
            return {"handled": False, "reason": "sender"}
        if not _first_time(sessions, message_id):
            return {"handled": False, "reason": "duplicate"}
        replies = bot.handle(number, text)
        if sender is not None:
            background.add_task(_send, sender, replies)
        return {"handled": True, "replies": len(replies)}

    return app


def _incoming(body: dict) -> tuple[str, str, str] | None:
    """(sender number, message id, text) of an incoming chat message, else None."""
    event = str(body.get("event", "")).lower().replace("_", ".")
    if event != "messages.upsert":
        return None
    data = body.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None
    jid = next(
        (j for j in (key.get("remoteJid"), key.get("remoteJidAlt"), key.get("senderPn")) if j and j.endswith("@s.whatsapp.net")),
        None,
    )
    if jid is None:  # groups, broadcasts, unresolved anonymous ids
        return None
    msg = data.get("message") or {}
    text = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text") or ""
    number = jid.split("@")[0].split(":")[0]
    return number, str(key.get("id") or ""), text


def _first_time(sessions: sessionmaker[Session], message_id: str) -> bool:
    if not message_id:
        return True
    try:
        with sessions.begin() as s:
            s.add(InboundMessage(message_id=message_id))
        return True
    except IntegrityError:
        return False


def _send(sender: Notifier, replies: list[Reply]) -> None:
    for r in replies:
        try:
            if r.file is not None:
                sender.document(r.file, r.text)
            else:
                sender.text(r.text)
        except (NotifyError, OSError) as e:
            log.warning("Could not send a WhatsApp reply: %s", e)
            return


def _register_until_done(admin: EvolutionAdmin, url: str, secret: str, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            admin.ensure_instance()
            admin.set_webhook(url, {SECRET_HEADER: secret})
            log.info("Evolution webhook points at %s", url)
            return
        except WhatsAppError as e:
            log.warning("Evolution webhook not set yet (%s); retrying in %ss", e, REGISTER_RETRY_SECONDS)
        stop.wait(REGISTER_RETRY_SECONDS)


def serve(app: FastAPI, host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)

