"""Link the bot's WhatsApp account to Evolution API, by QR code or by phone-number pairing code.

The app owns the Evolution instance: it creates it on first use, starts the link, and
reports its state. The CLI uses this now; the web UI's WhatsApp page will call the same
functions. Evolution's own manager page never needs to be exposed.

Evolution v2 endpoints used (all with the global `apikey` header from EVOLUTION_API_KEY):
  POST   /instance/create                       {instanceName, integration: WHATSAPP-BAILEYS, qrcode: false}
  GET    /instance/connectionState/{instance}   -> {instance: {state: open | connecting | close}}
  GET    /instance/connect/{instance}[?number=] -> {code, base64, pairingCode, count}
  DELETE /instance/logout/{instance}
  GET    /instance/fetchInstances?instanceName=
  POST   /webhook/set/{instance}                {webhook: {enabled, url, headers, events: [MESSAGES_UPSERT]}}
A pairing code is only issued when a connection starts with a number, so a pending QR
session is logged out first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import EvolutionConfig


POLL_SECONDS = 2


class WhatsAppError(Exception):
    pass


@dataclass(frozen=True)
class LinkState:
    state: str  # open, connecting, close, missing
    number: str | None = None  # the linked bot number, once open
    name: str | None = None  # its WhatsApp profile name

    @property
    def linked(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True)
class LinkChallenge:
    """What to show the person linking: a QR code, or an 8-character code to type in WhatsApp."""

    qr_text: str | None = None  # raw QR content, for drawing it in a terminal
    qr_png_base64: str | None = None  # data URL from Evolution, for an <img> in the web UI
    pairing_code: str | None = None
    already_linked: bool = False

    @property
    def pairing_code_display(self) -> str | None:
        c = self.pairing_code
        return f"{c[:4]}-{c[4:]}" if c and len(c) == 8 else c


class EvolutionAdmin:
    def __init__(
        self,
        cfg: EvolutionConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.sleep = sleep
        self.client = httpx.Client(base_url=cfg.base_url, headers={"apikey": cfg.api_key}, timeout=30, transport=transport)

    # --- state ---

    def state(self) -> LinkState:
        r = self._request("GET", f"/instance/connectionState/{self.cfg.instance}", missing_ok=True)
        if r is None:
            return LinkState("missing")
        state = (r.json().get("instance") or {}).get("state") or "close"
        if state != "open":
            return LinkState(state)
        info = self._instance_info()
        owner = (info.get("ownerJid") or "").split("@")[0].split(":")[0] or None
        return LinkState("open", number=owner, name=info.get("profileName"))

    def ensure_instance(self) -> None:
        if self.state().state == "missing":
            self._request(
                "POST",
                "/instance/create",
                json={"instanceName": self.cfg.instance, "integration": "WHATSAPP-BAILEYS", "qrcode": False},
            )

    # --- linking ---

    def start_link(self, number: str | None = None, *, wait_seconds: float = 20) -> LinkChallenge:
        """Begin linking. With a number, returns a pairing code; without, a QR code."""
        number = _digits(number) if number else None
        if number is not None and not 10 <= len(number) <= 15:
            raise WhatsAppError("Give the bot's number with its country code, e.g. 923001234567")
        self.ensure_instance()
        current = self.state()
        if current.linked:
            return LinkChallenge(already_linked=True)
        if current.state == "connecting" and number:
            # A QR session is pending; a pairing code needs a fresh start with the number
            self._request("DELETE", f"/instance/logout/{self.cfg.instance}", missing_ok=True)

        params = {"number": number} if number else None
        for _ in range(max(1, int(wait_seconds / POLL_SECONDS))):
            r = self._request("GET", f"/instance/connect/{self.cfg.instance}", params=params)
            challenge = _challenge(r.json(), want_pairing=bool(number))
            if challenge is not None:
                return challenge
            self.sleep(POLL_SECONDS)
            params = None  # the connection is starting; later calls just read its code
        raise WhatsAppError("Evolution API did not produce a code in time; try again")

    def current_challenge(self) -> LinkChallenge | None:
        """The latest QR or pairing code of a link in progress (QR codes rotate)."""
        r = self._request("GET", f"/instance/connect/{self.cfg.instance}")
        return _challenge(r.json(), want_pairing=False)

    def set_webhook(self, url: str, headers: dict[str, str]) -> None:
        """Have Evolution post incoming messages to the app, with a secret header."""
        self._request(
            "POST",
            f"/webhook/set/{self.cfg.instance}",
            json={
                "webhook": {
                    "enabled": True,
                    "url": url,
                    "headers": headers,
                    "byEvents": False,
                    "base64": False,
                    "events": ["MESSAGES_UPSERT"],
                }
            },
        )

    def unlink(self) -> None:
        if self.state().state in ("open", "connecting"):
            self._request("DELETE", f"/instance/logout/{self.cfg.instance}")

    # --- http ---

    def _instance_info(self) -> dict:
        r = self._request("GET", "/instance/fetchInstances", params={"instanceName": self.cfg.instance})
        items = r.json()
        items = items if isinstance(items, list) else [items]
        for item in items:
            item = item.get("instance", item) if isinstance(item, dict) else {}
            if item.get("name", item.get("instanceName")) == self.cfg.instance:
                return item
        return {}

    def _request(self, method: str, path: str, *, missing_ok: bool = False, **kw) -> httpx.Response | None:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise WhatsAppError(f"Cannot reach Evolution API at {self.cfg.base_url}: {e}") from e
        if missing_ok and (r.status_code == 404 or (r.status_code == 400 and "does not exist" in r.text)):
            return None
        if r.status_code == 401:
            raise WhatsAppError("Evolution API rejected EVOLUTION_API_KEY")
        if r.status_code >= 300:
            raise WhatsAppError(f"Evolution API {method} {path} returned {r.status_code}: {r.text[:300]}")
        return r


def _challenge(body: dict, *, want_pairing: bool) -> LinkChallenge | None:
    if (body.get("instance") or {}).get("state") == "open":
        return LinkChallenge(already_linked=True)
    qr = body.get("qrcode") if isinstance(body.get("qrcode"), dict) else body
    pairing, code, png = qr.get("pairingCode"), qr.get("code"), qr.get("base64")
    if want_pairing:
        return LinkChallenge(pairing_code=pairing, qr_text=code, qr_png_base64=png) if pairing else None
    return LinkChallenge(qr_text=code, qr_png_base64=png, pairing_code=pairing) if code else None


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())
