"""Worker configuration.

For the spike everything comes from environment variables (or worker/.env).
Once the web UI exists, credentials move to the encrypted settings table and
only the worker decrypts them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

CMS_LOGIN_URL = "https://cms.bahria.edu.pk/Logins/Student/Login.aspx"
LMS_DASHBOARD_URL = "https://lms.bahria.edu.pk/Student/Dashboard.php"


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
class Settings:
    urls: PortalUrls
    state_dir: Path
    credentials: CMSCredentials | None


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
    return Settings(
        urls=PortalUrls(
            cms_login=os.environ.get("CMS_LOGIN_URL", CMS_LOGIN_URL),
            lms_dashboard=os.environ.get("LMS_DASHBOARD_URL", LMS_DASHBOARD_URL),
        ),
        state_dir=Path(os.environ.get("STATE_DIR", "./state")),
        credentials=credentials,
    )
