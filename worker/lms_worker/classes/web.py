"""Your Classes page, the CR's page, and the invite sign-up.

Yours (signed in with the setup password; JSON calls need the X-LMS header):
GET  /classes                         the page
GET  /api/classes                     the class bot, every class, invites, your default rules
POST /api/classes/bot/link {number?}  link the class bot's number (a QR code, or a pairing code)
GET  /api/classes/bot/qr              the current QR code
POST /api/classes/bot/unlink
POST /api/classes/pause-all {on}      stop or restart every class at once
POST /api/classes/invites {name, role, class_id?}, POST /api/classes/invites/{id}/revoke
POST /api/classes/{id}/status {status}   approve (active), pause, block
POST /api/classes/{id}/remove
POST /api/classes/{id}/poll           check its LMS now
POST /api/classes/rules {rules}       the defaults for every class
POST /api/classes/{id}/rules {rules}  this class's own rules; a null rule goes back to the default
GET  /api/classes/{id}/users
GET  /api/classes/blocks, POST /api/classes/blocks {person, class_id?}, POST /api/classes/blocks/remove
GET  /api/classes/logs?class_id&person&kind&days, GET /classes/logs.csv?...

The CR's (a cookie after a WhatsApp code; JSON calls need the X-LMS header):
GET  /cr                              their page, or the sign-in form
POST /api/cr/code {phone}, POST /api/cr/verify {phone, code}, POST /api/cr/logout
GET  /api/cr/state                    their class, its settings as your rules allow, usage, log
POST /api/cr/lms {enrollment, password, remember}, GET /api/cr/lms   sign in to the LMS again
POST /api/cr/forget-password
GET  /api/cr/groups, POST /api/cr/groups {groups}   groups where they are an admin
POST /api/cr/settings {key, value}, POST /api/cr/mute {days}, POST /api/cr/pause {on}
POST /api/cr/block {person}, POST /api/cr/unblock {person}
POST /api/cr/announce {text}
POST /api/cr/remove {confirm}

The invite (the link you send; no account yet):
GET  /join/{token}
POST /api/join/{token}/code {phone, name}, POST /api/join/{token}/verify {phone, code}
POST /api/join/{token}/lms {enrollment, password, institute, remember}, GET /api/join/{token}/lms
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import time
from datetime import timedelta
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models import ClassPerson, ClassStatus, SchoolClass
from ..web import CALL_JS, STYLE, shell
from ..whatsapp import WhatsAppError
from . import access, policy
from .lms import SignInError
from .store import Classes, SignupRefused, poll_key

log = logging.getLogger(__name__)


class Body(BaseModel):
    model_config = {"extra": "allow"}


def _body(b: Body) -> dict:
    return b.model_dump()


def _err(e: Exception, status: int = 400):
    raise HTTPException(status_code=status, detail=str(e))


def classes_router(*, auth, classes: Classes, settings, admin=None, sender=None, jobs: access.SignInJobs | None = None,
                   sign_in: Callable | None = None, alert: Callable[[str], None] | None = None, secret: str,
                   clock: Callable[[], float] = time.time) -> APIRouter:
    """`admin`: the class bot's EvolutionAdmin. `sender`: sending.ClassSender, for codes.
    `sign_in(enrollment, password, institute)`: classes.lms.sign_in bound to the settings."""
    router = APIRouter()
    jobs = jobs or access.SignInJobs()
    alert = alert or (lambda _: None)
    codes = access.Codes(classes.kv, lambda to, text: _send_code(to, text), clock=clock)
    cr_cookie = access.Signer(secret, "class-cr")
    join_cookie = access.Signer(secret, "class-join")

    def _send_code(to: str, text: str) -> None:
        if sender is None or admin is None:
            raise ValueError("The class bot is not set up yet; ask the admin")
        try:
            if not admin.state().linked:
                raise ValueError("The class bot's WhatsApp is not linked yet; ask the admin")
        except WhatsAppError as e:
            raise ValueError(f"The class bot is not reachable: {e}") from e
        from ..notify import NotifyError

        try:
            sender.text(to, text)
        except NotifyError as e:
            raise ValueError(f"Could not send the code on WhatsApp: {e}") from e

    def base_url(request: Request) -> str:
        if settings.public_url:
            return settings.public_url.rstrip("/")
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}"

    # ================= yours =================

    def guard(request: Request) -> None:
        auth.guard(request)

    @router.get("/classes", response_class=HTMLResponse)
    def classes_page(request: Request):
        if not auth.signed_in(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(shell("Classes", "classes", ADMIN_BODY, ADMIN_JS, EXTRA_STYLE))

    @router.get("/api/classes")
    def overview(request: Request) -> dict:
        guard(request)
        bot: dict = {"available": admin is not None}
        if admin is not None:
            try:
                st = admin.state()
                bot.update(state=st.state, linked=st.linked, number=st.number, name=st.name)
            except WhatsAppError as e:
                bot.update(state="unreachable", linked=False, detail=str(e))
        with classes.sessions() as s:
            rows = s.scalars(select(SchoolClass).where(SchoolClass.status != ClassStatus.REMOVED)
                             .order_by(SchoolClass.status, SchoolClass.name)).all()
            items = [classes.summary(k) for k in rows]
        return {"bot": bot, "paused_all": classes.paused_all, "classes": items, "invites": classes.invites(),
                "defaults": classes.defaults(), "specs": [s.__dict__ for s in policy.SPECS], "modes": list(policy.MODES)}

    @router.post("/api/classes/bot/link")
    def bot_link(request: Request, body: Body) -> dict:
        guard(request)
        if admin is None:
            _err(ValueError("Set EVOLUTION_API_URL and EVOLUTION_API_KEY first"), 503)
        try:
            c = admin.start_link(_body(body).get("number") or None)
        except WhatsAppError as e:
            _err(e, 502)
        return {"already_linked": c.already_linked, "qr": c.qr_png_base64, "pairing_code": c.pairing_code_display}

    @router.get("/api/classes/bot/qr")
    def bot_qr(request: Request) -> dict:
        guard(request)
        try:
            c = admin.current_challenge() if admin else None
        except WhatsAppError as e:
            _err(e, 502)
        return {"qr": c.qr_png_base64 if c else None}

    @router.post("/api/classes/bot/unlink")
    def bot_unlink(request: Request) -> dict:
        guard(request)
        try:
            admin.unlink()
        except (WhatsAppError, AttributeError) as e:
            _err(e, 502)
        return {"ok": True}

    @router.post("/api/classes/pause-all")
    def pause_all(request: Request, body: Body) -> dict:
        guard(request)
        on = bool(_body(body).get("on"))
        classes.pause_all(on)
        with classes.sessions.begin() as s:
            classes.log(s, None, "admin", "admin", "Every class paused" if on else "Every class back on")
        return {"paused_all": on}

    @router.post("/api/classes/invites")
    def invite_new(request: Request, body: Body) -> dict:
        guard(request)
        b = _body(body)
        try:
            token = classes.create_invite(str(b.get("name") or ""), role=str(b.get("role") or "cr"),
                                          class_id=int(b["class_id"]) if b.get("class_id") else None)
        except (ValueError, LookupError) as e:
            _err(e)
        return {"link": f"{base_url(request)}/join/{token}", "hours": access.INVITE_HOURS}

    @router.post("/api/classes/invites/{invite_id}/revoke")
    def invite_revoke(request: Request, invite_id: int) -> dict:
        guard(request)
        try:
            classes.revoke_invite(invite_id)
        except LookupError as e:
            _err(e, 404)
        return {"ok": True}

    @router.post("/api/classes/{class_id}/status")
    def class_status(request: Request, class_id: int, body: Body) -> dict:
        guard(request)
        status = _body(body).get("status")
        if status not in (ClassStatus.ACTIVE, ClassStatus.PAUSED, ClassStatus.BLOCKED):
            _err(ValueError("Approve, pause or block"))
        try:
            classes.set_status(class_id, status, "admin")
        except (LookupError, PermissionError) as e:
            _err(e)
        return {"ok": True}

    @router.post("/api/classes/{class_id}/remove")
    def class_remove(request: Request, class_id: int) -> dict:
        guard(request)
        try:
            with classes.sessions.begin() as s:
                klass = classes.get(s, class_id)
                classes.tell_crs(s, klass, f"The admin removed the class {klass.name}. Its LMS login and files are deleted.")
            _flush_now()
            classes.remove(class_id, "admin")
        except LookupError as e:
            _err(e, 404)
        return {"ok": True}

    def _flush_now() -> None:
        """Messages to a CR about to be removed go out before their row does."""
        if sender is None:
            return
        from ..db.models import ClassMessage
        from ..notify import NotifyError

        with classes.sessions.begin() as s:
            for m in s.scalars(select(ClassMessage).where(ClassMessage.sent_at.is_(None), ClassMessage.kind == "cr")).all():
                try:
                    sender.text(m.to, m.text)
                    m.sent_at = classes.now()
                except NotifyError:
                    pass

    @router.post("/api/classes/{class_id}/poll")
    def class_poll(request: Request, class_id: int) -> dict:
        guard(request)
        classes.kv.set(poll_key(class_id), classes.now().isoformat())
        return {"ok": True}

    @router.post("/api/classes/rules")
    def rules_default(request: Request, body: Body) -> dict:
        guard(request)
        try:
            classes.save_defaults_for_all(_body(body).get("rules") or {})
        except policy.PolicyError as e:
            _err(e)
        return {"defaults": classes.defaults()}

    @router.post("/api/classes/{class_id}/rules")
    def rules_class(request: Request, class_id: int, body: Body) -> dict:
        guard(request)
        try:
            notes = classes.save_class_rules(class_id, _body(body).get("rules") or {})
        except (policy.PolicyError, LookupError) as e:
            _err(e)
        return {"told_cr": notes}

    @router.get("/api/classes/{class_id}/users")
    def class_users(request: Request, class_id: int) -> dict:
        guard(request)
        try:
            return {"users": classes.users(class_id)}
        except LookupError as e:
            _err(e, 404)

    @router.get("/api/classes/blocks")
    def blocks(request: Request) -> dict:
        guard(request)
        return {"blocks": classes.blocks()}

    @router.post("/api/classes/blocks")
    def block_add(request: Request, body: Body) -> dict:
        guard(request)
        b = _body(body)
        try:
            classes.block(int(b["class_id"]) if b.get("class_id") else None, str(b.get("person") or ""), "admin", "admin")
        except (ValueError, LookupError) as e:
            _err(e)
        return {"ok": True}

    @router.post("/api/classes/blocks/remove")
    def block_remove(request: Request, body: Body) -> dict:
        guard(request)
        b = _body(body)
        classes.unblock(int(b["class_id"]) if b.get("class_id") else None, str(b.get("person") or ""), "admin", "admin")
        return {"ok": True}

    def _log_query(request: Request) -> dict:
        q = request.query_params
        days = int(q.get("days") or 7) if (q.get("days") or "7").isdigit() else 7
        return {"class_id": int(q["class_id"]) if (q.get("class_id") or "").isdigit() else None,
                "person": "".join(ch for ch in q.get("person", "") if ch.isdigit()) or None,
                "kind": q.get("kind") or None, "since": classes.now() - timedelta(days=min(days, 60))}

    @router.get("/api/classes/logs")
    def logs(request: Request) -> dict:
        guard(request)
        return {"logs": classes.logs(**_log_query(request))}

    @router.get("/classes/logs.csv")
    def logs_csv(request: Request):
        if not auth.signed_in(request):
            raise HTTPException(status_code=401, detail="Sign in first")
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["time", "class", "who", "kind", "person", "detail"])
        for r in classes.logs(**_log_query(request), limit=20000):
            w.writerow([r["at"], r["class"], r["actor"], r["kind"], r["person"], r["detail"]])
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="class-log.csv"'})

    # ================= the CR's =================

    def cr_person(request: Request) -> tuple[int, ClassPerson] | None:
        value = cr_cookie.read(request.cookies.get(access.CR_COOKIE), clock())
        if not value or not value.isdigit():
            return None
        with classes.sessions() as s:
            person = s.get(ClassPerson, int(value))
            if person is None or person.klass.status == ClassStatus.REMOVED:
                return None
            return person.class_id, person

    def cr_guard(request: Request) -> tuple[int, ClassPerson]:
        found = cr_person(request)
        if found is None:
            raise HTTPException(status_code=401, detail="Sign in first")
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")
        return found

    def cr_writable(class_id: int) -> None:
        with classes.sessions() as s:
            if s.get(SchoolClass, class_id).status == ClassStatus.BLOCKED:
                raise HTTPException(status_code=403, detail="The admin blocked this class")

    def set_cr_cookie(resp: Response, person_id: int) -> Response:
        resp.set_cookie(access.CR_COOKIE, cr_cookie.make(str(person_id), access.CR_SECONDS, clock()),
                        max_age=access.CR_SECONDS, httponly=True, samesite="strict", secure=settings.public_url is not None
                        and settings.public_url.startswith("https://"))
        return resp

    @router.get("/cr", response_class=HTMLResponse)
    def cr_page(request: Request):
        if cr_person(request) is None:
            return HTMLResponse(_page("Class sign-in", CR_LOGIN_BODY, CR_LOGIN_JS))
        return HTMLResponse(_page("Your class", CR_BODY, CR_JS, signed_in=True))

    @router.post("/api/cr/code")
    def cr_code(request: Request, body: Body) -> dict:
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")
        try:
            number = access.phone(_body(body).get("phone"))
        except ValueError as e:
            _err(e)
        with classes.sessions() as s:
            known = s.scalar(select(ClassPerson).join(SchoolClass).where(
                ClassPerson.phone == number, SchoolClass.status != ClassStatus.REMOVED))
        if known is not None:  # the same answer either way: a stranger learns nothing
            try:
                codes.issue("cr", number, "your class page")
            except ValueError as e:
                _err(e)
        return {"sent": True}

    @router.post("/api/cr/verify")
    def cr_verify(request: Request, body: Body):
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")
        b = _body(body)
        try:
            number = access.phone(b.get("phone"))
        except ValueError as e:
            _err(e)
        if not codes.check("cr", number, str(b.get("code") or "")):
            _err(ValueError("That code is wrong or has expired"))
        with classes.sessions() as s:
            person = s.scalar(select(ClassPerson).join(SchoolClass).where(
                ClassPerson.phone == number, SchoolClass.status != ClassStatus.REMOVED).order_by(ClassPerson.id.desc()))
            if person is None:
                _err(ValueError("This number runs no class"))
            person_id, class_id = person.id, person.class_id
        with classes.sessions.begin() as s:
            classes.log(s, class_id, f"cr:{number}", "cr", "Signed in to the class page", person=number)
        return set_cr_cookie(JSONResponse({"ok": True}), person_id)

    @router.post("/api/cr/logout")
    def cr_logout() -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(access.CR_COOKIE)
        return resp

    @router.get("/api/cr/state")
    def cr_state(request: Request) -> dict:
        class_id, person = cr_guard(request)
        with classes.sessions() as s:
            klass = s.get(SchoolClass, class_id)
            p = classes.policy(klass)
            info = classes.summary(klass)
            info.pop("rules", None)
            announce_left = (p.value("limit_announce") - classes.hits(s, class_id, person.phone, "announce", hours=24)
                             if p.on("announce") else 0)
        return {"me": {"name": person.name, "phone": person.phone, "role": person.role}, "class": info,
                "settings": p.for_cr(), "announce_left": max(0, announce_left), "users": classes.users(class_id),
                "blocks": classes.blocks(class_id, everywhere=False),
                "logs": classes.logs(class_id=class_id, limit=100), "public_url": settings.public_url}

    @router.post("/api/cr/lms")
    def cr_lms(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        if person.role != "cr":
            _err(ValueError("Only the CR signs the class in to the LMS"), 403)
        b = _body(body)
        with classes.sessions() as s:
            klass = s.get(SchoolClass, class_id)
            enrollment, institute = klass.enrollment, klass.institute
        password, remember = str(b.get("password") or ""), bool(b.get("remember"))

        def run():
            done = sign_in(enrollment, password, institute)
            classes.signed_in_again(class_id, enrollment, password if remember else None, done.state, f"cr:{person.phone}")
            return {"ok": True}

        try:
            jobs.start(f"cr:{class_id}", run)
        except ValueError as e:
            _err(e)
        return {"started": True}

    @router.get("/api/cr/lms")
    def cr_lms_state(request: Request) -> dict:
        class_id, _ = cr_guard(request)
        job = jobs.get(f"cr:{class_id}")
        job.pop("result", None)
        return job

    @router.post("/api/cr/forget-password")
    def cr_forget(request: Request) -> dict:
        class_id, person = cr_guard(request)
        classes.forget_password(class_id, f"cr:{person.phone}")
        return {"ok": True}

    def _admin_groups(number: str) -> dict[str, dict]:
        """Groups the class bot is in where this number is an admin: id -> {name, size}."""
        if admin is None:
            raise HTTPException(status_code=503, detail="The class bot is not set up yet")
        from .bot import admin_groups

        try:
            return admin_groups(admin, number)
        except WhatsAppError as e:
            _err(e, 502)

    @router.get("/api/cr/groups")
    def cr_groups(request: Request) -> dict:
        class_id, person = cr_guard(request)
        mine = _admin_groups(person.phone)
        with classes.sessions() as s:
            ticked = {g.group_jid for g in s.get(SchoolClass, class_id).groups}
            from .store import find_group

            taken = {gid for gid in mine if (g := find_group(s, gid)) is not None and g.class_id != class_id}
        return {"groups": [{"id": gid, "name": g["name"], "size": g["size"], "chosen": gid in ticked, "taken": gid in taken}
                           for gid, g in sorted(mine.items(), key=lambda x: x[1]["name"].lower())]}

    @router.post("/api/cr/groups")
    def cr_groups_save(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        picked = [g for g in (_body(body).get("groups") or []) if isinstance(g, dict) and g.get("id")]
        mine = _admin_groups(person.phone)
        try:
            classes.save_groups(class_id, picked, {gid: g["name"] for gid, g in mine.items()}, f"cr:{person.phone}")
        except (PermissionError, LookupError) as e:
            _err(e, 403)
        return {"ok": True}

    @router.post("/api/cr/settings")
    def cr_settings(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        b = _body(body)
        try:
            classes.set_choice(class_id, str(b.get("key")), b.get("value"), f"cr:{person.phone}")
        except (policy.PolicyError, LookupError) as e:
            _err(e)
        return {"ok": True}

    @router.post("/api/cr/mute")
    def cr_mute(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        try:
            classes.mute(class_id, int(_body(body).get("days") or 0), f"cr:{person.phone}")
        except (ValueError, TypeError) as e:
            _err(e)
        return {"ok": True}

    @router.post("/api/cr/pause")
    def cr_pause(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        status = ClassStatus.PAUSED if _body(body).get("on") else ClassStatus.ACTIVE
        try:
            classes.set_status(class_id, status, f"cr:{person.phone}")
        except PermissionError as e:
            _err(e, 403)
        return {"ok": True}

    @router.post("/api/cr/block")
    def cr_block(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        try:
            classes.block(class_id, str(_body(body).get("person") or ""), "cr", f"cr:{person.phone}")
        except ValueError as e:
            _err(e)
        return {"ok": True}

    @router.post("/api/cr/unblock")
    def cr_unblock(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        try:
            classes.unblock(class_id, str(_body(body).get("person") or ""), "cr", f"cr:{person.phone}")
        except PermissionError as e:
            _err(e, 403)
        return {"ok": True}

    @router.post("/api/cr/announce")
    def cr_announce(request: Request, body: Body) -> dict:
        class_id, person = cr_guard(request)
        cr_writable(class_id)
        try:
            count = classes.announce(class_id, person.phone, person.name or "your CR", str(_body(body).get("text") or ""))
        except (ValueError, PermissionError) as e:
            _err(e)
        return {"groups": count}

    @router.post("/api/cr/remove")
    def cr_remove(request: Request, body: Body) -> Response:
        class_id, person = cr_guard(request)
        if person.role != "cr":
            _err(ValueError("Only the CR can remove the class"), 403)
        with classes.sessions() as s:
            name = s.get(SchoolClass, class_id).name
        if str(_body(body).get("confirm") or "").strip().lower() != name.lower():
            _err(ValueError(f"Type the class name ({name}) to confirm"))
        classes.remove(class_id, f"cr:{person.phone}")
        alert(f"{person.name or person.phone} removed the class {name}. Its LMS login and files are deleted.")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(access.CR_COOKIE)
        return resp

    # ================= the invite =================

    def joining(request: Request, token: str) -> tuple[str, str] | None:
        value = join_cookie.read(request.cookies.get(access.JOIN_COOKIE), clock())
        if not value or value.count(":") != 2:
            return None
        tok_hash, number, name = value.split(":", 2)
        if tok_hash != access.token_hash(token)[:32]:
            return None
        return number, name

    def open_invite(token: str):
        with classes.sessions() as s:
            inv = classes.invite(s, token)
            return (inv.name, inv.role) if inv else None

    @router.get("/join/{token}", response_class=HTMLResponse)
    def join_page(token: str):
        found = open_invite(token)
        if found is None:
            return HTMLResponse(_page("Invite", '<div class="card"><h2>This invite link no longer works</h2>'
                                      '<p class="muted">It was used, expired or withdrawn. Ask the admin for a new one.</p></div>'),
                                status_code=404)
        name, role = found
        body = JOIN_BODY.replace("{{NAME}}", html.escape(name)).replace("{{ROLE}}", "deputy CR" if role == "deputy" else "CR")
        return HTMLResponse(_page(f"Join {name}", body, JOIN_JS.replace("{{TOKEN}}", json.dumps(token))
                                  .replace("{{ROLE}}", json.dumps(role))))

    @router.post("/api/join/{token}/code")
    def join_code(request: Request, token: str, body: Body) -> dict:
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")
        if open_invite(token) is None:
            _err(ValueError("This invite link no longer works"), 404)
        b = _body(body)
        try:
            number = access.phone(b.get("phone"))
            if not str(b.get("name") or "").strip():
                raise ValueError("Enter your name")
            codes.issue("join", number, "joining the class bot")
        except ValueError as e:
            _err(e)
        return {"sent": True}

    @router.post("/api/join/{token}/verify")
    def join_verify(request: Request, token: str, body: Body) -> Response:
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")
        found = open_invite(token)
        if found is None:
            _err(ValueError("This invite link no longer works"), 404)
        b = _body(body)
        try:
            number = access.phone(b.get("phone"))
        except ValueError as e:
            _err(e)
        name = " ".join(str(b.get("name") or "").split())[:64].replace(":", " ")
        if not codes.check("join", number, str(b.get("code") or "")):
            _err(ValueError("That code is wrong or has expired"))
        if found[1] == "deputy":
            try:
                class_id, person_id = classes.join_as_deputy(token, number, name)
            except SignupRefused as e:
                _err(e)
            return set_cr_cookie(JSONResponse({"done": True}), person_id)
        resp = JSONResponse({"verified": True})
        resp.set_cookie(access.JOIN_COOKIE, join_cookie.make(f"{access.token_hash(token)[:32]}:{number}:{name}",
                                                             access.JOIN_SECONDS, clock()),
                        max_age=access.JOIN_SECONDS, httponly=True, samesite="strict")
        return resp

    @router.post("/api/join/{token}/lms")
    def join_lms(request: Request, token: str, body: Body) -> dict:
        if request.headers.get("x-lms") != "1":
            raise HTTPException(status_code=403, detail="Missing X-LMS header")
        who = joining(request, token)
        if who is None or open_invite(token) is None:
            _err(ValueError("Prove your number first"), 401)
        number, name = who
        b = _body(body)
        enrollment, password = str(b.get("enrollment") or "").strip(), str(b.get("password") or "")
        institute = str(b.get("institute") or "").strip() or (settings.credentials.institute if settings.credentials else "Karachi Campus")
        remember = bool(b.get("remember"))

        def run():
            done = sign_in(enrollment, password, institute)
            try:
                class_id, person_id = classes.signup(token, number, name, enrollment, institute,
                                                     password if remember else None, done.state, done.courses)
            except SignupRefused as e:
                alert(f"A CR sign-up was refused: {name} (+{number}): {e}")
                raise SignInError(str(e)) from e
            with classes.sessions() as s:
                klass = s.get(SchoolClass, class_id)
                cname, note = klass.name, classes.overlap_note(classes.overlaps(s, klass))
            alert(f"New CR waiting for your approval: {name} (+{number}) for {cname}, {len(done.courses)} courses."
                  + (f"\n{note}" if note else "") + "\nApprove it on the Classes page, or message the class bot: menu.")
            return {"person_id": person_id}

        try:
            jobs.start(f"join:{access.token_hash(token)}", run)
        except ValueError as e:
            _err(e)
        return {"started": True}

    @router.get("/api/join/{token}/lms")
    def join_lms_state(request: Request, token: str) -> Response:
        if joining(request, token) is None:
            _err(ValueError("Prove your number first"), 401)
        key = f"join:{access.token_hash(token)}"
        job = jobs.get(key)
        result = job.pop("result", None)
        if job.get("state") == "done" and result:
            jobs.pop(key)
            resp = set_cr_cookie(JSONResponse({"state": "done"}), result["person_id"])
            resp.delete_cookie(access.JOIN_COOKIE)
            return resp
        return JSONResponse(job)

    return router


def _page(title: str, body: str, script: str = "", *, signed_in: bool = False) -> str:
    out = ('<form method="post" action="/api/cr/logout" onsubmit="event.preventDefault();fetch(\'/api/cr/logout\','
           '{method:\'POST\'}).then(()=>location.href=\'/cr\')"><button>Sign out</button></form>') if signed_in else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{STYLE}{EXTRA_STYLE}</style></head><body>'
            f'<header><h1>Class bot</h1>{out}</header><main>{body}</main><script>{CALL_JS}{script}</script></body></html>')


EXTRA_STYLE = """
table.t { width:100%; border-collapse:collapse; font-size:14px; } table.t th { text-align:left; color:var(--muted); font-weight:600; }
table.t td, table.t th { padding:6px 4px; border-top:1px solid var(--rule); vertical-align:top; }
.scroll { overflow-x:auto; } select { font:inherit; padding:8px; border-radius:8px; border:1px solid var(--rule); background:var(--card); color:var(--ink); }
textarea { font:inherit; width:100%; min-height:90px; padding:9px 10px; border-radius:8px; border:1px solid var(--rule); background:var(--card); color:var(--ink); }
.lock { color:var(--muted); font-size:13px; } label.inline { display:flex; gap:8px; align-items:center; margin:6px 0; }
label.inline input { width:auto; } .kv { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:14px; }
.rule { display:flex; flex-wrap:wrap; gap:6px; align-items:center; padding:8px 0; border-top:1px solid var(--rule); }
.rule.own { box-shadow:inset 3px 0 0 var(--gold); padding-left:8px; }
.rule .name { flex:1 1 220px; } .rule select, .rule input[type=number] { width:auto; }
"""

# ---------------- your page ----------------

ADMIN_BODY = """
<div class="card">
  <h2>Class bot</h2>
  <div>WhatsApp: <span class="state" id="b-state">checking…</span></div>
  <div class="muted" id="b-detail">A second number, only for other classes' groups. Your own number and groups stay as they are.</div>
  <div class="row"><button id="b-qr">Link with a QR code</button><input id="b-number" inputmode="numeric" placeholder="Or its number, e.g. 923001234567"><button class="secondary" id="b-pair">Pairing code</button></div>
  <div class="step" id="b-step" hidden><div id="b-how"></div><img class="qr" id="b-img" hidden alt="QR code"><div class="code" id="b-code" hidden></div></div>
  <div class="row"><button class="secondary" id="b-unlink" hidden>Unlink</button><button class="secondary" id="b-pause">Pause every class</button></div>
  <div class="err" id="b-err"></div>
