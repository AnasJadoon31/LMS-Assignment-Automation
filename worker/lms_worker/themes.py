"""Each course's report theme, uploaded on its course page and kept in the settings table.

A default theme, uploaded on the Courses page, serves every course without its own.
A solver run copies the theme into work/theme and reads it there, so it needs no access to
the theme artifact (a token is enough). With neither, it falls back to the theme artifact.
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
DEFAULT = "default"  # in place of a course id: the theme for courses without their own


def _key(course_id: int | str) -> str:
    return f"theme:{DEFAULT if course_id == DEFAULT else int(course_id)}"


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


def save(kv: KeyValue, course_id: int | str, files: dict[str, bytes]) -> None:
    kv.set(_key(course_id), json.dumps({k: base64.b64encode(v).decode() for k, v in files.items()}))


def load(kv: KeyValue, course_id: int | str) -> dict[str, bytes] | None:
    raw = kv.get(_key(course_id))
    return {k: base64.b64decode(v) for k, v in json.loads(raw).items()} if raw else None


def mark(files: dict[str, bytes]) -> str:
    """Changes whenever any file does."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return h.hexdigest()[:16]


def for_course(kv: KeyValue, course_id: int) -> dict[str, bytes] | None:
    """The course's own theme, else the default one."""
    return load(kv, course_id) or load(kv, DEFAULT)


def remove(kv: KeyValue, course_id: int | str) -> None:
    kv.delete(_key(course_id))


def write(files: dict[str, bytes], dest: Path) -> None:
    """Replace dest with the theme's files."""
    if dest.exists():
        shutil.rmtree(dest)
    for name, data in files.items():
        path = dest / clean_name(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


# A course's report kit: the code a solve wrote to build its report in the theme (page setup, cover,
# header, every component, screenshot tools). The next solve gets it in work/kit and writes only the
# assignment's content. It is kept for the theme it was made for; a new theme starts a new kit.

def _kit_key(course_id: int) -> str:
    return f"kit:{int(course_id)}"


def read_dir(src: Path) -> dict[str, bytes] | None:
    """A folder's files, or None when there are none or they are over the limits."""
    if not src.is_dir():
        return None
    out = {}
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if p.is_file() and "__pycache__" not in rel and not rel.endswith(".pyc"):
            try:
                out[clean_name(rel)] = p.read_bytes()
            except ValueError:
                continue
    if not out or len(out) > MAX_FILES or sum(map(len, out.values())) > MAX_BYTES:
        return None
    return out


def save_kit(kv: KeyValue, course_id: int, theme_mark: str, files: dict[str, bytes], source: str) -> None:
    kv.set(_kit_key(course_id), json.dumps({"theme": theme_mark, "source": source,
                                            "files": {k: base64.b64encode(v).decode() for k, v in files.items()}}))


def kit_info(kv: KeyValue, course_id: int) -> dict | None:
    raw = kv.get(_kit_key(course_id))
    return json.loads(raw) if raw else None


def load_kit(kv: KeyValue, course_id: int, theme_mark: str) -> dict[str, bytes] | None:
    """The course's kit, if it was made for this theme."""
    info = kit_info(kv, course_id)
    if not info or info.get("theme") != theme_mark:
        return None
    return {k: base64.b64decode(v) for k, v in info["files"].items()}


def remove_kit(kv: KeyValue, course_id: int) -> None:
    kv.delete(_kit_key(course_id))
