"""The setup page: sign Claude in, link WhatsApp, connect Google Drive and Discord.

GET  /setup                         the page (needs the password, ADMIN_PASSWORD)
GET  /login, POST /login, /logout   password sign-in; a signed cookie for 7 days
GET  /api/setup/status              Claude's login and WhatsApp's link
POST /api/setup/claude/start        start a Claude sign-in; returns the link to open
POST /api/setup/claude/code         {code}: the code the sign-in page shows
POST /api/setup/whatsapp/link       {number?}: a QR code, or a pairing code for that number
GET  /api/setup/whatsapp/qr         the current QR code (they rotate)
POST /api/setup/whatsapp/unlink
POST /api/setup/whatsapp/number     {number}: your own number, which messages go to
POST /api/setup/whatsapp/number/remove
POST /api/setup/whatsapp/test       a test message to that number
GET  /setup/google/start            go to Google to connect Drive (drive.file only)
GET  /setup/google/callback         Google comes back here
POST /api/setup/google/disconnect
POST /api/setup/discord             {url}: send messages to this Discord webhook too
POST /api/setup/discord/test        a test message to the webhook
POST /api/setup/discord/off

The JSON calls need the cookie and an X-LMS header, which a form on another site cannot
send. Five wrong passwords from one address lock it out for 15 minutes. Google's callback
carries no cookie (it comes from another site), so it is checked against a one-time state
made by the signed-in start.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import actions
from .claude_login import ClaudeLogin, LoginError
from .gdrive import CALLBACK, DriveError, GoogleDrive
from .notify import DiscordSetting, Notifier, NotifyError, WhatsAppNumber, masked
from .whatsapp import EvolutionAdmin, WhatsAppError

COOKIE = "lms_session"
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILURES, LOCKOUT_SECONDS = 5, 15 * 60
STATE_SECONDS = 10 * 60


def _sign(key: str, value: str) -> str:
    return hmac.new(key.encode(), f"web-session:{value}".encode(), hashlib.sha256).hexdigest()


def session_cookie(key: str, now: float) -> str:
    expires = str(int(now) + SESSION_SECONDS)
    return f"{expires}.{_sign(key, expires)}"


def session_valid(key: str, cookie: str | None, now: float) -> bool:
    if not cookie or "." not in cookie:
        return False
    expires, sig = cookie.split(".", 1)
    return expires.isdigit() and int(expires) > now and hmac.compare_digest(sig, _sign(key, expires))


class Auth:
    """The page password and the signed session cookie, shared by every page."""

    def __init__(self, password: str | None, secret_key: str, clock=time.time, kv=None) -> None:
        self.password, self.secret_key, self.clock, self.kv = password, secret_key, clock, kv

    @property
    def configured(self) -> bool:
        return bool(self.password) or actions.stored_password(self.kv) is not None

    def check(self, given: str) -> bool:
        """The password reset from the chat when there is one, else ADMIN_PASSWORD."""
        return actions.password_matches(self.kv, self.password, given)

    @property
    def session_key(self) -> str:
        """Sessions are signed with the password's hash too, so a reset signs every browser out."""
        stored = actions.stored_password(self.kv)
        return self.secret_key + (stored["hash"] if stored else "")

    def signed_in(self, request: Request) -> bool:
        return self.configured and session_valid(self.session_key, request.cookies.get(COOKIE), self.clock())

    def guard(self, request: Request) -> None:
        """For JSON calls: the cookie, and the X-LMS header a form on another site cannot send."""
        if not self.signed_in(request):
            raise HTTPException(status_code=401, detail="Sign in first")
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")


class Code(BaseModel):
    code: str


class Webhook(BaseModel):
    url: str


class Number(BaseModel):
    number: str | None = None


