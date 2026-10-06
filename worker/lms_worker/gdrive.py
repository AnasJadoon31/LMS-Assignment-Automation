"""Saving reports to your Google Drive.

The worker uploads with the Drive API under your Google account, connected once on the setup
page (Connect Google Drive). It asks only for the drive.file permission: the app sees and
changes only the files and folders it creates, nothing else in your Drive.

Layout: University Assignments/<semester>/<course>/<report name>/ with the question file, the
.docx and the .pdf. Folders are made as needed. A file with the same name already in the
folder (a redo) is replaced in place, so its Drive link stays the same.

Needs a Google Cloud OAuth client of type "Web application" whose redirect URI is
<PUBLIC_URL>/setup/google/callback: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET. The refresh
token is kept encrypted with APP_SECRET_KEY.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .kv import KeyValue, SecretBox

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER = "application/vnd.google-apps.folder"
CALLBACK = "/setup/google/callback"
KEY = "google_drive"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DriveError(Exception):
    pass


@dataclass(frozen=True)
class DriveStatus:
    connected: bool
    email: str | None = None


class GoogleDrive:
    def __init__(self, client_id: str, client_secret: str, kv: KeyValue, box: SecretBox, *,
                 transport: httpx.BaseTransport | None = None, clock=time.time) -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.kv, self.box, self.clock = kv, box, clock
        self.http = httpx.Client(transport=transport, timeout=120)
        self._access: tuple[str, float] | None = None

    # --- connecting (the setup page) ---

    def auth_url(self, redirect_uri: str, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
        })

    def connect(self, code: str, redirect_uri: str) -> DriveStatus:
        r = self.http.post(TOKEN_URL, data={"code": code, "client_id": self.client_id, "client_secret": self.client_secret,
                                            "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        d = _json(r, "Google refused the sign-in")
        if not d.get("refresh_token"):
            raise DriveError("Google sent no refresh token. Remove the app at myaccount.google.com/permissions, then connect again.")
        self._access = (d["access_token"], self.clock() + int(d.get("expires_in", 3600)) - 60)
        email = None
        try:
            email = self._get(f"{API}/about", {"fields": "user(emailAddress)"}).get("user", {}).get("emailAddress")
        except DriveError:
            pass
        self.kv.set(KEY, self.box.encrypt(json.dumps({"refresh_token": d["refresh_token"], "email": email})))
        return DriveStatus(True, email)

    def status(self) -> DriveStatus:
        stored = self._stored()
        return DriveStatus(bool(stored), stored.get("email") if stored else None)

    def disconnect(self) -> None:
        stored = self._stored()
        if stored:
            try:
                self.http.post(REVOKE_URL, data={"token": stored["refresh_token"]})
            except httpx.HTTPError:
                pass
        self.kv.delete(KEY)
        self._access = None

    # --- saving (the solver) ---

    def save(self, folders: list[str], files: list[Path]) -> str:
        """Upload files into the folder path, making folders as needed; returns the folder's link."""
        parent = "root"
        for name in folders:
            parent = self._folder(name, parent)
        for f in files:
            self._upload(f, parent)
        return f"https://drive.google.com/drive/folders/{parent}"

    def _folder(self, name: str, parent: str) -> str:
        found = self._find(name, parent, folder=True)
        if found:
            return found
        r = self._call("POST", f"{API}/files", params={"fields": "id"},
                       json={"name": name, "mimeType": FOLDER, "parents": [parent]})
        return r["id"]

    def _upload(self, path: Path, parent: str) -> str:
        mime = DOCX if path.suffix.lower() == ".docx" else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        data = path.read_bytes()
        existing = self._find(path.name, parent, folder=False)
        if existing:
            return self._call("PATCH", f"{UPLOAD}/files/{existing}", params={"uploadType": "media", "fields": "id"},
                              content=data, headers={"Content-Type": mime})["id"]
        boundary = "lms" + secrets.token_hex(12)
        meta = json.dumps({"name": path.name, "parents": [parent]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return self._call("POST", f"{UPLOAD}/files", params={"uploadType": "multipart", "fields": "id"}, content=body,
                          headers={"Content-Type": f"multipart/related; boundary={boundary}"})["id"]

    def _find(self, name: str, parent: str, *, folder: bool) -> str | None:
        q = f"name = '{_quote(name)}' and '{_quote(parent)}' in parents and trashed = false"
        q += f" and mimeType {'=' if folder else '!='} '{FOLDER}'"
        files = self._get(f"{API}/files", {"q": q, "fields": "files(id)", "spaces": "drive", "pageSize": "1"}).get("files")
        return files[0]["id"] if files else None

    # --- HTTP ---

    def _stored(self) -> dict | None:
        raw = self.kv.get(KEY)
        if not raw:
            return None
        try:
            return json.loads(self.box.decrypt(raw))
        except ValueError:
            return None

    def _token(self) -> str:
        if self._access and self._access[1] > self.clock():
            return self._access[0]
        stored = self._stored()
        if not stored:
            raise DriveError("Google Drive is not connected; connect it on the setup page")
        r = self.http.post(TOKEN_URL, data={"refresh_token": stored["refresh_token"], "client_id": self.client_id,
                                            "client_secret": self.client_secret, "grant_type": "refresh_token"})
        if r.status_code == 400 and "invalid_grant" in r.text:
            raise DriveError("Google Drive access expired or was removed; connect it again on the setup page")
        d = _json(r, "Google refused the Drive token")
        self._access = (d["access_token"], self.clock() + int(d.get("expires_in", 3600)) - 60)
        return self._access[0]

    def _get(self, url: str, params: dict) -> dict:
        return self._call("GET", url, params=params)

    def _call(self, method: str, url: str, **kw) -> dict:
        headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {self._token()}"}
        try:
            r = self.http.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise DriveError(f"Google Drive is not reachable: {e}") from e
        return _json(r, "Google Drive refused the request")


def _json(r: httpx.Response, what: str) -> dict:
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code >= 400:
        err = d.get("error")
        msg = (err.get("message") if isinstance(err, dict) else d.get("error_description") or err) or r.text[:200]
        raise DriveError(f"{what}: {msg}")
    return d


def _quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")
