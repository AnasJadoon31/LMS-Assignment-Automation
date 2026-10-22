"""Class rows and what happens to them: the log, blocks, usage limits, the class outbox, and
the class's life from sign-up to removal."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, object_session, sessionmaker

from ..db.models import (BotUser, ChatSession, ClassAssignment, ClassBlock, ClassCourse, ClassGroup, ClassHit, ClassInvite,
                         ClassLog, ClassMessage, ClassPerson, ClassStatus, Course, SchoolClass, utcnow)
from ..kv import KeyValue, SecretBox
from ..lms import models as lms
from . import policy

RULES_KEY = "class_rules"  # your rules for every class
PAUSE_KEY = "classes_paused"  # "pause everything"
LOG_DAYS = 60


def class_dir(data_dir: Path, class_id: int) -> Path:
    """Everything a class's bot fetched from its LMS: <data>/classes/<id>/<course>/..."""
    return data_dir / "classes" / str(int(class_id))


def session_key(class_id: int) -> str:
    return f"class:{int(class_id)}:lms_session"


def guard_key(class_id: int) -> str:
    return f"class:{int(class_id)}:login_guard"


def poll_key(class_id: int) -> str:
    return f"class:{int(class_id)}:poll_requested"


class SignupRefused(ValueError):
    """A sign-up that cannot make a class, in words for the person signing up."""


