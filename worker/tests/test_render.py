import shutil
import struct
import zlib
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest

from lms_worker.solver.content import ContentError, Exercise, Report, Shot, parse_report, report_from_dict, report_to_dict, split_cells
from lms_worker.solver.render import Renderer, Student, CourseInfo, to_pdf

STUDENT = Student(name="Student Name", enrollment="00-000000-000", klass="BS(CS) 3-A")
COURSE = CourseInfo(name="Artificial Intelligence", code="CSL-411")
PROGRAM = '''# %% the data
"""
Lab 5 - Exercise 1
Start node = S
"""
from queue import PriorityQueue
graph = {'S': ['A']}  # neighbours

# %% the search
def search(start):
    if start not in graph:
        return None
    print("Expanding:", start)
'''


def png(path: Path, w: int = 40, h: int = 20) -> Path:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def answer(**changes):
    data = {
        "kind": "lab", "number": 5, "title": "Greedy and A* Search Algorithm", "course_code": "CSL-411",
        "exercises": [{
            "title": "Greedy Best First Search", "tag": "Greedy Search",
            "task": "Write a Python program for greedy best first search.",
            "result": "The search reaches the goal through **S → A → G**",
            "sections": [
                {"heading": "Given Graph", "blocks": [
                    {"type": "paragraph", "text": "I treated the table as a grid."},
                    {"type": "grid", "rows": [[{"label": "S", "note": "h = 3", "dark": True}, {"label": "A", "note": "h = 5"}]],
                     "caption": "Heuristic grid"},
                    {"type": "figure", "file": "graph.png", "caption": "Route graph"},
                ]},
                {"heading": "How It Works", "blocks": [
                    {"type": "paragraph", "text": "The dry run:"},
                    {"type": "table", "header": ["Step", "Expanded"], "rows": [["1", "S"], ["2", "G"]], "highlight": [1, 7],
                     "caption": "Dry run"},
                ]},
            ],
            "code_file": "exercise1_greedy.py",
        }],
    }
    data.update(changes)
    return data


def build(tmp_path, report: Report) -> dict[str, str]:
    files = tmp_path / "files"
    png(files / "figures" / "graph.png")
    png(files / "shots" / "cell1.png")
    png(files / "shots" / "cell2.png")
    docx = Renderer().docx(report, STUDENT, COURSE, date(2026, 10, 3), tmp_path / "out" / "report.docx", files)
    with ZipFile(docx) as z:
        return {n: z.read(n).decode() for n in z.namelist() if n.endswith(".xml")}


def solved(**changes) -> Report:
    r = parse_report(answer(**changes))
    ex = r.exercises[0]
    ex.code, cells = split_cells(PROGRAM)
    ex.cells = [c for c, _ in cells]
    ex.screenshots = [Shot("cell1.png", "Cell 1, the data")]
    ex.output_shot = Shot("cell2.png", "Cell 2 after running, with the output below the cell")
    return r


def test_answer_is_checked_beyond_the_schema():
    r = parse_report(answer())
    table = r.exercises[0].sections[1]["blocks"][1]
    assert table["highlight"] == [1]  # out-of-range rows dropped
    assert r.label == "Lab 05"
    with pytest.raises(ContentError, match="not a plain file name"):
        parse_report(answer(exercises=[{**answer()["exercises"][0], "code_file": "../../etc/passwd"}]))
    with pytest.raises(ContentError, match="No exercises"):
        parse_report(answer(exercises=[]))
    blocked = parse_report({"blocked_reason": "Needs an Arduino board", "exercises": []})
    assert blocked.blocked_reason == "Needs an Arduino board" and blocked.exercises == []
    assert report_from_dict(report_to_dict(solved())) == solved()


def test_cell_markers_split_the_program():
    code, cells = split_cells(PROGRAM)
    assert [c for c, _ in cells] == ["the data", "the search"]
    assert "# %%" not in code and code.startswith('"""') and "def search" in code
    assert split_cells("print(1)\n")[1] == [("", "print(1)")]