def setup_router(*, password: str | None, secret_key: str, login: ClaudeLogin | None, admin: EvolutionAdmin | None,
                 drive: GoogleDrive | None = None, discord: DiscordSetting | None = None, public_url: str | None = None,
                 clock=time.time, auth: Auth | None = None, whatsapp: WhatsAppNumber | None = None,
                 sender: Notifier | None = None) -> APIRouter:
    router = APIRouter()
    auth = auth or Auth(password, secret_key, clock)
    failures: dict[str, list[float]] = defaultdict(list)
    states: dict[str, float] = {}  # Google sign-ins started here, by state, until they expire

    def redirect_uri(request: Request) -> str:
        if public_url:
            return public_url.rstrip("/") + CALLBACK
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}{CALLBACK}"

    signed_in, guard = auth.signed_in, auth.guard

    @router.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/courses", status_code=303)

    @router.get("/login", response_class=HTMLResponse)
    def login_page(error: str = "") -> HTMLResponse:
        return HTMLResponse(_login_html(error, configured=auth.configured))

    @router.post("/login")
    def login_post(request: Request, password_field: str = Form(alias="password")):
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
        now = clock()
        failures[ip] = [t for t in failures[ip] if now - t < LOCKOUT_SECONDS]
        if len(failures[ip]) >= MAX_FAILURES:
            return HTMLResponse(_login_html("Too many wrong passwords. Try again in 15 minutes.", True), status_code=429)
        if not auth.check(password_field):
            failures[ip].append(now)
            return HTMLResponse(_login_html("Wrong password.", auth.configured), status_code=401)
        failures.pop(ip, None)
        resp = RedirectResponse("/courses", status_code=303)
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        resp.set_cookie(COOKIE, session_cookie(auth.session_key, now), max_age=SESSION_SECONDS, httponly=True,
                        secure=https, samesite="strict")
        return resp

    @router.post("/logout")
    def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @router.get("/setup", response_class=HTMLResponse)
    def setup(request: Request):
        if not signed_in(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(shell("LMS setup", "setup", SETUP_BODY, SETUP_JS))

    @router.get("/api/setup/status")
    def status(request: Request) -> dict:
        guard(request)
        out: dict = {}
        if login is None:
            out["claude"] = {"available": False, "detail": "Claude Code is not installed in this container"}
        else:
            s = login.status()
            out["claude"] = {"available": True, "logged_in": s.logged_in, "method": s.method, "email": s.email,
                             "detail": s.detail, "waiting": login.waiting}
        if admin is None:
            out["whatsapp"] = {"available": False, "detail": "Set EVOLUTION_API_URL and EVOLUTION_API_KEY"}
        else:
            try:
                st = admin.state()
                out["whatsapp"] = {"available": True, "state": st.state, "linked": st.linked, "number": st.number,
                                   "name": st.name}
            except WhatsAppError as e:
                out["whatsapp"] = {"available": True, "state": "unreachable", "linked": False, "detail": str(e)}
        if whatsapp is not None:
            out["whatsapp"].update(send_to=whatsapp.get(), send_to_source=whatsapp.source())
        if drive is None:
            out["drive"] = {"available": False, "detail": "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"}
        else:
            d = drive.status()
            out["drive"] = {"available": True, "connected": d.connected, "email": d.email}
        out["drive"]["redirect_uri"] = redirect_uri(request)
        if discord is None:
            out["discord"] = {"available": False}
        else:
            url = discord.url()
            out["discord"] = {"available": True, "on": bool(url), "source": discord.source(), "masked": masked(url),
                              "editable": discord.editable}
        return out

    def _discord() -> DiscordSetting:
        if discord is None:
            raise HTTPException(status_code=503, detail="Discord is not available here")
        return discord

    @router.post("/api/setup/discord")
    def discord_save(request: Request, body: Webhook) -> dict:
        guard(request)
        try:
            _discord().save(body.url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/setup/discord/test")
    def discord_test(request: Request) -> dict:
        guard(request)
        try:
            _discord().notifier().text("Test message from LMS Assignment Automation. Discord notifications work.")
        except NotifyError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"ok": True}

    @router.post("/api/setup/discord/off")
    def discord_off(request: Request) -> dict:
        guard(request)
        try:
            _discord().turn_off()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.get("/setup/google/start")
    def google_start(request: Request):
        if not signed_in(request):
            return RedirectResponse("/login", status_code=303)
        if drive is None:
            raise HTTPException(status_code=503, detail="Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
        now = clock()
        for k in [k for k, t in states.items() if t < now]:
            states.pop(k)
        state = secrets.token_urlsafe(24)
        states[state] = now + STATE_SECONDS
        return RedirectResponse(drive.auth_url(redirect_uri(request), state), status_code=303)

    @router.get("/setup/google/callback", response_class=HTMLResponse)
    def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        expires = states.pop(state, None) if state else None
        if expires is None or expires < clock():
            return HTMLResponse(_back_html("This Google sign-in was not started here, or it is too old. Start again."), 400)
        if error or not code or drive is None:
            return HTMLResponse(_back_html(f"Google Drive was not connected: {error or 'no code'}."), 400)
        try:
            d = drive.connect(code, redirect_uri(request))
        except DriveError as e:
            return HTMLResponse(_back_html(str(e)), 502)
        return HTMLResponse(_back_html(f"Google Drive connected{' as ' + d.email if d.email else ''}.", ok=True))

    @router.post("/api/setup/google/disconnect")
    def google_disconnect(request: Request) -> dict:
        guard(request)
        if drive is None:
            raise HTTPException(status_code=503, detail="Google Drive is not configured")
        drive.disconnect()
        return {"ok": True}

    @router.post("/api/setup/claude/start")
    def claude_start(request: Request) -> dict:
        guard(request)
        if login is None:
            raise HTTPException(status_code=503, detail="Claude Code is not installed in this container")
        try:
            return {"link": login.start()}
        except LoginError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @router.post("/api/setup/claude/code")
    def claude_code(request: Request, body: Code) -> dict:
        guard(request)
        if login is None:
            raise HTTPException(status_code=503, detail="Claude Code is not installed in this container")
        try:
            s = login.finish(body.code)
        except LoginError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"logged_in": s.logged_in, "email": s.email, "method": s.method}

    @router.post("/api/setup/whatsapp/link")
    def whatsapp_link(request: Request, body: Number) -> dict:
        guard(request)
        if admin is None:
            raise HTTPException(status_code=503, detail="WhatsApp is not configured")
        number = "".join(ch for ch in (body.number or "") if ch.isdigit()) or None
        try:
            c = admin.start_link(number)
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"already_linked": c.already_linked, "qr": c.qr_png_base64, "pairing_code": c.pairing_code_display}

    @router.get("/api/setup/whatsapp/qr")
    def whatsapp_qr(request: Request) -> dict:
        guard(request)
        if admin is None:
            raise HTTPException(status_code=503, detail="WhatsApp is not configured")
        try:
            c = admin.current_challenge()
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"qr": c.qr_png_base64 if c else None}

    @router.post("/api/setup/whatsapp/unlink")
    def whatsapp_unlink(request: Request) -> dict:
        guard(request)
        if admin is None:
            raise HTTPException(status_code=503, detail="WhatsApp is not configured")
        try:
            admin.unlink()
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"ok": True}

    def _whatsapp() -> WhatsAppNumber:
        if whatsapp is None:
            raise HTTPException(status_code=503, detail="WhatsApp is not available here")
        return whatsapp

    @router.post("/api/setup/whatsapp/number")
    def whatsapp_number_save(request: Request, body: Number) -> dict:
        guard(request)
        try:
            return {"ok": True, "number": _whatsapp().save(body.number or "")}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/setup/whatsapp/number/remove")
    def whatsapp_number_remove(request: Request) -> dict:
        guard(request)
        _whatsapp().remove()
        return {"ok": True}

    @router.post("/api/setup/whatsapp/test")
    def whatsapp_test(request: Request) -> dict:
        guard(request)
        if sender is None:
            raise HTTPException(status_code=503, detail="WhatsApp is not configured")
        try:
            sender.text("Test message from LMS Assignment Automation. WhatsApp notifications work.")
        except NotifyError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"ok": True}

    return router


