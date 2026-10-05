"""Spike commands for the LMS adapter.

    lms-worker probe            read the CMS login form; submits nothing
    lms-worker login            log in through CMS and save the LMS session
    lms-worker check            is the saved LMS session still valid?
    lms-worker list             list this semester's courses and assignments
    lms-worker paste-session    save a PHPSESSID copied from your browser
    lms-worker retry            clear the failed-login pause
    lms-worker record           open a browser and record LMS traffic for mapping
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import httpx

from .config import Settings, load_settings
from .lms.bahria import BahriaAdapter
from .lms.cms_login import inspect_login_html
from .lms.errors import LMSError
from .lms.session import USER_AGENT, session_is_valid


def cmd_probe(settings: Settings, args) -> int:
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


def cmd_login(settings: Settings, args) -> int:
    adapter = BahriaAdapter(settings, headless=not args.headed)
    adapter.relogin()
    print(f"Logged in through CMS; LMS session saved to {adapter.store.path}")
    return 0


def cmd_check(settings: Settings, args) -> int:
    adapter = BahriaAdapter(settings)
    ok = session_is_valid(adapter.store.load(), settings.urls)
    print("Session valid" if ok else "Session missing or expired")
    g = adapter.guard.state
    if g.paused_reason:
        print(f"Re-login paused: {g.paused_reason}")
    elif g.failures:
        print(f"Failed logins so far: {g.failures} of {adapter.guard.max_failures}")
    return 0 if ok else 1


def cmd_list(settings: Settings, args) -> int:
    with BahriaAdapter(settings, headless=not args.headed) as adapter:
        adapter.ensure_session()
        courses = adapter.list_courses()
        print(f"{len(courses)} courses in {courses[0].semester_name if courses else '?'}")
        for course in courses:
            assignments = adapter.list_assignments(course)
            print(f"\n{course.name} ({len(assignments)})")
            for a in assignments:
                status = adapter.submission_status(a) if args.status else None
                due = a.deadline.strftime("%d %b %Y %H:%M") if a.deadline else "no deadline"
                line = f"  {a.number}. {a.title}  due {due}  file {a.attachment_name or '-'}"
                if status:
                    line += f"  {'submitted' if status.submitted else 'not submitted'}"
                    line += ", closed" if status.deadline_passed else ", open"
                print(line)
    return 0


def cmd_paste_session(settings: Settings, args) -> int:
    adapter = BahriaAdapter(settings)
    state = adapter.store.save_pasted(args.phpsessid, settings.urls)
    if not session_is_valid(state, settings.urls):
        print("Saved, but the LMS does not accept this session")
        return 1
    adapter.guard.reset()
    print("Session saved and valid; re-login pause cleared")
    return 0


def cmd_retry(settings: Settings, args) -> int:
    BahriaAdapter(settings).guard.reset()
    print("Failed-login count reset")
    return 0


def cmd_record(settings: Settings, args) -> int:
    from playwright.sync_api import sync_playwright

    adapter = BahriaAdapter(settings)
    state = adapter.store.load()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lms-worker", description=__doc__.split("\n")[0])
    parser.add_argument("--env-file", help="path to a .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="read the CMS login form; submits nothing")
    p_login = sub.add_parser("login", help="log in through CMS and save the LMS session")
    p_login.add_argument("--headed", action="store_true", help="show the browser")
    sub.add_parser("check", help="check the saved LMS session")
    p_list = sub.add_parser("list", help="list this semester's courses and assignments")
    p_list.add_argument("--status", action="store_true", help="also show submission status (one more request each)")
    p_list.add_argument("--headed", action="store_true", help="show the browser if a re-login is needed")
    p_paste = sub.add_parser("paste-session", help="save a PHPSESSID copied from your browser")
    p_paste.add_argument("phpsessid")
    sub.add_parser("retry", help="clear the failed-login pause")
    sub.add_parser("record", help="record LMS pages for mapping (needs a display)")
    args = parser.parse_args(argv)

    settings = load_settings(args.env_file)
    handler = {
        "probe": cmd_probe,
        "login": cmd_login,
        "check": cmd_check,
        "list": cmd_list,
        "paste-session": cmd_paste_session,
        "retry": cmd_retry,
        "record": cmd_record,
    }[args.command]
    try:
        return handler(settings, args)
    except LMSError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