</div>
<div class="card">
  <h2>Invite a CR</h2>
  <div class="muted">Each link works once, for 48 hours. The CR proves their number with a WhatsApp code, signs in to the LMS, and the class waits for your approval.</div>
  <div class="row"><input id="i-name" placeholder="Class name, e.g. BSCS 5-B"><button id="i-make">Make invite link</button></div>
  <div class="row"><select id="i-class"><option value="">Deputy for…</option></select><button class="secondary" id="i-deputy">Make deputy link</button></div>
  <div class="step" id="i-out" hidden><div>Send this link to them (it is shown once):</div><p><code id="i-link"></code></p><button class="secondary" id="i-copy">Copy</button></div>
  <div id="i-list" class="scroll" style="margin-top:10px"></div>
  <div class="err" id="i-err"></div>
</div>
<div class="card"><h2>Classes</h2><div id="c-list" class="muted">Loading…</div><div class="err" id="c-err"></div></div>
<div class="card" id="rules-card">
  <h2 id="r-title">Default rules for every class</h2>
  <div class="muted" id="r-help">Always on and always off are yours; CR decides lets the CR switch it (starting where you set). Limits: the CR may only go lower if you allow it.
    For one class, a row marked default follows the defaults; change it and it becomes that class's own rule (marked in gold).</div>
  <div class="row"><select id="r-which"><option value="">Defaults (every class)</option></select></div>
  <div id="r-list"></div>
  <div class="row"><button id="r-save">Save rules</button></div>
  <div class="err" id="r-err"></div>