STYLE = """
:root { --navy:#2d398c; --sky:#c4e4f5; --soft:#eef7fc; --gold:#ffca0b; --ink:#1b1c1f; --muted:#565b6b; --rule:#d3d9e6;
        --ok:#1e7a46; --bad:#b3261e; --bg:#f6f8fb; --card:#ffffff; }
@media (prefers-color-scheme: dark) { :root { --ink:#eef1f7; --muted:#a8afc0; --rule:#323a52; --bg:#121521;
        --card:#1b2030; --soft:#222a3e; --sky:#2c3a5c; --ok:#5fd394; --bad:#ff8a80; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { background:var(--navy); color:#fff; padding:14px 16px; border-bottom:4px solid var(--gold); display:flex; align-items:center; }
header h1 { font-size:18px; margin:0; flex:1; }
header form button { background:transparent; color:#fff; border:1px solid rgba(255,255,255,.5); }
nav.tabs { display:flex; gap:4px; padding:0 max(16px, calc((100% - 720px) / 2 + 16px)); background:var(--card); border-bottom:1px solid var(--rule); }
nav.tabs a { padding:12px 14px; color:var(--muted); text-decoration:none; font-weight:600; border-bottom:3px solid transparent; }
nav.tabs a.on { color:var(--ink); border-bottom-color:var(--navy); }
@media (prefers-color-scheme: dark) { nav.tabs a.on { border-bottom-color:var(--gold); } }
main { max-width:720px; margin:0 auto; padding:16px; }
.card { background:var(--card); border:1px solid var(--rule); border-radius:10px; padding:16px; margin-bottom:16px; }
.card h2 { margin:0 0 6px; font-size:17px; }
.state { font-weight:600; } .state.ok { color:var(--ok); } .state.bad { color:var(--bad); }
.muted { color:var(--muted); font-size:14px; }
button { font:inherit; padding:9px 14px; border-radius:8px; border:0; background:var(--navy); color:#fff; cursor:pointer; }
button.secondary { background:var(--soft); color:var(--ink); border:1px solid var(--rule); }
button:disabled { opacity:.6; cursor:wait; }
input { font:inherit; padding:9px 10px; border-radius:8px; border:1px solid var(--rule); background:var(--card); color:var(--ink); width:100%; }
.row { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; } .row input { flex:1; min-width:180px; }
.step { margin-top:12px; padding-top:12px; border-top:1px dashed var(--rule); }
a { color:var(--navy); word-break:break-all; } @media (prefers-color-scheme: dark) { a { color:#9fc3ff; } }
.code { font:600 26px/1 ui-monospace, monospace; letter-spacing:.12em; margin:10px 0; }
img.qr { width:240px; max-width:100%; background:#fff; padding:8px; border-radius:8px; }
.err { color:var(--bad); font-size:14px; margin-top:8px; }
code { word-break:break-all; }
.seg { display:inline-flex; border:1px solid var(--rule); border-radius:8px; overflow:hidden; margin-top:10px; }
.seg button { background:var(--card); color:var(--ink); border-radius:0; border:0; border-right:1px solid var(--rule); padding:7px 12px; font-size:15px; }
.seg button:last-child { border-right:0; }
.seg button.on { background:var(--navy); color:#fff; }
.counts { color:var(--muted); font-size:14px; margin-top:4px; }
.counts b { color:var(--ink); }
.item { border-top:1px solid var(--rule); padding:12px 0; }
.item:first-of-type { border-top:0; }
.item h3 { font-size:16px; margin:0 0 2px; }
.chip { display:inline-block; font-size:13px; font-weight:600; padding:2px 8px; border-radius:999px; background:var(--soft); color:var(--ink); border:1px solid var(--rule); margin:4px 4px 0 0; }
.chip.ok { color:var(--ok); } .chip.bad { color:var(--bad); } .chip.wait { color:#8a5a00; background:#fff4d6; border-color:#f1d488; }
@media (prefers-color-scheme: dark) { .chip.wait { color:#ffd27a; background:#3a3020; border-color:#5c4a22; } }
.row.small button, .row.small a.btn { padding:6px 10px; font-size:14px; }
a.btn { display:inline-block; padding:9px 14px; border-radius:8px; background:var(--soft); color:var(--ink); border:1px solid var(--rule); text-decoration:none; }
details.hist { margin-top:8px; font-size:14px; color:var(--muted); } details.hist summary { cursor:pointer; }
a.course { text-decoration:none; color:inherit; } a.course h2 { color:var(--navy); }
@media (prefers-color-scheme: dark) { a.course h2 { color:#9fc3ff; } }
"""


