"""Course plan, course outline and lecture notes: three more LMS pages, read on request.

CoursePlan.php?s=&oc=     one table per course: Week No., Content, Outcome; a single
                          "No course plan updated yet." row when the teacher posted none.
                          There is no file: the table is the plan.
CourseOutline.php         one table for the whole semester: Sr.No., Course, a Download link.
LectureNotes.php?s=&oc=   one row per week: title, lecture files (several per week), the
                          teacher's and other video links, remarks.

Download links carry today's date, so the pages are read again whenever a file is asked for.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from .bahria_pages import decode_download_key
from .errors import ContractBroken

NOTHING = ("no course plan", "no lecture", "please select a course", "no course outline", "not uploaded")


@dataclass(frozen=True)
class PlanWeek:
    week: str
    content: str
    outcome: str


@dataclass(frozen=True)
class LectureFile:
    week: str
    title: str
    filename: str
    href: str


@dataclass(frozen=True)
class Lecture:
    week: str
    title: str
    files: tuple[LectureFile, ...]
    videos: tuple[str, ...]
    remarks: str | None


def _text(cell: Tag) -> str:
    return " ".join(cell.get_text(" ").split())


def _rows(page: str, what: str, first_header: str, columns: int) -> list[list[Tag]]:
    table = BeautifulSoup(page, "html.parser").find("table", class_="table-hover")
    if table is None:
        raise ContractBroken(f"The {what} page has no table")
    headers = [_text(th).lower() for th in table.find_all("th")]
    if len(headers) < columns or not headers[0].startswith(first_header):
        raise ContractBroken(f"The {what} table's columns changed: {headers}")
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) == 1 and any(m in _text(cells[0]).lower() for m in NOTHING):
            continue
        if len(cells) >= columns:
            rows.append(cells)
        elif cells:
            raise ContractBroken(f"A {what} row has {len(cells)} cells, expected {columns}")
    return rows


def parse_plan(page: str) -> list[PlanWeek]:
    return [PlanWeek(_text(c[0]), _text(c[1]), _text(c[2])) for c in _rows(page, "course plan", "week", 3)]


def parse_outlines(page: str) -> dict[str, tuple[str, str]]:
    """Course name (lower case) -> (file name, download link)."""
    out = {}
    for c in _rows(page, "course outline", "sr", 3):
        link = c[2].find("a", href=re.compile(r"Download\.php"))
        key = decode_download_key(link["href"]) if link else None
        if key:
            out[_text(c[1]).lower()] = (key.filename, link["href"])
    return out


def parse_lectures(page: str) -> list[Lecture]:
    lectures = []
    for c in _rows(page, "lecture notes", "week", 6):
        week, title = _text(c[0]), _text(c[1])
        files = []
        for link in c[2].find_all("a", href=re.compile(r"Download\.php")):
            key = decode_download_key(link["href"])
            if key:
                files.append(LectureFile(week, title, key.filename, link["href"]))
        videos = []
        for cell in c[3:5]:
            for link in cell.find_all("a"):
                url = link.get("data-lecurl") or link.get("data-lecUrl") or link.get("href") or ""
                if url.startswith(("http://", "https://")) and url not in videos:
                    videos.append(url)
        lectures.append(Lecture(week, title, tuple(files), tuple(videos), _text(c[5]) or None))
    return lectures


PLAN_CSS = """body{margin:0;padding:28px;background:#fff;font:15px/1.45 Arial,Helvetica,sans-serif;color:#1c2733;width:1040px}
h1{font-size:22px;margin:0 0 4px}p{margin:0 0 18px;color:#5b6876}
table{border-collapse:collapse;width:100%}th{background:#1f4e79;color:#fff;text-align:left}
th,td{padding:9px 12px;border:1px solid #c9d3de;vertical-align:top}tr:nth-child(even) td{background:#f3f6fa}
td:first-child,th:first-child{width:70px;text-align:center}"""


def plan_html(course: str, semester: str, weeks: list[PlanWeek]) -> str:
    """The plan as a page of its own, for the picture that is sent to you."""
    e = html.escape
    rows = "".join(f"<tr><td>{e(w.week)}</td><td>{e(w.content)}</td><td>{e(w.outcome)}</td></tr>" for w in weeks)
    return (f"<!doctype html><meta charset='utf-8'><style>{PLAN_CSS}</style><h1>Course plan · {e(course)}</h1>"
            f"<p>{e(semester)}</p><table><tr><th>Week</th><th>Content</th><th>Outcome</th></tr>{rows}</table>")