</div>
<div class="card">
  <h2>Blocked people</h2>
  <div class="row"><input id="k-person" inputmode="numeric" placeholder="Their number"><select id="k-class"><option value="">Everywhere</option></select><button id="k-add">Block</button></div>
  <div id="k-list" class="scroll" style="margin-top:8px"></div>
  <div class="err" id="k-err"></div>
</div>
<div class="card">
  <h2>Usage</h2>
  <div class="row"><select id="u-class"></select><button class="secondary" id="u-load">Show who uses it</button></div>
  <div id="u-list" class="scroll" style="margin-top:8px"></div>
</div>
<div class="card">
  <h2>Log</h2>
  <div class="row small"><select id="l-class"><option value="">All classes</option></select>
    <select id="l-kind"><option value="">Everything</option><option value="admin">Your changes</option><option value="cr">CR actions</option>
      <option value="sent">Bot messages</option><option value="menu">Menu use</option><option value="limit">Limits</option>
      <option value="blocked">Blocked</option><option value="problem">Problems</option></select>
    <input id="l-person" inputmode="numeric" placeholder="Number" style="max-width:160px"><select id="l-days"><option>1</option><option selected>7</option><option>30</option><option>60</option></select>
    <button class="secondary" id="l-load">Show</button><a class="btn" id="l-csv" href="/classes/logs.csv">CSV</a></div>
  <div id="l-list" class="scroll" style="margin-top:8px"></div>
