"""The CR's controls on WhatsApp: everything their class page offers, in a private chat with
the class bot.

A CR or deputy who messages the class bot privately gets this menu instead of the student one
("Student menu" shows what students see; "cr" comes back). The same rules apply as on the page:
what you locked stays locked, limits only go lower, your pause and block beat theirs, and every
change is logged as theirs, "on WhatsApp". Signing in to the LMS again stays on the page, so a
password never sits in a chat; the bot sends the link.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from ..bot import Reply
from ..db.models import ChatSession, ClassPerson, ClassStatus, SchoolClass, utcnow
from . import policy
from .access import phone
from .chatmenu import ChatMenu, Stop, when
from .store import Classes, find_group

MODES = {"off": "Off", "notify": "Notices only", "menu": "Notices and menu"}


class CRMenu(ChatMenu):
    PREFIX = "crm:"

    def __init__(self, classes: Classes, *, groups_for: Callable[[str], dict[str, dict]] | None = None,
                 public_url: str | None = None, now: Callable[[], datetime] = utcnow) -> None:
        """`groups_for(number)`: the class bot's groups where that number is an admin: id -> {name, size}."""
        super().__init__(classes, now=now)
        self.groups_for, self.public_url = groups_for, public_url

    # who = (class id, person id)

    def _id(self, who) -> str:
        return f"person:{who[1]}"

    def extra_start_words(self) -> tuple[str, ...]:
        return ("cr", "controls")

    def person(self, ids: list[str]) -> tuple[int, int] | None:
        """(class id, person id) when one of these ids is a CR or deputy of a class that is not removed."""
        with self.sessions() as s:
            p = s.scalar(select(ClassPerson).join(SchoolClass).where(
                ClassPerson.phone.in_([i for i in ids if i]), SchoolClass.status != ClassStatus.REMOVED)
                .order_by(ClassPerson.id.desc()))
            return (p.class_id, p.id) if p else None

    def student_mode(self, who, text: str) -> bool:
        """True while this CR looks at the student menu; "cr" ends it."""
        state = self._load(who)
        if not state.get("student"):
            return False
        if text.strip().lower() in ("cr", "controls"):
            self._save(who, {})
            return False
        self._save(who, state)
        return True

    def _ctx(self, who):
        with self.sessions() as s:
            k = s.get(SchoolClass, who[0])
            p = s.get(ClassPerson, who[1])
            if k is None or p is None or k.status == ClassStatus.REMOVED:
                raise LookupError("Your class is no longer set up")
            list(k.groups), list(k.people)  # loaded now: used after the session closes
            s.expunge_all()
            return k, p

    def _actor(self, person: ClassPerson) -> str:
        return f"cr:{person.phone} on WhatsApp"

    # --- screens ---

    def screen_main(self, who, ctx):
        with self.sessions() as s:
            k = s.get(SchoolClass, who[0])
            person = s.get(ClassPerson, who[1])
            p = self.classes.policy(k)
            status = {"pending": "⏳ waiting for the admin's approval", "active": "🟢 active", "blocked": "⛔ blocked by the admin",
                      "paused": "⏸️ paused by the admin" if k.paused_by == "admin" else "⏸️ paused by you"}[k.status]
            left = max(0, p.value("limit_announce") - self.classes.hits(s, k.id, person.phone, "announce", hours=24))
            muted = bool(k.muted_until and k.muted_until > self.now())
            lines = [f"Status: {status}",
                     f"Bot: {MODES[p.value('mode')]}" + (f" · notices paused until {when(k.muted_until)}" if muted else ""),
                     f"LMS: {'✅ signed in' if k.login_state == 'ok' else '⚠️ sign in again'} · checked {when(k.last_poll_at)}",
                     f"Groups: {', '.join(g.name for g in k.groups) or 'none ticked yet'}"]
            if self.classes.paused_all:
                lines.append("⏸️ The admin has paused every class for now.")
            blocked, pending = k.status == ClassStatus.BLOCKED, k.status == ClassStatus.PENDING
            options = []
            if p.on("announce") and not blocked:
                options.append((f"📣 Announce ({left} left today)", {"do": "announce"}))
            if not blocked:
                options += [("⚙️ Settings", {"do": "open", "screen": "settings"}),
                            ("👥 Groups", {"do": "open", "screen": "groups"}),
                            ("🚫 People and blocks", {"do": "open", "screen": "people"}),
                            ("🔕 Notices on/pause", {"do": "open", "screen": "mute"})]
                if not pending and not (k.status == ClassStatus.PAUSED and k.paused_by == "admin"):
                    options.append(("▶️ Resume the bot" if k.status == ClassStatus.PAUSED else "⏸️ Pause the bot", {"do": "pause"}))
                if person.role == "cr":
                    options.append(("🔑 LMS sign-in", {"do": "open", "screen": "lms"}))
            options.append(("📜 Log", {"do": "open", "screen": "log"}))
            if p.menu and not blocked:
                options.append(("📚 Student menu (what students see)", {"do": "student"}))
            if person.role == "cr":
                options.append(("🗑️ Remove my class", {"do": "remove"}))
            title = f"🛠️ *{k.name}* · {'CR' if person.role == 'cr' else 'deputy'} controls"
        return title, lines, options

    def screen_settings(self, who, ctx):
        with self.sessions() as s:
            items = self.classes.policy(s.get(SchoolClass, who[0])).for_cr()
        options = []
        for it in items:
            value = f"{it['value']} (at most {it['max']})" if it["kind"] == "limit" else self.describe(policy.BY_KEY[it["key"]], it["value"])
            options.append((f"{it['label']}: *{value}*{'' if it['editable'] else ' 🔒'}", {"do": "setting", "key": it["key"]}))
        return "⚙️ *Settings*", ["Pick one to change it. 🔒 = set by the admin."], options

    def screen_mode(self, who, ctx):
        with self.sessions() as s:
            item = next(i for i in self.classes.policy(s.get(SchoolClass, who[0])).for_cr() if i["key"] == "mode")
        return "🤖 *What the bot does*", [f"Now: {MODES[item['value']]}"], [
            (MODES[m], {"do": "set", "key": "mode", "value": m}) for m in item["choices"]]

    def screen_groups(self, who, ctx):
        k, person = self._ctx(who)
        mine = self._admin_groups(person.phone)
        with self.sessions() as s:
            ticked = {g.group_jid: g.name for g in s.get(SchoolClass, who[0]).groups}
            taken = {gid for gid in mine if (g := find_group(s, gid)) is not None and g.class_id != who[0]}
        options = [(f"{'✅' if gid in ticked else '⬜'} {g['name']} ({g['size']} members)", {"do": "toggle_group", "id": gid})
                   for gid, g in sorted(mine.items(), key=lambda x: x[1]["name"].lower()) if gid not in taken]
        lines = ["Pick a group to tick or untick it. Ticked groups get the notices (and the menu, if on)."]
        missing = [name for gid, name in ticked.items() if gid not in mine]
        if missing:
            lines.append("Also ticked: " + ", ".join(missing))
        if not options:
            lines.append("To add a group: add the class bot's number to it and make sure you are a group admin, then open this again.")
        return "👥 *Groups*", lines, options

    def screen_people(self, who, ctx):
        users = self.classes.users(who[0])[:10]
        blocks = self.classes.blocks(who[0], everywhere=False)
        lines = [f"{len(users)} recent" + (f" · {len(blocks)} blocked" if blocks else "")]
        options = [(f"{'🚫 ' if u['blocked'] else ''}{u['name'] or '—'} +{u['person']} · {u['requests']} requests",
                    {"do": "person", "person": u["person"], "blocked": u["blocked"]}) for u in users]
        options.append(("Block a number", {"do": "block_number"}))
        shown = {u["person"] for u in users}
        options += [(f"Unblock +{b['person']}", {"do": "unblock", "person": b["person"]}) for b in blocks if b["person"] not in shown]
        return "🚫 *People using the bot*", lines, options

    def screen_mute(self, who, ctx):
        options = [("Notices on (resume now)", {"do": "mute", "days": 0})]
        options += [(f"Pause notices for {d} day{'s' if d > 1 else ''}", {"do": "mute", "days": d}) for d in (1, 3, 7, 14)]
        return "🔕 *Notices*", ["Paused notices are skipped, not sent later. The menu keeps working."], options

    def screen_lms(self, who, ctx):
        k, _ = self._ctx(who)
        link = f"{self.public_url}/cr" if self.public_url else "your class page (/cr)"
        lines = ["✅ Signed in" if k.login_state == "ok" else f"⚠️ Sign in again: {k.login_error or ''}",
                 f"Password: {'kept (encrypted)' if k.password_enc else 'not kept'}", "",
                 "To sign in again, open your class page, so your password never sits in a chat:", link]
        return "🔑 *LMS sign-in*", lines, [("Forget my kept password", {"do": "forget"})] if k.password_enc else []

    def screen_log(self, who, ctx):
        rows = self.classes.logs(class_id=who[0], limit=10)
        return "📜 *Log* · latest 10", [f"{when(datetime.fromisoformat(r['at']))} · {r['detail']}" for r in rows] or ["Nothing yet."], []

    def _admin_groups(self, number: str) -> dict[str, dict]:
        if self.groups_for is None:
            return {}
        try:
            return self.groups_for(number)
        except Exception as e:
            raise Stop(f"Could not read your WhatsApp groups right now ({type(e).__name__})") from e

    # --- actions ---

    def do(self, who, state, option):
        k, person = self._ctx(who)
        do, actor = option["do"], self._actor(person)
        if k.status == ClassStatus.BLOCKED and do not in ("open",):
            raise Stop("The admin blocked this class")
        if do == "open":
            return self.show(who, option["screen"], back=["main", {}] if option["screen"] != "mode" else ["settings", {}])
        if do == "student":
            return [Reply("You now see the student menu: send *menu*. Send *cr* to come back to your controls.")], {"student": True}
        if do == "announce":
            return self.ask("📣 Send the announcement as your next message (up to 1500 characters).\n"
                            "It goes to every ticked group as *From your CR*. 0 = cancel.", state, "announce")
        if do == "setting":
            with self.sessions() as s:
                item = next(i for i in self.classes.policy(s.get(SchoolClass, who[0])).for_cr() if i["key"] == option["key"])
            if not item["editable"]:
                raise Stop(f"🔒 {item['label']} is set by the admin")
            if item["kind"] == "switch":
                self.classes.set_choice(who[0], item["key"], not item["value"], actor)
                return self.show(who, "settings", note=f"✅ {item['label']}: {'off' if item['value'] else 'on'}.")
            if item["kind"] == "limit":
                return self.ask(f"{item['label']}: now {item['value']}. Send a number from 0 to {item['max']} "
                                "(send *none* for 0). 0 = cancel.", state, "limit", key=item["key"])
            return self.show(who, "mode", back=["settings", {}])
        if do == "set":
            self.classes.set_choice(who[0], option["key"], option["value"], actor)
            return self.show(who, "settings", note="✅ Saved.")
        if do == "toggle_group":
            mine = self._admin_groups(person.phone)
            with self.sessions() as s:
                ticked = {g.group_jid: g.name for g in s.get(SchoolClass, who[0]).groups}
            want = [gid for gid in ticked if gid != option["id"]]
            if len(want) == len(ticked):
                want.append(option["id"])
            names = {gid: g["name"] for gid, g in mine.items()} | ticked  # groups already ticked may stay
            self.classes.save_groups(who[0], [{"id": gid} for gid in want], names, actor)
            return self.show(who, "groups", note="✅ Saved.")
        if do == "person":
            verb = "Unblock" if option["blocked"] else "Block"
            return self.confirm(f"{verb} +{option['person']} from the bot in your class?", state,
                                {"do": "unblock" if option["blocked"] else "block", "person": option["person"]})
        if do == "block_number":
            return self.ask("Send the number to block, e.g. 03001234567. 0 = cancel.", state, "block_number")
        if do == "unblock":
            self.classes.unblock(who[0], option["person"], "cr", actor)
            return self.show(who, "people", note=f"✅ +{option['person']} unblocked.")
        if do == "mute":
            self.classes.mute(who[0], option["days"], actor)
            return self.show(who, "main", note="✅ Notices are on." if not option["days"] else f"✅ Notices paused for {option['days']} days.")
        if do == "pause":
            paused = k.status == ClassStatus.PAUSED
            self.classes.set_status(who[0], ClassStatus.ACTIVE if paused else ClassStatus.PAUSED, actor)
            return self.show(who, "main", note="✅ The bot is back on." if paused else "✅ The bot is paused.")
        if do == "forget":
            return self.confirm("Delete your kept LMS password? The bot keeps working until the LMS signs it out.", state, {"do": "forget"})
        if do == "remove":
            return self.ask(f"🗑️ This deletes your LMS login and everything the bot fetched for {k.name}, and stops the bot for it.\n\n"
                            f"To confirm, send the class name exactly: *{k.name}*\n0 = cancel.", state, "remove")
        raise ValueError(do)

    def answer(self, who, state, text):
        k, person = self._ctx(who)
        waiting, data, actor = state["await"], state.get("data") or {}, self._actor(person)
        if waiting == "announce":
            preview = f"📣 *From your CR* ({person.name or 'your CR'})\n\n{text}"
            n = len(k.groups)
            return self.confirm(f"This goes to {n} group{'s' if n != 1 else ''}:\n\n{preview}\n\nSend it?",
                                {"screen": "main", "ctx": {}}, {"do": "announce", "text": text})
        if waiting == "confirm":
            if text not in ("1", "2"):
                return [Reply("Reply 1 for yes or 2 for no. 0 = cancel.")], state
            if text == "2":
                return self.show(who, *self._back(state), note="Cancelled.")
            action = data["action"]
            if action["do"] == "announce":
                n = self.classes.announce(who[0], person.phone, person.name or "your CR", action["text"])
                return self.show(who, "main", note=f"✅ Announcement queued for {n} group{'s' if n != 1 else ''}.")
            if action["do"] == "block":
                self.classes.block(who[0], action["person"], "cr", actor)
                return self.show(who, "people", note=f"✅ +{action['person']} blocked in your class.")
            if action["do"] == "unblock":
                self.classes.unblock(who[0], action["person"], "cr", actor)
                return self.show(who, "people", note=f"✅ +{action['person']} unblocked.")
            if action["do"] == "forget":
                self.classes.forget_password(who[0], actor)
                return self.show(who, "lms", note="✅ Your kept password is deleted.")
        if waiting == "limit":
            value = 0 if text.lower() == "none" else int(text) if text.isdigit() else None
            if value is None:
                return [Reply("Send a number, or *none* for 0. 0 = cancel.")], state
            self.classes.set_choice(who[0], data["key"], value, actor)
            return self.show(who, "settings", note="✅ Saved.")
        if waiting == "block_number":
            number = phone(text)
            return self.confirm(f"Block +{number} from the bot in your class?", {"screen": "people", "ctx": {}},
                                {"do": "block", "person": number})
        if waiting == "remove":
            if text.strip().lower() != k.name.lower():
                return [Reply(f"That is not the class name. Send *{k.name}* to remove it, or 0 to cancel.")], state
            self.classes.remove(who[0], actor)
            return [Reply(f"🗑️ {k.name} is removed. Its LMS login and files are deleted. Thank you for running it!")], {}
        return self.show(who, "main")
