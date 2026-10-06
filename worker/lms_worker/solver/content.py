"""The report content: what the agent returns, and what the renderer turns into a document.

The layout follows the submitted Lab 5 report (see lms_worker/theme/README.md): a cover
page, then per exercise a banner, a Task and Result box, the agent's own sections
(paragraphs, tables, grid figures, drawn figures, each table and figure captioned), the
code, the Jupyter Notebook screenshots and the Output screenshot.

The agent writes text and names files. The code, the notebook cells, the screenshots and
the output are taken from the sandbox after the code has been run there again, so what
the report shows is what actually ran.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_EXERCISES = 20
MAX_TEXT = 6000
CELL_MARK = re.compile(r"^#\s*%%\s*(.*)$")

_BLOCK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type"],
    "properties": {
        "type": {"type": "string", "enum": ["paragraph", "table", "grid", "figure"]},
        "text": {"type": "string", "description": "paragraph: plain text; wrap the key result in **double stars** to bold it."},
        "header": {"type": "array", "items": {"type": "string"}, "description": "table: column headings."},
        "rows": {
            "type": "array",
            "description": "table: rows of cell text. grid: rows of cells, each {label, note, dark}.",
            "items": {"type": "array", "items": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "object", "additionalProperties": False, "required": ["label"], "properties": {
                        "label": {"type": "string"}, "note": {"type": "string"}, "dark": {"type": "boolean"}}},
                ]}},
        },
        "highlight": {"type": "array", "items": {"type": "integer"}, "description": "table: 0-based indexes of rows to highlight (the answer row)."},
        "file": {"type": "string", "description": "figure: an image your code saved under /work/out/figures, e.g. route_graph.png."},
        "caption": {"type": "string", "description": "table, grid, figure: caption text without the 'Table 1:' prefix."},
    },
}

# The agent's final answer, enforced by the CLI's --json-schema
SOLVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "number", "title", "course_code", "exercises"],
    "properties": {
        "blocked_reason": {"type": "string", "description": "Set only if this cannot be done here (needs hardware, a group, handwriting, a login). Leave the rest minimal."},
        "kind": {"type": "string", "enum": ["lab", "assignment"]},
        "number": {"type": "integer", "description": "The lab or assignment number from the sheet."},
        "title": {"type": "string", "description": 'The topic only, as on the sheet: "Greedy and A* Search Algorithm" (no "Lab 5:" prefix).'},
        "course_code": {"type": "string", "description": 'As printed on the sheet, e.g. "CSL-411"; empty if none.'},
        "exercises": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_EXERCISES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "tag", "task", "result", "sections"],
                "properties": {
                    "title": {"type": "string", "description": 'e.g. "Greedy Best First Search" (no "Exercise 1:" prefix).'},
                    "tag": {"type": "string", "description": 'Two or three words under the title, e.g. "Greedy Search".'},
                    "task": {"type": "string", "description": "The question, quoted from the sheet."},
                    "result": {"type": "string", "description": "The answer in one sentence; wrap the answer itself in **double stars**."},
                    "sections": {
                        "type": "array",
                        "description": 'Headed sections before the code, e.g. "Given Graph", "How It Works".',
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["heading", "blocks"],
                            "properties": {"heading": {"type": "string"}, "blocks": {"type": "array", "items": _BLOCK}},
                        },
                    },
                    "code_file": {"type": "string", "description": "The program under /work/code, e.g. exercise1_greedy.py. Split it into notebook cells with '# %% <what the cell holds>' lines."},
                    "notebook_intro": {"type": "string", "description": "One or two sentences before the notebook screenshots."},
                },
            },
        },
        "notes_for_student": {"type": "string", "description": "Anything the student should check before submitting. Not printed."},
    },
}


@dataclass
class Shot:
    file: str
    caption: str


@dataclass
class Exercise:
    title: str
    tag: str
    task: str
    result: str
    sections: list[dict] = field(default_factory=list)
    code_file: str | None = None
    notebook_intro: str | None = None
    # Filled from the sandbox run, never by the agent
    code: str | None = None
    cells: list[str] = field(default_factory=list)  # cell captions, in order
    screenshots: list[Shot] = field(default_factory=list)
    output_shot: Shot | None = None
    output_text: str | None = None


@dataclass
class Report:
    kind: str  # lab or assignment
    number: int
    title: str
    course_code: str
    exercises: list[Exercise]
    notes_for_student: str | None = None
    blocked_reason: str | None = None

    @property
    def label(self) -> str:
        """'Lab 05', 'Assignment 02'."""
        return f"{'Lab' if self.kind == 'lab' else 'Assignment'} {self.number:02d}"


class ContentError(ValueError):
    pass


def _text(value: Any, name: str, limit: int = MAX_TEXT, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ContentError(f"{name} is empty")
        return None
    if not isinstance(value, str):
        raise ContentError(f"{name} is not text")
    return value.strip()[:limit]


def safe_name(value: str | None, name: str) -> str | None:
    """A file name inside the work folder: no absolute paths, no going up."""
    if value is None:
        return None
    for prefix in ("/work/code/", "/work/out/figures/", "./"):
        value = value.strip().removeprefix(prefix)
    if not value or value.startswith("/") or ".." in value.split("/") or "\\" in value:
        raise ContentError(f"{name} is not a plain file name: {value!r}")
    return value


def _block(b: Any, where: str) -> dict:
    if not isinstance(b, dict) or b.get("type") not in ("paragraph", "table", "grid", "figure"):
        raise ContentError(f"{where}: unknown block")
    kind = b["type"]
    caption = _text(b.get("caption"), f"{where} caption", 300)
    if kind == "paragraph":
        return {"type": kind, "text": _text(b.get("text"), f"{where} text", required=True)}
    if kind == "figure":
        return {"type": kind, "file": safe_name(_text(b.get("file"), f"{where} file", 200, required=True), where), "caption": caption}
    rows = b.get("rows") or []
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) and r for r in rows):
        raise ContentError(f"{where}: no rows")
    if kind == "grid":
        cells = [[{"label": str(c.get("label", ""))[:20], "note": str(c.get("note", ""))[:40], "dark": bool(c.get("dark"))}
                  if isinstance(c, dict) else {"label": str(c)[:20], "note": "", "dark": False} for c in r[:8]] for r in rows[:8]]
        return {"type": kind, "rows": cells, "caption": caption}
    header = [str(h)[:80] for h in (b.get("header") or [])][:10]
    body = [[str(c)[:200] if not isinstance(c, dict) else str(c.get("label", "")) for c in r[:10]] for r in rows[:80]]
    width = max([len(header)] + [len(r) for r in body])
    highlight = sorted({i for i in (b.get("highlight") or []) if isinstance(i, int) and 0 <= i < len(body)})
    return {"type": kind, "header": header + [""] * (width - len(header)) if header else [],
            "rows": [r + [""] * (width - len(r)) for r in body], "highlight": highlight, "caption": caption}


def parse_report(data: dict[str, Any]) -> Report:
    """Check the agent's answer beyond what the schema enforces."""
    if not isinstance(data, dict):
        raise ContentError("The answer is not an object")
    blocked = _text(data.get("blocked_reason"), "blocked reason", 1000)
    items = data.get("exercises") or []
    if not blocked and (not isinstance(items, list) or not items):
        raise ContentError("No exercises")
    exercises = []
    for i, item in enumerate(items[:MAX_EXERCISES], 1):
        where = f"Exercise {i}"
        if not isinstance(item, dict):
            raise ContentError(f"{where} is not an object")
        sections = []
        for j, sec in enumerate(item.get("sections") or [], 1):
            if not isinstance(sec, dict):
                raise ContentError(f"{where} section {j} is not an object")
            sections.append({
                "heading": _text(sec.get("heading"), f"{where} section {j} heading", 120, required=True),
                "blocks": [_block(b, f"{where} section {j} block {k}") for k, b in enumerate(sec.get("blocks") or [], 1)],
            })
        exercises.append(Exercise(
            title=_text(item.get("title"), f"{where} title", 200, required=True),
            tag=_text(item.get("tag"), f"{where} tag", 60) or "",
            task=_text(item.get("task"), f"{where} task", required=True),
            result=_text(item.get("result"), f"{where} result", 1000, required=True),
            sections=sections,
            code_file=safe_name(_text(item.get("code_file"), f"{where} code file", 200), f"{where} code file"),
            notebook_intro=_text(item.get("notebook_intro"), f"{where} notebook intro", 1000),
        ))
    number = data.get("number")
    return Report(
        kind="assignment" if data.get("kind") == "assignment" else "lab",
        number=number if isinstance(number, int) and 0 <= number < 100 else 0,
        title=_text(data.get("title"), "title", 300, required=not blocked) or "",
        course_code=_text(data.get("course_code"), "course code", 40) or "",
        exercises=exercises,
        notes_for_student=_text(data.get("notes_for_student"), "notes"),
        blocked_reason=blocked,
    )