</div>
"""

ADMIN_JS = r"""
let data = null, qrTimer = null, rulesDirty = false;
const when = (t) => t ? new Date(t).toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "—";
const chip = (text, cls) => '<span class="chip ' + (cls || "") + '">' + esc(text) + "</span>";
const STATUS = { pending: ["waiting for approval", "wait"], active: ["active", "ok"], paused: ["paused", "wait"], blocked: ["blocked", "bad"] };
async function load() {
  try { data = await call("/api/classes"); } catch (e) { $("c-err").textContent = e.message; return; }
  const b = data.bot;
  $("b-state").textContent = !b.available ? "not configured" : b.linked ? "linked" : (b.state === "unreachable" ? "Evolution API not reachable" : "not linked");
  $("b-state").className = "state " + (b.linked ? "ok" : "bad");
  if (b.linked) { $("b-detail").textContent = [b.name, b.number && "+" + b.number].filter(Boolean).join(" · "); $("b-step").hidden = true; clearInterval(qrTimer); }
  $("b-unlink").hidden = !b.linked;
  $("b-pause").textContent = data.paused_all ? "Restart every class" : "Pause every class";
  $("b-pause").className = data.paused_all ? "" : "secondary";
  const opts = data.classes.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "</option>").join("");
  for (const id of ["i-class", "k-class", "l-class", "r-which"]) {
    const keep = $(id).value, first = $(id).options[0].outerHTML; $(id).innerHTML = first + opts; $(id).value = keep; }
  const keepU = $("u-class").value; $("u-class").innerHTML = opts; if (keepU) $("u-class").value = keepU;
  $("i-list").innerHTML = data.invites.length ? '<table class="t"><tr><th>Invite</th><th>For</th><th>State</th><th></th></tr>' +
    data.invites.map((i) => "<tr><td>" + esc(i.name) + "</td><td>" + (i.role === "deputy" ? "deputy" : "CR") + "</td><td>" + esc(i.state) +
      (i.used_by ? " by +" + esc(i.used_by) : i.state === "open" ? " · until " + when(i.expires_at) : "") + "</td><td>" +
      (i.state === "open" ? '<button class="secondary" data-revoke="' + i.id + '">Withdraw</button>' : "") + "</td></tr>").join("") + "</table>" : "";
  $("c-list").innerHTML = data.classes.length ? data.classes.map(card).join("") : "No classes yet. Make an invite link above.";
  if (!rulesDirty) renderRules();  // the refresh never throws away rules you are still editing
}
function card(c) {
  const [word, cls] = STATUS[c.status] || [c.status, ""];
  const login = c.login_state === "ok" ? chip("LMS ok", "ok") : chip(c.login_state === "needs_signin" ? "LMS sign-in needed" : "LMS error", "bad");
  const btn = (label, status, sec) => '<button class="' + (sec ? "secondary" : "") + '" data-status="' + status + '" data-id="' + c.id + '">' + label + "</button>";
  const actions = c.status === "pending" ? btn("Approve", "active") + btn("Block", "blocked", 1)
    : c.status === "active" ? btn("Pause", "paused", 1) + btn("Block", "blocked", 1)
    : btn(c.status === "blocked" ? "Unblock" : "Resume", "active") ;
  return '<div class="item"><h3>' + esc(c.name) + "</h3>" + chip(word, cls) + chip("bot: " + c.mode) + login +
    (c.muted_until ? chip("muted until " + when(c.muted_until), "wait") : "") +
    '<div class="kv muted" style="margin-top:6px"><span>CR</span><span>' + c.people.map((p) => esc(p.name) + " +" + esc(p.phone) + (p.role === "deputy" ? " (deputy)" : "")).join(", ") + "</span>" +
    "<span>LMS</span><span>" + esc(c.enrollment) + " · " + esc(c.semester || "") + " · password " + (c.remembers_password ? "kept" : "not kept") + "</span>" +
    "<span>Groups</span><span>" + (c.groups.map((g) => esc(g.name)).join(", ") || "none ticked yet") + "</span>" +
    "<span>Courses</span><span>" + (c.courses.map(esc).join(", ") || "—") + "</span>" +
    (c.overlap ? "<span>Note</span><span" + (c.overlap.startsWith("⚠️") ? ' class="state bad"' : "") + ">" + esc(c.overlap) + "</span>" : "") +
    "<span>Checked</span><span>" + when(c.last_poll_at) + (c.last_error ? ' · <span class="state bad">' + esc(c.last_error) + "</span>" : "") + "</span></div>" +
    '<div class="row small">' + actions + '<button class="secondary" data-rules="' + c.id + '">Rules</button><button class="secondary" data-poll="' + c.id +
    '">Check now</button><button class="secondary" data-logs="' + c.id + '">Log</button><button class="secondary" data-remove="' + c.id + '" data-name="' + esc(c.name) + '">Remove</button></div></div>';
}
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button"); if (!t) return;
  try {
    if (t.dataset.status) { if (t.dataset.status !== "active" && !confirm("Are you sure?")) return;
      await call("/api/classes/" + t.dataset.id + "/status", { status: t.dataset.status }); await load(); }
    else if (t.dataset.remove) { if (prompt("This deletes the class's LMS login and files. Type its name to confirm:") !== t.dataset.name) return;
      await call("/api/classes/" + t.dataset.remove + "/remove", {}); await load(); }
    else if (t.dataset.poll) { await call("/api/classes/" + t.dataset.poll + "/poll", {}); t.textContent = "Asked"; }
    else if (t.dataset.rules) { $("r-which").value = t.dataset.rules; renderRules(); $("rules-card").scrollIntoView({ behavior: "smooth" }); }
    else if (t.dataset.logs) { $("l-class").value = t.dataset.logs; await loadLogs(); }
    else if (t.dataset.revoke) { await call("/api/classes/invites/" + t.dataset.revoke + "/revoke", {}); await load(); }
    else if (t.dataset.unblock !== undefined) { await call("/api/classes/blocks/remove", { person: t.dataset.unblock, class_id: t.dataset.cls || null }); await loadBlocks(); }
    else if (t.dataset.blockperson) { await call("/api/classes/blocks", { person: t.dataset.blockperson, class_id: t.dataset.cls }); await loadUsers(); await loadBlocks(); }
  } catch (e) { alert(e.message); }
});
function ruleRow(spec, rule, inherited) {
  const k = spec.key, use = inherited ? '<label class="inline"><input type="checkbox" data-inherit="' + k + '"' + (rule ? "" : " checked") + "> default</label>" : "";
  const r = rule || (data.defaults[k]) || {};
  let ctl = "";
  if (spec.kind === "switch") {
    ctl = '<select data-lock="' + k + '"><option value="on">Always on</option><option value="off">Always off</option><option value="cr">CR decides</option></select>' +
      '<select data-default="' + k + '"><option value="1">starts on</option><option value="0">starts off</option></select>' +
      '<label class="inline"><input type="checkbox" data-hide="' + k + '"> hide from CR</label>';
  } else if (spec.kind === "limit") {
    ctl = '<input type="number" min="0" max="1000" data-max="' + k + '" style="width:90px"><label class="inline"><input type="checkbox" data-cr="' + k + '"> CR may lower</label>';
  } else {
    ctl = '<select data-mode="' + k + '">' + data.modes.map((m) => '<option value="' + m + '">' + ({ off: "Off", notify: "Notices only", menu: "Notices and menu" }[m]) + "</option>").join("") +
      '</select><label class="inline"><input type="checkbox" data-cr="' + k + '"> CR may pick less</label>';
  }
  return '<div class="rule' + (inherited && rule ? " own" : "") + '" data-key="' + k + '"><div class="name"><b>' + esc(spec.label) + '</b><div class="muted">' + esc(spec.section) + (spec.help ? " · " + esc(spec.help) : "") + "</div></div>" + ctl + use + "</div>";
}
function renderRules() {
  if (!data) return;
  const cid = $("r-which").value, klass = data.classes.find((c) => String(c.id) === cid);
  $("r-which").dataset.shown = cid;
  $("r-title").textContent = klass ? "Rules for " + klass.name : "Default rules for every class";
  $("r-list").innerHTML = data.specs.map((s) => ruleRow(s, klass ? klass.rules[s.key] : data.defaults[s.key], !!klass)).join("");
  for (const s of data.specs) {
    const r = (klass ? klass.rules[s.key] : null) || data.defaults[s.key] || {}, q = (sel) => document.querySelector(sel + '="' + s.key + '"]');
    if (s.kind === "switch") { q("[data-lock").value = r.lock || "cr"; q("[data-default").value = r.default === false ? "0" : "1"; q("[data-hide").checked = !!r.hide; }
    else if (s.kind === "limit") { q("[data-max").value = r.max ?? 0; q("[data-cr").checked = !!r.cr; }
    else { q("[data-mode").value = r.max || "notify"; q("[data-cr").checked = !!r.cr; }
  }
}
$("r-which").onchange = () => {
  if (rulesDirty && !confirm("Drop the changes you have not saved?")) { $("r-which").value = $("r-which").dataset.shown || ""; return; }
  rulesDirty = false; renderRules();
};
// Changing a row means you want your own rule for it: untick "default" for that row
$("r-list").addEventListener("change", (ev) => {
  const t = ev.target, row = t.closest(".rule"); if (!row) return;
  rulesDirty = true; $("r-err").textContent = "Not saved yet.";
  if (t.dataset.inherit) { row.classList.toggle("own", !t.checked); return; }
  const inherit = row.querySelector("[data-inherit]");
  if (inherit && inherit.checked) { inherit.checked = false; row.classList.add("own"); }
});
$("r-save").onclick = async () => {
  $("r-err").textContent = ""; const cid = $("r-which").value, rules = {};
  for (const s of data.specs) {
    const q = (sel) => document.querySelector(sel + '="' + s.key + '"]'), inherit = q("[data-inherit");
    if (inherit && inherit.checked) { rules[s.key] = null; continue; }
    if (s.kind === "switch") rules[s.key] = { lock: q("[data-lock").value, default: q("[data-default").value === "1", hide: q("[data-hide").checked };
    else if (s.kind === "limit") rules[s.key] = { max: Number(q("[data-max").value), cr: q("[data-cr").checked };
    else rules[s.key] = { max: q("[data-mode").value, cr: q("[data-cr").checked };
  }
  try {
    let note;
    if (cid) { const r = await call("/api/classes/" + cid + "/rules", { rules }); const own = Object.values(rules).filter(Boolean).length;
      note = "Saved: " + own + " rule" + (own === 1 ? "" : "s") + " of this class's own" + (r.told_cr.length ? "; the CR was told: " + r.told_cr.join("; ") : "."); }
    else { await call("/api/classes/rules", { rules: Object.fromEntries(Object.entries(rules).filter(([, v]) => v)) }); note = "Saved for every class."; }
    rulesDirty = false;
    await load();
    $("r-err").textContent = note;
  } catch (e) { $("r-err").textContent = e.message; }
};
async function link(number) {
  $("b-err").textContent = "";
  try {
    const r = await call("/api/classes/bot/link", { number });
    if (r.already_linked) { await load(); return; }
    $("b-step").hidden = false; $("b-img").hidden = !r.qr || !!r.pairing_code; $("b-code").hidden = !r.pairing_code;
    if (r.pairing_code) { $("b-code").textContent = r.pairing_code; $("b-how").textContent = "On the class bot's phone: WhatsApp → Linked devices → Link a device → Link with phone number instead, then type this code."; }
    else { $("b-img").src = r.qr; $("b-how").textContent = "On the class bot's phone: WhatsApp → Linked devices → Link a device, then scan this.";
      clearInterval(qrTimer); qrTimer = setInterval(async () => { try { const q = await call("/api/classes/bot/qr"); if (q.qr) $("b-img").src = q.qr; await load(); } catch (e) {} }, 15000); }
  } catch (e) { $("b-err").textContent = e.message; }
}
$("b-qr").onclick = () => link(null);
$("b-pair").onclick = () => link($("b-number").value);
$("b-unlink").onclick = async () => { if (!confirm("Unlink the class bot's WhatsApp?")) return; try { await call("/api/classes/bot/unlink", {}); await load(); } catch (e) { $("b-err").textContent = e.message; } };
$("b-pause").onclick = async () => { const on = !data.paused_all; if (on && !confirm("Pause every class? Nothing is checked, sent or answered until you restart.")) return;
  try { await call("/api/classes/pause-all", { on }); await load(); } catch (e) { $("b-err").textContent = e.message; } };
