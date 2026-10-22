"""Reads each active class's LMS, keeps its assignments, and sends its notices.

Runs in the worker, next to your own poller, one class after another so the LMS sees one
request a second at most. Each class is checked every 10 minutes by day and every 30 overnight,
with longer waits while the LMS is down. It only reads: courses, the assignment lists and the
question files. Lab papers are never read for a class.

The first check of a new class records what is already there without announcing it; from then
on a new assignment, a changed one and a moved deadline are announced, as the class's rules allow.
Reminders and the morning and evening summaries come from the database, so they go out even
while the LMS is down.
"""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .. import files
from ..db.models import ClassAssignment, ClassCourse, ClassMessage, ClassStatus, SchoolClass, utcnow
from ..groups import deadline_changed_text, new_assignment_text, question_file_text, reminder_text, summary_text
from ..lms import models as lms
from ..lms.bahria_pages import LMS_TZ
from ..lms.errors import BadCredentials, ContractBroken, LMSError, LoginError, ReloginPaused, SessionExpired
from ..notify import NotifyError
from .store import Classes, class_dir, poll_key

log = logging.getLogger(__name__)

DAY_START, DAY_END = 8, 23
DAY_MINUTES, NIGHT_MINUTES = 10, 30
DOWN_MINUTES = (5, 15, 60)
TICK_SECONDS = 15
SUMMARY_HOURS = {8: "summary_morning", 21: "summary_evening"}
SUMMARY_WINDOW = timedelta(hours=2)
REMINDERS = {24: "remind_24h", 2: "remind_2h"}
MAX_DOWNLOADS = 3
MAX_SEND_ATTEMPTS = 10
CLEANUP_KEY = "classes_cleanup"
SPIKE_KEY = "classes_send_alert"


def next_check(now: datetime, rng: random.Random) -> datetime:
    hour = now.astimezone(LMS_TZ).hour
    minutes = DAY_MINUTES if DAY_START <= hour < DAY_END else NIGHT_MINUTES
    return now + timedelta(minutes=minutes, seconds=rng.uniform(0, 60))


