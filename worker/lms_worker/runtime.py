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
from .notify import (ConsoleNotifier, DiscordSetting, EvolutionNotifier, LiveDiscord, Notifier, Outbox,
                     WhatsAppNumber, active)
from .watcher import Watcher


@dataclass
class Runtime:
    settings: Settings
    kv: KeyValue
    adapter: BahriaAdapter
    engine: Engine | None = None
    sessions: sessionmaker[Session] | None = None

    def discord(self) -> DiscordSetting:
        st = self.settings
        box = SecretBox(st.app_secret_key) if st.app_secret_key and self.sessions is not None else None
        return DiscordSetting(self.kv if box else None, box, st.discord_webhook_url)

    def whatsapp_number(self) -> WhatsAppNumber:
        evo = self.settings.evolution
        return WhatsAppNumber(self.kv, evo.number if evo else None)

    def outbox(self) -> Outbox:
        """Every channel that is on gets every message; with none on, they go to the log."""
        channels: dict[str, Notifier] = {}
        evo = self.settings.evolution
        if evo:
            channels["whatsapp"] = EvolutionNotifier(evo, number=self.whatsapp_number().get)
        channels["discord"] = LiveDiscord(self.discord())
        others = dict(channels)
        channels["console"] = ConsoleNotifier(when=lambda: not any(active(n) for n in others.values()))
        return Outbox(channels)

    def drive(self):
        """Google Drive saving, when an OAuth client is set; it still needs connecting on the setup page."""
        st = self.settings
        if not (st.google_client_id and st.google_client_secret and st.app_secret_key):
            return None
        from .gdrive import GoogleDrive

        return GoogleDrive(st.google_client_id, st.google_client_secret, self.kv, SecretBox(st.app_secret_key))

    def solver(self):
        """The solver, when SOLVER_IMAGE is set; None otherwise."""
        cfg = self.settings.solver
        if cfg is None:
            return None
        if self.sessions is None:
            raise ConfigError("Solving needs DATABASE_URL")
        from .claude_login import ClaudeLogin
        from .solver.pipeline import Solver
        from .solver.sandbox import DockerSandbox
        from .solver.usage import UsageGuard

        home = str(self.settings.claude_home)
        sandbox = DockerSandbox.deployed(cfg.image, cfg.network, cfg.login_volume, home, docker_host=cfg.docker_host)
        env = {"HTTPS_PROXY": cfg.proxy, "https_proxy": cfg.proxy} if cfg.proxy else {}
        return Solver(
            self.sessions, self.kv, self.outbox(), sandbox, self.settings.data_dir,
            guard=UsageGuard(self.kv, pause_at=cfg.pause_at, daily_cap=cfg.daily_cap),
            login=ClaudeLogin(self.settings.claude_home), drive=self.drive(),
            public_url=self.settings.public_url, model=cfg.model, env=env,
        )

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