async function invite(body) {
  $("i-err").textContent = "";
  try { const r = await call("/api/classes/invites", body); $("i-link").textContent = r.link; $("i-out").hidden = false; await load(); }
  catch (e) { $("i-err").textContent = e.message; }
}
$("i-make").onclick = () => invite({ name: $("i-name").value, role: "cr" });
$("i-deputy").onclick = () => $("i-class").value ? invite({ role: "deputy", class_id: Number($("i-class").value) }) : ($("i-err").textContent = "Pick the class first");
$("i-copy").onclick = () => navigator.clipboard && navigator.clipboard.writeText($("i-link").textContent);
async function loadBlocks() {
  try { const r = await call("/api/classes/blocks");
    $("k-list").innerHTML = r.blocks.length ? '<table class="t"><tr><th>Number</th><th>Where</th><th>By</th><th>When</th><th></th></tr>' + r.blocks.map((b) =>
      "<tr><td>+" + esc(b.person) + "</td><td>" + esc(b.class) + "</td><td>" + esc(b.by) + "</td><td>" + when(b.at) + '</td><td><button class="secondary" data-unblock="' +
      esc(b.person) + '" data-cls="' + (b.class_id ?? "") + '">Unblock</button></td></tr>').join("") + "</table>" : '<span class="muted">No one is blocked.</span>';
  } catch (e) { $("k-err").textContent = e.message; }
}
$("k-add").onclick = async () => { $("k-err").textContent = "";
  try { await call("/api/classes/blocks", { person: $("k-person").value, class_id: $("k-class").value || null }); $("k-person").value = ""; await loadBlocks(); }
  catch (e) { $("k-err").textContent = e.message; } };
