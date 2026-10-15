import os
import stat
import sys
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lms_worker.claude_login import ClaudeLogin, LoginError
from lms_worker.kv import FileKV
from lms_worker.notify import NotifyError, WhatsAppNumber
from lms_worker.web import COOKIE, session_cookie, session_valid, setup_router
from lms_worker.whatsapp import LinkChallenge, LinkState

KEY = "k" * 48
PASSWORD = "correct horse battery"
GOOD_CODE = "goodcode-123456"

FAKE_CLAUDE = textwrap.dedent(f"""\
    #!{sys.executable}
    # Stands in for `claude auth status --json` and `claude auth login --claudeai`
    import json, os, sys
    home = os.environ["HOME"]
    flag = os.path.join(home, ".logged-in")
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps({{"loggedIn": os.path.exists(flag), "authMethod": "claude.ai", "email": "student@example.com"}}))
    elif sys.argv[1:3] == ["auth", "login"]:
        print("Opening browser to sign in…")
        print("If the browser didn't open, visit: \\x1b]8;;https://claude.com/cai/oauth/authorize?code=true&state=abc\\x07"
              "https://claude.com/cai/oauth/authorize?code=true&state=abc\\x1b]8;;\\x07")
        sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
        code = sys.stdin.readline().strip()
        if code == "{GOOD_CODE}":
            open(flag, "w").close()
            print("Login successful.")
        else:
            print("Invalid code"); sys.exit(1)
""")


@pytest.fixture
def claude_bin(tmp_path):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class StubAdmin:
    def __init__(self):
        self.linked = False
        self.calls = []

    def state(self):
        return LinkState("open", number="923001112223", name="LMS Bot") if self.linked else LinkState("close")

    def start_link(self, number=None):
        self.calls.append(("link", number))
        return LinkChallenge(pairing_code="ABCD1234") if number else LinkChallenge(qr_png_base64="data:image/png;base64,QQ==")

    def current_challenge(self):
        return LinkChallenge(qr_png_base64="data:image/png;base64,Ug==")

    def unlink(self):
        self.calls.append(("unlink",))
        self.linked = False


def client(tmp_path, claude_bin=None, password=PASSWORD, admin=None, **parts):
    login = ClaudeLogin(tmp_path / "home", binary=str(claude_bin)) if claude_bin else None
    app = FastAPI()
    app.include_router(setup_router(password=password, secret_key=KEY, login=login, admin=admin or StubAdmin(), **parts))
    return TestClient(app, base_url="https://lms.example.com")


def signed_in(c):
    r = c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/courses"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    return c


def test_session_cookie_is_signed_and_expires():
    cookie = session_cookie(KEY, 1000)
    assert session_valid(KEY, cookie, 1001)
    assert not session_valid(KEY, cookie, 1000 + 8 * 24 * 3600)
    assert not session_valid("other" * 10, cookie, 1001)
    expires, sig = cookie.split(".")
    assert not session_valid(KEY, f"{int(expires) + 999}.{sig}", 1001)


def test_the_page_needs_the_password(tmp_path):
    c = client(tmp_path)
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.get("/api/setup/status", headers={"X-LMS": "1"}).status_code == 401
    signed_in(c)
    assert "Sign in to Claude" in c.get("/setup").text
    assert c.get("/api/setup/status").status_code == 403  # no X-LMS header: a form on another site cannot call it


def test_a_password_reset_from_the_chat_replaces_the_variable_and_signs_browsers_out(tmp_path):
    from lms_worker import actions
    from lms_worker.web import Auth

    kv = FileKV(tmp_path / "kv")
    app = FastAPI()
    app.include_router(setup_router(password=PASSWORD, secret_key=KEY, login=None, admin=StubAdmin(), auth=Auth(PASSWORD, KEY, kv=kv)))
    c = signed_in(TestClient(app, base_url="https://lms.example.com"))
    assert c.get("/setup", follow_redirects=False).status_code == 200
    new = actions.reset_password(kv)
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"  # signed out
    assert c.post("/login", data={"password": PASSWORD}).status_code == 401  # the old one is dead
    assert c.post("/login", data={"password": new}, follow_redirects=False).status_code == 303
    assert c.get("/setup", follow_redirects=False).status_code == 200


def test_wrong_passwords_lock_out(tmp_path):
    c = client(tmp_path)
    for _ in range(5):
        assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.post("/login", data={"password": PASSWORD}).status_code == 429


def test_without_a_password_the_page_stays_closed(tmp_path):
    c = client(tmp_path, password=None)
    assert "Set <b>ADMIN_PASSWORD</b>" in c.get("/login").text
    assert c.post("/login", data={"password": ""}).status_code in (401, 422)


