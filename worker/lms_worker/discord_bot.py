"""Chatting with the bot on Discord: a Discord bot answers your direct messages with the menu.

The webhook (notify.py) only posts into a channel. To answer you, Discord needs a bot: you make
one in the Discord developer portal and paste its token on the setup page (kept encrypted).
The page then shows a 6-digit code; the first person who sends that code to the bot in a
direct message becomes its owner, and it answers nobody else. Files work both ways: lectures
and reports come as attachments, and a file you attach is taken like one sent on WhatsApp.

It connects to Discord's gateway (a websocket) for incoming direct messages, which Discord
gives a bot without any privileged intent, and answers over the REST API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
from pathlib import Path
from typing import Callable

import httpx

from .bot import Incoming, Reply
from .files import MAX_BYTES
from .kv import KeyValue, SecretBox

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
KEY = "discord_bot"
DIRECT_MESSAGES = 1 << 12
MAX_TEXT = 1900
TOKEN = re.compile(r"[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}")
RETRY_SECONDS = 15

Handle = Callable[[str, str, Incoming | None], list[Reply]]


class DiscordBotError(Exception):
    pass


class DiscordBotSetting:
    """The bot's token (encrypted), the pairing code and the one Discord user it answers."""

    def __init__(self, kv: KeyValue, box: SecretBox, *, transport: httpx.BaseTransport | None = None) -> None:
        self.kv, self.box, self.transport = kv, box, transport

    def _stored(self) -> dict:
        raw = self.kv.get(KEY)
        return json.loads(raw) if raw else {}

    def token(self) -> str | None:
        stored = self._stored()
        try:
            return self.box.decrypt(stored["token"]) if stored.get("token") else None
        except ValueError:
            return None

    def owner(self) -> str | None:
        return self._stored().get("owner")

    def status(self) -> dict:
        st = self._stored()
        return {"on": bool(st.get("token")), "name": st.get("name"), "linked": bool(st.get("owner")),
                "owner": st.get("owner_name"), "code": None if st.get("owner") else st.get("code"), "error": st.get("error")}

    def save(self, token: str) -> None:
        token = token.strip().removeprefix("Bot ").strip()
        if not TOKEN.fullmatch(token):
            raise DiscordBotError("That is not a Discord bot token (Developer Portal, your application, Bot, Reset Token)")
        try:
            with httpx.Client(timeout=30, transport=self.transport) as client:
                r = client.get(f"{API}/users/@me", headers={"Authorization": f"Bot {token}"})
        except httpx.HTTPError as e:
            raise DiscordBotError(f"Discord unreachable: {e}") from e
        if r.status_code == 401:
            raise DiscordBotError("Discord rejected that token")
        if r.status_code >= 300:
            raise DiscordBotError(f"Discord answered {r.status_code}: {r.text[:200]}")
        self.kv.set(KEY, json.dumps({"token": self.box.encrypt(token), "name": r.json().get("username"),
                                     "code": f"{secrets.randbelow(10**6):06d}"}))

    def off(self) -> None:
        self.kv.delete(KEY)

    def pair(self, user_id: str, user_name: str, text: str) -> bool:
        """The first direct message carrying the code makes its sender the owner."""
        st = self._stored()
        if st.get("owner") or not st.get("code") or not secrets.compare_digest(text.strip(), st["code"]):
            return False
        st.update(owner=user_id, owner_name=user_name)
        st.pop("code")
        self.kv.set(KEY, json.dumps(st))
        return True

    def note_error(self, error: str | None) -> None:
        st = self._stored()
        if st and st.get("error") != error:
            st["error"] = error
            self.kv.set(KEY, json.dumps(st))