async function loadUsers() {
  const cid = $("u-class").value; if (!cid) return;
  try { const r = await call("/api/classes/" + cid + "/users");
    $("u-list").innerHTML = r.users.length ? '<table class="t"><tr><th>Name</th><th>Number</th><th>Where</th><th>Requests</th><th>Last pick</th><th>Last seen</th><th></th></tr>' +
      r.users.map((u) => "<tr><td>" + esc(u.name || "—") + "</td><td>+" + esc(u.person) + "</td><td>" + esc(u.where) + "</td><td>" + u.requests + "</td><td>" + esc(u.last_option || "—") +
        "</td><td>" + when(u.last_at) + "</td><td>" + (u.blocked ? "blocked" : '<button class="secondary" data-blockperson="' + esc(u.person) + '" data-cls="' + cid + '">Block</button>') + "</td></tr>").join("") + "</table>"
      : '<span class="muted">No one has used this class\'s menu yet.</span>';
  } catch (e) { $("u-list").textContent = e.message; }
}
$("u-load").onclick = loadUsers;
function logQuery() { return "class_id=" + $("l-class").value + "&kind=" + $("l-kind").value + "&person=" + encodeURIComponent($("l-person").value) + "&days=" + $("l-days").value; }
async function loadLogs() {
  $("l-csv").href = "/classes/logs.csv?" + logQuery();
  try { const r = await call("/api/classes/logs?" + logQuery());
    $("l-list").innerHTML = r.logs.length ? '<table class="t"><tr><th>When</th><th>Class</th><th>Who</th><th>What</th></tr>' + r.logs.map((l) =>
      "<tr><td>" + when(l.at) + "</td><td>" + esc(l.class) + "</td><td>" + esc(l.actor) + "</td><td>" + chip(l.kind) + " " + esc(l.detail) + "</td></tr>").join("") + "</table>"
      : '<span class="muted">Nothing logged.</span>';
  } catch (e) { $("l-list").textContent = e.message; }
}
$("l-load").onclick = loadLogs;
load(); loadBlocks(); loadLogs(); setInterval(load, 20000);
"""

# ---------------- the CR's page ----------------

CR_LOGIN_BODY = """
<div class="card"><h2>Sign in to your class page</h2>
  <div class="muted">For CRs and deputies. The class bot sends a code to your WhatsApp.</div>
  <div class="row"><input id="p" inputmode="numeric" autocomplete="tel" placeholder="Your WhatsApp number, e.g. 03001234567"><button id="send">Send code</button></div>
  <div class="row" id="code-row" hidden><input id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code"><button id="verify">Sign in</button></div>
  <div class="err" id="err"></div></div>
