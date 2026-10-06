"""LMS worker commands.

    lms-worker run              poll the LMS on schedule and solve until stopped (the worker service)
    lms-worker solve            solve the next waiting assignment now
    lms-worker api              answer WhatsApp messages through the menu (the api service)
    lms-worker poll             run one poll cycle now
    lms-worker status           last poll, pauses, session and recent jobs
    lms-worker resume           clear a polling pause after the LMS layout changed
    lms-worker db-upgrade       create or migrate the database tables
    lms-worker notify-test      send a test message to each configured channel
    lms-worker whatsapp         link the bot's WhatsApp (QR or phone number), show or remove the link

    lms-worker probe            read the CMS login form; submits nothing
    lms-worker login            log in through CMS and save the LMS session
    lms-worker check            is the saved LMS session still valid?
    lms-worker list             list this semester's courses and assignments
    lms-worker paste-session    save a PHPSESSID copied from your browser
    lms-worker retry            clear the failed-login pause
    lms-worker record           open a browser and record LMS traffic for mapping

With DATABASE_URL set, state is kept in PostgreSQL; without it, in STATE_DIR files.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from . import db, notify, runtime
from .config import load_settings
from .lms.cms_login import inspect_login_html
from .lms.errors import LMSError
from .lms.session import USER_AGENT, session_is_valid
from .config import ConfigError
from .runtime import Runtime
from .schedule import run_forever
from .whatsapp import EvolutionAdmin, WhatsAppError
from .watcher import HEARTBEAT_KEY, POLL_NOW_KEY, POLL_PAUSE_KEY, PollResult


def cmd_probe(rt: Runtime, args) -> int:
    settings = rt.settings
    r = httpx.get(settings.urls.cms_login, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    info = inspect_login_html(r.text)
    print(f"Login page: {settings.urls.cms_login}")
    for name, found in info.fields_found.items():
        print(f"  {name:<11} {'found' if found else 'MISSING'}")
    print(f"  institutes  {', '.join(f'{k} ({v})' for k, v in info.institutes.items() if v)}")
    print(f"  roles       {', '.join(f'{k} ({v})' for k, v in info.roles.items())}")
    print(f"  CAPTCHA     {'PRESENT: automatic login will pause' if info.captcha else 'not on the page'}")
    creds = settings.credentials
    if creds:
        if creds.institute not in info.institutes:
            print(f"Institute {creds.institute!r} is not an option on the form")
            return 1
        if creds.role not in info.roles:
            print(f"Role {creds.role!r} is not an option on the form")
            return 1
    return 0 if info.contract_ok else 1


def cmd_login(rt: Runtime, args) -> int:
    rt.adapter.relogin()
    print("Logged in through CMS; LMS session saved")
    return 0


def cmd_check(rt: Runtime, args) -> int:
    ok = session_is_valid(rt.adapter.store.load(), rt.settings.urls)
    print("Session valid" if ok else "Session missing or expired")
    g = rt.adapter.guard.state
    if g.paused_reason:
        print(f"Re-login paused: {g.paused_reason}")
    elif g.failures:
        print(f"Failed logins so far: {g.failures} of {rt.adapter.guard.max_failures}")
    return 0 if ok else 1


def cmd_list(rt: Runtime, args) -> int:
    with rt.adapter as adapter:
        adapter.ensure_session()
        courses = adapter.list_courses()
        print(f"{len(courses)} courses in {courses[0].semester_name if courses else '?'}")
        for course in courses:
            assignments = adapter.list_assignments(course)
            print(f"\n{course.name} ({len(assignments)})")
            for a in assignments:
                due = a.deadline.strftime("%d %b %Y %H:%M") if a.deadline else "no deadline"
                state = ("submitted" if a.submitted_file else "not submitted") + (", closed" if a.deadline_passed else ", open")
                print(f"  {a.number}. {a.title}  due {due}  {state}  file {a.attachment_name or '-'}")
    return 0


def cmd_paste_session(rt: Runtime, args) -> int:
    state = rt.adapter.store.save_pasted(args.phpsessid, rt.settings.urls)
    if not session_is_valid(state, rt.settings.urls):
        print("Saved, but the LMS does not accept this session")
        return 1
    rt.adapter.guard.reset()
    print("Session saved and valid; re-login pause cleared")
    return 0


def cmd_retry(rt: Runtime, args) -> int:
    rt.adapter.guard.reset()
    print("Failed-login count reset")
    return 0


def cmd_record(rt: Runtime, args) -> int:
    from playwright.sync_api import sync_playwright

    settings = rt.settings
    state = rt.adapter.store.load()
    valid = session_is_valid(state, settings.urls)
    out = settings.state_dir / "spike" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    if not valid:
        print("No valid saved session: starting at the CMS login page.")
        print("The recording will include your password; delete it once the pages are mapped.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            storage_state=state if valid else None,
            record_har_path=str(out / "lms.har"),
            record_har_content="embed",
        )
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.goto(settings.urls.lms_dashboard if valid else settings.urls.cms_login)
        print(
            "Open each course, an assignment list, an assignment page, a download and "
            "(on a test assignment only) the upload form."
        )
        input("Press Enter here when you are done... ")
        context.tracing.stop(path=str(out / "trace.zip"))
        context.close()
        browser.close()

    print(f"Saved {out / 'lms.har'} and {out / 'trace.zip'} (open with: playwright show-trace)")
    return 0


def cmd_notify_test(rt: Runtime, args) -> int:
    channels = rt.outbox().channels
    results = notify.send_test(channels, "Test message from the LMS bot. If you can read this, notifications work.")
    for name, error in results.items():
        print(f"{name}: {'sent' if error is None else 'FAILED: ' + error}")
    return 0 if all(e is None for e in results.values()) else 1


LINK_WAIT_SECONDS = 180


def cmd_whatsapp(rt: Runtime, args) -> int:
    evo = rt.settings.evolution
    if evo is None:
        raise ConfigError("Set EVOLUTION_API_URL and EVOLUTION_API_KEY first")
    admin = EvolutionAdmin(evo)

    if args.action == "status":
        st = admin.state()
        if st.linked:
            print(f"Linked: {st.name or 'WhatsApp'} (+{st.number})")
        else:
            print(f"Not linked (instance {evo.instance}: {st.state}). Run: lms-worker whatsapp link")
        to = rt.whatsapp_number().get()
        print(f"Messages go to: +{to}" if to else "No number to send to: save one on the setup page, or set WHATSAPP_NUMBER")
        return 0 if st.linked else 1

    if args.action == "unlink":
        admin.unlink()
        print("WhatsApp unlinked")
        return 0

    challenge = admin.start_link(args.number)
    if challenge.already_linked:
        print("Already linked. To link another account, run: lms-worker whatsapp unlink")
        return 0
    if args.number:
        print(f"Pairing code: {challenge.pairing_code_display}\n")
        print("On the bot phone: WhatsApp > Settings > Linked devices > Link a device >")
        print("Link with phone number instead, then type the code above.")
    else:
        _print_qr(challenge.qr_text)
        print("On the bot phone: WhatsApp > Settings > Linked devices > Link a device, and scan this.")

    shown = challenge.qr_text
    deadline = time.monotonic() + LINK_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(3)
        st = admin.state()
        if st.linked:
            print(f"\nLinked: {st.name or 'WhatsApp'} (+{st.number})")
            if not rt.whatsapp_number().get():
                print("Now save the number that should receive messages on the setup page, or set WHATSAPP_NUMBER.")
            return 0
        if not args.number:
            latest = admin.current_challenge()
            if latest and latest.qr_text and latest.qr_text != shown:
                shown = latest.qr_text
                print("\nThe QR code changed; scan this one:")
                _print_qr(shown)
    print("Not linked yet. Run the command again to get a fresh code.")
    return 1


def _print_qr(text: str | None) -> None:
    import segno

    if not text:
        raise WhatsAppError("Evolution API returned no QR code")
    segno.make(text, error="L").terminal(compact=True)


def cmd_api(rt: Runtime, args) -> int:
    from .api import DEFAULT_INTERNAL_URL, create_app, serve

    if not rt.settings.evolution:
        print("WhatsApp is not configured (EVOLUTION_API_URL, EVOLUTION_API_KEY); "
              "the api will run but cannot answer messages", flush=True)
    elif not rt.whatsapp_number().get():
        print("WhatsApp has no number to send to yet; save one on the setup page, or set WHATSAPP_NUMBER", flush=True)
    app = create_app(
        rt.settings,
        rt.sessions,
        rt.kv,
        internal_url=os.environ.get("API_INTERNAL_URL", DEFAULT_INTERNAL_URL),
    )
    serve(app, host=args.host, port=args.port)
    return 0


def cmd_db_upgrade(rt: Runtime, args) -> int:
    db.upgrade(rt.engine)
    print("Database is up to date")
    return 0


def _print_result(result: PollResult, delay: float | None = None) -> None:
    line = (
        f"{datetime.now().astimezone().isoformat(timespec='seconds')} poll {result.status}: {result.courses} courses, {result.seen} assignments, "
        f"{result.new_jobs} new, {result.revisions} changed, {result.skipped} skipped, "
        f"{result.stopped} stopped, {result.downloaded} downloaded, {result.papers} papers "
        f"({result.new_papers} new), {result.marks_posted} marks posted, {result.reminders} reminders, "
        f"{result.sent} messages sent"
    )
    if delay is not None:
        line += f"; next in {delay / 60:.1f} min"
    print(line, flush=True)
    for e in result.errors:
        print(f"  ! {e}", flush=True)


def cmd_poll(rt: Runtime, args) -> int:
    result = rt.watcher().poll_once()
    _print_result(result)
    return 0 if result.status in ("ok", "paused") else 1


def cmd_run(rt: Runtime, args) -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    solver = rt.solver()
    if solver is not None:
        threading.Thread(target=solver.run_forever, args=(stop,), name="solver", daemon=True).start()
        print(f"Solving in {solver.sandbox.image} containers", flush=True)
    else:
        print("Solving is off: set SOLVER_IMAGE to turn it on", flush=True)
    print("Polling the LMS; stop with Ctrl-C", flush=True)
    run_forever(
        rt.watcher(),
        stop,
        clock=lambda: datetime.now().astimezone(),
        on_result=_print_result,
        poll_requested=lambda: rt.kv.get(POLL_NOW_KEY) is not None,
    )
    return 0


def cmd_solve(rt: Runtime, args) -> int:
    solver = rt.solver()
    if solver is None:
        raise ConfigError("Solving is off: set SOLVER_IMAGE")
    solver.recover()
    result = solver.run_once()
    print(f"{result.status}" + (f" (job {result.job_id})" if result.job_id else "") + (f": {result.detail}" if result.detail else ""))
    return 0 if result.status in ("solved", "handwritten", "idle", "paused") else 1


def cmd_resume(rt: Runtime, args) -> int:
    rt.kv.delete(POLL_PAUSE_KEY)
    rt.kv.delete("alert:contract")
    print("Polling resumed")
    return 0


def cmd_status(rt: Runtime, args) -> int:
    beat = rt.kv.get(HEARTBEAT_KEY)
    if beat:
        b = json.loads(beat)
        print(f"Last poll: {b['at']} ({b['status']}), {b['seen']} assignments seen")
    else:
        print("Last poll: never")
    paused = rt.kv.get(POLL_PAUSE_KEY)
    print(f"Polling: paused ({paused})" if paused else "Polling: running")
    g = rt.adapter.guard.state
    print(f"Re-login: paused ({g.paused_reason})" if g.paused_reason else f"Re-login: ok, {g.failures} recent failures")
    with rt.sessions() as s:
        jobs = s.scalars(select(db.Job).order_by(db.Job.updated_at.desc()).limit(10)).all()
        print(f"\nRecent jobs ({len(jobs)}):")
        for j in jobs:
            a = j.assignment
            print(f"  #{j.id} {j.state:<11} {a.course.name} · {a.title} (rev {j.revision})")
    return 0


NEEDS_DB = {"db-upgrade", "poll", "run", "solve", "resume", "status", "api"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lms-worker", description=__doc__.split("\n")[0])
    parser.add_argument("--env-file", help="path to a .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="poll the LMS on schedule until stopped")
    p_api = sub.add_parser("api", help="answer WhatsApp messages through the menu")
    p_api.add_argument("--host", default="0.0.0.0")
    p_api.add_argument("--port", type=int, default=8000)
    sub.add_parser("poll", help="run one poll cycle now")
    sub.add_parser("solve", help="solve the next waiting assignment now")
    sub.add_parser("status", help="last poll, pauses, session and recent jobs")
    sub.add_parser("resume", help="clear a polling pause after the LMS layout changed")
    sub.add_parser("db-upgrade", help="create or migrate the database tables")
    sub.add_parser("notify-test", help="send a test message to each configured channel")
    p_wa = sub.add_parser("whatsapp", help="link the bot's WhatsApp, show or remove the link")
    p_wa.add_argument("action", choices=["status", "link", "unlink"])
    p_wa.add_argument("--number", help="link by pairing code: the BOT phone's number, country code first")
    sub.add_parser("probe", help="read the CMS login form; submits nothing")
    p_login = sub.add_parser("login", help="log in through CMS and save the LMS session")
    p_login.add_argument("--headed", action="store_true", help="show the browser")
    sub.add_parser("check", help="check the saved LMS session")
    p_list = sub.add_parser("list", help="list this semester's courses and assignments")
    p_list.add_argument("--headed", action="store_true", help="show the browser if a re-login is needed")
    p_paste = sub.add_parser("paste-session", help="save a PHPSESSID copied from your browser")
    p_paste.add_argument("phpsessid")
    sub.add_parser("retry", help="clear the failed-login pause")
    sub.add_parser("record", help="record LMS pages for mapping (needs a display)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    handler = {
        "run": cmd_run,
        "api": cmd_api,
        "poll": cmd_poll,
        "solve": cmd_solve,
        "status": cmd_status,
        "resume": cmd_resume,
        "db-upgrade": cmd_db_upgrade,
        "notify-test": cmd_notify_test,
        "whatsapp": cmd_whatsapp,
        "probe": cmd_probe,
        "login": cmd_login,
        "check": cmd_check,
        "list": cmd_list,
        "paste-session": cmd_paste_session,
        "retry": cmd_retry,
        "record": cmd_record,
    }[args.command]
    try:
        settings = load_settings(args.env_file)
        rt = runtime.build(
            settings,
            headless=not getattr(args, "headed", False),
            require_db=args.command in NEEDS_DB,
        )
        return handler(rt, args)
    except (LMSError, ConfigError, WhatsAppError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
