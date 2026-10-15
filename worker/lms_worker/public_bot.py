"""The public menu: classmates in your WhatsApp groups tag the bot and get course files.

Who it answers:
- in a group ticked on the setup page, anyone who tags the bot or replies to one of its
  messages; every reply there tags them back and quotes their message;
- in a private chat, anyone who is a member of a ticked group (no tag needed).
Your own number keeps your private menu; strangers and other groups get no answer.

What it gives: question files of this semester's assignments, lecture files, the deadlines
list, and each course's outline and plan; in a group also a leaderboard of who asks most.
Nothing else is reachable from here: every file sent passes `allowed()`, which accepts only an
assignment's question file and the LMS's lecture, outline and plan files. Solved reports,
receipts, your uploads and lab papers are never sent.

Each person has their own place in the menu per chat (it remembers the last screen for 30
minutes), and a row in bot_users counting their requests and their last pick.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .bot import MaterialsError, Reply
from .config import EvolutionConfig
from .db.models import Artifact, Assignment, BotUser, ChatSession, Course, Job, utcnow
from .groups import WhatsAppGroups, open_assignments
from .lms.bahria_pages import LMS_TZ
from .lms.models import PAPER

log = logging.getLogger(__name__)

IDLE = timedelta(minutes=30)
PAGE = 10
PRIVATE = "private"
SAFE_DIRS = ("lectures", "outline", "plan")  # what Materials downloads from the LMS


# --- the message as WhatsApp sent it ---


def _user(jid: str | None) -> str:
    """'923001234567@s.whatsapp.net' or '1234@lid' or '92300...:12@s...' -> the bare id."""
    return (jid or "").split("@")[0].split(":")[0]


@dataclass
class Incoming:
    chat: str  # the group JID, or the sender's JID in a private chat
    group: bool
    ids: list[str]  # the sender's ids (phone digits and/or LID), most useful first
    jids: list[str]  # the same as full JIDs, for tagging back
    name: str
    message_id: str
    text: str
    mentions: list[str] = field(default_factory=list)  # bare ids tagged in the message
    replied_to: str | None = None  # bare id of the author of the quoted message
    key: dict = field(default_factory=dict)  # for quoting the message in replies


def parse(body: dict) -> Incoming | None:
    """A text message from someone else, in a group or a private chat; else None."""
    if str(body.get("event", "")).lower().replace("_", ".") != "messages.upsert":
        return None
    data = body.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None
    chat = key.get("remoteJid") or ""
    group = chat.endswith("@g.us")
    if group:
        candidates = [key.get("participantAlt"), key.get("participantPn"), key.get("participant"), data.get("participant")]
    elif chat.endswith(("@s.whatsapp.net", "@lid")):
        candidates = [key.get("remoteJidAlt"), key.get("senderPn"), chat]
    else:  # broadcasts, newsletters
        return None
    jids = []
    for j in candidates:
        if j and j.endswith(("@s.whatsapp.net", "@lid")) and j not in jids:
            jids.append(j)
    jids.sort(key=lambda j: not j.endswith("@s.whatsapp.net"))  # the phone first, when known
    if not jids:
        return None
    msg = data.get("message") or {}
    ext = msg.get("extendedTextMessage") or {}
    text = msg.get("conversation") or ext.get("text") or ""
    ctx = ext.get("contextInfo") or {}
    for part in msg.values():  # a caption or a reply on another kind of message
        if isinstance(part, dict) and isinstance(part.get("contextInfo"), dict) and not ctx:
            ctx = part["contextInfo"]
    if not text:
        return None
    return Incoming(
        chat=chat, group=group, ids=[_user(j) for j in jids], jids=jids, name=str(data.get("pushName") or ""),
        message_id=str(key.get("id") or ""), text=text,
        mentions=[_user(j) for j in ctx.get("mentionedJid") or [] if j],
        replied_to=_user(ctx.get("participant")) or None,
        key={"remoteJid": chat, "fromMe": False, "id": key.get("id"), **({"participant": key.get("participant")} if group else {})},
    )


# --- who may use it ---


class Directory:
    """The bot's own ids and the members of the ticked groups, read from Evolution API and kept
    for ten minutes."""

    TTL = 600

    def __init__(self, admin, groups: WhatsAppGroups, *, clock: Callable[[], float] = time.time) -> None:
        self.admin, self.groups, self.clock = admin, groups, clock
        self._at = 0.0
        self._bot: set[str] = set()
        self._members: dict[str, set[str]] = {}  # bare id -> the ticked groups they are in

    def _refresh(self) -> None:
        if self._at and self.clock() - self._at < self.TTL:
            return
        bot: set[str] = set()
        members: dict[str, set[str]] = {}
        try:
            own = self.admin.state().number
            if own:
                bot.add(own)
            for g in self.groups.get():
                for p in self.admin.participants(g.id):
                    ids = {_user(p.get(k)) for k in ("id", "jid", "phoneNumber", "lid") if p.get(k)}
                    ids.discard("")
                    if own and own in ids:
                        bot |= ids  # the bot's LID, as group mentions carry it
                    for i in ids:
                        members.setdefault(i, set()).add(g.id)
        except Exception as e:  # Evolution down: keep what we knew
            log.warning("Could not read WhatsApp group members: %s", e)
            if self._at:
                return
        self._bot, self._members, self._at = bot, members, self.clock()

    def is_bot(self, ids: list[str]) -> bool:
        self._refresh()
        return any(i in self._bot for i in ids)

    def ticked(self, chat: str) -> bool:
        return any(g.id == chat for g in self.groups.get())

    def member(self, ids: list[str]) -> bool:
        self._refresh()
        return any(i in self._members for i in ids)


def addressed(m: Incoming, directory: Directory) -> bool:
    """Should the public menu answer this message?"""
    if m.group:
        if not directory.ticked(m.chat):
            return False
        return directory.is_bot(m.mentions) or (m.replied_to is not None and directory.is_bot([m.replied_to]))
    return directory.member(m.ids)


def strip_tags(text: str) -> str:
    return re.sub(r"@\d{5,}\s*", "", text).strip()


# --- the menu ---


def allowed(path: Path, data_dir: Path, question_files: set[str]) -> bool:
    """Only an assignment's question file, or a lecture, outline or plan file from the LMS."""
    try:
        resolved = path.resolve()
        resolved.relative_to(data_dir.resolve())
    except (OSError, ValueError):
        return False
    if not resolved.is_file():
        return False
    if str(resolved) in question_files:
        return True
    return resolved.parent.name in SAFE_DIRS and resolved.parent.parent.parent == data_dir.resolve()