def test_claude_sign_in_from_the_page(tmp_path, claude_bin):
    c = signed_in(client(tmp_path, claude_bin))
    h = {"X-LMS": "1"}
    assert c.get("/api/setup/status", headers=h).json()["claude"]["logged_in"] is False
    link = c.post("/api/setup/claude/start", headers=h, json={}).json()["link"]
    assert link == "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    r = c.post("/api/setup/claude/code", headers=h, json={"code": GOOD_CODE})
    assert r.status_code == 200 and r.json() == {"logged_in": True, "email": "student@example.com", "method": "claude.ai"}
    status = c.get("/api/setup/status", headers=h).json()["claude"]
    assert status["logged_in"] and status["email"] == "student@example.com" and not status["waiting"]


def test_a_wrong_code_or_no_sign_in_is_refused(tmp_path, claude_bin):
    login = ClaudeLogin(tmp_path / "home", binary=str(claude_bin))
    with pytest.raises(LoginError, match="No sign-in is waiting"):
        login.finish(GOOD_CODE)
    login.start()
    with pytest.raises(LoginError, match="did not complete") as e:
        login.finish("wrongcode-0000")
    assert "wrongcode-0000" not in str(e.value) and "Invalid code" in str(e.value)
    with pytest.raises(LoginError, match="does not look like"):
        login.finish("x; rm -rf /")
    assert not login.status().logged_in


def test_whatsapp_linking_from_the_page(tmp_path):
    admin = StubAdmin()
    c = signed_in(client(tmp_path, admin=admin))
    h = {"X-LMS": "1"}
    assert c.get("/api/setup/status", headers=h).json()["whatsapp"]["linked"] is False
    assert c.post("/api/setup/whatsapp/link", headers=h, json={}).json()["qr"].startswith("data:image/png")
    assert c.post("/api/setup/whatsapp/link", headers=h, json={"number": "+92 300 1112223"}).json()["pairing_code"] == "ABCD-1234"
    assert admin.calls[-1] == ("link", "923001112223")
    assert c.get("/api/setup/whatsapp/qr", headers=h).json()["qr"] == "data:image/png;base64,Ug=="
    admin.linked = True
    assert c.get("/api/setup/status", headers=h).json()["whatsapp"]["name"] == "LMS Bot"
    assert c.post("/api/setup/whatsapp/unlink", headers=h, json={}).json() == {"ok": True}


class Phone:
    """Stands in for the WhatsApp sender; it sends to whatever number is saved, as the real one does."""

    def __init__(self, number):
        self.number, self.sent = number, []

    def text(self, message):
        if not self.number.get():
            raise NotifyError("WhatsApp has no number to send to")
        self.sent.append((self.number.get(), message))


def test_the_number_messages_go_to_is_saved_on_the_page(tmp_path):
    number = WhatsAppNumber(FileKV(tmp_path / "kv"))
    phone = Phone(number)
    c = signed_in(client(tmp_path, whatsapp=number, sender=phone))
    h = {"X-LMS": "1"}
    assert "Messages go to" in c.get("/setup").text
    assert c.get("/api/setup/status", headers=h).json()["whatsapp"]["send_to"] is None
    assert c.post("/api/setup/whatsapp/test", headers=h, json={}).status_code == 502  # nowhere to send yet
    for bad in ("", "12345", "0300 1234567"):  # too short, or no country code
        assert c.post("/api/setup/whatsapp/number", headers=h, json={"number": bad}).status_code == 400
    r = c.post("/api/setup/whatsapp/number", headers=h, json={"number": "+92 300 1234567"})
    assert r.json() == {"ok": True, "number": "923001234567"}
    w = c.get("/api/setup/status", headers=h).json()["whatsapp"]
    assert (w["send_to"], w["send_to_source"]) == ("923001234567", "page")
    assert c.post("/api/setup/whatsapp/test", headers=h, json={}).json() == {"ok": True}
    assert phone.sent[0][0] == "923001234567"
    assert c.post("/api/setup/whatsapp/number/remove", headers=h, json={}).json() == {"ok": True}
    assert c.get("/api/setup/status", headers=h).json()["whatsapp"]["send_to"] is None
    assert c.post("/api/setup/whatsapp/number", json={"number": "923001234567"}).status_code == 403  # no X-LMS header


def test_a_saved_number_wins_over_the_variable(tmp_path):
    number = WhatsAppNumber(FileKV(tmp_path / "kv"), "923001234567")
    assert (number.get(), number.source()) == ("923001234567", "env")
    number.save("923119999999")
    assert (number.get(), number.source()) == ("923119999999", "page")
    number.remove()
    assert (number.get(), number.source()) == ("923001234567", "env")
