"""The api service: receives WhatsApp messages from Evolution API and answers through the bot.

POST /api/webhooks/evolution   Evolution posts every incoming message here. The request
                               must carry the shared secret header; only your own number
                               (saved on the setup page, or WHATSAPP_NUMBER) gets answers; a message delivered twice
                               is answered once. Replies go out after the response. A document
                               or picture you send is fetched from Evolution and handed to the
                               menu with the message.
GET  /api/health               last poll, for Coolify's health check
/setup                         the setup page: Claude sign-in and WhatsApp linking (web.py)

With a Discord bot token saved on the setup page, the same menu answers your direct messages
on Discord (discord_bot.py).

On start it points the Evolution instance's webhook at itself (API_INTERNAL_URL) with
the secret, retrying until Evolution is up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import base64
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .bot import Bot, Incoming, Reply
from .discord_bot import DiscordBotSetting, DiscordChat
from .claude_login import ClaudeLogin
from .gdrive import GoogleDrive
from .config import Settings
from .db.models import InboundMessage
from .kv import KeyValue, SecretBox
from .notify import DiscordSetting, EvolutionNotifier, Notifier, NotifyError, WhatsAppNumber
from .watcher import HEARTBEAT_KEY
from .dashboard import dashboard_router
from .web import Auth, setup_router
from .public_bot import Directory, PublicBot, PublicSender, addressed, parse as parse_public
from .db.models import BotUser
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
    materials=None,  # materials.Materials: lecture notes, plan and outline for the menu
    discord_bot: DiscordBotSetting | None = None,
    discord_chat: bool = True,  # connect to Discord when a bot token is saved
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
    if discord_bot is None and settings.app_secret_key:
        discord_bot = DiscordBotSetting(kv, SecretBox(settings.app_secret_key))
    bot = Bot(sessions, kv, login=login, setup_url=f"{settings.public_url}/setup" if settings.public_url else None,
              data_dir=settings.data_dir, materials=materials)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        if register_webhook and admin is not None:
            url = f"{internal_url.rstrip('/')}/api/webhooks/evolution"
            threading.Thread(target=_register_until_done, args=(admin, url, secret, stop), daemon=True).start()
        if discord_chat and discord_bot is not None:
            threading.Thread(target=DiscordChat(discord_bot, bot.handle).run_forever, args=(stop,), daemon=True).start()
        yield
        stop.set()

    from .db.models import utcnow
    from .groups import GroupNotifier, WhatsAppGroups, open_assignments, summary_text

    group_setting = WhatsAppGroups(kv)
    group_sender = GroupNotifier(settings.evolution, group_setting) if settings.evolution else None

    def group_summary() -> str:
        with sessions() as s:
            return summary_text(open_assignments(s, utcnow()), utcnow())

    public = PublicBot(sessions, settings.data_dir, materials=materials)
    directory = Directory(admin, group_setting) if admin is not None else None
    public_sender = PublicSender(settings.evolution) if settings.evolution else None

    async def _public(body: dict, background: BackgroundTasks) -> dict:
        m = parse_public(body)
        if m is None or directory is None or public_sender is None:
            return {"handled": False, "reason": "not a text message to the bot"}
        if not await run_in_threadpool(addressed, m, directory):
            return {"handled": False, "reason": "not for the bot"}
        if not _first_time(sessions, m.message_id):
            return {"handled": False, "reason": "duplicate"}
        replies = await run_in_threadpool(public.handle, m.chat, m.ids[0], m.name, m.text, group=m.group)
        background.add_task(public_sender.send, m, replies)
        return {"handled": True, "replies": len(replies), "public": True}

    def bot_users() -> list[dict]:
        names = {g.id: g.name for g in group_setting.get()}
        with sessions() as s:
            rows = s.scalars(select(BotUser).order_by(BotUser.last_at.desc()).limit(500)).all()
            return [{"name": u.name, "person": u.person, "where": "Private chat" if u.chat_id == "private" else names.get(u.chat_id, "A group"),
                     "requests": u.requests, "last_option": u.last_option, "last_at": u.last_at.isoformat()} for u in rows]

    app = FastAPI(title="LMS Assignment Automation", lifespan=lifespan, docs_url=None, redoc_url=None)
    auth = Auth(settings.admin_password, settings.app_secret_key or "", kv=kv)
    app.include_router(setup_router(password=settings.admin_password, secret_key=settings.app_secret_key or "",
                                    login=login, admin=admin, drive=drive, discord=discord,
                                    whatsapp=whatsapp, sender=sender, discord_bot=discord_bot,
                                    groups=group_setting, group_sender=group_sender, group_summary=group_summary,
                                    bot_users=bot_users,
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
        if message is None or message[0] != whatsapp.get():
            # not you: the public menu, for tagged messages in ticked groups and their members
            return await _public(body, background)
        number, message_id, text, media = message
        if not _first_time(sessions, message_id):
            return {"handled": False, "reason": "duplicate"}
        file = None
        if media is not None:
            try:
                data = base64.b64decode(media["inline"]) if media.get("inline") else await run_in_threadpool(admin.media, media["data"])
                file = Incoming(media["name"], data)
            except (WhatsAppError, ValueError, AttributeError) as e:
                log.warning("Could not fetch a file sent on WhatsApp: %s", e)
                if sender is not None:
                    background.add_task(_send, sender, [Reply("I could not fetch that file from WhatsApp; send it again as a document.")])
                return {"handled": True, "replies": 1}
        # In a worker thread: the menu reads the LMS and may draw the course plan with a browser
        replies = await run_in_threadpool(bot.handle, number, text, file)
        if sender is not None:
            background.add_task(_send, sender, replies)
        return {"handled": True, "replies": len(replies)}

    return app


MEDIA = {"documentMessage": None, "imageMessage": "image"}  # kinds of file the menu takes


def _media(msg: dict) -> tuple[dict, str] | None:
    """(the media part, a file name) of a message carrying a document or a picture."""
    inner = (msg.get("documentWithCaptionMessage") or {}).get("message") or msg
    for kind, stem in MEDIA.items():
        part = inner.get(kind)
        if isinstance(part, dict):
            ext = (part.get("mimetype") or "").split(";")[0].split("/")[-1] or "bin"
            return part, part.get("fileName") or part.get("title") or f"{stem or 'file'}.{'jpg' if ext == 'jpeg' else ext}"
    return None


def _incoming(body: dict) -> tuple[str, str, str, dict | None] | None:
    """(sender number, message id, text, file) of an incoming chat message, else None. The file
    is {name, data: the webhook's data, inline: its base64 when Evolution sent it along}."""
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
    media = None
    found = _media(msg)
    if found:
        part, name = found
        text = text or part.get("caption") or ""
        media = {"name": Path(name).name, "data": data, "inline": msg.get("base64") or data.get("base64")}
    number = jid.split("@")[0].split(":")[0]
    return number, str(key.get("id") or ""), text, media


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