class PublicBot:
    def __init__(self, sessions: sessionmaker[Session], data_dir: Path, *, materials=None,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.sessions, self.data_dir, self.materials, self.now = sessions, data_dir, materials, now

    def handle(self, chat: str, person: str, name: str, text: str, *, group: bool) -> list[Reply]:
        text = strip_tags(text)
        where = chat if group else PRIVATE
        key = "pub:" + hashlib.sha1(f"{where}|{person}".encode()).hexdigest()[:40]
        with self.sessions.begin() as s:
            row = s.get(ChatSession, key)
            if row is None:
                row = ChatSession(chat_id=key, state="{}")
                s.add(row)
            stack = json.loads(row.state or "{}").get("stack") or []
            if not stack or (row.updated_at and self.now() - row.updated_at > IDLE):
                stack = [{"name": "main"}]
            replies, picked = self._dispatch(s, stack, text, group=group, chat=where)
            row.state = json.dumps({"stack": stack})
            row.updated_at = self.now()
            self._count(s, where, person, name, picked)
        return replies

    def _count(self, s: Session, chat: str, person: str, name: str, picked: str | None) -> None:
        user = s.scalar(select(BotUser).where(BotUser.chat_id == chat, BotUser.person == person))
        if user is None:
            user = BotUser(chat_id=chat, person=person, name=name[:128], requests=0, first_at=self.now(), last_option="")
            s.add(user)
        user.requests += 1
        user.last_at = self.now()
        if name:
            user.name = name[:128]
        if picked:
            user.last_option = picked[:256]

    # --- choices ---

    def _dispatch(self, s: Session, stack: list[dict], text: str, *, group: bool, chat: str) -> tuple[list[Reply], str | None]:
        low = text.lower()
        if low in ("", "menu", "hi", "hello", "start", "help", "salam", "aoa"):
            del stack[1:]
            return [self._render(s, stack, group=group, chat=chat)], None
        if text == "0":
            if len(stack) > 1:
                stack.pop()
            return [self._render(s, stack, group=group, chat=chat)], None
        options = stack[-1].get("options") or {}
        if text not in options:
            return [self._render(s, stack, group=group, chat=chat, hint="Reply with one of the numbers below.")], None
        option = options[text]
        label = option.get("label", "")
        if option["do"] == "open":
            stack.append(option["screen"])
            return [self._render(s, stack, group=group, chat=chat)], label
        if option["do"] == "deadlines":
            return [Reply(self._deadlines(s))], label
        if option["do"] == "leaderboard":
            return [Reply(self._leaderboard(s, chat))], label
        if option["do"] in ("question", "lecture", "outline", "plan"):
            return [self._file(s, option)], label
        raise ValueError(option["do"])

    def _file(self, s: Session, option: dict) -> Reply:
        course = s.get(Course, option.get("course")) if option.get("course") else None
        try:
            if option["do"] == "question":
                a = s.get(Assignment, option["assignment"])
                path = self._question_file(a) if a else None
                caption = f"📝 {a.title}\n📘 {a.course.name}" if a else ""
            elif option["do"] == "lecture":
                path = self.materials.lecture(course, option["week"], option["file"]) if self.materials else None
                caption = f"📖 {option.get('title') or option['file']}\n📘 {course.name}"
            elif option["do"] == "outline":
                path = self.materials.outline(course) if self.materials else None
                caption = f"📋 Course outline\n📘 {course.name}"
            else:
                path = self.materials.plan(course) if self.materials else None
                caption = f"🗓️ Course plan\n📘 {course.name}"
        except MaterialsError as e:
            return Reply(f"⚠️ Could not get that from the LMS right now: {e}")
        if path is None:
            return Reply("That file is not on the LMS.")
        if not allowed(Path(path), self.data_dir, self._question_paths(s)):
            log.warning("Public menu refused to send %s", path)
            return Reply("That file can't be shared here.")
        return Reply(caption, file=Path(path))

    @staticmethod
    def _question_file(a: Assignment) -> Path | None:
        if a.kind == PAPER:
            return None
        for job in reversed(a.jobs):
            for art in sorted(job.artifacts, key=lambda x: -x.id):
                if art.kind == "question" and Path(art.path).is_file():
                    return Path(art.path)
        return Path(a.question_path) if a.question_path and Path(a.question_path).is_file() else None

    @staticmethod
    def _question_paths(s: Session) -> set[str]:
        rows = s.execute(select(Artifact.path).join(Job).join(Assignment)
                         .where(Artifact.kind == "question", Assignment.kind != PAPER)).scalars().all()
        rows += s.execute(select(Assignment.question_path).where(Assignment.kind != PAPER,
                                                                  Assignment.question_path.is_not(None))).scalars().all()
        out = set()
        for p in rows:
            try:
                out.add(str(Path(p).resolve()))
            except OSError:
                pass
        return out

    # --- screens ---

    def _render(self, s: Session, stack: list[dict], *, group: bool, chat: str, hint: str | None = None) -> Reply:
        screen = stack[-1]
        title, lines, options = getattr(self, f"_screen_{screen['name']}")(s, screen, group=group, chat=chat)
        screen["options"] = {str(n): opt for n, (_, opt) in enumerate(options, start=1)}
        body = [title] + ([""] + lines if lines else [])
        if options:
            body.append("")
            body += [f"{n}. {label}" for n, (label, _) in enumerate(options, start=1)]
        foot = "Reply with a number" + (" (tag me or reply to this message)" if group else "")
        foot += "." if len(stack) == 1 else ". 0 = back, menu = start."
        body += ["", hint or foot]
        return Reply("\n".join(body))

    def _screen_main(self, s, screen, *, group, chat):
        options = [
            ("📝 Assignment question files", {"do": "open", "label": "Assignment question files", "screen": {"name": "courses", "for": "question"}}),
            ("📖 Lectures", {"do": "open", "label": "Lectures", "screen": {"name": "courses", "for": "lecture"}}),
            ("⏰ Deadlines", {"do": "deadlines", "label": "Deadlines"}),
            ("📋 Course outline & plan", {"do": "open", "label": "Course outline & plan", "screen": {"name": "courses", "for": "outline"}}),
        ]
        if group:
            options.append(("🏆 Leaderboard", {"do": "leaderboard", "label": "Leaderboard"}))
        return "📚 *LMS Bot*", [], options

    def _screen_courses(self, s, screen, *, group, chat):
        semester = s.scalar(select(Course.semester).order_by(Course.last_seen_at.desc()).limit(1))
        courses = s.scalars(select(Course).where(Course.semester == semester).order_by(Course.name)).all() if semester else []
        what = screen["for"]
        nxt = {"question": "assignments", "lecture": "lectures", "outline": "outline"}[what]
        options = [(c.name, {"do": "open", "label": f"{c.name}", "screen": {"name": nxt, "course": c.id}}) for c in courses]
        head = {"question": "📝 *Question files*", "lecture": "📖 *Lectures*", "outline": "📋 *Outline & plan*"}[what]
        return f"{head} · pick a course", [] if options else ["No courses yet."], options

    def _screen_assignments(self, s, screen, *, group, chat):
        course = s.get(Course, screen["course"])
        rows = s.scalars(select(Assignment).where(Assignment.course_id == course.id, Assignment.kind != PAPER)
                         .options(selectinload(Assignment.jobs).selectinload(Job.artifacts))
                         .order_by(Assignment.deadline.desc().nulls_last(), Assignment.id.desc())).all()
        options = []
        for a in rows:
            if self._question_file(a) is None:
                continue
            due = f" · due {a.deadline.astimezone(LMS_TZ).strftime('%d %b').lstrip('0')}" if a.deadline else ""
            options.append((f"{a.title}{due}", {"do": "question", "label": f"{course.name} · {a.title}", "assignment": a.id}))
            if len(options) == PAGE * 2:
                break
        return f"📝 *{course.name}*", [] if options else ["No question files yet."], options

    def _screen_lectures(self, s, screen, *, group, chat):
        course = s.get(Course, screen["course"])
        if self.materials is None:
            return f"📖 *{course.name}*", ["Lectures are not available right now."], []
        try:
            lectures = self.materials.lectures(course)
        except MaterialsError as e:
            return f"📖 *{course.name}*", [f"⚠️ {e}"], []
        options = []
        for lec in lectures:
            for f in lec.files:
                label = f"Week {lec.week}: {lec.title}" if lec.week else lec.title
                if len(lec.files) > 1:
                    label += f" ({f.filename})"
                options.append((label, {"do": "lecture", "label": f"{course.name} · {label}", "course": course.id,
                                        "week": f.week, "file": f.filename, "title": label}))
        return f"📖 *{course.name}*", [] if options else ["No lecture files on the LMS yet."], options[-PAGE * 3:]

    def _screen_outline(self, s, screen, *, group, chat):
        course = s.get(Course, screen["course"])
        options = [("📋 Course outline", {"do": "outline", "label": f"{course.name} · outline", "course": course.id}),
                   ("🗓️ Course plan", {"do": "plan", "label": f"{course.name} · plan", "course": course.id})]
        return f"📋 *{course.name}*", [], options

    def _deadlines(self, s: Session) -> str:
        now = self.now()
        items = open_assignments(s, now)
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

    def _leaderboard(self, s: Session, chat: str) -> str:
        rows = s.scalars(select(BotUser).where(BotUser.chat_id == chat).order_by(BotUser.requests.desc()).limit(10)).all()
        if not rows:
            return "🏆 *Leaderboard*\n\nNo one yet."
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 *Leaderboard* · most requests", ""]
        for i, u in enumerate(rows):
            lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {u.name or 'Someone'} · {u.requests}")
        return "\n".join(lines)


# --- sending, with the tag ---


class PublicSender:
    """Replies to a group or a private chat. In a group each message tags the person and quotes
    the message they sent."""

    def __init__(self, cfg: EvolutionConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
        self.client = httpx.Client(base_url=cfg.base_url, headers={"apikey": cfg.api_key}, timeout=60, transport=transport)

    def send(self, m: Incoming, replies: list[Reply]) -> None:
        tag = m.jids[0]
        for r in replies:
            text = f"@{_user(tag)} {r.text}" if m.group else r.text
            extra = {"mentioned": [tag], "quoted": {"key": m.key, "message": {"conversation": m.text}}} if m.group else {}
            try:
                if r.file is not None:
                    mimetype = mimetypes.guess_type(r.file.name)[0] or "application/octet-stream"
                    body = {"number": m.chat, "mediatype": "image" if mimetype in ("image/png", "image/jpeg") else "document",
                            "mimetype": mimetype, "caption": text, "fileName": r.file.name,
                            "media": base64.b64encode(r.file.read_bytes()).decode(), **extra}
                    self._post("sendMedia", body)
                else:
                    self._post("sendText", {"number": m.chat, "text": text, **extra})
            except (httpx.HTTPError, OSError, RuntimeError) as e:
                log.warning("Could not send a public reply: %s", e)
                return

    def _post(self, endpoint: str, body: dict) -> None:
        r = self.client.post(f"/message/{endpoint}/{self.cfg.instance}", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"Evolution API {endpoint} returned {r.status_code}: {r.text[:300]}")