class Classes:
    def __init__(self, sessions: sessionmaker[Session], kv: KeyValue, data_dir: Path, *,
                 now: Callable[[], datetime] = utcnow, box: SecretBox | None = None) -> None:
        self.sessions, self.kv, self.data_dir, self.now, self.box = sessions, kv, data_dir, now, box

    # --- your rules ---

    def defaults(self) -> dict:
        return {**policy.DEFAULT_RULES, **policy.load(self.kv.get(RULES_KEY))}

    def save_defaults(self, rules: dict) -> dict:
        clean = policy.clean_rules(rules)
        self.kv.set(RULES_KEY, json.dumps(clean))
        return clean

    def policy(self, klass: SchoolClass) -> policy.Policy:
        return policy.Policy(self.defaults(), policy.load(klass.rules), policy.load(klass.choices))

    @property
    def paused_all(self) -> bool:
        return bool(self.kv.get(PAUSE_KEY))

    def pause_all(self, on: bool) -> None:
        if on:
            self.kv.set(PAUSE_KEY, self.now().isoformat())
        else:
            self.kv.delete(PAUSE_KEY)

    # --- the log ---

    def log(self, s: Session, class_id: int | None, actor: str, kind: str, detail: str, *, person: str | None = None) -> None:
        s.add(ClassLog(at=self.now(), class_id=class_id, actor=actor[:64], kind=kind[:32], detail=detail, person=person))

    def logs(self, *, class_id: int | None = None, person: str | None = None, kind: str | None = None,
             since: datetime | None = None, until: datetime | None = None, limit: int = 300) -> list[dict]:
        q = select(ClassLog).order_by(ClassLog.at.desc(), ClassLog.id.desc()).limit(limit)
        if class_id is not None:
            q = q.where(ClassLog.class_id == class_id)
        if person:
            q = q.where(ClassLog.person == person)
        if kind:
            q = q.where(ClassLog.kind == kind)
        if since:
            q = q.where(ClassLog.at >= since)
        if until:
            q = q.where(ClassLog.at < until)
        with self.sessions() as s:
            names = dict(s.execute(select(SchoolClass.id, SchoolClass.name)).all())
            return [{"at": r.at.isoformat(), "class_id": r.class_id, "class": names.get(r.class_id, "—" if r.class_id is None else "?"),
                     "actor": r.actor, "kind": r.kind, "detail": r.detail, "person": r.person or ""}
                    for r in s.scalars(q).all()]

    # --- blocks and limits ---

    @staticmethod
    def blocked(s: Session, class_id: int, ids: Iterable[str]) -> bool:
        ids = [i for i in ids if i]
        if not ids:
            return False
        return s.scalar(select(func.count()).select_from(ClassBlock).where(
            ClassBlock.person.in_(ids), (ClassBlock.class_id == class_id) | ClassBlock.class_id.is_(None))) > 0

    def hits(self, s: Session, class_id: int, who: str, kind: str, *, hours: float = 1) -> int:
        return s.scalar(select(func.count()).select_from(ClassHit).where(
            ClassHit.class_id == class_id, ClassHit.who == who, ClassHit.kind == kind,
            ClassHit.at > self.now() - timedelta(hours=hours))) or 0

    def first_hit(self, s: Session, class_id: int, who: str, kind: str, *, hours: float = 1) -> datetime | None:
        return s.scalar(select(func.min(ClassHit.at)).where(
            ClassHit.class_id == class_id, ClassHit.who == who, ClassHit.kind == kind,
            ClassHit.at > self.now() - timedelta(hours=hours)))

    def hit(self, s: Session, class_id: int, who: str, kind: str) -> None:
        s.add(ClassHit(class_id=class_id, who=who[:64], kind=kind, at=self.now()))

    # --- the class outbox ---

    def queue(self, s: Session, klass: SchoolClass, kind: str, text: str, *, file: Path | None = None,
              to: Iterable[str] | None = None, force: bool = False) -> int:
        """One row per ticked group (or per `to`). Nothing while the class is muted, paused or
        not active, unless `force` (messages to the CR about their own class)."""
        if not force:
            if klass.status != ClassStatus.ACTIVE or self.paused_all:
                return 0
            if klass.muted_until and klass.muted_until > self.now():
                return 0
        targets = list(to) if to is not None else [g.group_jid for g in klass.groups]
        for target in targets:
            s.add(ClassMessage(class_id=klass.id, to=target, kind=kind, text=text, file_path=str(file) if file else None,
                               created_at=self.now()))
        return len(targets)

    def tell_crs(self, s: Session, klass: SchoolClass, text: str) -> None:
        """A message from the class bot to the class's CR and deputies, in private."""
        self.queue(s, klass, "cr", text, to=[p.phone for p in klass.people], force=True)

    # --- a class's life ---

    def get(self, s: Session, class_id: int) -> SchoolClass:
        klass = s.get(SchoolClass, class_id)
        if klass is None or klass.status == ClassStatus.REMOVED:
            raise LookupError("No such class")
        return klass

    def set_status(self, class_id: int, status: str, actor: str) -> SchoolClass:
        by = "cr" if actor.startswith("cr:") else "admin"
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            if klass.status in (ClassStatus.BLOCKED,) and by == "cr":
                raise PermissionError("The admin blocked this class")
            if klass.status == ClassStatus.PAUSED and klass.paused_by == "admin" and by == "cr" and status == ClassStatus.ACTIVE:
                raise PermissionError("The admin paused this class; only the admin can resume it")
            if status == ClassStatus.ACTIVE and klass.status == ClassStatus.PENDING and by != "admin":
                raise PermissionError("Only the admin approves a class")
            before = klass.status
            klass.status = status
            klass.paused_by = by if status == ClassStatus.PAUSED else None
            if status == ClassStatus.ACTIVE and klass.approved_at is None:
                klass.approved_at = self.now()
                klass.next_poll_at = self.now()
            self.log(s, klass.id, actor, "admin" if by == "admin" else "cr", f"Status {before} → {status}")
            if by == "admin" and before != status:
                words = {ClassStatus.ACTIVE: "approved and on" if before == ClassStatus.PENDING else "back on",
                         ClassStatus.PAUSED: "paused by the admin", ClassStatus.BLOCKED: "blocked by the admin"}
                if status in words:
                    self.tell_crs(s, klass, f"Your class {klass.name} is {words[status]}.")
            return klass

    def remove(self, class_id: int, actor: str) -> None:
        """Delete the class's LMS login and everything fetched for it. The log stays."""
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            klass.status = ClassStatus.REMOVED
            klass.password_enc = None
            klass.paused_by = None
            for g in list(klass.groups):
                s.delete(g)
            for c in list(klass.courses):
                s.delete(c)  # and their assignments
            s.execute(delete(ClassMessage).where(ClassMessage.class_id == class_id, ClassMessage.sent_at.is_(None)))
            s.execute(delete(ClassPerson).where(ClassPerson.class_id == class_id))
            s.execute(delete(ClassHit).where(ClassHit.class_id == class_id))
            s.execute(delete(BotUser).where(BotUser.chat_id == f"class:{class_id}"))
            s.execute(delete(ChatSession).where(ChatSession.chat_id.like(f"cls:{class_id}:%")))
            self.log(s, class_id, actor, "cr" if actor.startswith("cr:") else "admin", "Class removed: LMS login and files deleted")
        for key in (session_key(class_id), guard_key(class_id), poll_key(class_id)):
            self.kv.delete(key)
        shutil.rmtree(class_dir(self.data_dir, class_id), ignore_errors=True)

    def cleanup(self) -> None:
        """Old log rows (60 days), usage counts (2 days) and sent messages (30 days)."""
        with self.sessions.begin() as s:
            s.execute(delete(ClassLog).where(ClassLog.at < self.now() - timedelta(days=LOG_DAYS)))
            s.execute(delete(ClassHit).where(ClassHit.at < self.now() - timedelta(days=2)))
            s.execute(delete(ClassMessage).where(ClassMessage.sent_at.is_not(None),
                                                 ClassMessage.sent_at < self.now() - timedelta(days=30)))

    # --- invites and sign-up ---

    def create_invite(self, name: str, *, role: str = "cr", class_id: int | None = None, hours: int = 48) -> str:
        from .access import new_token, token_hash

        name = " ".join(name.split())[:128]
        if role not in ("cr", "deputy"):
            raise ValueError("An invite is for a CR or a deputy")
        token = new_token()
        with self.sessions.begin() as s:
            if role == "deputy":
                klass = self.get(s, class_id)
                name = klass.name
            elif not name:
                raise ValueError("Name the class, e.g. BSCS 5-B")
            s.add(ClassInvite(token_hash=token_hash(token), name=name, role=role, class_id=class_id if role == "deputy" else None,
                              created_at=self.now(), expires_at=self.now() + timedelta(hours=hours)))
            self.log(s, class_id, "admin", "admin", f"Invite made for {'a deputy of ' if role == 'deputy' else 'the CR of '}{name}")
        return token

    def invites(self) -> list[dict]:
        with self.sessions() as s:
            rows = s.scalars(select(ClassInvite).where(ClassInvite.created_at > self.now() - timedelta(days=14))
                             .order_by(ClassInvite.id.desc())).all()
            out = []
            for i in rows:
                state = ("revoked" if i.revoked else "used" if i.used_at else "expired" if i.expires_at <= self.now() else "open")
                out.append({"id": i.id, "name": i.name, "role": i.role, "state": state, "used_by": i.used_by,
                            "expires_at": i.expires_at.isoformat(), "created_at": i.created_at.isoformat()})
            return out

    def revoke_invite(self, invite_id: int) -> None:
        with self.sessions.begin() as s:
            i = s.get(ClassInvite, invite_id)
            if i is None:
                raise LookupError("No such invite")
            i.revoked = True
            self.log(s, i.class_id, "admin", "admin", f"Invite for {i.name} revoked")

    def invite(self, s: Session, token: str) -> ClassInvite | None:
        """The invite this token opens, if it is still open."""
        from .access import token_hash

        i = s.scalar(select(ClassInvite).where(ClassInvite.token_hash == token_hash(token or "")))
        if i is None or i.revoked or i.used_at is not None or i.expires_at <= self.now():
            return None
        if i.class_id is not None:
            k = s.get(SchoolClass, i.class_id)
            if k is None or k.status in (ClassStatus.REMOVED, ClassStatus.BLOCKED):
                return None
        return i

    def taken(self, s: Session, courses: list[lms.Course], enrollment: str) -> str | None:
        """Why this sign-up cannot make a class at all: no courses, or this LMS account already runs
        one. Sections of the same program and semester are separate classes, so shared courses never
        refuse a sign-up; `overlaps` shows them to you for the approval instead."""
        if not courses:
            return "The LMS lists no courses for this account"
        if s.scalar(select(SchoolClass).where(SchoolClass.enrollment == enrollment,
                                             SchoolClass.status.not_in([ClassStatus.REMOVED]))):
            return "This LMS account already runs a class"
        return None

    @staticmethod
    def overlaps(s: Session, klass: SchoolClass) -> list[dict]:
        """Other classes (and your own) that list the same LMS courses this semester: a hint that it
        may be the same section signed up twice. [{name, shared, total}], most shared first."""
        mine = {c.lms_ref for c in klass.courses if c.semester == klass.semester}
        if not mine:
            return []
        out = []
        own = set(s.scalars(select(Course.lms_ref).where(Course.semester == klass.semester, Course.lms_ref.in_(mine))).all())
        if own:
            out.append({"name": "your own class", "shared": len(own), "total": len(mine)})
        rows = s.execute(select(ClassCourse.class_id, ClassCourse.lms_ref).join(SchoolClass).where(
            ClassCourse.semester == klass.semester, ClassCourse.lms_ref.in_(mine), ClassCourse.class_id != klass.id,
            SchoolClass.status.not_in([ClassStatus.REMOVED]))).all()
        by_class: dict[int, set] = {}
        for cid, ref in rows:
            by_class.setdefault(cid, set()).add(ref)
        for cid, refs in by_class.items():
            out.append({"name": s.get(SchoolClass, cid).name, "shared": len(refs), "total": len(mine)})
        return sorted(out, key=lambda o: -o["shared"])

    @staticmethod
    def overlap_note(found: list[dict]) -> str:
        """'⚠️ Shares 5 of 6 courses with BSCS 5-A: maybe the same section' for the biggest overlap."""
        if not found:
            return ""
        top = found[0]
        same = top["shared"] * 4 >= top["total"] * 3  # three quarters or more: likely one section twice
        return (f"{'⚠️ ' if same else ''}Shares {top['shared']} of {top['total']} courses with {top['name']}"
                + (": maybe the same section signed up twice" if same else ""))

    def signup(self, token: str, number: str, cr_name: str, enrollment: str, institute: str, password: str | None,
               state: dict, courses: list[lms.Course]) -> tuple[int, int]:
        """A CR's invite, number and LMS sign-in become a class waiting for your approval.
        Returns (class id, person id)."""
        from ..lms.session import SessionStore

        with self.sessions.begin() as s:
            inv = self.invite(s, token)
            if inv is None or inv.role != "cr":
                raise SignupRefused("This invite link no longer works. Ask the admin for a new one.")
            why = self.taken(s, courses, enrollment)
            if why:
                self.log(s, None, f"cr:{number}", "problem", f"Sign-up for {inv.name} refused: {why}", person=number)
                raise SignupRefused(why)
            klass = SchoolClass(name=inv.name, status=ClassStatus.PENDING, enrollment=enrollment, institute=institute,
                                semester=courses[0].semester, semester_name=courses[0].semester_name,
                                password_enc=self.box.encrypt(password) if password and self.box else None,
                                created_at=self.now())
            person = ClassPerson(phone=number, name=cr_name[:128], role="cr", created_at=self.now())
            klass.people.append(person)
            for c in courses:
                klass.courses.append(ClassCourse(code=c.code, name=c.name, lms_ref=c.lms_ref, semester=c.semester,
                                                 semester_name=c.semester_name, last_seen_at=self.now()))
            s.add(klass)
            s.flush()
            inv.used_at, inv.used_by, inv.class_id = self.now(), number, klass.id
            self.log(s, klass.id, f"cr:{number}", "cr", f"{cr_name} signed up as CR of {klass.name} "
                     f"({len(courses)} courses, password {'kept' if klass.password_enc else 'not kept'})", person=number)
            ids = (klass.id, person.id)
        store = SessionStore(self.kv, self.box)
        store.KEY = session_key(ids[0])
        store.save(state)
        return ids

    def join_as_deputy(self, token: str, number: str, name: str) -> tuple[int, int]:
        with self.sessions.begin() as s:
            inv = self.invite(s, token)
            if inv is None or inv.role != "deputy":
                raise SignupRefused("This invite link no longer works. Ask the admin for a new one.")
            klass = self.get(s, inv.class_id)
            if any(p.phone == number for p in klass.people):
                raise SignupRefused("This number already runs this class")
            person = ClassPerson(phone=number, name=name[:128], role="deputy", created_at=self.now())
            klass.people.append(person)
            s.flush()
            inv.used_at, inv.used_by = self.now(), number
            self.log(s, klass.id, f"cr:{number}", "cr", f"{name} joined {klass.name} as deputy", person=number)
            return klass.id, person.id

    def signed_in_again(self, class_id: int, enrollment: str, password: str | None, state: dict, actor: str) -> None:
        """The CR signed in to the LMS again: keep the new session (and the password, if asked)."""
        from ..lms.session import SessionStore

        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            if enrollment.strip() != klass.enrollment:
                raise SignupRefused(f"Sign in with the class's LMS account ({klass.enrollment})")
            klass.password_enc = self.box.encrypt(password) if password and self.box else None
            klass.login_state, klass.login_error, klass.next_poll_at = "ok", None, self.now()
            self.log(s, class_id, actor, "cr", f"Signed in to the LMS again (password {'kept' if password else 'not kept'})")
        store = SessionStore(self.kv, self.box)
        store.KEY = session_key(class_id)
        store.save(state)
        self.kv.delete(guard_key(class_id))

    def forget_password(self, class_id: int, actor: str) -> None:
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            klass.password_enc = None
            self.log(s, class_id, actor, "cr", "Kept password deleted; the LMS session alone is kept")

    # --- what the CR changes ---

    def set_choice(self, class_id: int, key: str, value, actor: str) -> None:
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            choices = self.policy(klass).choose(key, value)
            klass.choices = json.dumps(choices)
            self.log(s, class_id, actor, "cr", f"{policy.BY_KEY[key].label}: {value}")

    def save_groups(self, class_id: int, picked: list[dict], allowed: dict[str, str], actor: str) -> None:
        """The CR's ticks. `allowed`: group id -> name, the groups where they are an admin."""
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            want = {g["id"] for g in picked}
            for gid in want:
                if gid not in allowed:
                    raise PermissionError("You can only pick groups where you are an admin and the bot is a member")
                other = find_group(s, gid)
                if other is not None and other.class_id != class_id:
                    raise PermissionError(f"{allowed[gid]} already belongs to another class")
            for g in list(klass.groups):
                if g.group_jid not in want:
                    klass.groups.remove(g)
                    self.log(s, class_id, actor, "cr", f"Group unticked: {g.name}")
            have = {g.group_jid for g in klass.groups}
            for gid in want - have:
                klass.groups.append(ClassGroup(group_jid=gid, name=allowed[gid][:255], added_by=actor[:32], added_at=self.now()))
                self.log(s, class_id, actor, "cr", f"Group ticked: {allowed[gid]}")

    def mute(self, class_id: int, days: int, actor: str) -> None:
        if not 0 <= days <= 30:
            raise ValueError("Pause notices for 0 to 30 days")
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            klass.muted_until = self.now() + timedelta(days=days) if days else None
            self.log(s, class_id, actor, "cr", f"Notices paused for {days} days" if days else "Notices back on")

    def announce(self, class_id: int, number: str, name: str, text: str) -> int:
        text = text.strip()
        if not 3 <= len(text) <= 1500:
            raise ValueError("Write a message of 3 to 1500 characters")
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            p = self.policy(klass)
            if not p.on("announce"):
                raise PermissionError("Announcements are switched off for your class")
            if klass.status != ClassStatus.ACTIVE:
                raise PermissionError("Your class is not active")
            if self.paused_all:
                raise PermissionError("The admin has paused every class for now")
            if klass.muted_until and klass.muted_until > self.now():
                raise PermissionError("Notices are paused for your class; turn them back on first")
            if not klass.groups:
                raise PermissionError("Tick a group first")
            limit = p.value("limit_announce")
            if self.hits(s, class_id, number, "announce", hours=24) >= limit:
                raise PermissionError(f"You can send {limit} announcements a day")
            self.hit(s, class_id, number, "announce")
            count = self.queue(s, klass, "announce", f"📣 *From your CR* ({name})\n\n{text}")
            self.log(s, class_id, f"cr:{number}", "cr", f"Announcement to {count} groups: {text[:200]}", person=number)
            return count

    # --- what you change ---

    def save_class_rules(self, class_id: int, rules: dict, actor: str = "admin") -> list[str]:
        """Your rules for one class (None for a setting: back to the default). Tells the CR what changed."""
        clean = policy.clean_rules({k: v for k, v in rules.items() if v is not None})
        with self.sessions.begin() as s:
            klass = self.get(s, class_id)
            before = self.policy(klass)
            klass.rules = json.dumps(clean)
            notes = self._changes(before, self.policy(klass))
            self.log(s, class_id, actor, "admin", "Class rules: " + (", ".join(notes) or "no change for the CR"))
            if notes and klass.status != ClassStatus.PENDING:
                self.tell_crs(s, klass, "The admin changed your class's settings:\n" + "\n".join(f"• {n}" for n in notes))
            return notes

    def save_defaults_for_all(self, rules: dict, actor: str = "admin") -> None:
        with self.sessions.begin() as s:
            klasses = s.scalars(select(SchoolClass).where(SchoolClass.status.in_(
                [ClassStatus.ACTIVE, ClassStatus.PAUSED]))).all()
            before = {k.id: self.policy(k) for k in klasses}
        self.save_defaults(rules)
        with self.sessions.begin() as s:
            self.log(s, None, actor, "admin", "Default rules changed")
            for k in s.scalars(select(SchoolClass).where(SchoolClass.id.in_(list(before)))).all():
                notes = self._changes(before[k.id], self.policy(k))
                if notes:
                    self.tell_crs(s, k, "The admin changed your class's settings:\n" + "\n".join(f"• {n}" for n in notes))

    @staticmethod
    def _changes(before: policy.Policy, after: policy.Policy) -> list[str]:
        notes = []
        for spec in policy.SPECS:
            if before.rule(spec.key) != after.rule(spec.key) and (before.value(spec.key) != after.value(spec.key)
                                                                   or before.editable(spec.key) != after.editable(spec.key)):
                notes.append(f"{spec.label}: {policy.describe_lock(spec.key, after.rule(spec.key))}")
        return notes

    def block(self, class_id: int | None, person: str, by: str, actor: str) -> None:
        person = "".join(ch for ch in person if ch.isdigit())
        if not person:
            raise ValueError("Give the person's number")
        with self.sessions.begin() as s:
            if class_id is not None:
                self.get(s, class_id)
            exists = s.scalar(select(ClassBlock).where(ClassBlock.person == person, ClassBlock.class_id.is_(None) if class_id is None
                                                       else ClassBlock.class_id == class_id))
            if exists is None:
                s.add(ClassBlock(class_id=class_id, person=person, by=by, at=self.now()))
            self.log(s, class_id, actor, by, f"Blocked +{person}" + ("" if class_id else " everywhere"), person=person)

    def unblock(self, class_id: int | None, person: str, by: str, actor: str) -> None:
        with self.sessions.begin() as s:
            row = s.scalar(select(ClassBlock).where(ClassBlock.person == person, ClassBlock.class_id.is_(None) if class_id is None
                                                    else ClassBlock.class_id == class_id))
            if row is None:
                return
            if by == "cr" and row.by == "admin":
                raise PermissionError("The admin blocked this person; only the admin can unblock them")
            s.delete(row)
            self.log(s, class_id, actor, by, f"Unblocked +{person}", person=person)

    def blocks(self, class_id: int | None = None, *, everywhere: bool = True) -> list[dict]:
        with self.sessions() as s:
            q = select(ClassBlock).order_by(ClassBlock.at.desc())
            if class_id is not None:
                q = q.where((ClassBlock.class_id == class_id) | (ClassBlock.class_id.is_(None) if everywhere else False))
            names = dict(s.execute(select(SchoolClass.id, SchoolClass.name)).all())
            return [{"person": b.person, "class_id": b.class_id, "class": names.get(b.class_id, "Everywhere"), "by": b.by,
                     "at": b.at.isoformat()} for b in s.scalars(q).all()]

    def users(self, class_id: int) -> list[dict]:
        """Who uses the class's menu: in its groups and in private chat."""
        with self.sessions() as s:
            klass = self.get(s, class_id)
            chats = {g.group_jid: g.name for g in klass.groups} | {f"class:{class_id}": "Private chat"}
            blocked = {b.person for b in s.scalars(select(ClassBlock).where(
                (ClassBlock.class_id == class_id) | ClassBlock.class_id.is_(None))).all()}
            rows = s.scalars(select(BotUser).where(BotUser.chat_id.in_(list(chats))).order_by(BotUser.last_at.desc())).all()
            return [{"name": u.name, "person": u.person, "where": chats[u.chat_id], "requests": u.requests,
                     "last_option": u.last_option, "last_at": u.last_at.isoformat(), "blocked": u.person in blocked}
                    for u in rows]

    # --- views ---

    def summary(self, klass: SchoolClass) -> dict:
        p = self.policy(klass)
        return {
            "id": klass.id, "name": klass.name, "status": klass.status, "paused_by": klass.paused_by,
            "muted_until": klass.muted_until.isoformat() if klass.muted_until else None,
            "semester": klass.semester_name, "enrollment": klass.enrollment, "institute": klass.institute,
            "remembers_password": klass.password_enc is not None, "login_state": klass.login_state,
            "login_error": klass.login_error, "last_poll_at": klass.last_poll_at.isoformat() if klass.last_poll_at else None,
            "last_error": klass.last_error, "created_at": klass.created_at.isoformat(),
            "approved_at": klass.approved_at.isoformat() if klass.approved_at else None,
            "people": [{"id": x.id, "name": x.name, "phone": x.phone, "role": x.role} for x in klass.people],
            "groups": [{"id": g.group_jid, "name": g.name} for g in klass.groups],
            "courses": sorted(c.name for c in klass.courses if c.semester == klass.semester),
            "mode": p.value("mode"),
            "rules": policy.load(klass.rules),
            "overlap": self.overlap_note(self.overlaps(s, klass)) if (s := object_session(klass)) else "",
        }

    def assignments(self, s: Session, class_id: int) -> list[ClassAssignment]:
        klass = s.get(SchoolClass, class_id)
        return list(s.scalars(select(ClassAssignment).join(ClassCourse).where(
            ClassCourse.class_id == class_id, ClassCourse.semester == (klass.semester if klass else ""))).all())


def find_group(s: Session, group_jid: str) -> ClassGroup | None:
    return s.scalar(select(ClassGroup).where(ClassGroup.group_jid == group_jid))
