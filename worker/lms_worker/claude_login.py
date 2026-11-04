"""Signing Claude Code in to your claude.ai account, from the web page.

The solver runs under a full claude.ai login (a setup token cannot read the theme artifact).
The login lives in CLAUDE_HOME, the claude-home volume that every solver container mounts
as its home.

`claude auth login --claudeai` prints a sign-in link and waits at "Paste code here if
prompted >". You open the link, sign in, and the page shows a code. start() runs the
command in a pseudo-terminal and returns the link; finish(code) types the code in.

Alternatively a token from `claude setup-token` skips the sign-in: pasted on the setup page
(kept encrypted, read on every use, so no restart) or CLAUDE_CODE_OAUTH_TOKEN; the page's wins.
Claude Code then uses it instead of the stored login. It cannot read the theme artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .kv import KeyValue, SecretBox

LINK = re.compile(r"https://\S*oauth/authorize\?[^\s\x07\x1b]+")
ANSI = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]")
START_TIMEOUT = 40
FINISH_TIMEOUT = 90
TOKEN_KEY = "claude_token"
TOKEN = re.compile(r"[A-Za-z0-9_\-.]{20,1000}")


class LoginError(Exception):
    pass


@dataclass(frozen=True)
class LoginStatus:
    logged_in: bool
    method: str | None = None
    email: str | None = None
    detail: str | None = None


class ClaudeLogin:
    def __init__(self, home: Path, binary: str = "claude", token: str | None = None,
                 kv: KeyValue | None = None, box: SecretBox | None = None):
        self.home = Path(home)
        self.binary = binary
        self.env_token = token  # CLAUDE_CODE_OAUTH_TOKEN
        self.kv, self.box = kv, box  # where the setup page saves a token
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._fd: int | None = None
        self._started = 0.0

    def _saved_token(self) -> str | None:
        raw = self.kv.get(TOKEN_KEY) if self.kv and self.box else None
        try:
            return self.box.decrypt(raw) if raw else None
        except ValueError:
            return None

    @property
    def token(self) -> str | None:
        """The token Claude Code runs with, instead of the stored login; None uses the login."""
        return self._saved_token() or self.env_token

    def token_source(self) -> str | None:
        """page, env, or None."""
        return "page" if self._saved_token() else "env" if self.env_token else None

    def save_token(self, token: str) -> LoginStatus:
        token = "".join(token.split())
        if not TOKEN.fullmatch(token):
            raise LoginError("That does not look like a token from `claude setup-token`")
        if not (self.kv and self.box):
            raise LoginError("Saving needs DATABASE_URL and APP_SECRET_KEY; set CLAUDE_CODE_OAUTH_TOKEN instead")
        self.kv.set(TOKEN_KEY, self.box.encrypt(token))
        return self.status()

    def remove_token(self) -> None:
        if self.kv:
            self.kv.delete(TOKEN_KEY)

    def _env(self) -> dict[str, str]:
        keep = ("PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
                "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")
        return {**{k: os.environ[k] for k in keep if k in os.environ}, "HOME": str(self.home), "TERM": "dumb",
                "DISABLE_AUTOUPDATER": "1", **({"CLAUDE_CODE_OAUTH_TOKEN": token} if (token := self.token) else {})}

    def status(self) -> LoginStatus:
        try:
            r = subprocess.run([self.binary, "auth", "status", "--json"], env=self._env(), capture_output=True,
                               text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return LoginStatus(False, detail=f"Could not run Claude Code: {e}")
        try:
            d = json.loads(r.stdout)
        except ValueError:
            return LoginStatus(False, detail=(r.stderr or r.stdout).strip()[:300] or "No answer from Claude Code")
        account = d.get("account") if isinstance(d.get("account"), dict) else {}
        return LoginStatus(bool(d.get("loggedIn")), method=d.get("authMethod"),
                           email=d.get("email") or account.get("email") or account.get("emailAddress"))

    def start(self) -> str:
        """Begin a sign-in; returns the link to open. A sign-in already waiting is replaced."""
        with self._lock:
            self._stop()
            self.home.mkdir(parents=True, exist_ok=True)
            pid, fd = pty.fork()
            if pid == 0:  # the child: become `claude auth login`
                try:
                    os.execvpe(self.binary, [self.binary, "auth", "login", "--claudeai"], self._env())
                finally:
                    os._exit(127)
            self._pid, self._fd, self._started = pid, fd, time.monotonic()
            text = self._read_until(lambda t: LINK.search(t) and "Paste code" in t, START_TIMEOUT)
            m = LINK.search(text)
            if not m:
                self._stop()
                raise LoginError("Claude Code did not show a sign-in link: " + text.strip()[-300:])
            return m.group(0)

    def finish(self, code: str) -> LoginStatus:
        """Type the code from the sign-in page; returns the login afterwards."""
        code = code.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-#.~]{8,512}", code):
            raise LoginError("That does not look like the code from the sign-in page")
        with self._lock:
            if self._fd is None:
                raise LoginError("No sign-in is waiting; start again")
            os.write(self._fd, code.encode() + b"\r")
            text = self._read_until(lambda t: False, FINISH_TIMEOUT, until_exit=True)
            self._stop()
        status = self.status()
        if not status.logged_in:
            said = " ".join(text.replace(code, "").split())  # the terminal echoes the code back
            raise LoginError("Sign-in did not complete: " + (said[-300:] or "no answer"))
        return status

    def stamp(self) -> str | None:
        """Changes whenever the stored login or the token does, e.g. after a sign-in on the setup page."""
        try:
            st = (self.home / ".claude" / ".credentials.json").stat()
            login = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            login = ""
        token = self.token
        mark = hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""
        return f"{login}|{mark}" if login or mark else None

    @property
    def waiting(self) -> bool:
        return self._pid is not None

    def _read_until(self, done, timeout: float, until_exit: bool = False) -> str:
        out = ""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self._fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(self._fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += ANSI.sub("", chunk.decode("utf-8", "replace"))
                if done(out):
                    return out
            elif until_exit and self._pid is not None:
                pid, _ = os.waitpid(self._pid, os.WNOHANG)
                if pid:
                    self._pid = None
                    break
        return out

    def _stop(self) -> None:
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGKILL)
                os.waitpid(self._pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._pid = self._fd = None
