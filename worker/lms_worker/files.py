"""Files you send in the chat: where they are kept on the data volume.

<data>/<course>/<assignment>/own/    your own file to upload instead of a solved report
<data>/<course>/<assignment>/refs/   reference files for solving that assignment
<data>/<course>/theme-refs/          reference files for the look of that course's reports
<data>/<course>/lectures/, outline/  what was fetched from the LMS for you
"""

from __future__ import annotations

import re
from pathlib import Path

from .db.models import Assignment, Course

MAX_BYTES = 40 * 1024 * 1024
MAX_FILES = 8


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "course"


def course_dir(data_dir: Path, course: Course) -> Path:
    return data_dir / _safe(course.code)


def own_dir(data_dir: Path, a: Assignment) -> Path:
    return course_dir(data_dir, a.course) / str(a.id) / "own"


def refs_dir(data_dir: Path, a: Assignment) -> Path:
    return course_dir(data_dir, a.course) / str(a.id) / "refs"


def theme_dir(data_dir: Path, course: Course) -> Path:
    return course_dir(data_dir, course) / "theme-refs"


def listed(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []


def clean_name(name: str) -> str:
    """A file name from a chat, made safe to keep: no folders, no odd characters."""
    name = Path(name.replace("\\", "/")).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", stem).strip(" ._")[:100] or "file"
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext)[:10].lower()
    return f"{stem}.{ext}" if ext else stem


def keep(folder: Path, name: str, data: bytes, *, only: bool = False) -> Path:
    """Save into folder. With only, it replaces whatever was there."""
    folder.mkdir(parents=True, exist_ok=True)
    if only:
        for old in listed(folder):
            old.unlink()
    path = folder / clean_name(name)
    path.write_bytes(data)
    return path
