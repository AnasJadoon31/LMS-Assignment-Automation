"""The Courses pages: each course's mode, its assignments and lab papers, what the service is
doing with each, the files, and approvals. Same data and rules as the WhatsApp menu.

GET  /courses                       every course this semester, with its mode
GET  /courses/{id}                  one course: assignments, lab papers, files, approvals
GET  /api/courses                   the list as JSON
GET  /api/courses/{id}              one course as JSON
POST /api/courses/{id}/mode         {mode}: approve, auto or notify
POST /api/jobs/{id}/approve         upload the solved PDF
POST /api/jobs/{id}/reject
POST /api/jobs/{id}/redo            {note}: solve again with this note
GET  /files/question/{assignment}   the question file or paper
GET  /files/{artifact}              a solved PDF, DOCX or LMS receipt
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import actions
from .actions import MODE_WORDS
from .bot import _in, _paper_timing, _question_file, _relative, _status, _when, _window
from .db.models import Artifact, Assignment, Course, Event, Job, JobState, utcnow
from .kv import KeyValue
from .lms.models import PAPER
from .watcher import is_lab
from .web import Auth, shell

FILE_LABELS = {"pdf": "Solved PDF", "docx": "DOCX", "receipt": "LMS receipt"}


class Mode(BaseModel):
    mode: str


class Note(BaseModel):
    note: str = ""


def dashboard_router(auth: Auth, sessions: sessionmaker[Session], kv: KeyValue, *,
                     now: Callable[[], datetime] = utcnow) -> APIRouter:
    router = APIRouter()

    def page(request: Request, title: str, body: str, script: str):
        if not auth.signed_in(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(shell(title, "courses", body, script))

    @router.get("/courses", response_class=HTMLResponse)
    def courses_page(request: Request):
        return page(request, "Courses", COURSES_BODY, COURSES_JS)

    @router.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_page(request: Request, course_id: int):
        return page(request, "Course", COURSE_BODY, COURSE_JS.replace("__ID__", str(int(course_id))))

    @router.get("/api/courses")
    def list_courses(request: Request) -> dict:
        auth.guard(request)
        t = now()
        with sessions() as s:
            semester = _semester(s)
            out = []
            for c in s.scalars(select(Course).where(Course.semester == semester).order_by(Course.name)):
                items = _items(s, c.id)
                a = [i for i in items if i.kind != PAPER]
                p = [i for i in items if i.kind == PAPER]
                out.append({
                    "id": c.id, "code": c.code, "name": c.name, "semester": c.semester_name, "mode": c.mode,
                    "lab": is_lab(c.name),
                    "counts": {
                        "remaining": sum(_in(i, "remaining", t) for i in a),
                        "due_soon": sum(_in(i, "due_soon", t) for i in a),
                        "approval": sum(_in(i, "approval", t) for i in a),
                        "needs_you": sum(_in(i, "needs_you", t) for i in a),
                        "papers_coming": sum(_in(i, "upcoming", t) or _in(i, "open_now", t) for i in p),
                    },
                })
            name = s.scalar(select(Course.semester_name).where(Course.semester == semester).limit(1)) if semester else None
        return {"semester": name, "courses": out, "modes": MODE_WORDS}

    @router.get("/api/courses/{course_id}")
    def get_course(request: Request, course_id: int) -> dict:
        auth.guard(request)
        t = now()
        with sessions() as s:
            c = s.get(Course, course_id)
            if c is None:
                raise HTTPException(status_code=404, detail="No such course")
            items = sorted(_items(s, c.id), key=_order)
            return {
                "course": {"id": c.id, "code": c.code, "name": c.name, "semester": c.semester_name, "mode": c.mode,
                           "lab": is_lab(c.name)},
                "assignments": [_item(s, i, t) for i in items if i.kind != PAPER],
                "papers": [_item(s, i, t) for i in items if i.kind == PAPER],
                "modes": MODE_WORDS,
            }

    @router.post("/api/courses/{course_id}/mode")
    def set_mode(request: Request, course_id: int, body: Mode) -> dict:
        auth.guard(request)
        with sessions.begin() as s:
            c = s.get(Course, course_id)
            if c is None:
                raise HTTPException(status_code=404, detail="No such course")
            ok, message = actions.set_mode(c, body.mode)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"message": message}

    def job_action(request: Request, job_id: int, do: Callable[[Session, Job], tuple[bool, str]]) -> dict:
        auth.guard(request)
        with sessions.begin() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="No such job")
            ok, message = do(s, job)
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"message": message}

    @router.post("/api/jobs/{job_id}/approve")
    def approve(request: Request, job_id: int) -> dict:
        return job_action(request, job_id, lambda s, job: actions.approve(s, job, kv, now(), "the web page"))

    @router.post("/api/jobs/{job_id}/reject")
    def reject(request: Request, job_id: int) -> dict:
        return job_action(request, job_id, lambda s, job: actions.reject(s, job, "the web page"))

    @router.post("/api/jobs/{job_id}/redo")
    def redo(request: Request, job_id: int, body: Note) -> dict:
        return job_action(request, job_id, lambda s, job: actions.redo(s, job, body.note, "the web page"))

    @router.get("/files/question/{assignment_id}")
    def question_file(request: Request, assignment_id: int):
        if not auth.signed_in(request):
            raise HTTPException(status_code=401, detail="Sign in first")
        with sessions() as s:
            a = s.get(Assignment, assignment_id)
            path = _question_file(a) if a else None
        if path is None:
            raise HTTPException(status_code=404, detail="That file is not on the server yet")
        return _send(path)

    @router.get("/files/{artifact_id}")
    def artifact_file(request: Request, artifact_id: int):
        if not auth.signed_in(request):
            raise HTTPException(status_code=401, detail="Sign in first")
        with sessions() as s:
            art = s.get(Artifact, artifact_id)
            path = Path(art.path) if art and art.kind in FILE_LABELS else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="That file is no longer on the server")
        return _send(path)

    return router


# --- data ---


def _semester(s: Session) -> str | None:
    return s.scalar(select(Course.semester).order_by(Course.last_seen_at.desc()).limit(1))


def _items(s: Session, course_id: int) -> list[Assignment]:
    q = (select(Assignment).where(Assignment.course_id == course_id)
         .options(selectinload(Assignment.jobs).selectinload(Job.artifacts)))
    return list(s.scalars(q))


def _order(a: Assignment):
    """Open work first, soonest first; then closed work, newest first."""
    when = a.starts_at or a.deadline
    stamp = when.timestamp() if when else float("inf")
    return (a.deadline_passed, -stamp if a.deadline_passed else stamp, a.id)


def _item(s: Session, a: Assignment, now: datetime) -> dict:
    job = a.jobs[-1] if a.jobs else None
    if a.kind == PAPER and a.starts_at:
        when = f"{_window(a)} ({_paper_timing(a, now)})"
    elif a.deadline:
        when = f"Due {_when(a.deadline)}" + ("" if a.deadline_passed else f" ({_relative(a.deadline, now)})")
    else:
        when = "No deadline shown"
    status = _status(a, now)
    tone = "ok" if status.startswith("submitted") else "bad" if status.startswith("missed") else ""
    files = []
    if _question_file(a):
        files.append({"label": "Paper" if a.kind == PAPER else "Question file", "url": f"/files/question/{a.id}"})
    item = {
        "id": a.id, "kind": a.kind, "number": a.number, "title": a.title, "when": when, "status": status, "tone": tone,
        "marks": a.marks, "remarks": a.remarks, "skipped": a.skipped_reason, "files": files, "job": None,
    }
    if job and a.kind != PAPER:
        latest: dict[str, Artifact] = {}
        for art in sorted(job.artifacts, key=lambda x: x.id):
            if art.kind in FILE_LABELS:
                latest[art.kind] = art
        files += [{"label": FILE_LABELS[k], "url": f"/files/{art.id}"} for k, art in sorted(latest.items(), key=lambda kv: list(FILE_LABELS).index(kv[0]))]
        events = s.scalars(select(Event).where(Event.job_id == job.id).order_by(Event.id.desc()).limit(8)).all()
        words = actions.job_words(job)
        if job.state == JobState.NEEDS_HUMAN and job.last_error:
            words += f": {job.last_error}"
        item["job"] = {
            "id": job.id, "state": job.state, "words": words, "revision": job.revision,
            "tone": "wait" if job.state == JobState.AWAITING_APPROVAL else "bad" if job.state == JobState.NEEDS_HUMAN else "",
            "can_approve": job.state == JobState.AWAITING_APPROVAL, "can_redo": actions.can_redo(job),
            "history": [{"at": _when(e.at), "detail": e.detail} for e in reversed(events)],
        }
    return item


def _send(path: Path) -> FileResponse:
    inline = path.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg")
    return FileResponse(path, filename=path.name, content_disposition_type="inline" if inline else "attachment")


# --- pages ---

MODE_JS = """
const MODE_ORDER = ["approve", "auto", "notify"];
function modeControl(course, modes, onDone) {
  const seg = document.createElement("div"); seg.className = "seg";
  for (const m of MODE_ORDER) {
    const b = document.createElement("button"); b.textContent = m[0].toUpperCase() + m.slice(1);
    if (m === course.mode) b.className = "on";
    b.onclick = async () => {
      if (m === course.mode) return;
      if (m === "auto" && !confirm("Switch " + course.name + " to auto?\\nWork will be uploaded under your name without you reading it first.")) return;
      try { await call("/api/courses/" + course.id + "/mode", { mode: m }); course.mode = m; onDone(); }
      catch (e) { alert(e.message); }
    };
    seg.appendChild(b);
  }
  const help = document.createElement("div"); help.className = "muted"; help.style.marginTop = "6px";
  help.textContent = modes[course.mode] || "";
  const wrap = document.createElement("div"); wrap.append(seg, help); return wrap;
}
"""

COURSES_BODY = """
<p class="muted" id="sem">Loading…</p>
<div id="list"></div>
<div class="err" id="err"></div>
"""

COURSES_JS = MODE_JS + """
async function load() {
  let d;
  try { d = await call("/api/courses"); } catch (e) { $("err").textContent = e.message; return; }
  $("sem").textContent = d.courses.length ? (d.semester || "This semester") + " · " + d.courses.length + " courses. Approve sends you the PDF first; auto uploads without asking; notify only sends the question."
                                          : "No courses yet: they appear after the first check of the LMS.";
  const list = $("list"); list.innerHTML = "";
  for (const c of d.courses) {
    const n = c.counts, bits = [];
    if (n.remaining) bits.push("<b>" + n.remaining + "</b> remaining");
    if (n.due_soon) bits.push("<b>" + n.due_soon + "</b> due in 48 h");
    if (n.approval) bits.push("<b>" + n.approval + "</b> waiting for you");
    if (n.needs_you) bits.push("<b>" + n.needs_you + "</b> stopped");
    if (n.papers_coming) bits.push("<b>" + n.papers_coming + "</b> lab paper" + (n.papers_coming > 1 ? "s" : "") + " coming");
    const card = document.createElement("div"); card.className = "card";
    card.innerHTML = '<a class="course" href="/courses/' + c.id + '"><h2>' + esc(c.name) + ' ›</h2></a>' +
      '<div class="muted">' + esc(c.code) + (c.lab ? " · lab" : "") + '</div>' +
      '<div class="counts">' + (bits.join(" · ") || "Nothing remaining") + '</div>';
    card.appendChild(modeControl(c, d.modes, load));
    list.appendChild(card);
  }
}
load(); setInterval(load, 60000);
"""

COURSE_BODY = """
<p><a href="/courses">← All courses</a></p>
<div class="card"><h2 id="name">Loading…</h2><div class="muted" id="sub"></div><div id="mode"></div></div>
<div class="card"><h2>Assignments</h2><div id="assignments"></div></div>
<div class="card" id="papers-card" hidden><h2>Lab papers</h2>
  <p class="muted">Tracked only: the service never solves or uploads papers.</p><div id="papers"></div></div>
