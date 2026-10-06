"""The setup page: sign Claude in and link WhatsApp from a browser.

GET  /setup                         the page (needs the password, ADMIN_PASSWORD)
GET  /login, POST /login, /logout   password sign-in; a signed cookie for 7 days
GET  /api/setup/status              Claude's login and WhatsApp's link
POST /api/setup/claude/start        start a Claude sign-in; returns the link to open
POST /api/setup/claude/code         {code}: the code the sign-in page shows
POST /api/setup/whatsapp/link       {number?}: a QR code, or a pairing code for that number
GET  /api/setup/whatsapp/qr         the current QR code (they rotate)
POST /api/setup/whatsapp/unlink

The JSON calls need the cookie and an X-LMS header, which a form on another site cannot
send. Five wrong passwords from one address lock it out for 15 minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
from collections import defaultdict

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .claude_login import ClaudeLogin, LoginError
from .whatsapp import EvolutionAdmin, WhatsAppError

COOKIE = "lms_session"
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILURES, LOCKOUT_SECONDS = 5, 15 * 60


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


class Code(BaseModel):
    code: str


class Number(BaseModel):
    number: str | None = None


def setup_router(*, password: str | None, secret_key: str, login: ClaudeLogin | None, admin: EvolutionAdmin | None,
                 clock=time.time) -> APIRouter:
    router = APIRouter()
    failures: dict[str, list[float]] = defaultdict(list)

    def signed_in(request: Request) -> bool:
        return bool(password) and session_valid(secret_key, request.cookies.get(COOKIE), clock())

    def guard(request: Request) -> None:
        if not signed_in(request):
            raise HTTPException(status_code=401, detail="Sign in first")
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")

    @router.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/setup", status_code=303)

    @router.get("/login", response_class=HTMLResponse)
    def login_page(error: str = "") -> HTMLResponse:
        return HTMLResponse(_login_html(error, configured=bool(password)))

    @router.post("/login")
    def login_post(request: Request, password_field: str = Form(alias="password")):
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
        now = clock()
        failures[ip] = [t for t in failures[ip] if now - t < LOCKOUT_SECONDS]
        if len(failures[ip]) >= MAX_FAILURES:
            return HTMLResponse(_login_html("Too many wrong passwords. Try again in 15 minutes.", True), status_code=429)
        if not password or not hmac.compare_digest(password_field.encode(), password.encode()):
            failures[ip].append(now)
            return HTMLResponse(_login_html("Wrong password.", bool(password)), status_code=401)
        failures.pop(ip, None)
        resp = RedirectResponse("/setup", status_code=303)
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        resp.set_cookie(COOKIE, session_cookie(secret_key, now), max_age=SESSION_SECONDS, httponly=True,
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
        return HTMLResponse(SETUP_HTML)

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
        return out

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

    return router


STYLE = """
:root { --navy:#2d398c; --sky:#c4e4f5; --soft:#eef7fc; --gold:#ffca0b; --ink:#1b1c1f; --muted:#565b6b; --rule:#d3d9e6;
        --ok:#1e7a46; --bad:#b3261e; --bg:#f6f8fb; --card:#ffffff; }
@media (prefers-color-scheme: dark) { :root { --ink:#eef1f7; --muted:#a8afc0; --rule:#323a52; --bg:#121521;
        --card:#1b2030; --soft:#222a3e; --sky:#2c3a5c; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { background:var(--navy); color:#fff; padding:14px 16px; border-bottom:4px solid var(--gold); display:flex; align-items:center; }
header h1 { font-size:18px; margin:0; flex:1; }
header form button { background:transparent; color:#fff; border:1px solid rgba(255,255,255,.5); }
main { max-width:640px; margin:0 auto; padding:16px; }
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


SETUP_HTML = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LMS setup</title><style>{STYLE}</style></head>
<body><header><h1>LMS Assignment Automation</h1><form method="post" action="/logout"><button>Sign out</button></form></header>
<main>
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
</div>
</main>
<script>
const $ = (id) => document.getElementById(id);
async function call(path, body) {{
  const r = await fetch(path, {{ method: body === undefined ? "GET" : "POST", headers: {{ "X-LMS": "1", "Content-Type": "application/json" }},
                                 body: body === undefined ? undefined : JSON.stringify(body) }});
  if (r.status === 401) {{ location.href = "/login"; return {{}}; }}
  const data = await r.json().catch(() => ({{}}));
  if (!r.ok) throw new Error(data.detail || ("Error " + r.status));
  return data;
}}
function busy(btn, on) {{ btn.disabled = on; }}
let qrTimer = null;
async function refresh() {{
  let s;
  try {{ s = await call("/api/setup/status"); }} catch (e) {{ $("c-err").textContent = e.message; return; }}
  const c = s.claude || {{}};
  $("c-state").textContent = !c.available ? "not available" : c.logged_in ? "signed in" : "signed out";
  $("c-state").className = "state " + (c.logged_in ? "ok" : "bad");
  $("c-detail").textContent = c.logged_in ? [c.email, c.method].filter(Boolean).join(" · ") : (c.detail || "Solving waits until Claude is signed in.");
  $("c-start").textContent = c.logged_in ? "Sign in again" : "Sign in to Claude";
  $("c-start").hidden = !c.available;
  const w = s.whatsapp || {{}};
  const words = {{ open: "linked", connecting: "linking…", close: "not linked", missing: "not set up", unreachable: "Evolution API not reachable" }};
  $("w-state").textContent = !w.available ? "not configured" : (words[w.state] || w.state);
  $("w-state").className = "state " + (w.linked ? "ok" : "bad");
  $("w-detail").textContent = w.linked ? [w.name, w.number].filter(Boolean).join(" · ") : (w.detail || "");
  $("w-unlink").hidden = !w.linked;
  if (w.linked) {{ $("w-step").hidden = true; clearInterval(qrTimer); }}
}}
$("c-start").onclick = async () => {{
  $("c-err").textContent = ""; busy($("c-start"), true);
  try {{ const r = await call("/api/setup/claude/start", {{}}); $("c-link").href = r.link; $("c-link").textContent = r.link; $("c-step").hidden = false; }}
  catch (e) {{ $("c-err").textContent = e.message; }}
  busy($("c-start"), false);
}};
$("c-send").onclick = async () => {{
  $("c-err").textContent = ""; busy($("c-send"), true);
  try {{ await call("/api/setup/claude/code", {{ code: $("c-code").value }}); $("c-step").hidden = true; $("c-code").value = ""; await refresh(); }}
  catch (e) {{ $("c-err").textContent = e.message; }}
  busy($("c-send"), false);
}};
async function link(number) {{
  $("w-err").textContent = "";
  try {{
    const r = await call("/api/setup/whatsapp/link", {{ number }});
    if (r.already_linked) {{ await refresh(); return; }}
    $("w-step").hidden = false;
    $("w-img").hidden = !r.qr || !!r.pairing_code; $("w-code").hidden = !r.pairing_code;
    if (r.pairing_code) {{ $("w-code").textContent = r.pairing_code;
      $("w-how").textContent = "On the bot phone: WhatsApp → Linked devices → Link a device → Link with phone number instead, then type this code."; }}
    else {{ $("w-img").src = r.qr;
      $("w-how").textContent = "On the bot phone: WhatsApp → Linked devices → Link a device, then scan this code.";
      clearInterval(qrTimer);
      qrTimer = setInterval(async () => {{ try {{ const q = await call("/api/setup/whatsapp/qr"); if (q.qr) $("w-img").src = q.qr; await refresh(); }} catch (e) {{}} }}, 15000); }}
  }} catch (e) {{ $("w-err").textContent = e.message; }}
}}
$("w-qr").onclick = () => link(null);
$("w-pair").onclick = () => link($("w-number").value);
$("w-unlink").onclick = async () => {{ if (!confirm("Unlink the bot's WhatsApp?")) return;
  try {{ await call("/api/setup/whatsapp/unlink", {{}}); await refresh(); }} catch (e) {{ $("w-err").textContent = e.message; }} }};
refresh(); setInterval(refresh, 30000);
</script></body></html>"""
