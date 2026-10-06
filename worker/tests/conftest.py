"""A fake CMS and LMS on two local hosts, shaped like the real portal.

CMS (127.0.0.1): student login form, then a dashboard with an LMS link.
LMS (localhost): the link sets PHPSESSID; the dashboard redirects to CMS without it.
"""

from __future__ import annotations

import base64
import re
import threading
from email.parser import BytesParser
from email.policy import default
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from lms_worker.config import CMSCredentials, PortalUrls, Settings

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_HTML = (FIXTURES / "cms_login.html").read_text()
ASSIGNMENTS_HTML = (FIXTURES / "lms_assignments.html").read_text()
EMPTY_ASSIGNMENTS_HTML = (FIXTURES / "lms_assignments_empty.html").read_text()
PAPERS_HTML = (FIXTURES / "lms_papers.html").read_text()
EMPTY_PAPERS_HTML = (FIXTURES / "lms_papers_empty.html").read_text()
QUESTION_PDF = b"%PDF-1.7 fixture"
CAPTCHA_HTML = (
    '<div id="BodyPH_divRecaptcha" class="form-group">'
    '<div id="BodyPH_reCaptcha" class="g-recaptcha" data-sitekey="fixture"></div></div>'
)

ENROLLMENT = "02-000000-001"
PASSWORD = "correct-horse"
LOGIN_PATH = "/Logins/Student/Login.aspx"


@dataclass
class PortalState:
    cms_port: int = 0
    lms_port: int = 0
    captcha: bool = False
    # The CMS dashboard's link to the LMS; {lms} is filled with the LMS origin
    dashboard_link: str = '<a href="{lms}/sso.php?token=t0k3n" target="_blank">LMS</a>'
    posts: list[dict] = field(default_factory=list)
    lms_requests: list[str] = field(default_factory=list)
    uploads: list[dict] = field(default_factory=list)  # Submit form posts to the LMS
    uploaded: str | None = None  # the file name the LMS shows on row 3 after an upload

    @property
    def cms(self) -> str:
        return f"http://127.0.0.1:{self.cms_port}"

    @property
    def lms(self) -> str:
        return f"http://localhost:{self.lms_port}"


def assignments_page(oc: str | None, uploaded: str | None = None) -> str:
    """The fixture course (MTAwMDAy) has three assignments; any other course has none.
    After an upload, row 3 shows the file and a Delete button, as the LMS does."""
    html = ASSIGNMENTS_HTML if oc == "MTAwMDAy" else EMPTY_ASSIGNMENTS_HTML
    if uploaded and oc == "MTAwMDAy":
        key = base64.b64encode(f"100001,2026-10-06,AssignmentSubmissions,0,{uploaded},x".encode()).decode()
        html = html.replace("<td>No Submission </td>",
                            f'<td> <a href="Download.php?k={key}" class="label label-info">Submission</a></td>')
        html = re.sub(r'data-target="#modal-assignment-solution-3"(.*?)Submit</div>',
                      r'data-target="#modal-delete-assignment-submission-3"\1Delete</div>', html, flags=re.S)
    html = re.sub(r'(<option value="MTAw[^"]*")\s*selected', r"\1", html)
    if oc:
        html = html.replace(f'<option value="{oc}"', f'<option value="{oc}" selected')
    return html


def papers_page(oc: str | None) -> str:
    """The fixture course (MTAwMDAy) has two papers; any other course has none."""
    html = PAPERS_HTML if oc == "MTAwMDAy" else EMPTY_PAPERS_HTML
    html = re.sub(r'(<option value="MTAw[^"]*")\s*selected', r"\1", html)
    if oc:
        html = html.replace(f'<option value="{oc}"', f'<option value="{oc}" selected')
    return html


