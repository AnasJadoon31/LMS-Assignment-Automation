"""Stored LMS session and the per-poll validity check.

The LMS is PHP; its session is the PHPSESSID cookie on lms.bahria.edu.pk.
Without a valid one, the dashboard redirects to cms.bahria.edu.pk.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import PortalUrls
from ..kv import KeyValue, SecretBox

log = logging.getLogger(__name__)

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
    """The LMS session (a Playwright storage_state) in a key-value store, encrypted when a box is given."""

    KEY = "lms_session"

    def __init__(self, kv: KeyValue, box: SecretBox | None = None) -> None:
        self.kv = kv
        self.box = box

    def load(self) -> dict | None:
        """The stored session, or None when there is none or it no longer decrypts
        (APP_SECRET_KEY changed); either way the next poll logs in again."""
        raw = self.kv.get(self.KEY)
        if raw is None:
            return None
        if self.box:
            try:
                raw = self.box.decrypt(raw)
            except ValueError:
                log.warning("Stored LMS session cannot be decrypted; it will be replaced by a fresh login")
                return None
        return json.loads(raw)

    def save(self, state: dict) -> None:
        raw = json.dumps(state)
        self.kv.set(self.KEY, self.box.encrypt(raw) if self.box else raw)

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
        self.kv.delete(self.KEY)


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
