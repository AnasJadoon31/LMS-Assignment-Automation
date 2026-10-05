"""Small key-value state: the LMS session, the login guard, pauses, alert de-duplication.

FileKV keeps it in a directory for local runs without a database. DbKV keeps it in
the settings table. SecretBox encrypts values that must not sit in plain text.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session, sessionmaker

from .db.models import Setting

MIN_SECRET_LENGTH = 32


class KeyValue(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class FileKV:
    """One file per key, owner-only permissions."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_:.-]+", key):
            raise ValueError(f"Bad key {key!r}")
        return self.directory / f"{key.replace(':', '__')}.json"

    def get(self, key: str) -> str | None:
        p = self.path(key)
        return p.read_text() if p.exists() else None

    def set(self, key: str, value: str) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.replace(tmp, p)

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


class DbKV:
    """Rows in the settings table."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def get(self, key: str) -> str | None:
        with self.sessions() as s:
            row = s.get(Setting, key)
            return row.value if row else None

    def set(self, key: str, value: str) -> None:
        with self.sessions.begin() as s:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value

    def delete(self, key: str) -> None:
        with self.sessions.begin() as s:
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)


class SecretBox:
    """Symmetric encryption keyed from APP_SECRET_KEY."""

    def __init__(self, secret: str) -> None:
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"APP_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("Stored value cannot be decrypted; APP_SECRET_KEY changed?") from e