def _handler(portal: PortalState, site: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _cookies(self) -> dict[str, str]:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return {k: v.value for k, v in c.items()}

        def _send(self, status: int, body: str = "", headers: dict | None = None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, data: bytes, filename: str):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _upload(self, body: bytes):
            path = urlsplit(self.path)
            portal.lms_requests.append(path.path + "?" + path.query)
            if self._cookies().get("PHPSESSID") != "good":
                return self._send(302, headers={"Location": f"{portal.cms}/"})
            if path.path != "/Student/includes/studentprocess.php" or path.query != "s=assignmentSubmissionAdd":
                return self._send(404, "not found")
            msg = BytesParser(policy=default).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body)
            fields, file = {}, None
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    file = {"field": name, "filename": part.get_filename(), "data": part.get_payload(decode=True),
                            "type": part.get_content_type()}
                else:
                    fields[name] = part.get_content()
            portal.uploads.append({"fields": fields, "file": file})
            portal.uploaded = f"02-000000-001-100002{file['filename']}"
            return self._send(302, headers={"Location": "/Student/Assignments.php?msg=added"})

        def _login_page(self, alert: str = ""):
            html = LOGIN_HTML.replace("{{ALERT}}", alert).replace(
                "{{CAPTCHA}}", CAPTCHA_HTML if portal.captcha else ""
            )
            self._send(200, html)

        def do_GET(self):
            path = urlsplit(self.path).path
            if site == "cms":
                if path == LOGIN_PATH:
                    return self._login_page()
                if path == "/Sys/Student/Dashboard.aspx":
                    if self._cookies().get("cms") != "authed":
                        return self._send(302, headers={"Location": LOGIN_PATH})
                    return self._send(200, "<h1>Dashboard</h1>" + portal.dashboard_link.format(lms=portal.lms))
                return self._send(200, "<h1>CMS home</h1>")

            portal.lms_requests.append(path)
            if path == "/sso.php":
                if parse_qs(urlsplit(self.path).query).get("token") != ["t0k3n"]:
                    return self._send(302, headers={"Location": f"{portal.cms}/"})
                return self._send(
                    302,
                    headers={"Location": "/Student/Dashboard.php", "Set-Cookie": "PHPSESSID=good; path=/"},
                )
            if path == "/Student/Dashboard.php":
                if self._cookies().get("PHPSESSID") != "good":
                    return self._send(302, headers={"Location": f"{portal.cms}/"})
                return self._send(200, "<title>LMS | Student</title><h1>Dashboard</h1>")
            if path in ("/Student/Assignments.php", "/Student/Papers.php", "/Student/Download.php"):
                if self._cookies().get("PHPSESSID") != "good":
                    return self._send(302, headers={"Location": f"{portal.cms}/"})
                query = parse_qs(urlsplit(self.path).query)
                if path == "/Student/Download.php":
                    return self._send_file(QUESTION_PDF, "Lab-2-Q-25092026-105052pm.pdf")
                if path == "/Student/Papers.php":
                    return self._send(200, papers_page(query.get("oc", [None])[0]))
                return self._send(200, assignments_page(query.get("oc", [None])[0], portal.uploaded))
            if path == "/Student/Hop.php":
                return self._send(302, headers={"Location": "/Student/Dashboard.php"})
            return self._send(404, "not found")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if site == "lms":
                return self._upload(self.rfile.read(length))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode(), keep_blank_values=True).items()}
            portal.posts.append(form)
            ok = (
                form.get("ctl00$BodyPH$tbEnrollment") == ENROLLMENT
                and form.get("ctl00$BodyPH$tbPassword") == PASSWORD
                and form.get("ctl00$BodyPH$ddlInstituteID") == "2"
                and form.get("ctl00$BodyPH$ddlSubUserType") == "None"
            )
            if not ok:
                return self._login_page('<div class="alert alert-danger">Invalid Enrollment or Password.</div>')
            return self._send(
                302,
                headers={"Location": "/Sys/Student/Dashboard.aspx", "Set-Cookie": "cms=authed; path=/"},
            )

    return Handler


@pytest.fixture
def portal():
    state = PortalState()
    servers = []
    for site, host in (("cms", "127.0.0.1"), ("lms", "127.0.0.1")):
        srv = ThreadingHTTPServer((host, 0), _handler(state, site))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    state.cms_port = servers[0].server_address[1]
    state.lms_port = servers[1].server_address[1]
    yield state
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def urls(portal) -> PortalUrls:
    return PortalUrls(cms_login=f"{portal.cms}{LOGIN_PATH}", lms_dashboard=f"{portal.lms}/Student/Dashboard.php")


def make_settings(urls: PortalUrls, state_dir: Path, password: str = PASSWORD, **cred_overrides) -> Settings:
    creds = CMSCredentials(enrollment=ENROLLMENT, password=password, **cred_overrides)
    return Settings(urls=urls, state_dir=state_dir, credentials=creds)


# --- PostgreSQL, for the watcher and outbox tests ---
# Set TEST_DATABASE_URL to a scratch database; these tests drop and recreate its tables.

import os

from sqlalchemy import text


@pytest.fixture(scope="session")
def engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    from lms_worker.db import make_engine, upgrade

    eng = make_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    upgrade(eng)  # the migration itself is under test
    yield eng
    eng.dispose()


@pytest.fixture
def sessions(engine):
    from lms_worker.db import Base, make_sessionmaker

    with engine.begin() as conn:
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return make_sessionmaker(engine)
