"""The menu in a class's groups: the same screens as yours, answered only from the class's own
courses, question files and LMS. A file goes out only if it lies in the class's own folder
(`public_bot.allowed`), so nothing of yours or of another class can be sent."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ..bot import MaterialsError, Reply
from ..db.models import ClassAssignment, ClassCourse, SchoolClass, utcnow
from ..lms.bahria_pages import LMS_TZ
from ..public_bot import PAGE, PublicBot
from . import policy as policy_
from .store import class_dir

SECTIONS = {"question": "menu_questions", "lecture": "menu_lectures", "outline": "menu_outline", "plan": "menu_outline",
            "deadlines": "menu_deadlines", "leaderboard": "menu_leaderboard"}


class ClassMenu(PublicBot):
    def __init__(self, sessions: sessionmaker[Session], data_dir: Path, klass: SchoolClass, pol: policy_.Policy, *,
                 materials=None, now: Callable[[], datetime] = utcnow) -> None:
        super().__init__(sessions, class_dir(data_dir, klass.id), materials=materials, now=now)
        self.class_id, self.class_name, self.semester, self.policy = klass.id, klass.name, klass.semester, pol
        self.KEY_PREFIX = f"cls:{klass.id}:"
        self.PRIVATE_CHAT = f"class:{klass.id}"

    def _dispatch(self, s, stack, text, *, group, chat):
        option = (stack[-1].get("options") or {}).get(text)
        if option and SECTIONS.get(option.get("do")) and not self.policy.on(SECTIONS[option["do"]]):
            return [Reply("That part of the menu is switched off for this class.")], None
        return super()._dispatch(s, stack, text, group=group, chat=chat)

    # --- screens, from the class's own tables ---

    def _screen_main(self, s, screen, *, group, chat):
        p, options = self.policy, []
        if p.on("menu_questions"):
            options.append(("📝 Assignment question files", {"do": "open", "label": "Assignment question files",
                                                            "screen": {"name": "courses", "for": "question"}}))
        if p.on("menu_lectures"):
            options.append(("📖 Lectures", {"do": "open", "label": "Lectures", "screen": {"name": "courses", "for": "lecture"}}))
        if p.on("menu_deadlines"):
            options.append(("⏰ Deadlines", {"do": "deadlines", "label": "Deadlines"}))
        if p.on("menu_outline"):
            options.append(("📋 Course outline & plan", {"do": "open", "label": "Course outline & plan",
                                                        "screen": {"name": "courses", "for": "outline"}}))
        if group and p.on("menu_leaderboard"):
            options.append(("🏆 Leaderboard", {"do": "leaderboard", "label": "Leaderboard"}))
        return f"📚 *{self.class_name}*", [] if options else ["The menu has nothing switched on right now."], options

    def _courses(self, s: Session) -> list[ClassCourse]:
        return list(s.scalars(select(ClassCourse).where(ClassCourse.class_id == self.class_id,
                                                        ClassCourse.semester == self.semester)
                              .order_by(ClassCourse.name)).all())

    def _course_row(self, s: Session, course_id) -> ClassCourse:
        return self._course(s, course_id)

    def _course(self, s: Session, course_id) -> ClassCourse:
        course = s.get(ClassCourse, course_id)
        if course is None or course.class_id != self.class_id:
            raise MaterialsError("That course is not in this class")
        return course

    def _screen_courses(self, s, screen, *, group, chat):
        what = screen["for"]
        nxt = {"question": "assignments", "lecture": "lectures", "outline": "outline"}[what]
        options = [(c.name, {"do": "open", "label": c.name, "screen": {"name": nxt, "course": c.id}}) for c in self._courses(s)]
        head = {"question": "📝 *Question files*", "lecture": "📖 *Lectures*", "outline": "📋 *Outline & plan*"}[what]
        return f"{head} · pick a course", [] if options else ["No courses yet."], options

    def _screen_assignments(self, s, screen, *, group, chat):
        course = self._course(s, screen["course"])
        rows = s.scalars(select(ClassAssignment).where(ClassAssignment.course_id == course.id)
                         .order_by(ClassAssignment.deadline.desc().nulls_last(), ClassAssignment.id.desc())).all()
        options = []
        for a in rows:
            if not a.question_path or not Path(a.question_path).is_file():
                continue
            due = f" · due {a.deadline.astimezone(LMS_TZ).strftime('%d %b').lstrip('0')}" if a.deadline else ""
            options.append((f"{a.title}{due}", {"do": "question", "label": f"{course.name} · {a.title}", "assignment": a.id}))
            if len(options) == PAGE * 2:
                break
        return f"📝 *{course.name}*", [] if options else ["No question files yet."], options

    def _file(self, s: Session, option: dict) -> Reply:
        if option["do"] == "question":
            a = s.get(ClassAssignment, option["assignment"])
            if a is None or a.course.class_id != self.class_id or not a.question_path:
                return Reply("That file is not on the LMS.")
            path = Path(a.question_path)
            if not self._allowed(s, path):
                return Reply("That file can't be shared here.")
            return Reply(f"📝 {a.title}\n📘 {a.course.name}", file=path)
        return super()._file(s, option)

    def _allowed(self, s: Session, path: Path) -> bool:
        from ..public_bot import allowed

        return allowed(path, self.data_dir, self._question_paths(s))

    def _question_paths(self, s: Session) -> set[str]:
        out = set()
        for p in s.scalars(select(ClassAssignment.question_path).join(ClassCourse)
                           .where(ClassCourse.class_id == self.class_id, ClassAssignment.question_path.is_not(None))).all():
            try:
                out.add(str(Path(p).resolve()))
            except OSError:
                pass
        return out

    def _deadlines(self, s: Session) -> str:
        now = self.now()
        items = s.scalars(select(ClassAssignment).join(ClassCourse)
                          .where(ClassCourse.class_id == self.class_id, ClassCourse.semester == self.semester,
                                 ClassAssignment.deadline_passed.is_(False), ClassAssignment.deadline.is_not(None),
                                 ClassAssignment.deadline > now)
                          .options(selectinload(ClassAssignment.course)).order_by(ClassAssignment.deadline)).all()
        if not items:
            return "⏰ *Deadlines*\n\n✅ No assignment deadlines coming up."
        lines = ["⏰ *Deadlines*", ""]
        for a in items:
            left = a.deadline - now
            dot = "🔴" if left <= timedelta(hours=24) else "🟠" if left <= timedelta(days=3) else "🟢"
            hours = int(left.total_seconds() // 3600)
            when = a.deadline.astimezone(LMS_TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")
            lines += [f"{dot} *{a.title}*", f"     📘 {a.course.name}", f"     ⏰ {when} · {hours // 24} d {hours % 24} h left", ""]
        return "\n".join(lines).rstrip()
