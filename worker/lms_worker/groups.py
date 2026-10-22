"""Assignment notices for WhatsApp groups: add the bot's number to a group, tick the group on
the setup page, and the group gets:

- every new or changed assignment, with its question file;
- a reminder 24 hours and 2 hours before each deadline, submitted or not;
- a summary at 8:00 AM and 9:00 PM (Pakistan time) of every assignment whose deadline has
  not passed, nearest first.

Groups only ever get these notices: your own alerts, solved reports and the menu stay in
your chat. Lab papers and anything about solving never go to a group.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .config import EvolutionConfig
from .db.models import Assignment, Course, utcnow
from .kv import KeyValue
from .lms.bahria_pages import LMS_TZ
from .lms.models import PAPER
from .notify import EvolutionNotifier, NotifyError, Outbox

log = logging.getLogger(__name__)

GROUPS_KEY = "whatsapp_groups"
CHANNEL = "group"
SUMMARY_HOURS = (8, 21)  # 8:00 AM and 9:00 PM, Pakistan time
SUMMARY_WINDOW = timedelta(hours=2)  # a summary missed by more than this (server down) is skipped
REMINDER_HOURS = (24, 2)
SENT_KEY = "group_summary_sent"


@dataclass(frozen=True)
class Group:
    id: str  # the group's JID, e.g. 120363012345678901@g.us
    name: str


class WhatsAppGroups:
    """The groups that get notices, chosen on the setup page."""

    def __init__(self, kv: KeyValue | None) -> None:
        self.kv = kv

    def get(self) -> list[Group]:
        raw = self.kv.get(GROUPS_KEY) if self.kv else None
        return [Group(g["id"], g.get("name") or g["id"]) for g in json.loads(raw)] if raw else []

    def save(self, groups: list[Group]) -> None:
        if self.kv is None:
            raise ValueError("Saving groups needs DATABASE_URL")
        for g in groups:
            if not g.id.endswith("@g.us"):
                raise ValueError(f"{g.id} is not a WhatsApp group")
        if groups:
            self.kv.set(GROUPS_KEY, json.dumps([{"id": g.id, "name": g.name} for g in groups]))
        else:
            self.kv.delete(GROUPS_KEY)


class GroupNotifier:
    """Sends each notice to every chosen group. A group that fails is logged and skipped; the
    notice is retried only when every group failed."""

    def __init__(self, cfg: EvolutionConfig, groups: WhatsAppGroups, *, transport: httpx.BaseTransport | None = None):
        self.cfg, self.groups, self.transport = cfg, groups, transport

    def active(self) -> bool:
        return bool(self.groups.get())

    def text(self, message: str) -> None:
        self._each(lambda n: n.text(message))

    def document(self, path: Path, caption: str) -> None:
        self._each(lambda n: n.document(path, caption))

    def _each(self, send: Callable[[EvolutionNotifier], None]) -> None:
        errors = []
        groups = self.groups.get()
        for g in groups:
            try:
                send(EvolutionNotifier(self.cfg, number=lambda jid=g.id: jid, transport=self.transport))
            except NotifyError as e:
                log.warning("WhatsApp group %s: %s", g.name, e)
                errors.append(f"{g.name}: {e}")
        if groups and len(errors) == len(groups):
            raise NotifyError("; ".join(errors))


# --- the messages ---


def _when(dt: datetime) -> str:
    return dt.astimezone(LMS_TZ).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _left(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def _dot(deadline: datetime, now: datetime) -> str:
    left = deadline - now
    return "🔴" if left <= timedelta(hours=24) else "🟠" if left <= timedelta(days=3) else "🟢"


def new_assignment_text(a: Assignment, now: datetime, *, revision: int = 1, file_follows: bool = False) -> str:
    head = "📢 *New assignment*" if revision == 1 else "✏️ *Assignment updated*"
    lines = [head, "", f"📘 {a.course.name}", f"📝 *{a.title}*"]
    if a.deadline:
        lines.append(f"⏰ Due {_when(a.deadline)} ({_left(a.deadline - now)} left)")
    else:
        lines.append("⏰ No deadline shown")
    if a.remarks:
        lines.append(f"🗒️ Teacher's note: {a.remarks}")
    if file_follows:
        lines.append("📎 The question file will follow.")
    return "\n".join(lines)


def deadline_changed_text(a: Assignment, old: datetime | None, now: datetime) -> str:
    lines = ["🗓️ *Deadline changed*", "", f"📘 {a.course.name}", f"📝 *{a.title}*"]
    if old:
        lines.append(f"❌ Was {_when(old)}")
    lines.append(f"✅ Now {_when(a.deadline)} ({_left(a.deadline - now)} left)")
    return "\n".join(lines)


def question_file_text(a: Assignment) -> str:
    return f"📎 Question file: *{a.title}*\n📘 {a.course.name}"


def reminder_text(a: Assignment, now: datetime) -> str:
    left = a.deadline - now
    head = "🚨 *Due in 2 hours*" if left <= timedelta(hours=2) else "⏳ *Due in 24 hours*"
    return "\n".join([head, "", f"📘 {a.course.name}", f"📝 *{a.title}*",
                      f"⏰ {_when(a.deadline)} ({_left(left)} left)"])


def summary_text(items: list[Assignment], now: datetime) -> str:
    local = now.astimezone(LMS_TZ)
    greeting = "☀️ *Good morning!*" if local.hour < 12 else "🌙 *Good evening!*"
    head = f"{greeting} Assignments due · {local.strftime('%a %d %b').replace(' 0', ' ')}"
    if not items:
        return f"{head}\n\n✅ No assignment deadlines coming up."
    lines = [head, ""]
    for a in items:
        lines += [f"{_dot(a.deadline, now)} *{a.title}*", f"     📘 {a.course.name}",
                  f"     ⏰ {_when(a.deadline)} · {_left(a.deadline - now)} left", ""]
    lines.append(f"🔴 under 24 h · 🟠 under 3 days · 🟢 later  ({len(items)} due)")
    return "\n".join(lines)


def open_assignments(s: Session, now: datetime) -> list[Assignment]:
    """This semester's assignments whose deadline has not passed, submitted or not, nearest first."""
    semester = s.scalar(select(Course.semester).order_by(Course.last_seen_at.desc()).limit(1))
    if semester is None:
        return []
    rows = s.scalars(
        select(Assignment).join(Course)
        .where(Course.semester == semester, Assignment.kind != PAPER, Assignment.deadline_passed.is_(False),
               Assignment.deadline.is_not(None), Assignment.deadline > now)
        .options(selectinload(Assignment.course))
        .order_by(Assignment.deadline)
    ).all()
    return list(rows)


