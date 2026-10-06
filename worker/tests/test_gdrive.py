import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lms_worker.gdrive import FOLDER, KEY, DriveError, GoogleDrive
from lms_worker.kv import FileKV, SecretBox
from lms_worker.web import setup_router

from .test_web import KEY as SESSION_KEY, PASSWORD, StubAdmin, signed_in


class FakeGoogle:
    """The OAuth token endpoint and the parts of the Drive API the app uses."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.revoked = []
        self.grant_ok = True
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            if url.path == "/revoke":
                self.revoked.append(form["token"][0])
                return httpx.Response(200, json={})
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["the-code"] and form["redirect_uri"] == ["https://lms.example.com/setup/google/callback"]
                return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-1"})
            if not self.grant_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        assert request.headers["authorization"].startswith("Bearer at-")
        if url.path == "/drive/v3/about":
            return httpx.Response(200, json={"user": {"emailAddress": "student@gmail.com"}})
        if url.path == "/drive/v3/files" and request.method == "GET":
            q = url.params["q"]
            name = q.split("name = '")[1].split("' and '")[0].replace("\\'", "'")
            parent = q.split("' and '")[1].split("' in parents")[0]
            want_folder = f"mimeType = '{FOLDER}'" in q
            hits = [{"id": i} for i, f in self.files.items()
                    if f["name"] == name and parent in f["parents"] and (f["mimeType"] == FOLDER) == want_folder]
            return httpx.Response(200, json={"files": hits[:1]})
        if url.path == "/drive/v3/files" and request.method == "POST":
            return httpx.Response(200, json={"id": self._add(json.loads(request.content))})
        if url.path == "/upload/drive/v3/files" and request.method == "POST":
            boundary = request.headers["content-type"].split("boundary=")[1]
            parts = request.content.split(f"--{boundary}".encode())
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1])
            mime, data = parts[2].split(b"\r\n\r\n", 1)
            return httpx.Response(200, json={"id": self._add({**meta, "mimeType": mime.decode().split(": ")[1],
                                                               "data": data.removesuffix(b"\r\n")})})
        if url.path.startswith("/upload/drive/v3/files/") and request.method == "PATCH":
            fid = url.path.rsplit("/", 1)[1]
            self.files[fid]["data"] = request.content
            return httpx.Response(200, json={"id": fid})
        return httpx.Response(404, json={"error": {"message": f"no route {request.method} {url.path}"}})

    def _add(self, meta):
        self.n += 1
        fid = f"f{self.n}"
        self.files[fid] = {"name": meta["name"], "parents": meta["parents"], "mimeType": meta.get("mimeType", ""),
                           "data": meta.get("data")}
        return fid

    def path_of(self, fid):
        names = []
        while fid != "root":
            names.append(self.files[fid]["name"])
            fid = self.files[fid]["parents"][0]
        return "/".join(reversed(names))


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def drive(google, tmp_path):
    return GoogleDrive("cid", "secret", FileKV(tmp_path / "kv"), SecretBox(SESSION_KEY),
                       transport=httpx.MockTransport(google.handler))


def test_connect_save_and_replace_on_a_redo(drive, google, tmp_path):
    assert not drive.status().connected
    assert drive.connect("the-code", "https://lms.example.com/setup/google/callback").email == "student@gmail.com"
    assert "rt-1" not in (tmp_path / "kv" / f"{KEY}.json").read_text()  # encrypted at rest
    pdf, docx = tmp_path / "Assignment 01 - O'Brien's Sort.pdf", tmp_path / "Assignment 01 - O'Brien's Sort.docx"
    pdf.write_bytes(b"%PDF one")
    docx.write_bytes(b"PK one")
    folders = ["University Assignments", "Fall-2026", "Design & Analysis of Algorithms", "Assignment 01 - O'Brien's Sort"]

    link = drive.save(folders, [docx, pdf])
    folder = link.rsplit("/", 1)[1]
    assert google.path_of(folder) == "/".join(folders)
    docs = {f["name"]: f for f in google.files.values() if f["mimeType"] != FOLDER}
    assert docs["Assignment 01 - O'Brien's Sort.pdf"]["data"] == b"%PDF one"
    assert docs["Assignment 01 - O'Brien's Sort.docx"]["mimeType"].endswith("wordprocessingml.document")

    pdf.write_bytes(b"%PDF two")
    drive.save(folders, [pdf])
    assert len(google.files) == 6  # four folders and two files: nothing duplicated
    assert [f["data"] for f in google.files.values() if f["name"].endswith(".pdf")] == [b"%PDF two"]


def test_an_expired_grant_says_to_connect_again(drive, google, tmp_path):
    drive.connect("the-code", "https://lms.example.com/setup/google/callback")
    drive._access = None
    google.grant_ok = False
    with pytest.raises(DriveError, match="connect it again on the setup page"):
        drive.save(["University Assignments"], [])
    drive.disconnect()
    assert google.revoked == ["rt-1"] and not drive.status().connected


def test_connecting_from_the_setup_page(drive, google, tmp_path):
    app = FastAPI()
    app.include_router(setup_router(password=PASSWORD, secret_key=SESSION_KEY, login=None, admin=StubAdmin(),
                                    drive=drive, public_url="https://lms.example.com"))
    c = TestClient(app, base_url="https://lms.example.com")
    assert c.get("/setup/google/start", follow_redirects=False).headers["location"] == "/login"
    signed_in(c)
    status = c.get("/api/setup/status", headers={"X-LMS": "1"}).json()["drive"]
    assert status == {"available": True, "connected": False, "email": None,
                      "redirect_uri": "https://lms.example.com/setup/google/callback"}

    to_google = urlsplit(c.get("/setup/google/start", follow_redirects=False).headers["location"])
    q = parse_qs(to_google.query)
    assert to_google.netloc == "accounts.google.com" and q["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    assert q["access_type"] == ["offline"] and q["redirect_uri"] == ["https://lms.example.com/setup/google/callback"]

    fresh = TestClient(app, base_url="https://lms.example.com")  # Google's redirect carries no cookie
    assert fresh.get("/setup/google/callback", params={"state": "made-up", "code": "the-code"}).status_code == 400
    r = fresh.get("/setup/google/callback", params={"state": q["state"][0], "code": "the-code"})
    assert r.status_code == 200 and "Google Drive connected as student@gmail.com." in r.text
    assert fresh.get("/setup/google/callback", params={"state": q["state"][0], "code": "the-code"}).status_code == 400  # once
    assert c.get("/api/setup/status", headers={"X-LMS": "1"}).json()["drive"]["email"] == "student@gmail.com"
    assert c.post("/api/setup/google/disconnect", headers={"X-LMS": "1"}, json={}).json() == {"ok": True}