class ClassPoller:
    def __init__(self, classes: Classes, adapter_for: Callable[[SchoolClass], object], *, sender=None,
                 alert: Callable[[str], None] | None = None, now: Callable[[], datetime] = utcnow,
                 rng: random.Random | None = None, public_url: str | None = None) -> None:
        """`adapter_for(klass)`: the class's LMS adapter. `sender`: sending.ClassSender.
        `alert(text)`: a message to you (your own WhatsApp)."""
        self.classes, self.adapter_for, self.sender, self.alert = classes, adapter_for, sender, alert or (lambda _: None)
        self.sessions: sessionmaker[Session] = classes.sessions
        self.now, self.rng, self.public_url = now, rng or random.Random(), public_url
        self.down: dict[int, int] = {}

    # --- the loop ---

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Class poller cycle crashed")
            stop.wait(TICK_SECONDS)

    def tick(self) -> None:
        if self.classes.paused_all:
            return
        self._cleanup_daily()
        now = self.now()
        with self.sessions() as s:
            due = [k.id for k in s.scalars(select(SchoolClass).where(SchoolClass.status == ClassStatus.ACTIVE)).all()
                   if k.next_poll_at is None or k.next_poll_at <= now or self.classes.kv.get(poll_key(k.id))]
        for class_id in due:
            self.poll(class_id)
        self.remind()
        self.summaries()
        self.flush()

    def _cleanup_daily(self) -> None:
        today = self.now().astimezone(LMS_TZ).date().isoformat()
        if self.classes.kv.get(CLEANUP_KEY) != today:
            self.classes.kv.set(CLEANUP_KEY, today)
            self.classes.cleanup()

    # --- one class's LMS ---

    def poll(self, class_id: int) -> str:
        """Returns ok, signin, contract, down or error."""
        self.classes.kv.delete(poll_key(class_id))
        with self.sessions() as s:
            klass = s.get(SchoolClass, class_id)
            if klass is None or klass.status != ClassStatus.ACTIVE:
                return "skipped"
            adapter = self.adapter_for(klass)
        outcome, error = "ok", None
        try:
            adapter.ensure_session()
            courses = adapter.list_courses()
            listed = []
            for c in courses:
                try:
                    listed.append((c, adapter.list_assignments(c)))
                except (ContractBroken, SessionExpired, httpx.HTTPError):
                    raise
                except LMSError as e:
                    log.warning("Class %s: %s: %s", class_id, c.name, e)
            self._sync(class_id, courses, listed, adapter)
        except (SessionExpired, ReloginPaused, BadCredentials, LoginError) as e:
            outcome, error = "signin", str(e)
        except ContractBroken as e:
            outcome, error = "contract", str(e)
        except httpx.HTTPError as e:
            outcome, error = "down", f"{type(e).__name__}: {e}"
        except Exception as e:  # one class's bug must not stop the others
            log.exception("Class %s poll failed", class_id)
            outcome, error = "error", f"{type(e).__name__}: {e}"
        finally:
            close = getattr(adapter, "close", None)
            if close:
                close()
        self._after(class_id, outcome, error)
        return outcome

    def _after(self, class_id: int, outcome: str, error: str | None) -> None:
        now = self.now()
        with self.sessions.begin() as s:
            klass = s.get(SchoolClass, class_id)
            klass.last_poll_at = now if outcome == "ok" else klass.last_poll_at
            klass.last_error = error
            if outcome == "down":
                self.down[class_id] = self.down.get(class_id, 0) + 1
                wait = DOWN_MINUTES[min(self.down[class_id], len(DOWN_MINUTES)) - 1]
                klass.next_poll_at = now + timedelta(minutes=wait)
                return
            self.down.pop(class_id, None)
            klass.next_poll_at = next_check(now, self.rng)
            was = klass.login_state
            if outcome == "ok":
                klass.login_state, klass.login_error = "ok", None
                if was != "ok":
                    self.classes.log(s, class_id, "system", "problem", "LMS login works again")
            elif outcome == "signin":
                klass.login_state, klass.login_error = "needs_signin", error
                if was != "needs_signin":
                    self.classes.log(s, class_id, "system", "problem", f"LMS sign-in needed: {error}")
                    link = f" at {self.public_url}/cr" if self.public_url else " on your class page"
                    self.classes.tell_crs(s, klass, f"⚠️ The LMS signed {klass.name} out. Notices are on hold until you "
                                                    f"sign in again{link}.")
                    self.alert(f"Class {klass.name}: its LMS login stopped working ({error}). Its CR was asked to sign in again.")
            else:
                klass.login_state = "error" if outcome == "contract" else klass.login_state
                if was != klass.login_state or outcome == "error":
                    self.classes.log(s, class_id, "system", "problem", f"LMS check failed: {error}")
                if outcome == "contract" and was != "error":
                    self.alert(f"Class {klass.name}: the LMS page looked different than expected: {error}")

    def _sync(self, class_id: int, courses: list[lms.Course], listed: list[tuple[lms.Course, list[lms.AssignmentMeta]]],
              adapter) -> None:
        now = self.now()
        with self.sessions.begin() as s:
            klass = s.get(SchoolClass, class_id)
            first = klass.last_poll_at is None
            p = self.classes.policy(klass)
            if courses:
                klass.semester, klass.semester_name = courses[0].semester, courses[0].semester_name
            rows = {(c.semester, c.lms_ref): c for c in klass.courses}
            for c in courses:
                row = rows.get((c.semester, c.lms_ref))
                if row is None:
                    row = ClassCourse(class_id=class_id, code=c.code, name=c.name, lms_ref=c.lms_ref, semester=c.semester,
                                      semester_name=c.semester_name, last_seen_at=now)
                    klass.courses.append(row)
                    rows[(c.semester, c.lms_ref)] = row
                row.name, row.last_seen_at = c.name, now
            s.flush()
            for c, metas in listed:
                course = rows[(c.semester, c.lms_ref)]
                known = {a.lms_ref: a for a in course.assignments}
                for meta in metas:
                    if meta.kind != lms.ASSIGNMENT:
                        continue
                    self._sync_one(s, klass, p, course, known.get(meta.lms_ref), meta, adapter, quiet=first)

    def _sync_one(self, s: Session, klass: SchoolClass, p, course: ClassCourse, row: ClassAssignment | None,
                  meta: lms.AssignmentMeta, adapter, *, quiet: bool) -> None:
        now, fp = self.now(), meta.fingerprint()
        if row is None:
            row = ClassAssignment(course=course, lms_ref=meta.lms_ref, number=meta.number, title=meta.title,
                                  deadline=meta.deadline, attachment_name=meta.attachment_name, remarks=meta.remarks,
                                  fingerprint=fp, deadline_passed=meta.deadline_passed, first_seen_at=now, last_seen_at=now)
            s.add(row)
            row.reminders = ",".join(self._crossed(row))
            s.flush()
            file = self._download(row, meta, adapter)
            if quiet or meta.deadline_passed or not p.notices or not p.on("notice_new"):
                return
            send_file = file if p.on("notice_file") else None
            text = new_assignment_text(row, now, file_follows=bool(meta.attachment_name and p.on("notice_file") and not file))
            if meta.attachment_name and p.on("notice_file") and not file:
                row.reminders = ",".join(filter(None, [row.reminders, "file-follows"]))
            self.classes.queue(s, klass, "new", text, file=send_file)
            return
        row.last_seen_at = now
        row.deadline_passed = meta.deadline_passed
        if fp == row.fingerprint:
            return
        old_deadline, old_attachment, old_title = row.deadline, row.attachment_name, row.title
        row.number, row.title, row.deadline = meta.number, meta.title, meta.deadline
        row.attachment_name, row.remarks, row.fingerprint = meta.attachment_name, meta.remarks, fp
        file = None
        if meta.attachment_name != old_attachment:
            row.question_path, row.download_attempts = None, 0
            file = self._download(row, meta, adapter)
        if meta.deadline != old_deadline:
            row.reminders = ",".join(self._crossed(row))
        if not p.notices or not p.on("notice_change") or meta.deadline_passed:
            return
        if meta.attachment_name != old_attachment or meta.title != old_title:
            text = new_assignment_text(row, now, revision=2)
            self.classes.queue(s, klass, "changed", text, file=file if p.on("notice_file") else None)
        elif meta.deadline != old_deadline and meta.deadline is not None:
            self.classes.queue(s, klass, "deadline", deadline_changed_text(row, old_deadline, now))

    def _download(self, row: ClassAssignment, meta: lms.AssignmentMeta, adapter) -> Path | None:
        if not meta.attachment_name or row.download_attempts >= MAX_DOWNLOADS:
            return None
        row.download_attempts += 1
        dest = class_dir(self.classes.data_dir, row.course.class_id) / files._safe(row.course.code) / "questions" / str(row.id)
        try:
            paths = adapter.download(meta, dest)
        except (LMSError, httpx.HTTPError, OSError) as e:
            log.warning("Class question file %s: %s", meta.title, e)
            return None
        if not paths:
            return None
        row.question_path = str(paths[0])
        return paths[0]

    # --- from the database: reminders and summaries ---

    def _crossed(self, row: ClassAssignment) -> list[str]:
        if row.deadline is None or row.deadline_passed:
            return []
        left = (row.deadline - self.now()).total_seconds() / 3600
        return [f"{h}h" for h in REMINDERS if left <= h]

    def remind(self) -> None:
        now = self.now()
        with self.sessions.begin() as s:
            for klass in s.scalars(select(SchoolClass).where(SchoolClass.status == ClassStatus.ACTIVE)).all():
                p = self.classes.policy(klass)
                for a in self._open(s, klass, now, within=timedelta(hours=max(REMINDERS))):
                    sent = set(filter(None, a.reminders.split(",")))
                    due = [k for k in self._crossed(a) if k not in sent]
                    if not due:
                        continue
                    a.reminders = ",".join(sorted(sent | set(due)))
                    # the nearest one only: an assignment found 1 hour before gets the 2-hour reminder, once
                    key = REMINDERS[min(int(k[:-1]) for k in due)]
                    if p.notices and p.on(key):
                        self.classes.queue(s, klass, "reminder", reminder_text(a, now))
                # files that could not be downloaded at first: try again, and send them if promised
                for a in self._open(s, klass, now):
                    if "file-follows" in a.reminders and a.question_path:
                        a.reminders = ",".join(k for k in a.reminders.split(",") if k != "file-follows")
                        if p.notices and p.on("notice_file"):
                            self.classes.queue(s, klass, "file", question_file_text(a), file=Path(a.question_path))

    def summaries(self) -> None:
        now = self.now()
        local = now.astimezone(LMS_TZ)
        for hour, key in SUMMARY_HOURS.items():
            slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
            if not slot <= local < slot + SUMMARY_WINDOW:
                continue
            stamp = f"{slot.date().isoformat()}@{hour}"
            with self.sessions.begin() as s:
                for klass in s.scalars(select(SchoolClass).where(SchoolClass.status == ClassStatus.ACTIVE)).all():
                    if klass.summary_sent == stamp:
                        continue
                    klass.summary_sent = stamp
                    p = self.classes.policy(klass)
                    if p.notices and p.on(key) and klass.groups:
                        self.classes.queue(s, klass, "summary", summary_text(self._open(s, klass, now), now))

    @staticmethod
    def _open(s: Session, klass: SchoolClass, now: datetime, *, within: timedelta | None = None) -> list[ClassAssignment]:
        q = (select(ClassAssignment).join(ClassCourse)
             .where(ClassCourse.class_id == klass.id, ClassCourse.semester == klass.semester,
                    ClassAssignment.deadline_passed.is_(False), ClassAssignment.deadline.is_not(None),
                    ClassAssignment.deadline > now)
             .options(selectinload(ClassAssignment.course)).order_by(ClassAssignment.deadline))
        if within is not None:
            q = q.where(ClassAssignment.deadline <= now + within)
        return list(s.scalars(q).all())

    # --- the class outbox ---

    def flush(self) -> int:
        """Send waiting class messages, oldest first, spaced out. A target that fails waits
        with its later messages; others go on."""
        if self.sender is None or self.classes.paused_all:
            return 0
        sent = failures = 0
        stuck: set[str] = set()
        with self.sessions() as s:
            names = {}
            pending = s.scalars(select(ClassMessage).where(ClassMessage.sent_at.is_(None),
                                                           ClassMessage.attempts < MAX_SEND_ATTEMPTS)
                                .order_by(ClassMessage.id).limit(200)).all()
            for m in pending:
                if m.to in stuck:
                    continue
                klass = s.get(SchoolClass, m.class_id) if m.class_id else None
                if klass is not None and m.kind != "cr" and (klass.status != ClassStatus.ACTIVE or
                                                              (klass.muted_until and klass.muted_until > self.now())):
                    continue  # held while the class is paused or muted
                m.attempts += 1
                try:
                    if m.file_path and Path(m.file_path).is_file():
                        self.sender.document(m.to, Path(m.file_path), m.text)
                    else:
                        self.sender.text(m.to, m.text)
                except (NotifyError, OSError) as e:
                    m.last_error = str(e)
                    stuck.add(m.to)
                    failures += 1
                    s.commit()
                    continue
                m.sent_at, m.last_error = self.now(), None
                if klass is not None:
                    if m.to not in names:
                        names[m.to] = next((g.name for g in klass.groups if g.group_jid == m.to), "the CR")
                    self.classes.log(s, klass.id, "bot", "sent", f"{m.kind} → {names[m.to]}: {m.text.splitlines()[0][:120]}")
                s.commit()
                sent += 1
        if failures >= 5:
            today = self.now().astimezone(LMS_TZ).date().isoformat()
            if self.classes.kv.get(SPIKE_KEY) != today:
                self.classes.kv.set(SPIKE_KEY, today)
                self.alert(f"The class bot could not send {failures} messages just now. WhatsApp may be limiting the number; "
                           "check it on the Classes page.")
        return sent