# --- when they go out ---


class GroupNotices:
    """Called by the poller; queues group-only rows in the outbox, which delivers them."""

    def __init__(self, sessions: sessionmaker[Session], kv: KeyValue, outbox: Outbox, groups: WhatsAppGroups,
                 *, now: Callable[[], datetime] = utcnow) -> None:
        self.sessions, self.kv, self.outbox, self.groups, self.now = sessions, kv, outbox, groups, now

    @property
    def on(self) -> bool:
        return CHANNEL in self.outbox.channels and bool(self.groups.get())

    def announce(self, s: Session, a: Assignment, *, revision: int, file: Path | None, job_id: int | None,
                 early: bool = False) -> None:
        """A new or changed assignment. `early`: the file did not download yet; it follows."""
        if not self.on or a.kind == PAPER:
            return
        key = f"group:announced:{a.id}:{revision}"
        if self.kv.get(key):  # announced early; now only the file follows
            if file is not None:
                self.outbox.queue(s, "group-file", question_file_text(a), file=file, job_id=job_id, to=(CHANNEL,))
            return
        self.kv.set(key, "1")
        text = new_assignment_text(a, self.now(), revision=revision, file_follows=early)
        self.outbox.queue(s, "group-assignment", text, file=file, job_id=job_id, to=(CHANNEL,))
        # the announcement already says how long is left: no reminder right after it
        sent = set(filter(None, a.reminders.split(",")))
        a.reminders = ",".join(sorted(sent | set(self._crossed(a))))

    def deadline_changed(self, s: Session, a: Assignment, old: datetime | None) -> None:
        """A new date on an assignment that gets no new revision (e.g. already submitted)."""
        if not self.on or a.kind == PAPER or a.deadline is None:
            return
        self.outbox.queue(s, "group-deadline", deadline_changed_text(a, old, self.now()), to=(CHANNEL,))
        sent = set(filter(None, a.reminders.split(",")))
        a.reminders = ",".join(sorted(sent | set(self._crossed(a))))  # the notice says how long is left

    def remind(self) -> int:
        if not self.on:
            return 0
        now, count = self.now(), 0
        with self.sessions() as s:
            for a in s.scalars(
                select(Assignment).where(Assignment.kind != PAPER, Assignment.deadline_passed.is_(False),
                                         Assignment.deadline.is_not(None), Assignment.deadline > now,
                                         Assignment.deadline <= now + timedelta(hours=max(REMINDER_HOURS)))
            ).all():
                sent = set(filter(None, a.reminders.split(",")))
                due = [k for k in self._crossed(a) if k not in sent]
                if not due:
                    continue
                a.reminders = ",".join(sorted(sent | set(due)))
                self.outbox.queue(s, "group-reminder", reminder_text(a, now), to=(CHANNEL,))
                count += 1
            s.commit()
        return count

    def _crossed(self, a: Assignment) -> list[str]:
        if a.deadline is None:
            return []
        left = (a.deadline - self.now()).total_seconds() / 3600
        return [f"g{h}h" for h in REMINDER_HOURS if left <= h]

    def summary_if_due(self) -> bool:
        """At 8:00 AM and 9:00 PM: one summary per slot, sent within two hours of it."""
        if not self.on:
            return False
        local = self.now().astimezone(LMS_TZ)
        for hour in SUMMARY_HOURS:
            slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot <= local < slot + SUMMARY_WINDOW:
                stamp = f"{slot.date().isoformat()}@{hour}"
                if self.kv.get(SENT_KEY) == stamp:
                    return False
                self.kv.set(SENT_KEY, stamp)
                self.queue_summary()
                return True
        return False

    def queue_summary(self) -> str:
        with self.sessions.begin() as s:
            text = summary_text(open_assignments(s, self.now()), self.now())
            self.outbox.queue(s, "group-summary", text, to=(CHANNEL,))
        return text

    def next_summary(self) -> datetime:
        """When the next summary is due, so the poller can wake for it."""
        local = self.now().astimezone(LMS_TZ)
        for days in (0, 1):
            for hour in SUMMARY_HOURS:
                slot = (local + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
                if slot > local:
                    return slot
        return local + timedelta(days=1)
