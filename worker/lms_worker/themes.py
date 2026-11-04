"""Each course's report theme, uploaded on its course page and kept in the settings table.

A solver run copies the course's theme into work/theme and reads it there, so it needs no
access to the theme artifact (a CLAUDE_CODE_OAUTH_TOKEN is enough). Courses without one
fall back to the theme artifact.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath

from .kv import KeyValue

MAX_FILES = 200
MAX_BYTES = 15 * 1024 * 1024
PART = re.compile(r"[\w.\- ()&+,@]+")


def _key(course_id: int) -> str:
    return f"theme:{int(course_id)}"


def clean_name(name: str) -> str:
    """A relative path inside the theme folder, e.g. components/cover/README.md."""
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("/", ".")]
    if not parts or any(p == ".." or not PART.fullmatch(p) for p in parts):
        raise ValueError(f"Bad file name: {name!r}")
    return "/".join(parts)


def decode(files: list[tuple[str, str]]) -> dict[str, bytes]:
    """(name, base64) pairs from the page into checked files."""
    if not files:
        raise ValueError("Choose the theme's files first")
    if len(files) > MAX_FILES:
        raise ValueError(f"At most {MAX_FILES} files")
    out: dict[str, bytes] = {}
    for name, data in files:
        try:
            out[clean_name(name)] = base64.b64decode(data, validate=True)
        except binascii.Error:
            raise ValueError(f"{name} did not arrive whole; try again") from None
    if sum(len(v) for v in out.values()) > MAX_BYTES:
        raise ValueError(f"The theme is over {MAX_BYTES // (1024 * 1024)} MB")
    return out


def save(kv: KeyValue, course_id: int, files: dict[str, bytes]) -> None:
    kv.set(_key(course_id), json.dumps({k: base64.b64encode(v).decode() for k, v in files.items()}))


def load(kv: KeyValue, course_id: int) -> dict[str, bytes] | None:
    raw = kv.get(_key(course_id))
    return {k: base64.b64decode(v) for k, v in json.loads(raw).items()} if raw else None


def mark(files: dict[str, bytes]) -> str:
    """Changes whenever any file does."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return h.hexdigest()[:16]


def remove(kv: KeyValue, course_id: int) -> None:
    kv.delete(_key(course_id))


def write(files: dict[str, bytes], dest: Path) -> None:
    """Replace dest with the theme's files."""
    if dest.exists():
        shutil.rmtree(dest)
    for name, data in files.items():
        path = dest / clean_name(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