def _login_html(error: str, configured: bool) -> str:
    body = ('<p class="muted">Set <b>ADMIN_PASSWORD</b> in the environment to use this page.</p>' if not configured else
            '<form method="post" action="/login"><label for="p">Password</label>'
            '<div class="row"><input id="p" name="password" type="password" autocomplete="current-password" required autofocus>'
            '<button>Sign in</button></div></form>')
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>LMS setup</title><style>{STYLE}</style></head><body><header><h1>LMS Assignment Automation</h1></header>'
            f'<main><div class="card"><h2>Sign in</h2>{body}{err}</div></main></body></html>')


def _back_html(message: str, ok: bool = False) -> str:
    """Google's callback page. It sends you back with a fresh navigation from this site, so the
    session cookie (SameSite=strict) comes along."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="{2 if ok else 8};url=/setup"><title>LMS setup</title><style>{STYLE}</style></head>'
            f'<body><header><h1>LMS Assignment Automation</h1></header><main><div class="card"><h2>Google Drive</h2>'
            f'<p class="{"state ok" if ok else "err"}">{html.escape(message)}</p><p><a href="/setup">Back to setup</a></p>'
            f'</div></main></body></html>')


def shell(title: str, tab: str, body: str, script: str = "") -> str:
    """A signed-in page: header, the Courses and Setup tabs, then the body."""
    tabs = "".join(f'<a href="/{t.lower()}" class="{"on" if t.lower() == tab else ""}">{t}</a>' for t in ("Courses", "Setup"))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>'
            f'<header><h1>LMS Assignment Automation</h1><form method="post" action="/logout"><button>Sign out</button></form></header>'
            f'<nav class="tabs">{tabs}</nav><main>{body}</main><script>{CALL_JS}{script}</script></body></html>')


CALL_JS = """
const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function call(path, body) {
  const r = await fetch(path, { method: body === undefined ? "GET" : "POST", headers: { "X-LMS": "1", "Content-Type": "application/json" },
                                body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401) { location.href = "/login"; return {}; }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("Error " + r.status));
  return data;
}
function busy(btn, on) { btn.disabled = on; }
"""


SETUP_BODY = """
<div class="card" id="claude">
  <h2>Claude</h2>
  <div>Login: <span class="state" id="c-state">checking…</span></div>
  <div class="muted" id="c-detail"></div>
  <div class="row"><button id="c-start">Sign in to Claude</button></div>
  <div class="step" id="c-step" hidden>
    <div>1. Open this link and sign in with your Claude account:</div>
    <p><a id="c-link" target="_blank" rel="noopener"></a></p>
    <div>2. Paste the code the page shows:</div>
    <div class="row"><input id="c-code" autocomplete="off" placeholder="Code from the sign-in page"><button id="c-send">Finish</button></div>
  </div>
  <div class="err" id="c-err"></div>
