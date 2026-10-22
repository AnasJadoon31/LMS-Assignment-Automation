"""The class bot's incoming messages: which class they belong to, and whether to answer.

In a class group: when someone tags the bot or replies to it, and the class's mode allows the
menu. In a private chat: members of an active class's groups, if that class allows the menu
in private chat; someone in two such classes is asked which one first.

Before the menu answers: blocked people are ignored; a person over their hourly request limit
gets one polite note and then silence until the hour is up; a group over its hourly reply
limit gets nothing; files over the person's hourly file limit are replaced by a note. Each
answer is logged with the option picked, never the person's own words.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from ..bot import Reply
from ..db.models import ChatSession, ClassGroup, ClassStatus, SchoolClass, utcnow
from ..groups import Group
from ..lms.bahria_pages import LMS_TZ
from ..public_bot import Directory, Incoming
from .menu import ClassMenu
from .store import Classes, find_group

log = logging.getLogger(__name__)

ALERT_AFTER_LIMITS = 3  # times a person hits a limit in a day before you are told


class ClassGroupsView:
    """The groups of active classes, for the Directory (which reads members from WhatsApp)."""

    def __init__(self, classes: Classes) -> None:
        self.classes = classes

    def get(self) -> list[Group]:
        with self.classes.sessions() as s:
            rows = s.execute(select(ClassGroup.group_jid, ClassGroup.name).join(SchoolClass)
                             .where(SchoolClass.status == ClassStatus.ACTIVE)).all()
        return [Group(jid, name or jid) for jid, name in rows]


@dataclass
class Outcome:
    reason: str
    replies: list[Reply] = field(default_factory=list)
    class_id: int | None = None


def admin_groups(admin, number: str) -> dict[str, dict]:
    """The class bot's groups where this number is a group admin: id -> {name, size}."""
    out = {}
    for g in admin.groups_with_members():
        for p in g["participants"]:
            ids = {str(p.get(k) or "").split("@")[0].split(":")[0] for k in ("id", "jid", "phoneNumber")}
            if number in ids and p.get("admin") in ("admin", "superadmin"):
                out[g["id"]] = {"name": g["name"], "size": g["size"]}
    return out


