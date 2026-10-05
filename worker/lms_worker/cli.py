"""LMS worker commands.

    lms-worker run              poll the LMS on schedule until stopped (the worker service)
    lms-worker poll             run one poll cycle now
    lms-worker status           last poll, pauses, session and recent jobs
    lms-worker resume           clear a polling pause after the LMS layout changed
    lms-worker db-upgrade       create or migrate the database tables

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
import signal
import sys
import threading
from datetime import datetime

import httpx
from sqlalchemy import select

from . import db, runtime
from .config import load_settings
from .lms.cms_login import inspect_login_html
from .lms.errors import LMSError
from .lms.session import USER_AGENT, session_is_valid
from .config import ConfigError
from .runtime import Runtime
from .schedule import run_forever
from .watcher import HEARTBEAT_KEY, POLL_PAUSE_KEY, PollResult


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


def cmd_db_upgrade(rt: Runtime, args) -> int:
    db.upgrade(rt.engine)
    print("Database is up to date")
    return 0


def _print_result(result: PollResult, delay: float | None = None) -> None:
    line = (
        f"{datetime.now().astimezone().isoformat(timespec='seconds')} poll {result.status}: {result.courses} courses, {result.seen} assignments, "
        f"{result.new_jobs} new, {result.revisions} changed, {result.skipped} skipped, "
        f"{result.stopped} stopped, {result.downloaded} downloaded, {result.sent} messages sent"
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
    print("Polling the LMS; stop with Ctrl-C", flush=True)
    run_forever(rt.watcher(), stop, clock=lambda: datetime.now().astimezone(), on_result=_print_result)
    return 0


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


NEEDS_DB = {"db-upgrade", "poll", "run", "resume", "status"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lms-worker", description=__doc__.split("\n")[0])
    parser.add_argument("--env-file", help="path to a .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="poll the LMS on schedule until stopped")
    sub.add_parser("poll", help="run one poll cycle now")
    sub.add_parser("status", help="last poll, pauses, session and recent jobs")
    sub.add_parser("resume", help="clear a polling pause after the LMS layout changed")
    sub.add_parser("db-upgrade", help="create or migrate the database tables")
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
        "poll": cmd_poll,
        "status": cmd_status,
        "resume": cmd_resume,
        "db-upgrade": cmd_db_upgrade,
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
    except (LMSError, ConfigError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
