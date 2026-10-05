"""Stored LMS session and the per-poll validity check.

The LMS is PHP; its session is the PHPSESSID cookie on lms.bahria.edu.pk.
Without a valid one, the dashboard redirects to cms.bahria.edu.pk.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import PortalUrls

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


class Throttle:
    """At most one request per min_interval seconds. The portal is shared."""

    def __init__(
        self,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                now += remaining
        self._last = now


class SessionStore:
    """Playwright storage_state saved to disk with owner-only permissions.

    Moves to the encrypted settings table once the database exists.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text())

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.path)

    def save_pasted(self, phpsessid: str, urls: PortalUrls) -> dict:
        """Store a PHPSESSID copied from your own browser's LMS tab."""
        value = phpsessid.strip()
        if value.upper().startswith("PHPSESSID="):
            value = value.split("=", 1)[1]
        state = {
            "cookies": [
                {
                    "name": "PHPSESSID",
                    "value": value,
                    "domain": urls.lms_host,
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": urls.lms_dashboard.startswith("https://"),
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }
        self.save(state)
        return state

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def lms_cookies(state: dict | None, urls: PortalUrls) -> dict[str, str]:
    host = urls.lms_host
    cookies = {}
    for c in (state or {}).get("cookies", []):
        domain = c.get("domain", "").lstrip(".")
        if host == domain or host.endswith("." + domain):
            cookies[c["name"]] = c["value"]
    return cookies


def session_is_valid(
    state: dict | None,
    urls: PortalUrls,
    *,
    throttle: Throttle | None = None,
    max_hops: int = 5,
) -> bool:
    """True when the LMS dashboard loads with this session instead of bouncing to CMS.

    Redirects are followed only while they stay on the LMS host, so an
    expired session costs one request and never touches CMS.
    """
    cookies = lms_cookies(state, urls)
    if not cookies:
        return False
    url = urls.lms_dashboard
    with httpx.Client(cookies=cookies, headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        for _ in range(max_hops):
            if throttle:
                throttle.wait()
            r = client.get(url, follow_redirects=False)
            if not r.is_redirect:
                return r.status_code == 200
            url = urljoin(url, r.headers.get("location", ""))
            if urlsplit(url).hostname != urls.lms_host:
                return False
    return False