class ClassBot:
    def __init__(self, classes: Classes, directory: Directory, *, materials_for: Callable[[SchoolClass], object] | None = None,
                 alert: Callable[[str], None] | None = None, now: Callable[[], datetime] = utcnow,
                 cr_menu=None, admin_menu=None, is_admin: Callable[[list[str]], bool] | None = None) -> None:
        """`cr_menu`: crbot.CRMenu, for CRs in a private chat. `admin_menu`: adminbot.AdminMenu, for you
        (`is_admin(ids)` tells your number)."""
        self.classes, self.directory, self.materials_for = classes, directory, materials_for
        self.alert, self.now = alert or (lambda _: None), now
        self.cr_menu, self.admin_menu, self.is_admin = cr_menu, admin_menu, is_admin or (lambda ids: False)
        self.sessions = classes.sessions

    def respond(self, m: Incoming) -> Outcome:
        if not m.group and not m.unreadable:
            # you and the CRs get your controls in a private chat, even while every class is paused
            if self.admin_menu is not None and self.is_admin(m.ids):
                return Outcome("admin", self.admin_menu.handle(m.ids[0], m.text))
            found = self.cr_menu.person(m.ids) if self.cr_menu is not None else None
            if found is not None and not self.cr_menu.student_mode(found, m.text):
                return Outcome("cr", self.cr_menu.handle(found, m.text), found[0])
        if self.classes.paused_all:
            return Outcome("all classes are paused")
        if m.unreadable:
            return Outcome("unreadable")
        if m.group:
            with self.sessions() as s:
                grp = find_group(s, m.chat)
                class_id = grp.class_id if grp and grp.klass.status == ClassStatus.ACTIVE else None
            if class_id is None:
                return Outcome("not a class group")
            if not (self.directory.is_bot(m.mentions) or (m.replied_to and self.directory.is_bot([m.replied_to]))):
                return Outcome("not for the bot")
            text = m.text
        else:
            picked = self._private_class(m)
            if isinstance(picked, Outcome):
                return picked
            class_id, text = picked
        return self._answer(class_id, m, text)

    def _private_class(self, m: Incoming) -> tuple[int, str] | Outcome:
        groups = self.directory.groups_of(m.ids)
        if not groups:
            return Outcome("not in a class group")
        with self.sessions() as s:
            rows = s.scalars(select(SchoolClass).join(ClassGroup).where(
                ClassGroup.group_jid.in_(groups), SchoolClass.status == ClassStatus.ACTIVE).distinct()).all()
            options = [(k.id, k.name) for k in rows if (p := self.classes.policy(k)).menu and p.on("menu_private")]
        if not options:
            return Outcome("private chat is off for their class")
        if len(options) == 1:
            return options[0][0], m.text
        options.sort(key=lambda o: o[1].lower())
        key = "clspick:" + hashlib.sha1(m.ids[0].encode()).hexdigest()[:40]
        with self.sessions.begin() as s:
            row = s.get(ChatSession, key) or ChatSession(chat_id=key, state="{}")
            s.add(row)
            chosen = json.loads(row.state or "{}").get("class")
            ids = [o[0] for o in options]
            text = m.text.strip()
            if text.lower() in ("class", "switch"):
                chosen = None
            elif chosen not in ids and text.isdigit() and 1 <= int(text) <= len(options):
                chosen, text = ids[int(text) - 1], "menu"
            if chosen in ids:
                row.state, row.updated_at = json.dumps({"class": chosen}), self.now()
                return chosen, text
            row.state, row.updated_at = json.dumps({}), self.now()
        lines = ["You are in more than one class. Reply with the number of yours:", ""]
        lines += [f"{n}. {name}" for n, (_, name) in enumerate(options, start=1)]
        lines += ["", "(Send *switch* later to pick another class.)"]
        return Outcome("asked which class", [Reply("\n".join(lines))])

    def _limit_note(self, s, class_id: int, person: str, kind: str, limit: int, what: str) -> Reply:
        first = self.classes.first_hit(s, class_id, person, kind) or self.now()
        when = (first + timedelta(hours=1)).astimezone(LMS_TZ).strftime("%I:%M %p").lstrip("0")
        return Reply(f"⏳ You have reached the limit of {limit} {what} an hour. Try again after {when}.")

    def _answer(self, class_id: int, m: Incoming, text: str) -> Outcome:
        person = m.ids[0]
        with self.sessions.begin() as s:
            klass = s.get(SchoolClass, class_id)
            p = self.classes.policy(klass)
            if not p.menu:
                return Outcome("the menu is off for this class", class_id=class_id)
            if self.classes.blocked(s, class_id, m.ids):
                self.classes.log(s, class_id, f"student:{person}", "blocked", "Ignored: blocked", person=person)
                return Outcome("blocked", class_id=class_id)
            limit = p.value("limit_requests")
            if self.classes.hits(s, class_id, person, "request") >= limit:
                if self.classes.hits(s, class_id, person, "limited"):
                    return Outcome("over the limit", class_id=class_id)
                self.classes.hit(s, class_id, person, "limited")
                self.classes.log(s, class_id, f"student:{person}", "limit", f"Reached {limit} requests an hour", person=person)
                self._maybe_alert(s, klass, person, m.name)
                return Outcome("limited", [self._limit_note(s, class_id, person, "request", limit, "menu requests")], class_id)
            if m.group and self.classes.hits(s, class_id, m.chat, "reply") >= p.value("limit_replies"):
                if not self.classes.hits(s, class_id, m.chat, "limited"):
                    self.classes.hit(s, class_id, m.chat, "limited")
                    self.classes.log(s, class_id, "bot", "limit", f"Group reached {p.value('limit_replies')} replies an hour")
                return Outcome("group over its limit", class_id=class_id)
            self.classes.hit(s, class_id, person, "request")
            menu = ClassMenu(self.sessions, self.classes.data_dir, klass, p,
                             materials=self.materials_for(klass) if self.materials_for else None, now=self.now)
        replies = menu.handle(m.chat, person, m.name, text, group=m.group)
        out = []
        with self.sessions.begin() as s:
            files = self.classes.hits(s, class_id, person, "file")
            limit = p.value("limit_files")
            for r in replies:
                if r.file is not None:
                    if files >= limit:
                        r = self._limit_note(s, class_id, person, "file", limit, "files")
                        self.classes.log(s, class_id, f"student:{person}", "limit", f"Reached {limit} files an hour", person=person)
                    else:
                        files += 1
                        self.classes.hit(s, class_id, person, "file")
                out.append(r)
                if m.group:
                    self.classes.hit(s, class_id, m.chat, "reply")
            where = "a group" if m.group else "private chat"
            self.classes.log(s, class_id, f"student:{person}", "menu",
                             f"{m.name or person} in {where}: {menu.last_pick or 'menu'}", person=person)
        return Outcome("ok", out, class_id)

    def _maybe_alert(self, s, klass: SchoolClass, person: str, name: str) -> None:
        count = self.classes.hits(s, klass.id, person, "limited", hours=24) + 1
        if count >= ALERT_AFTER_LIMITS:
            key = f"class_limit_alert:{klass.id}:{person}:{self.now().astimezone(LMS_TZ).date().isoformat()}"
            if not self.classes.kv.get(key):
                self.classes.kv.set(key, "1")
                self.alert(f"{name or person} (+{person}) in {klass.name} hit the class bot's limits {count} times today. "
                           "You can block them on the Classes page.")