"""

CR_LOGIN_JS = """
$("send").onclick = async () => { $("err").textContent = ""; busy($("send"), true);
  try { await call("/api/cr/code", { phone: $("p").value }); $("code-row").hidden = false; $("err").textContent = "If this number runs a class, a code is on its way on WhatsApp."; }
  catch (e) { $("err").textContent = e.message; } busy($("send"), false); };
$("verify").onclick = async () => { $("err").textContent = "";
  try { await call("/api/cr/verify", { phone: $("p").value, code: $("code").value }); location.reload(); } catch (e) { $("err").textContent = e.message; } };
"""

CR_BODY = """
<div class="card"><h2 id="name">Your class</h2><div id="status"></div><div class="muted" id="who"></div>
  <div class="row small"><button class="secondary" id="pause"></button><select id="mute"><option value="0">Notices on</option><option value="1">Pause notices 1 day</option>
    <option value="3">Pause notices 3 days</option><option value="7">Pause notices 7 days</option><option value="14">Pause notices 14 days</option></select></div>
  <div class="err" id="s-err"></div></div>
<div class="card" id="lms-card"><h2>LMS sign-in</h2><div id="lms-state"></div>
  <div class="muted">The bot reads your class's assignments with your LMS account. It never submits, deletes or changes anything there.</div>
  <div class="row"><input id="lms-pass" type="password" autocomplete="current-password" placeholder="Your LMS password"><button id="lms-go">Sign in again</button></div>
  <label class="inline"><input type="checkbox" id="lms-remember"> Remember my password (kept encrypted, so the bot never needs you to sign in again)</label>
  <div class="row small"><button class="secondary" id="lms-forget" hidden>Forget my kept password</button></div>
  <div class="err" id="lms-err"></div></div>
<div class="card"><h2>Groups</h2>
  <div class="muted">Add the class bot's number to your class group and make sure you are a group admin, then tick the group here.</div>
  <div id="g-list" class="muted" style="margin-top:8px"></div>
  <div class="row"><button class="secondary" id="g-load">Show my groups</button><button id="g-save" hidden>Save</button></div>
  <div class="err" id="g-err"></div></div>
<div class="card"><h2>Settings</h2><div class="muted">Settings with a 🔒 are set by the admin.</div><div id="set-list"></div><div class="err" id="set-err"></div></div>
<div class="card" id="ann-card" hidden><h2>Announcement</h2><div class="muted" id="ann-left"></div>
  <textarea id="ann-text" maxlength="1500" placeholder="E.g. Tomorrow's class is cancelled."></textarea>
  <div class="row"><button id="ann-send">Send to the class groups</button></div><div class="err" id="ann-err"></div></div>
<div class="card"><h2>People using the bot</h2><div id="u-list" class="scroll muted"></div><div class="err" id="u-err"></div></div>
<div class="card"><h2>Log</h2><div id="l-list" class="scroll muted"></div></div>
<div class="card"><h2>Remove my class</h2><div class="muted">Deletes your LMS login and everything the bot fetched for your class, and stops the bot for it.</div>
  <div class="row"><input id="rm-name" placeholder="Type the class name to confirm"><button class="secondary" id="rm-go">Remove</button></div><div class="err" id="rm-err"></div></div>
"""

CR_JS = r"""
let st = null, lmsTimer = null;
const when = (t) => t ? new Date(t).toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "—";
const MODES = { off: "Off", notify: "Notices only", menu: "Notices and menu" };
async function load() {
  try { st = await call("/api/cr/state"); } catch (e) { $("s-err").textContent = e.message; return; }
  const c = st.class, me = st.me;
  $("name").textContent = c.name;
  const words = { pending: "Waiting for the admin's approval", active: "Active", paused: c.paused_by === "admin" ? "Paused by the admin" : "Paused by you", blocked: "Blocked by the admin" };
  $("status").innerHTML = '<span class="state ' + (c.status === "active" ? "ok" : "bad") + '">' + esc(words[c.status] || c.status) + "</span>" +
    (c.muted_until ? ' · <span class="state bad">notices paused until ' + when(c.muted_until) + "</span>" : "");
  $("who").textContent = (me.role === "deputy" ? "Deputy: " : "CR: ") + me.name + " (+" + me.phone + ") · " + (c.courses.length) + " courses · last checked " + when(c.last_poll_at);
  $("pause").hidden = c.status === "pending" || c.status === "blocked" || (c.status === "paused" && c.paused_by === "admin");
  $("pause").textContent = c.status === "paused" ? "Resume the bot" : "Pause the bot";
  $("mute").value = "0";
  $("lms-card").hidden = me.role !== "cr";
  $("lms-state").innerHTML = c.login_state === "ok" ? '<span class="state ok">Signed in</span> · password ' + (c.remembers_password ? "kept" : "not kept")
    : '<span class="state bad">Sign in again</span> · ' + esc(c.login_error || "");
  $("lms-forget").hidden = !c.remembers_password;
  $("set-list").innerHTML = st.settings.map(setting).join("");
  $("ann-card").hidden = !st.settings.find((s) => s.key === "announce" && s.value);
  $("ann-left").textContent = st.announce_left + " left today. Each goes to every ticked group, labelled as from you.";
  $("u-list").innerHTML = st.users.length ? '<table class="t"><tr><th>Name</th><th>Number</th><th>Where</th><th>Requests</th><th>Last pick</th><th></th></tr>' + st.users.map((u) =>
    "<tr><td>" + esc(u.name || "—") + "</td><td>+" + esc(u.person) + "</td><td>" + esc(u.where) + "</td><td>" + u.requests + "</td><td>" + esc(u.last_option || "—") + "</td><td>" +
    '<button class="secondary" data-' + (u.blocked ? "unblock" : "block") + '="' + esc(u.person) + '">' + (u.blocked ? "Unblock" : "Block") + "</button></td></tr>").join("") + "</table>"
    : "No one has used the menu yet.";
  $("l-list").innerHTML = st.logs.length ? '<table class="t">' + st.logs.map((l) => "<tr><td>" + when(l.at) + "</td><td>" + esc(l.actor) + "</td><td>" + esc(l.detail) + "</td></tr>").join("") + "</table>" : "Nothing yet.";
}
function setting(s) {
  const lock = s.editable ? "" : ' <span class="lock">🔒 set by the admin</span>';
  let ctl;
  if (s.kind === "switch") ctl = '<label class="inline"><input type="checkbox" data-key="' + s.key + '"' + (s.value ? " checked" : "") + (s.editable ? "" : " disabled") + "> " + (s.value ? "on" : "off") + "</label>";
  else if (s.kind === "limit") ctl = '<input type="number" min="0" max="' + s.max + '" value="' + s.value + '" data-key="' + s.key + '" style="width:90px"' + (s.editable ? "" : " disabled") + '> <span class="muted">at most ' + s.max + "</span>";
  else ctl = '<select data-key="' + s.key + '"' + (s.editable ? "" : " disabled") + ">" + s.choices.map((m) => '<option value="' + m + '"' + (m === s.value ? " selected" : "") + ">" + MODES[m] + "</option>").join("") + "</select>";
  return '<div class="rule"><div class="name"><b>' + esc(s.label) + "</b>" + lock + (s.help ? '<div class="muted">' + esc(s.help) + "</div>" : "") + "</div>" + ctl + "</div>";
}
document.addEventListener("change", async (ev) => {
  const t = ev.target; if (!t.dataset.key) return; $("set-err").textContent = "";
  const value = t.type === "checkbox" ? t.checked : t.type === "number" ? Number(t.value) : t.value;
  try { await call("/api/cr/settings", { key: t.dataset.key, value }); } catch (e) { $("set-err").textContent = e.message; }
  await load();
});
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button"); if (!t) return;
  try {
    if (t.dataset.block) { if (!confirm("Block +" + t.dataset.block + " from the bot in your class?")) return; await call("/api/cr/block", { person: t.dataset.block }); await load(); }
    else if (t.dataset.unblock) { await call("/api/cr/unblock", { person: t.dataset.unblock }); await load(); }
  } catch (e) { $("u-err").textContent = e.message; }
});
$("pause").onclick = async () => { $("s-err").textContent = "";
  try { await call("/api/cr/pause", { on: st.class.status !== "paused" }); await load(); } catch (e) { $("s-err").textContent = e.message; } };
