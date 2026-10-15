import json

import httpx
import pytest

from lms_worker.bot import Reply
from lms_worker.discord_bot import DiscordBotError, DiscordBotSetting, DiscordChat, chunks, discord_text
from lms_worker.kv import FileKV, SecretBox

TOKEN = "M" * 26 + "." + "abcdef" + "." + "x" * 30


class FakeDiscord:
    def __init__(self):
        self.posts, self.reject = [], False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/@me"):
            if self.reject or request.headers["authorization"] != f"Bot {TOKEN}":
                return httpx.Response(401, json={"message": "401: Unauthorized"})
            return httpx.Response(200, json={"id": "1", "username": "lms-bot"})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"%PDF mine")
        body = request.content
        if request.headers["content-type"].startswith("application/json"):
            self.posts.append((request.url.path, json.loads(body)["content"], None))
        else:
            self.posts.append((request.url.path, "multipart", b'filename="notes.pdf"' in body))
        return httpx.Response(200, json={"id": "9"})


@pytest.fixture
def discord():
    return FakeDiscord()


@pytest.fixture
def setting(tmp_path, discord):
    return DiscordBotSetting(FileKV(tmp_path), SecretBox("s" * 40), transport=httpx.MockTransport(discord))


def test_the_token_is_checked_and_kept_encrypted(setting, discord, tmp_path):
    with pytest.raises(DiscordBotError, match="not a Discord bot token"):
        setting.save("hello")
    discord.reject = True
    with pytest.raises(DiscordBotError, match="Discord rejected that token"):
        setting.save(TOKEN)
    discord.reject = False
    setting.save(f"Bot {TOKEN}")
    assert setting.token() == TOKEN and TOKEN not in FileKV(tmp_path).get("discord_bot")
    st = setting.status()
    assert (st["on"], st["name"], st["linked"]) == (True, "lms-bot", False) and len(st["code"]) == 6
    setting.off()
    assert setting.token() is None and setting.status()["on"] is False


def message(user, text, **extra):
    return {"channel_id": "55", "author": {"id": user, "username": f"user{user}"}, "content": text, **extra}


def test_only_the_person_with_the_code_is_answered(setting, discord):
    setting.save(TOKEN)
    code = setting.status()["code"]
    seen = []

    def handle(chat, text, file):
        seen.append((chat, text, file.name if file else None, file.data if file else None))
        return [Reply("*LMS BOT*\n1. Assignments")]

    chat = DiscordChat(setting, handle, transport=httpx.MockTransport(discord))
    chat.on_message(TOKEN, message("7", "hello"))
    assert discord.posts[-1][1].startswith("Send the 6-digit code") and not seen
    chat.on_message(TOKEN, message("7", code))
    assert setting.owner() == "7" and setting.status()["code"] is None
    assert [p[1] for p in discord.posts[-2:]] == ["Linked: this chat now works like the WhatsApp one.", "**LMS BOT**\n1. Assignments"]
    assert seen == [("discord:7", "menu", None, None)]

    chat.on_message(TOKEN, message("8", code))  # someone else, even with the old code
    chat.on_message(TOKEN, message("7", "1", guild_id="3"))  # a server channel
    chat.on_message(TOKEN, {**message("7", "1"), "author": {"id": "7", "bot": True}})
    assert len(seen) == 1

    chat.on_message(TOKEN, message("7", "", attachments=[{"filename": "mine.pdf", "size": 9, "url": "https://cdn.example/mine.pdf"}]))
    assert seen[-1] == ("discord:7", "", "mine.pdf", b"%PDF mine")


def test_files_and_long_texts_go_out(setting, discord, tmp_path):
    setting.save(TOKEN)
    chat = DiscordChat(setting, lambda *a: [], transport=httpx.MockTransport(discord))
    notes = tmp_path / "notes.pdf"
    notes.write_bytes(b"%PDF")
    chat.send(TOKEN, "55", Reply("DAA · week 2", file=notes))
    assert discord.posts[-1] == ("/api/v10/channels/55/messages", "multipart", True)
    long = "\n".join(f"line {n} " + "x" * 80 for n in range(60))
    parts = chunks(long)
    assert len(parts) > 1 and all(len(p) <= 1900 for p in parts) and "\n".join(parts) == long
    assert discord_text("*Title*\n1. *not a title* here") == "**Title**\n1. *not a title* here"
