import base64

import pytest

from lms_worker.bot import MaterialsError
from lms_worker.db import Course
from lms_worker.kv import FileKV
from lms_worker.lms.errors import ContractBroken, LMSError, SessionExpired
from lms_worker.lms.materials import PlanWeek, parse_lectures, parse_outlines, parse_plan, plan_html
from lms_worker.materials import POLL_NOW_KEY, Materials


def key(kind, name):
    return base64.b64encode(f"100001,2026-10-09,{kind},0,{name},lms.example/Student/x.php".encode()).decode()


PLAN = """<table class="table table-hover"><tbody><tr><th>Week No.</th><th>Content</th><th>Outcome</th></tr>
<tr><td>1 </td><td>Languages &amp; alphabets </td><td>Explain formal languages </td></tr>
<tr><td>2 </td><td>Regular expressions </td><td>Design patterns </td></tr></tbody></table>"""
NO_PLAN = """<table class="table table-hover"><tbody><tr><th>Week No.</th><th>Content</th><th>Outcome</th></tr>
<tr><td colspan="3"> No course plan updated yet. </td></tr></tbody></table>"""
OUTLINES = f"""<table class="table table-hover"><tbody><tr><th>Sr.No.</th><th>Course</th><th>Course Outline</th></tr>
<tr><td>1 </td><td>Theory of Automata </td><td><a href="Download.php?k={key('CourseOutlines', 'TOA-plan.docx')}">Download Course Outline</a></td></tr>
<tr><td>2 </td><td>Operating Systems </td><td> </td></tr></tbody></table>"""
LECTURES = f"""<table class="table table-hover"><tbody><tr><th>Week #</th><th>Lecture Title</th><th>Lecture File(s)</th>
<th>Video Link(s)</th><th>Video Link</th><th>Remarks/Reference</th></tr>
<tr><td>1 </td><td>Intro </td><td><small>1.</small> <a href="Download.php?k={key('Lecture', 'Week-1.pptx')}">Download Lecture</a><br/>
<small>2.</small> <a href="Download.php?k={key('Lecture', 'Week-1-notes.pdf')}">Download Lecture</a></td>
<td><a class="video-link" data-lecUrl="https://video.example/1" href="#">Watch</a></td><td> </td><td>Chapter 1 </td></tr>
<tr><td>2 </td><td>Regex </td><td> </td><td> </td><td> </td><td> </td></tr></tbody></table>"""


def test_the_three_pages_are_parsed():
    assert parse_plan(PLAN) == [PlanWeek("1", "Languages & alphabets", "Explain formal languages"),
                                PlanWeek("2", "Regular expressions", "Design patterns")]
    assert parse_plan(NO_PLAN) == []
    assert {k: v[0] for k, v in parse_outlines(OUTLINES).items()} == {"theory of automata": "TOA-plan.docx"}
    first, second = parse_lectures(LECTURES)
    assert [(f.week, f.filename) for f in first.files] == [("1", "Week-1.pptx"), ("1", "Week-1-notes.pdf")]
    assert first.videos == ("https://video.example/1",) and first.remarks == "Chapter 1" and second.files == ()
    with pytest.raises(ContractBroken):
        parse_plan("<p>Something else</p>")
    page = plan_html("Theory <of> Automata", "Fall-2026", parse_plan(PLAN))
    assert "Theory &lt;of&gt; Automata" in page and "<td>Languages &amp; alphabets</td>" in page


class FakeAdapter:
    def __init__(self):
        self.error, self.closed, self.weeks = None, 0, parse_plan(PLAN)

    def close(self):
        self.closed += 1

    def course_plan(self, course):
        if self.error:
            raise self.error
        return self.weeks

    def download_outline(self, course, dest):
        return None

    def download_lecture(self, course, week, filename, dest):
        raise LMSError(f"{filename} is no longer in {course.name}'s lecture notes")


COURSE = Course(code="151698", name="Theory of Automata", lms_ref="x", semester="s", semester_name="Fall-2026")


def test_the_course_plan_becomes_a_picture_once(tmp_path):
    shots = []
    adapter = FakeAdapter()

    def shoot(page, path):
        shots.append(page)
        path.write_bytes(b"png")

    m = Materials(adapter, tmp_path, FileKV(tmp_path / "state"), shoot=shoot)
    picture = m.plan(COURSE)
    assert picture.suffix == ".png" and picture.parent == tmp_path / "151698" / "plan" and "Regular expressions" in shots[0]
    assert m.plan(COURSE) == picture and len(shots) == 1  # the same plan is not drawn twice
    assert adapter.closed == 2  # every request starts from the session the worker has now
    adapter.weeks = []
    assert m.plan(COURSE) is None and m.outline(COURSE) is None


def test_lms_trouble_becomes_a_message_for_you(tmp_path):
    adapter = FakeAdapter()
    kv = FileKV(tmp_path / "state")
    m = Materials(adapter, tmp_path, kv, shoot=lambda page, path: None)
    with pytest.raises(MaterialsError, match="no longer in Theory of Automata's lecture notes"):
        m.lecture(COURSE, "1", "gone.pptx")
    adapter.error = SessionExpired("bounced to CMS")
    with pytest.raises(MaterialsError, match="The LMS session has ended. A new login was asked for"):
        m.plan(COURSE)
    assert kv.get(POLL_NOW_KEY)  # the worker is asked to log in again