def chunks(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Discord takes 2000 characters a message: split on line ends."""
    out, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out, current, line = out + [current] * bool(current) + [line[:limit]], "", line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            out, current = out + [current], line
        else:
            current = f"{current}\n{line}" if current else line
    return out + [current] * bool(current)


def discord_text(text: str) -> str:
    """The menu's *title* is bold on WhatsApp; Discord wants **title**."""
    return re.sub(r"(?m)^\*([^*\n]+)\*$", r"**\1**", text)


class DiscordChat:
    """Receives your direct messages and answers them through `handle` (the menu)."""

    def __init__(self, setting: DiscordBotSetting, handle: Handle, *, transport: httpx.BaseTransport | None = None) -> None:
        self.setting, self.handle = setting, handle
        self.http = httpx.Client(timeout=60, transport=transport)

    # --- answering ---

    def on_message(self, token: str, d: dict) -> None:
        author = d.get("author") or {}
        if d.get("guild_id") or author.get("bot"):
            return  # direct messages from people only
        channel, user, text = d["channel_id"], str(author.get("id")), d.get("content") or ""
        owner = self.setting.owner()
        if owner is None:
            if self.setting.pair(user, author.get("global_name") or author.get("username") or user, text):
                self.send(token, channel, Reply("Linked: this chat now works like the WhatsApp one."))
                text = "menu"
            else:
                self.send(token, channel, Reply("Send the 6-digit code shown on the setup page (Discord chat) to link this chat."))
                return
        elif user != owner:
            return
        file = None
        for att in (d.get("attachments") or [])[:1]:
            if int(att.get("size") or 0) > MAX_BYTES:
                self.send(token, channel, Reply(f"{att.get('filename')} is over {MAX_BYTES // 1048576} MB; nothing was saved."))
                return
            try:
                r = self.http.get(att["url"])
                r.raise_for_status()
            except httpx.HTTPError as e:
                self.send(token, channel, Reply(f"Could not fetch {att.get('filename')} from Discord: {e}"))
                return
            file = Incoming(att.get("filename") or "file", r.content)
        for reply in self.handle(f"discord:{user}", text, file):
            self.send(token, channel, reply)

    def send(self, token: str, channel: str, reply: Reply) -> None:
        url, headers = f"{API}/channels/{channel}/messages", {"Authorization": f"Bot {token}"}
        parts = chunks(discord_text(reply.text)) or [""]
        try:
            for part in parts[:-1] if reply.file else parts:
                self._post(url, headers, json={"content": part, "allowed_mentions": {"parse": []}})
            if reply.file:
                payload = {"content": parts[-1], "allowed_mentions": {"parse": []}, "attachments": [{"id": 0, "filename": reply.file.name}]}
                r = self._post(url, headers, data={"payload_json": json.dumps(payload)},
                               files={"files[0]": (reply.file.name, reply.file.read_bytes())}, ok=(413,))
                if r.status_code == 413 or (r.status_code == 400 and "40005" in r.text):
                    size = reply.file.stat().st_size / 1048576
                    self._post(url, headers, json={"content": f"{parts[-1]}\n({reply.file.name} is {size:.1f} MB, over Discord's "
                                                              "limit; get it on WhatsApp or the web page.)"})
        except (httpx.HTTPError, OSError) as e:
            log.warning("Could not send a Discord reply: %s", e)

    def _post(self, url: str, headers: dict, ok: tuple[int, ...] = (), **kw) -> httpx.Response:
        for attempt in (1, 2):
            r = self.http.post(url, headers=headers, **kw)
            if r.status_code == 429 and attempt == 1:
                try:
                    wait = min(float(r.json().get("retry_after", 1)), 5.0)
                except ValueError:
                    wait = 1.0
                threading.Event().wait(wait)
                continue
            break
        if r.status_code >= 300 and r.status_code not in ok and not (r.status_code == 400 and "40005" in r.text):
            log.warning("Discord answered %s: %s", r.status_code, r.text[:200])
        return r

    # --- the gateway ---

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            token = self.setting.token()
            if token:
                try:
                    asyncio.run(self._session(token, stop))
                except Exception as e:  # a dropped connection is normal: connect again
                    log.info("Discord gateway connection ended: %s", e)
            stop.wait(RETRY_SECONDS)

    async def _session(self, token: str, stop: threading.Event) -> None:
        import websockets

        seq: list[int | None] = [None]
        async with websockets.connect(GATEWAY, max_size=2**22) as ws:
            hello = json.loads(await ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            await ws.send(json.dumps({"op": 2, "d": {"token": token, "intents": DIRECT_MESSAGES,
                                                      "properties": {"os": "linux", "browser": "lms-bot", "device": "lms-bot"}}}))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    if stop.is_set() or self.setting.token() != token:  # stopped, or the token was replaced
                        await ws.close()
                        return
                    await ws.send(json.dumps({"op": 1, "d": seq[0]}))

            beat = asyncio.create_task(heartbeat())
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    seq[0] = msg.get("s") or seq[0]
                    if msg["op"] == 0 and msg["t"] == "READY":
                        self.setting.note_error(None)
                        log.info("Discord bot connected as %s", (msg["d"].get("user") or {}).get("username"))
                    elif msg["op"] == 0 and msg["t"] == "MESSAGE_CREATE":
                        asyncio.get_running_loop().run_in_executor(None, self._safely, token, msg["d"])
                    elif msg["op"] in (7, 9):  # Discord asks for a fresh connection
                        return
            except websockets.ConnectionClosed as e:
                if e.rcvd is not None and e.rcvd.code == 4004:
                    self.setting.note_error("Discord rejected the bot token; paste a new one")
                elif e.rcvd is not None and e.rcvd.code in (4013, 4014):
                    self.setting.note_error("Discord refused the bot's intents")
                raise
            finally:
                beat.cancel()

    def _safely(self, token: str, d: dict) -> None:
        try:
            self.on_message(token, d)
        except Exception:
            log.exception("A Discord message could not be answered")