$("mute").onchange = async () => { try { await call("/api/cr/mute", { days: Number($("mute").value) }); await load(); } catch (e) { $("s-err").textContent = e.message; } };
$("lms-go").onclick = async () => { $("lms-err").textContent = ""; busy($("lms-go"), true);
  try { await call("/api/cr/lms", { password: $("lms-pass").value, remember: $("lms-remember").checked }); $("lms-pass").value = "";
    $("lms-err").textContent = "Signing in… this takes about half a minute.";
    clearInterval(lmsTimer); lmsTimer = setInterval(async () => { const j = await call("/api/cr/lms");
      if (j.state === "running") return; clearInterval(lmsTimer); busy($("lms-go"), false);
      $("lms-err").textContent = j.state === "done" ? "Signed in. Notices continue." : (j.error || ""); await load(); }, 3000);
  } catch (e) { $("lms-err").textContent = e.message; busy($("lms-go"), false); } };
$("lms-forget").onclick = async () => { if (!confirm("Delete your kept password? The bot keeps working until the LMS signs it out.")) return;
  try { await call("/api/cr/forget-password", {}); await load(); } catch (e) { $("lms-err").textContent = e.message; } };
$("g-load").onclick = async () => { $("g-err").textContent = ""; busy($("g-load"), true);
  try { const r = await call("/api/cr/groups");
    $("g-list").innerHTML = r.groups.length ? r.groups.map((g) => '<label class="inline" style="color:var(--ink)"><input type="checkbox" data-gid="' + esc(g.id) + '" data-gname="' + esc(g.name) + '"' +
      (g.chosen ? " checked" : "") + (g.taken ? " disabled" : "") + "> " + esc(g.name) + ' <span class="muted">(' + g.size + " members" + (g.taken ? ", another class's" : "") + ")</span></label>").join("")
      : "No groups yet where the bot is a member and you are an admin.";
    $("g-save").hidden = !r.groups.length; } catch (e) { $("g-err").textContent = e.message; } busy($("g-load"), false); };
$("g-save").onclick = async () => { $("g-err").textContent = "";
  const groups = [...document.querySelectorAll("#g-list input[data-gid]:checked")].map((i) => ({ id: i.dataset.gid, name: i.dataset.gname }));
  try { await call("/api/cr/groups", { groups }); $("g-err").textContent = "Saved."; await load(); } catch (e) { $("g-err").textContent = e.message; } };
$("ann-send").onclick = async () => { $("ann-err").textContent = ""; if (!confirm("Send this to every class group?")) return;
  try { const r = await call("/api/cr/announce", { text: $("ann-text").value }); $("ann-text").value = ""; $("ann-err").textContent = "Queued for " + r.groups + " groups."; await load(); }
  catch (e) { $("ann-err").textContent = e.message; } };
$("rm-go").onclick = async () => { $("rm-err").textContent = ""; if (!confirm("Remove your class for good?")) return;
  try { await call("/api/cr/remove", { confirm: $("rm-name").value }); location.href = "/cr"; } catch (e) { $("rm-err").textContent = e.message; } };
load(); setInterval(load, 30000);
"""

# ---------------- the invite ----------------

JOIN_BODY = """
<div class="card"><h2>Join the class bot: {{NAME}}</h2>
  <div class="muted">You were invited as the {{ROLE}}. The bot will post your class's assignments, reminders and summaries to your class group,
    within the rules the admin sets. It only reads the LMS: it never submits, deletes or changes anything, and never solves assignments.</div>
  <div class="step"><b>1. Your WhatsApp number</b>
    <div class="row"><input id="j-name" autocomplete="name" placeholder="Your name"></div>
    <div class="row"><input id="j-phone" inputmode="numeric" autocomplete="tel" placeholder="e.g. 03001234567"><button id="j-send">Send code</button></div>
    <div class="row" id="j-code-row" hidden><input id="j-code" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code from the class bot"><button id="j-verify">Confirm</button></div>
  </div>
  <div class="step" id="j-lms" hidden><b>2. Sign in to the LMS</b>
    <div class="muted">With your own CMS login. The bot uses it only to read your class's assignment lists and files.</div>
    <div class="row"><input id="j-enr" autocomplete="username" placeholder="Enrollment, e.g. 02-000000-000"></div>
    <div class="row"><input id="j-pass" type="password" autocomplete="current-password" placeholder="Password"></div>
    <div class="row"><input id="j-inst" list="campuses" placeholder="Campus (Karachi Campus)"><datalist id="campuses"><option>Karachi Campus</option><option>Islamabad E-8 Campus</option><option>Islamabad H-11 Campus</option><option>Lahore Campus</option></datalist></div>
    <label class="inline"><input type="checkbox" id="j-remember"> Remember my password (encrypted). Without it only the login session is kept, and you sign in again when the LMS ends it.</label>
    <div class="row"><button id="j-go">Sign in and join</button></div>
  </div>
  <div class="err" id="j-err"></div></div>
"""

JOIN_JS = r"""
const TOKEN = {{TOKEN}}, ROLE = {{ROLE}}, base = "/api/join/" + encodeURIComponent(TOKEN);
let timer = null;
$("j-send").onclick = async () => { $("j-err").textContent = ""; busy($("j-send"), true);
  try { await call(base + "/code", { phone: $("j-phone").value, name: $("j-name").value }); $("j-code-row").hidden = false; $("j-err").textContent = "Code sent on WhatsApp."; }
  catch (e) { $("j-err").textContent = e.message; } busy($("j-send"), false); };
$("j-verify").onclick = async () => { $("j-err").textContent = "";
  try { const r = await call(base + "/verify", { phone: $("j-phone").value, name: $("j-name").value, code: $("j-code").value });
    if (r.done) { location.href = "/cr"; return; } $("j-lms").hidden = false; $("j-err").textContent = ""; }
  catch (e) { $("j-err").textContent = e.message; } };
$("j-go").onclick = async () => { $("j-err").textContent = ""; busy($("j-go"), true);
  try { await call(base + "/lms", { enrollment: $("j-enr").value, password: $("j-pass").value, institute: $("j-inst").value, remember: $("j-remember").checked });
    $("j-err").textContent = "Signing in to the LMS… this takes about half a minute.";
    clearInterval(timer); timer = setInterval(async () => { let j; try { j = await call(base + "/lms"); } catch (e) { return; }
      if (j.state === "running") return; clearInterval(timer); busy($("j-go"), false);
      if (j.state === "done") location.href = "/cr"; else $("j-err").textContent = j.error || "Something went wrong"; }, 3000);
  } catch (e) { $("j-err").textContent = e.message; busy($("j-go"), false); } };
"""
