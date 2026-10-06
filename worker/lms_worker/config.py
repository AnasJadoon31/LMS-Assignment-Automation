"""Worker configuration, from environment variables (Coolify secrets) or worker/.env.

CMS credentials come from the environment until the web UI exists; then they move
to the encrypted settings table and only the worker decrypts them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

CMS_LOGIN_URL = "https://cms.bahria.edu.pk/Logins/Student/Login.aspx"
LMS_DASHBOARD_URL = "https://lms.bahria.edu.pk/Student/Dashboard.php"


class ConfigError(Exception):
    pass


EVOLUTION_VARS = ("EVOLUTION_API_URL", "EVOLUTION_API_KEY", "EVOLUTION_INSTANCE", "WHATSAPP_NUMBER")


@dataclass(frozen=True)
class CMSCredentials:
    """The four fields on the CMS student login form."""

    enrollment: str
    password: str = field(repr=False)
    institute: str = "Karachi Campus"
    role: str = "Student"


@dataclass(frozen=True)
class PortalUrls:
    cms_login: str = CMS_LOGIN_URL
    lms_dashboard: str = LMS_DASHBOARD_URL

    @property
    def cms_host(self) -> str:
        return urlsplit(self.cms_login).hostname or ""

    @property
    def lms_host(self) -> str:
        return urlsplit(self.lms_dashboard).hostname or ""


@dataclass(frozen=True)
class EvolutionConfig:
    """WhatsApp through Evolution API. URL and key come from the environment; the app
    creates and links the instance itself."""

    base_url: str
    api_key: str = field(repr=False)
    instance: str = "lms"
    number: str | None = None  # where messages go: your own number, digits with country code


@dataclass(frozen=True)
class Settings:
    urls: PortalUrls
    state_dir: Path
    credentials: CMSCredentials | None
    database_url: str | None = None
    app_secret_key: str | None = field(default=None, repr=False)
    data_dir: Path = Path("./data")
    evolution: EvolutionConfig | None = None
    discord_webhook_url: str | None = field(default=None, repr=False)


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file)
    enrollment = os.environ.get("CMS_ENROLLMENT", "").strip()
    password = os.environ.get("CMS_PASSWORD", "")
    credentials = None
    if enrollment and password:
        credentials = CMSCredentials(
            enrollment=enrollment,
            password=password,
            institute=os.environ.get("CMS_INSTITUTE", "Karachi Campus").strip(),
            role=os.environ.get("CMS_ROLE", "Student").strip(),
        )
    evolution = None
    evo = {k: os.environ.get(k, "").strip() for k in EVOLUTION_VARS}
    if evo["EVOLUTION_API_URL"] and evo["EVOLUTION_API_KEY"]:
        evolution = EvolutionConfig(
            base_url=evo["EVOLUTION_API_URL"].rstrip("/"),
            api_key=evo["EVOLUTION_API_KEY"],
            instance=evo["EVOLUTION_INSTANCE"] or "lms",
            number="".join(ch for ch in evo["WHATSAPP_NUMBER"] if ch.isdigit()) or None,
        )
    discord = os.environ.get("DISCORD_WEBHOOK_URL", "").strip() or None
    if discord and not re.match(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$", discord):
        raise ConfigError("DISCORD_WEBHOOK_URL must be a Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>)")
    return Settings(
        urls=PortalUrls(
            cms_login=os.environ.get("CMS_LOGIN_URL", CMS_LOGIN_URL),
            lms_dashboard=os.environ.get("LMS_DASHBOARD_URL", LMS_DASHBOARD_URL),
        ),
        state_dir=Path(os.environ.get("STATE_DIR", "./state")),
        credentials=credentials,
        database_url=os.environ.get("DATABASE_URL") or None,
        app_secret_key=os.environ.get("APP_SECRET_KEY") or None,
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        evolution=evolution,
        discord_webhook_url=discord,
    )