def split_cells(source: str) -> tuple[str, list[tuple[str, str]]]:
    """'# %% caption' lines split a program into notebook cells.

    Returns the program without the markers (for the Code block) and (caption, code)
    per cell. A program without markers is one cell.
    """
    cells: list[tuple[str, list[str]]] = []
    for line in source.splitlines():
        m = CELL_MARK.match(line)
        if m:
            cells.append((m[1].strip(), []))
        else:
            if not cells:
                cells.append(("", []))
            cells[-1][1].append(line)
    out = []
    for caption, lines in cells:
        body = "\n".join(lines).strip("\n")
        if body.strip():
            out.append((caption, body))
    return "\n\n".join(code for _, code in out) + "\n", out


def report_to_dict(r: Report) -> dict[str, Any]:
    d = dict(vars(r))
    d["exercises"] = []
    for e in r.exercises:
        x = dict(vars(e))
        x["screenshots"] = [vars(s) for s in e.screenshots]
        x["output_shot"] = vars(e.output_shot) if e.output_shot else None
        d["exercises"].append(x)
    return d


def report_from_dict(d: dict[str, Any]) -> Report:
    exercises = []
    for x in d["exercises"]:
        x = dict(x)
        x["screenshots"] = [Shot(**s) for s in x.get("screenshots") or []]
        x["output_shot"] = Shot(**x["output_shot"]) if x.get("output_shot") else None
        exercises.append(Exercise(**x))
    return Report(**{**d, "exercises": exercises})
