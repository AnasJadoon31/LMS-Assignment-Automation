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


DISCORD_WEBHOOK = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+")
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
class SolverConfig:
    """Solving in throwaway sandbox containers. On when SOLVER_IMAGE is set."""

    image: str
    network: str | None = "lms-sandbox"
    proxy: str | None = "http://egress:3128"  # the sandbox's only way out
    docker_host: str | None = None  # the socket proxy; the local Docker socket without it
    login_volume: str | None = None  # found from the worker's own mounts when not set
    model: str | None = None  # the CLI's default (your plan's) when not set
    daily_cap: int = 6
    pause_at: float = 0.8


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
    admin_password: str | None = field(default=None, repr=False)  # the setup page's password
    claude_home: Path = Path("/claude-home")  # the claude.ai login the solver runs under
    public_url: str | None = None  # where the setup page is reachable, for links in alerts
    solver: SolverConfig | None = None
    google_client_id: str | None = None  # Google Drive saving: an OAuth client of type Web application
    google_client_secret: str | None = field(default=None, repr=False)


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
    if discord and not DISCORD_WEBHOOK.fullmatch(discord):
        raise ConfigError("DISCORD_WEBHOOK_URL must be a Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>)")
    solver = None
    if os.environ.get("SOLVER_IMAGE", "").strip():
        try:
            daily_cap = int(os.environ.get("SOLVER_DAILY_CAP") or 6)
            pause_at = float(os.environ.get("SOLVER_PAUSE_AT") or 0.8)
        except ValueError as e:
            raise ConfigError(f"SOLVER_DAILY_CAP and SOLVER_PAUSE_AT must be numbers: {e}") from e
        if not 0 < pause_at <= 1:
            raise ConfigError("SOLVER_PAUSE_AT is a fraction of the plan's limit, e.g. 0.8")
        solver = SolverConfig(
            image=os.environ["SOLVER_IMAGE"].strip(),
            network=os.environ.get("SOLVER_NETWORK", "lms-sandbox").strip() or None,
            proxy=os.environ.get("SOLVER_PROXY", "http://egress:3128").strip() or None,
            docker_host=os.environ.get("DOCKER_HOST", "").strip() or None,
            login_volume=os.environ.get("SOLVER_LOGIN_VOLUME", "").strip() or None,
            model=os.environ.get("SOLVER_MODEL", "").strip() or None,
            daily_cap=daily_cap,
            pause_at=pause_at,
        )
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
        admin_password=os.environ.get("ADMIN_PASSWORD") or None,
        claude_home=Path(os.environ.get("CLAUDE_HOME", "/claude-home")),
        public_url=(os.environ.get("PUBLIC_URL") or "").rstrip("/") or None,
        solver=solver,
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip() or None,
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() or None,
    )