</div>
<div class="card" id="wa">
  <h2>WhatsApp</h2>
  <div>Bot: <span class="state" id="w-state">checking…</span></div>
  <div class="muted" id="w-detail"></div>
  <div class="row"><button id="w-qr">Link with a QR code</button></div>
  <div class="row"><input id="w-number" inputmode="numeric" placeholder="Or the bot phone's number, e.g. 923001234567"><button class="secondary" id="w-pair">Get a pairing code</button></div>
  <div class="step" id="w-step" hidden>
    <div id="w-how"></div>
    <img class="qr" id="w-img" alt="WhatsApp QR code" hidden>
    <div class="code" id="w-code" hidden></div>
  </div>
  <div class="row"><button class="secondary" id="w-unlink" hidden>Unlink</button></div>
  <div class="err" id="w-err"></div>
  <div class="step">
    <div>Messages go to: <span class="state" id="t-state">checking…</span></div>
    <div class="muted" id="t-detail"></div>
    <div class="row"><input id="t-number" inputmode="numeric" autocomplete="off" placeholder="Your own number, e.g. 923001234567"><button id="t-save">Save</button></div>
    <div class="row"><button class="secondary" id="t-test" hidden>Send a test message</button><button class="secondary" id="t-remove" hidden>Remove</button></div>
    <div class="err" id="t-err"></div>
  </div>
