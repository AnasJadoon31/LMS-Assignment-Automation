"""The report theme: a mirror of the Anas Assignment Theme artifact.

The artifact on claude.ai is the only theme. A server cannot open it (it is private, and
claude.ai answers bots with a Cloudflare check), so its files are mirrored into
lms_worker/theme/ exactly as published, with one change: the student's name, enrollment and
class become placeholders, so they stay out of git. `install` puts the theme into the solver's
work folder with the placeholders filled from the env file, giving Claude the artifact's
files as they are.

Refresh the mirror whenever the artifact changes: download its project/ files and logo,
then `lms-worker theme-import <folder>`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

ARTIFACT_URL = "https://claude.ai/artifact/F2Je3LZvKN92ysXVL7jM2R"
THEME_DIR = Path(str(resources.files("lms_worker") / "theme"))
TEXT = {".md", ".json", ".html"}
LOGO = "assets/Logos/bahria-university.png"


@dataclass(frozen=True)
class Student:
    name: str  # "First Last"
    enrollment: str  # "00-000000-000"
    klass: str  # "BS(CS) 3-A"

    def values(self) -> dict[str, str]:
        """Placeholder -> value. Longer values first, so a placeholder never splits another."""
        section = self.klass.split()[-1] if self.klass.split() else self.klass
        pairs = {
            "{{STUDENT_NAME_UNDERSCORED}}": "_".join(self.name.split()),
            "{{STUDENT_NAME}}": self.name,
            "{{ENROLLMENT}}": self.enrollment,
            "{{CLASS}}": self.klass,
            "{{SECTION}}": section,
        }
        return dict(sorted(pairs.items(), key=lambda kv: -len(kv[1])))


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name != "MIRROR.json")


def import_artifact(src: Path, student: Student, *, version: str = "", logo_blob: str = "", dest: Path = THEME_DIR) -> list[str]:
    """Mirror a downloaded copy of the artifact (its project/ folder) into dest.

    Only the student details change, and the previews' logo link points at the mirrored logo.
    Returns the mirrored paths.
    """
    project = src / "project" if (src / "project").is_dir() else src
    if not (project / "README.md").exists() or not (project / "tokens.json").exists():
        raise FileNotFoundError(f"{src} has no README.md and tokens.json from the theme artifact")
    if dest.exists():
        shutil.rmtree(dest)
    written = []
    for f in _files(project):
        rel = f.relative_to(project)
        if rel.name == "design-system.json" or rel.parts[0] in ("api", "archived"):
            continue  # the artifact's own index and generated cards are not theme content
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if f.suffix in TEXT:
            text = f.read_text()
            for placeholder, value in student.values().items():
                text = text.replace(value, placeholder)
            if logo_blob and rel.suffix == ".html":
                text = text.replace(f"/_blob/{logo_blob}", "../../" + LOGO)
            out.write_text(text)
        else:
            shutil.copyfile(f, out)
        written.append(str(rel))
    if not (dest / LOGO).exists():
        raise FileNotFoundError(f"The logo is missing: put it at {project / LOGO}")
    (dest / "MIRROR.json").write_text(json.dumps({
        "source": ARTIFACT_URL, "version": version, "mirrored": date.today().isoformat(),
        "changes": "student name, enrollment and class replaced by {{...}} placeholders; preview logo links made relative",
    }, indent=2) + "\n")
    return written


def install(dest: Path, student: Student, *, src: Path = THEME_DIR) -> Path:
    """The theme as the artifact has it, placeholders filled, for the solver's work folder."""
    if dest.exists():
        shutil.rmtree(dest)
    for f in _files(src):
        out = dest / f.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        if f.suffix in TEXT:
            text = f.read_text()
            for placeholder, value in student.values().items():
                text = text.replace(placeholder, value)
            out.write_text(text)
        else:
            shutil.copyfile(f, out)
    return dest