<div class="err" id="err"></div>
"""

COURSE_JS = MODE_JS + """
const ID = __ID__;
function itemHtml(i) {
  const what = (i.kind === "paper" ? "Paper " : "Assignment ") + esc(i.number);
  let h = '<div class="item"><h3>' + esc(i.title) + '</h3><div class="muted">' + what + " · " + esc(i.when) + "</div>";
  h += '<span class="chip ' + i.tone + '">' + esc(i.status) + "</span>";
  if (i.marks) h += '<span class="chip">Marks ' + esc(i.marks) + "</span>";
  if (i.job) h += '<span class="chip ' + i.job.tone + '">' + esc(i.job.words) + "</span>";
  else if (i.skipped) h += '<span class="chip">Not handled: ' + esc(i.skipped.toLowerCase()) + "</span>";
  if (i.remarks) h += '<div class="muted" style="margin-top:6px">Teacher\\'s note: ' + esc(i.remarks) + "</div>";
  const btns = i.files.map((f) => '<a class="btn" href="' + f.url + '" target="_blank" rel="noopener">' + esc(f.label) + "</a>");
  if (i.job && i.job.can_approve) {
    btns.push('<button data-act="approve" data-job="' + i.job.id + '" data-title="' + esc(i.title) + '">Approve and upload</button>');
    btns.push('<button class="secondary" data-act="reject" data-job="' + i.job.id + '" data-title="' + esc(i.title) + '">Reject</button>');
  }
  if (i.job && i.job.can_redo) btns.push('<button class="secondary" data-act="redo" data-job="' + i.job.id + '" data-title="' + esc(i.title) + '">Redo with a note</button>');
  if (btns.length) h += '<div class="row small">' + btns.join("") + "</div>";
  if (i.job && i.job.history.length) h += '<details class="hist"><summary>History</summary>' +
    i.job.history.map((e) => "<div>" + esc(e.at) + ": " + esc(e.detail) + "</div>").join("") + "</details>";
  return h + "</div>";
}
async function act(btn) {
  const job = btn.dataset.job, title = btn.dataset.title, kind = btn.dataset.act;
  let body = {};
  if (kind === "approve" && !confirm("Upload the solved PDF for " + title + " to the LMS now?")) return;
  if (kind === "reject" && !confirm("Reject the solution for " + title + "? Nothing will be uploaded.")) return;
  if (kind === "redo") { const note = prompt("What should change in the redo?"); if (!note) return; body = { note }; }
  btn.disabled = true;
  try { const r = await call("/api/jobs/" + job + "/" + kind, body); alert(r.message); } catch (e) { alert(e.message); }
  load();
}
async function load() {
  let d;
  try { d = await call("/api/courses/" + ID); } catch (e) { $("err").textContent = e.message; return; }
  document.title = d.course.name;
  $("name").textContent = d.course.name;
  $("sub").textContent = d.course.code + (d.course.semester ? " · " + d.course.semester : "");
  const m = $("mode"); m.innerHTML = ""; m.appendChild(modeControl(d.course, d.modes, load));
  $("assignments").innerHTML = d.assignments.map(itemHtml).join("") || '<p class="muted">No assignments yet.</p>';
  $("papers-card").hidden = !d.course.lab;
  $("papers").innerHTML = d.papers.map(itemHtml).join("") || '<p class="muted">No lab papers yet.</p>';
  for (const b of document.querySelectorAll("button[data-act]")) b.onclick = () => act(b);
}
load(); setInterval(load, 60000);
"""