def test_semester_comes_from_the_class():
    assert STUDENT.semester_label == "Semester 03"
    assert Student("a", "b", "BS(CS) 7-A", semester="Semester 08").semester_label == "Semester 08"


def test_cover_header_and_footer(tmp_path):
    xml = build(tmp_path, solved())
    doc = xml["word/document.xml"]
    cover, body = doc.split("<w:sectPr", 1)[0], doc
    assert "BAHRIA UNIVERSITY" in cover and "KARACHI CAMPUS" in cover and "Department of Computer Science" in cover
    assert "LAB REPORT 05" in cover and "Greedy and A* Search Algorithm" in cover
    assert "CSL-411 Artificial Intelligence   |   Semester 03" in cover
    for value in ("Submitted by", "Student Name", "00-000000-000", "BS(CS) 3-A", "3 October 2026"):
        assert value in cover
    sections = body.split("<w:sectPr")[1:]
    assert len(sections) == 2
    assert "w:pgBorders" in sections[0] and "w:pgBorders" not in sections[1]  # the border is the cover's only
    assert 'w:start="1"' in sections[1]
    header = next(v for k, v in xml.items() if k.startswith("word/header"))
    footer = next(v for k, v in xml.items() if k.startswith("word/footer"))
    assert "Bahria University Karachi Campus" in header and "CSL-411 Artificial Intelligence  |  Lab 05" in header
    assert "Student Name | 00-000000-000 | BS(CS) 3-A" in footer and "PAGE" in footer
    assert "w:pStyle" not in header  # the template's Header style would add its own tab stops


def test_exercise_blocks_captions_and_code_colours(tmp_path):
    doc = build(tmp_path, solved())["word/document.xml"]
    for text in ("EXERCISE", ">01<", "GREEDY SEARCH".title(), "TASK", "RESULT", "S → A → G",
                 "Given Graph", "How It Works", "Code", "exercise1_greedy.py", "Jupyter Notebook Screenshots", "Output"):
        assert text in doc, text
    order = [doc.index(c) for c in ("Figure 1: Heuristic grid.", "Figure 2: Route graph.", "Table 1: Dry run.",
                                     "Screenshot 1: Cell 1, the data.", "Screenshot 2: Cell 2 after running")]
    assert order == sorted(order)
    assert "The code is divided into two cells: the data, and the search." in doc
    # docstring green italic, keyword bold navy, string red
    assert '<w:i/><w:iCs/><w:color w:val="2E7D4F"/>' in doc
    assert '<w:b/><w:bCs/><w:color w:val="2D398C"/><w:sz w:val="17"/>' in doc
    assert '<w:color w:val="B3261E"/><w:sz w:val="17"/>' in doc
    assert "lineRule" not in doc  # line spacing written exactly as the submitted report does
    lead_in = doc[:doc.index("The dry run:")]
    assert lead_in[lead_in.rindex("<w:p>"):].count("<w:keepNext/>") == 1  # stays with its table


def test_code_starts_a_page_only_after_a_short_opening(tmp_path):
    short = build(tmp_path / "a", solved())["word/document.xml"]
    before_code = short[:short.index("exercise1_greedy.py")]
    assert before_code.count("<w:pageBreakBefore/>") == 1
    long_text = {"type": "paragraph", "text": "word " * 2000}
    report = solved()
    report.exercises[0].sections.append({"heading": "More", "blocks": [long_text]})
    long = build(tmp_path / "b", report)["word/document.xml"]
    assert long[:long.index("exercise1_greedy.py")].count("<w:pageBreakBefore/>") == 0


def test_each_later_exercise_starts_a_page(tmp_path):
    report = solved()
    second = Exercise(title="A* Search", tag="A* Search", task="t", result="r", sections=[])
    report.exercises.append(second)
    doc = build(tmp_path, report)["word/document.xml"]
    assert ">02<" in doc and doc.count("<w:pageBreakBefore/>") == 2


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")
def test_pdf_export(tmp_path):
    build(tmp_path, solved())
    pdf = to_pdf(tmp_path / "out" / "report.docx")
    assert pdf.read_bytes().startswith(b"%PDF")