</div>
<div class="card" id="drive">
  <h2>Google Drive</h2>
  <div>Drive: <span class="state" id="g-state">checking…</span></div>
  <div class="muted" id="g-detail"></div>
  <div class="row"><button id="g-connect" hidden>Connect Google Drive</button><button class="secondary" id="g-off" hidden>Disconnect</button></div>
  <p class="muted">Every report is saved to <b>University Assignments</b> in your Drive. Redirect URI for your Google OAuth client: <code id="g-uri"></code></p>
  <div class="err" id="g-err"></div>
</div>
<div class="card" id="discord">
  <h2>Discord</h2>
  <div>Webhook: <span class="state" id="d-state">checking…</span></div>
  <div class="muted" id="d-detail"></div>
  <div class="row"><input id="d-url" autocomplete="off" placeholder="https://discord.com/api/webhooks/…"><button id="d-save">Save</button></div>
  <div class="row"><button class="secondary" id="d-test" hidden>Send a test message</button><button class="secondary" id="d-off" hidden>Turn off</button></div>
  <p class="muted">Discord gets the same alerts and files as WhatsApp; approvals stay on WhatsApp and this page. In Discord: channel settings, Integrations, Webhooks, New Webhook, Copy Webhook URL.</p>
  <div class="err" id="d-err"></div>
</div>
"""

SETUP_JS = """
let qrTimer = null;
async function refresh() {
  let s;
  try { s = await call("/api/setup/status"); } catch (e) { $("c-err").textContent = e.message; return; }
  const c = s.claude || {};
  $("c-state").textContent = !c.available ? "not available" : c.logged_in ? "signed in" : "signed out";
  $("c-state").className = "state " + (c.logged_in ? "ok" : "bad");
  $("c-detail").textContent = c.logged_in ? [c.email, c.method].filter(Boolean).join(" · ") : (c.detail || "Solving waits until Claude is signed in.");
  $("c-start").textContent = c.logged_in ? "Sign in again" : "Sign in to Claude";
  $("c-start").hidden = !c.available;
  const w = s.whatsapp || {};
  const words = { open: "linked", connecting: "linking…", close: "not linked", missing: "not set up", unreachable: "Evolution API not reachable" };
  $("w-state").textContent = !w.available ? "not configured" : (words[w.state] || w.state);
  $("w-state").className = "state " + (w.linked ? "ok" : "bad");
  $("w-detail").textContent = w.linked ? [w.name, w.number].filter(Boolean).join(" · ") : (w.detail || "");
  $("w-unlink").hidden = !w.linked;
  if (w.linked) { $("w-step").hidden = true; clearInterval(qrTimer); }
  $("t-state").textContent = w.send_to ? "+" + w.send_to : "not set";
  $("t-state").className = "state " + (w.send_to ? "ok" : "bad");
  $("t-detail").textContent = w.send_to ? (w.send_to_source === "env" ? "From WHATSAPP_NUMBER" : "Saved here") + " · the bot answers only this number"
    : "Your own WhatsApp number, country code first. The bot sends to it and answers only it.";
  $("t-test").hidden = !w.send_to || !w.linked; $("t-remove").hidden = w.send_to_source !== "page";
  $("t-save").textContent = w.send_to ? "Replace" : "Save";
  const g = s.drive || {};
  $("g-state").textContent = !g.available ? "not configured" : g.connected ? "connected" : "not connected";
  $("g-state").className = "state " + (g.connected ? "ok" : "bad");
  $("g-detail").textContent = g.connected ? (g.email || "") : (g.detail || "Reports are kept on the server only.");
  $("g-connect").hidden = !g.available || g.connected; $("g-off").hidden = !g.connected;
  $("g-uri").textContent = g.redirect_uri || "";
  const d = s.discord || {};
  $("d-state").textContent = !d.available ? "not available" : d.on ? "on" : "off";
  $("d-state").className = "state " + (d.on ? "ok" : "bad");
  $("d-detail").textContent = d.on ? d.masked + (d.source === "env" ? " · from DISCORD_WEBHOOK_URL" : " · saved here") : "Messages go to WhatsApp only.";
  $("d-test").hidden = !d.on; $("d-off").hidden = !d.on || !d.editable;
  $("d-save").textContent = d.on ? "Replace" : "Save";
}
$("g-connect").onclick = () => { location.href = "/setup/google/start"; };
$("g-off").onclick = async () => { if (!confirm("Stop saving reports to Google Drive?")) return;
  try { await call("/api/setup/google/disconnect", {}); await refresh(); } catch (e) { $("g-err").textContent = e.message; } };
