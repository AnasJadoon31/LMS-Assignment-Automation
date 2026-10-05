"""Wires settings to the store, adapter, notifier and watcher."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import ConfigError, Settings
from .db import make_engine, make_sessionmaker
from .kv import DbKV, FileKV, KeyValue, SecretBox
from .lms.bahria import BahriaAdapter
from .lms.login_guard import LoginGuard
from .lms.session import SessionStore
from .notify import ConsoleNotifier, DiscordNotifier, EvolutionNotifier, Notifier, Outbox
from .watcher import Watcher


@dataclass
class Runtime:
    settings: Settings
    kv: KeyValue
    adapter: BahriaAdapter
    engine: Engine | None = None
    sessions: sessionmaker[Session] | None = None

    def outbox(self) -> Outbox:
        """Every configured channel gets every message; with none, they go to the log."""
        channels: dict[str, Notifier] = {}
        evo = self.settings.evolution
        if evo and evo.number:
            channels["whatsapp"] = EvolutionNotifier(evo)
        if self.settings.discord_webhook_url:
            channels["discord"] = DiscordNotifier(self.settings.discord_webhook_url)
        return Outbox(channels or {"console": ConsoleNotifier()})

    def watcher(self) -> Watcher:
        if self.sessions is None:
            raise ConfigError("Polling needs DATABASE_URL")
        return Watcher(self.adapter, self.sessions, self.kv, self.outbox(), self.settings.data_dir)


def build(settings: Settings, *, headless: bool = True, require_db: bool = False) -> Runtime:
    """With DATABASE_URL, state lives in PostgreSQL and the LMS session is encrypted.
    Without it, state lives in STATE_DIR files, for local spike commands."""
    if settings.database_url:
        if not settings.app_secret_key:
            raise ConfigError("APP_SECRET_KEY is required with DATABASE_URL; it encrypts the LMS session")
        engine = make_engine(settings.database_url)
        sessions = make_sessionmaker(engine)
        kv: KeyValue = DbKV(sessions)
        store = SessionStore(kv, SecretBox(settings.app_secret_key))
    elif require_db:
        raise ConfigError("This command needs DATABASE_URL")
    else:
        engine = sessions = None
        kv = FileKV(settings.state_dir)
        store = SessionStore(kv)
    adapter = BahriaAdapter(settings, store=store, guard=LoginGuard(kv), headless=headless)
    return Runtime(settings=settings, kv=kv, adapter=adapter, engine=engine, sessions=sessions)