$("c-start").onclick = async () => {
  $("c-err").textContent = ""; busy($("c-start"), true);
  try { const r = await call("/api/setup/claude/start", {}); $("c-link").href = r.link; $("c-link").textContent = r.link; $("c-step").hidden = false; }
  catch (e) { $("c-err").textContent = e.message; }
  busy($("c-start"), false);
};
$("c-send").onclick = async () => {
  $("c-err").textContent = ""; busy($("c-send"), true);
  try { await call("/api/setup/claude/code", { code: $("c-code").value }); $("c-step").hidden = true; $("c-code").value = ""; await refresh(); }
  catch (e) { $("c-err").textContent = e.message; }
  busy($("c-send"), false);
};
async function link(number) {
  $("w-err").textContent = "";
  try {
    const r = await call("/api/setup/whatsapp/link", { number });
    if (r.already_linked) { await refresh(); return; }
    $("w-step").hidden = false;
    $("w-img").hidden = !r.qr || !!r.pairing_code; $("w-code").hidden = !r.pairing_code;
    if (r.pairing_code) { $("w-code").textContent = r.pairing_code;
      $("w-how").textContent = "On the bot phone: WhatsApp → Linked devices → Link a device → Link with phone number instead, then type this code."; }
    else { $("w-img").src = r.qr;
      $("w-how").textContent = "On the bot phone: WhatsApp → Linked devices → Link a device, then scan this code.";
      clearInterval(qrTimer);
      qrTimer = setInterval(async () => { try { const q = await call("/api/setup/whatsapp/qr"); if (q.qr) $("w-img").src = q.qr; await refresh(); } catch (e) {} }, 15000); }
  } catch (e) { $("w-err").textContent = e.message; }
}
$("w-qr").onclick = () => link(null);
$("w-pair").onclick = () => link($("w-number").value);
$("w-unlink").onclick = async () => { if (!confirm("Unlink the bot's WhatsApp?")) return;
  try { await call("/api/setup/whatsapp/unlink", {}); await refresh(); } catch (e) { $("w-err").textContent = e.message; } };
$("t-save").onclick = async () => {
  $("t-err").textContent = ""; busy($("t-save"), true);
  try { await call("/api/setup/whatsapp/number", { number: $("t-number").value }); $("t-number").value = ""; await refresh(); }
  catch (e) { $("t-err").textContent = e.message; }
  busy($("t-save"), false);
};
$("t-test").onclick = async () => {
  $("t-err").textContent = ""; busy($("t-test"), true);
  try { await call("/api/setup/whatsapp/test", {}); $("t-detail").textContent = $("t-detail").textContent.replace(/ · test message sent$/, "") + " · test message sent"; }
  catch (e) { $("t-err").textContent = e.message; }
  busy($("t-test"), false);
};
$("t-remove").onclick = async () => { if (!confirm("Stop sending WhatsApp messages to this number?")) return;
  try { await call("/api/setup/whatsapp/number/remove", {}); await refresh(); } catch (e) { $("t-err").textContent = e.message; } };
$("d-save").onclick = async () => {
  $("d-err").textContent = ""; busy($("d-save"), true);
  try { await call("/api/setup/discord", { url: $("d-url").value }); $("d-url").value = "";
        await call("/api/setup/discord/test", {}); await refresh(); $("d-detail").textContent += " · test message sent"; }
  catch (e) { $("d-err").textContent = e.message; await refresh(); }
  busy($("d-save"), false);
};
$("d-test").onclick = async () => {
  $("d-err").textContent = "";
  try { await call("/api/setup/discord/test", {}); $("d-err").textContent = ""; $("d-detail").textContent = $("d-detail").textContent.replace(/ · test message sent$/, "") + " · test message sent"; }
  catch (e) { $("d-err").textContent = e.message; }
};
$("d-off").onclick = async () => { if (!confirm("Stop sending messages to Discord?")) return;
  try { await call("/api/setup/discord/off", {}); await refresh(); } catch (e) { $("d-err").textContent = e.message; } };
refresh(); setInterval(refresh, 30000);
"""
