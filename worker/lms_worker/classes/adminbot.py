"""Your class controls on WhatsApp: message the class bot from your own number.

Everything the Classes page does, as numbered menus: approve, pause, block and remove classes,
make invite links for CRs and deputies, your rules (for every class and per class), blocked
people, who uses each class, the log, checking a class's LMS now, and pausing every class.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy import select

from ..bot import Reply
from ..db.models import ClassStatus, SchoolClass, utcnow
from . import policy
from .access import INVITE_HOURS, phone
from .chatmenu import ChatMenu, Stop, when
from .store import Classes, poll_key

MODES = {"off": "Off", "notify": "Notices only", "menu": "Notices and menu"}
STATUS = {"pending": "⏳ waiting", "active": "🟢 active", "paused": "⏸️ paused", "blocked": "⛔ blocked"}


def rule_text(spec: policy.Spec, rule: dict) -> str:
    if spec.kind == "switch":
        if rule["lock"] == "cr":
            return f"CR decides (starts {'on' if rule.get('default', True) else 'off'})"
        return ("always on" if rule["lock"] == "on" else "always off") + (", hidden" if rule.get("hide") else "")
    if spec.kind == "limit":
        return f"{rule['max']}" + (", CR may lower" if rule.get("cr") else "")
    return f"up to {MODES[rule['max']]}" + (", CR may pick less" if rule.get("cr") else "")


class AdminMenu(ChatMenu):
    PREFIX = "adm:"

    def __init__(self, classes: Classes, *, public_url: str | None = None,
                 notify: Callable[[str, str], None] | None = None, now: Callable[[], datetime] = utcnow) -> None:
        """`notify(number, text)`: a message straight from the class bot (to a CR, before their class goes)."""
        super().__init__(classes, now=now)
        self.public_url, self.notify = public_url, notify

    def extra_start_words(self) -> tuple[str, ...]:
        return ("admin", "classes")

    def _actor(self) -> str:
        return "admin on WhatsApp"

    def _link(self, token: str) -> str:
        return f"{self.public_url}/join/{token}" if self.public_url else f"/join/{token} (set PUBLIC_URL for a full link)"

    # --- screens ---

    def screen_main(self, who, ctx):
        with self.sessions() as s:
            rows = s.scalars(select(SchoolClass).where(SchoolClass.status != ClassStatus.REMOVED)).all()
            count = {st: sum(1 for k in rows if k.status == st) for st in STATUS}
        lines = [f"{count['active']} active · {count['pending']} waiting · {count['paused']} paused · {count['blocked']} blocked"]
        if self.classes.paused_all:
            lines.append("⏸️ Every class is paused.")
        options = [(f"⏳ Waiting for approval ({count['pending']})", {"do": "open", "screen": "list", "ctx": {"only": "pending"}}),
                   ("📋 All classes", {"do": "open", "screen": "list", "ctx": {}}),
                   ("➕ Invite a CR", {"do": "invite"}),
                   ("⚖️ Default rules (every class)", {"do": "open", "screen": "rules", "ctx": {}}),
                   ("🚫 Blocked people", {"do": "open", "screen": "blocks", "ctx": {}}),
                   ("▶️ Restart every class" if self.classes.paused_all else "⏸️ Pause every class", {"do": "pause_all"}),
                   ("📜 Recent log", {"do": "open", "screen": "log", "ctx": {}})]
        return "🏫 *Classes* · your controls", lines, options

    def screen_list(self, who, ctx):
        with self.sessions() as s:
            q = select(SchoolClass).where(SchoolClass.status != ClassStatus.REMOVED).order_by(SchoolClass.name)
            if ctx.get("only"):
                q = q.where(SchoolClass.status == ctx["only"])
            rows = [(k.id, k.name, k.status, len(k.groups)) for k in s.scalars(q).all()]
        options = [(f"{name} · {STATUS[st]} · {g} group{'s' if g != 1 else ''}", {"do": "open", "screen": "klass", "ctx": {"id": cid}})
                   for cid, name, st, g in rows]
        title = "⏳ *Waiting for approval*" if ctx.get("only") else "📋 *Classes*"
        return title, [] if options else ["None."], options

    def screen_klass(self, who, ctx):
        with self.sessions() as s:
            k = self.classes.get(s, ctx["id"])
            p = self.classes.policy(k)
            note = self.classes.overlap_note(self.classes.overlaps(s, k))
            lines = [f"Status: {STATUS[k.status]}" + (f" (by {'you' if k.paused_by == 'admin' else 'the CR'})" if k.status == "paused" else ""),
                     "CR: " + ", ".join(f"{x.name} +{x.phone}{' (deputy)' if x.role == 'deputy' else ''}" for x in k.people),
                     f"LMS: {k.enrollment} · {k.semester_name or '—'} · {'✅ ok' if k.login_state == 'ok' else '⚠️ ' + (k.login_state or '')}"
                     f" · checked {when(k.last_poll_at)}",
                     f"Courses: {len([c for c in k.courses if c.semester == k.semester])} · Groups: {', '.join(g.name for g in k.groups) or 'none'}",
                     f"Bot: {MODES[p.value('mode')]}"]
            if note:
                lines.append(note)
            if k.last_error:
                lines.append(f"Last problem: {k.last_error[:200]}")
            options = []
            if k.status == ClassStatus.PENDING:
                options.append(("✅ Approve", {"do": "status", "status": "active"}))
            elif k.status == ClassStatus.ACTIVE:
                options.append(("⏸️ Pause", {"do": "status", "status": "paused"}))
            else:
                options.append(("▶️ Unblock" if k.status == ClassStatus.BLOCKED else "▶️ Resume", {"do": "status", "status": "active"}))
            if k.status != ClassStatus.BLOCKED:
                options.append(("⛔ Block", {"do": "status", "status": "blocked"}))
            options += [("⚖️ Rules for this class", {"do": "open", "screen": "rules", "ctx": {"class_id": k.id}}),
                        ("👥 Who uses it", {"do": "open", "screen": "users", "ctx": {"id": k.id}}),
                        ("🔄 Check its LMS now", {"do": "poll"}),
                        ("📜 Log", {"do": "open", "screen": "log", "ctx": {"class_id": k.id}}),
                        ("➕ Invite a deputy", {"do": "deputy"}),
                        ("🗑️ Remove", {"do": "remove"})]
            return f"🏫 *{k.name}*", lines, options

    def screen_rules(self, who, ctx):
        cid = ctx.get("class_id")
        own, defaults, name = {}, self.classes.defaults(), "every class"
        if cid:
            with self.sessions() as s:
                k = self.classes.get(s, cid)
                own, name = policy.load(k.rules), k.name
        options = []
        for spec in policy.SPECS:
            rule = own.get(spec.key) or defaults.get(spec.key) or policy.DEFAULT_RULES[spec.key]
            mark = " ✳️" if spec.key in own else ""
            options.append((f"{spec.label}: {rule_text(spec, rule)}{mark}", {"do": "open", "screen": "rule",
                                                                            "ctx": {"class_id": cid, "key": spec.key}}))
        lines = ["Pick one to change it." + (" ✳️ = this class's own rule; the rest follow the defaults." if cid else "")]
        return f"⚖️ *Rules for {name}*", lines, options

    def screen_rule(self, who, ctx):
        spec, cid = policy.BY_KEY[ctx["key"]], ctx.get("class_id")
        rule, own = self._rule(cid, spec.key)
        lines = [f"Now: {rule_text(spec, rule)}" + (" (this class's own)" if own else " (default)" if cid else "")]
        if spec.help:
            lines.append(spec.help)
        if spec.kind == "switch":
            options = [("Always on", {"do": "rule", "rule": {"lock": "on"}}),
                       ("Always off", {"do": "rule", "rule": {"lock": "off"}}),
                       ("Always off, hidden from the CR", {"do": "rule", "rule": {"lock": "off", "hide": True}}),
                       ("CR decides, starts on", {"do": "rule", "rule": {"lock": "cr", "default": True}}),
                       ("CR decides, starts off", {"do": "rule", "rule": {"lock": "cr", "default": False}})]
        elif spec.kind == "limit":
            options = [("Set the number", {"do": "limit"}),
                       ("CR may set a lower number" if not rule.get("cr") else "CR may not change it",
                        {"do": "rule", "rule": {**rule, "cr": not rule.get("cr")}})]
        else:
            options = [(f"Up to: {MODES[m]}", {"do": "rule", "rule": {**rule, "max": m}}) for m in policy.MODES]
            options.append(("CR may pick less" if not rule.get("cr") else "CR may not change it",
                            {"do": "rule", "rule": {**rule, "cr": not rule.get("cr")}}))
        if cid and own:
            options.append(("Use the default", {"do": "rule", "rule": None}))
        return f"⚖️ *{spec.label}*", lines, options

    def screen_users(self, who, ctx):
        users = self.classes.users(ctx["id"])[:12]
        options = [(f"{'🚫 ' if u['blocked'] else ''}{u['name'] or '—'} +{u['person']} · {u['requests']} · {u['last_option'] or '—'}",
                    {"do": "open", "screen": "user", "ctx": {"id": ctx["id"], "person": u["person"], "name": u["name"]}})
                   for u in users]
        return "👥 *Who uses it*", [] if options else ["No one yet."], options

    def screen_user(self, who, ctx):
        blocks = {(b["class_id"], b["person"]) for b in self.classes.blocks()}
        here, everywhere = (ctx["id"], ctx["person"]) in blocks, (None, ctx["person"]) in blocks
        options = [("Unblock in this class" if here else "Block in this class", {"do": "block", "where": ctx["id"], "on": not here}),
                   ("Unblock everywhere" if everywhere else "Block everywhere", {"do": "block", "where": None, "on": not everywhere}),
                   ("Their log", {"do": "open", "screen": "log", "ctx": {"person": ctx["person"]}})]
        return f"👤 *{ctx.get('name') or ''} +{ctx['person']}*", [], options

    def screen_blocks(self, who, ctx):
        rows = self.classes.blocks()[:15]
        options = [(f"Unblock +{b['person']} ({b['class']})", {"do": "block", "where": b["class_id"], "on": False, "person": b["person"]})
                   for b in rows]
        options.append(("Block a number everywhere", {"do": "block_number"}))
        return "🚫 *Blocked people*", [] if rows else ["No one is blocked."], options

    def screen_log(self, who, ctx):
        rows = self.classes.logs(class_id=ctx.get("class_id"), person=ctx.get("person"), limit=15)
        lines = [f"{when(datetime.fromisoformat(r['at']))} · {r['class']} · {r['detail']}" for r in rows] or ["Nothing yet."]
        return "📜 *Log* · latest 15", lines, []

    def default_back(self, screen, ctx):
        if screen in ("klass",):
            return ["list", {}]
        if screen == "rule":
            return ["rules", {"class_id": ctx.get("class_id")}]
        if screen == "rules" and ctx.get("class_id"):
            return ["klass", {"id": ctx["class_id"]}]
        if screen in ("users",) or (screen == "log" and ctx.get("class_id")):
            return ["klass", {"id": ctx.get("id") or ctx.get("class_id")}]
        if screen == "user":
            return ["users", {"id": ctx["id"]}]
        return ["main", {}]

    # --- rules ---

    def _rule(self, cid, key) -> tuple[dict, bool]:
        defaults = self.classes.defaults()
        if cid:
            with self.sessions() as s:
                own = policy.load(self.classes.get(s, cid).rules)
            if key in own:
                return own[key], True
        return defaults.get(key) or policy.DEFAULT_RULES[key], False

    def _save_rule(self, cid, key, rule) -> str:
        if cid:
            with self.sessions() as s:
                own = policy.load(self.classes.get(s, cid).rules)
            own[key] = rule
            notes = self.classes.save_class_rules(cid, own, self._actor())
            return "✅ Saved." + (" The CR was told." if notes else "")
        if rule is None:
            raise Stop("The defaults have no default")
        self.classes.save_defaults_for_all({**self.classes.defaults(), key: rule}, self._actor())
        return "✅ Saved for every class."

    # --- actions ---

    def do(self, who, state, option):
        do, ctx = option["do"], state.get("ctx") or {}
        if do == "open":
            target = option.get("ctx") or {}
            return self.show(who, option["screen"], target)
        if do == "invite":
            return self.ask("➕ Send the class's name for the invite, e.g. *BSCS 5-B*. 0 = cancel.", state, "invite")
        if do == "pause_all":
            on = not self.classes.paused_all
            if on:
                return self.confirm("Pause every class? Nothing is checked, sent or answered until you restart.", state,
                                    {"do": "pause_all"})
            self._pause_all(False)
            return self.show(who, "main", note="▶️ Every class is back on.")
        if do == "status":
            if option["status"] == "blocked":
                return self.confirm("Block this class? Its CR can then change nothing and the bot stops for it.", state,
                                    {"do": "status", "status": "blocked", "id": ctx["id"]})
            self.classes.set_status(ctx["id"], option["status"], "admin")
            return self.show(who, "klass", ctx, note="✅ Done. The CR was told.")
        if do == "poll":
            self.classes.kv.set(poll_key(ctx["id"]), self.now().isoformat())
            return self.show(who, "klass", ctx, note="🔄 It is checked within a minute.")
        if do == "deputy":
            token = self.classes.create_invite("", role="deputy", class_id=ctx["id"])
            return self.show(who, "klass", ctx, note=f"➕ Deputy invite, works once for {INVITE_HOURS} hours:\n{self._link(token)}")
        if do == "remove":
            with self.sessions() as s:
                name = self.classes.get(s, ctx["id"]).name
            return self.ask(f"🗑️ This deletes {name}'s LMS login and files. Send its name exactly to confirm: *{name}*\n0 = cancel.",
                            state, "remove", id=ctx["id"])
        if do == "rule":
            note = self._save_rule(ctx.get("class_id"), ctx["key"], option["rule"])
            return self.show(who, "rule", ctx, note=note)
        if do == "limit":
            spec = policy.BY_KEY[ctx["key"]]
            return self.ask(f"{spec.label}: send the number, 1 to 1000 (send *none* for zero). 0 = cancel.", state, "limit")
        if do == "block":
            person = option.get("person") or ctx.get("person")
            where = option["where"]
            if option["on"]:
                self.classes.block(where, person, "admin", self._actor())
            else:
                self.classes.unblock(where, person, "admin", self._actor())
            screen = state.get("screen", "main")
            return self.show(who, screen, ctx, note=f"✅ +{person} {'blocked' if option['on'] else 'unblocked'}"
                             + (" everywhere." if where is None else " in this class."))
        if do == "block_number":
            return self.ask("Send the number to block everywhere, e.g. 03001234567. 0 = cancel.", state, "block_number")
        raise ValueError(do)

    def _pause_all(self, on: bool) -> None:
        self.classes.pause_all(on)
        with self.sessions.begin() as s:
            self.classes.log(s, None, self._actor(), "admin", "Every class paused" if on else "Every class back on")

    def answer(self, who, state, text):
        waiting, data = state["await"], state.get("data") or {}
        back = self._back(state)
        if waiting == "invite":
            token = self.classes.create_invite(text, role="cr")
            return self.show(who, "main", note=f"➕ Invite for the CR of *{' '.join(text.split())}*, works once for {INVITE_HOURS} hours."
                                              f" Send them this link:\n{self._link(token)}")
        if waiting == "confirm":
            if text not in ("1", "2"):
                return [Reply("Reply 1 for yes or 2 for no. 0 = cancel.")], state
            if text == "2":
                return self.show(who, *back, note="Cancelled.")
            action = data["action"]
            if action["do"] == "pause_all":
                self._pause_all(True)
                return self.show(who, "main", note="⏸️ Every class is paused.")
            if action["do"] == "status":
                self.classes.set_status(action["id"], action["status"], "admin")
                return self.show(who, "klass", {"id": action["id"]}, note="✅ Done. The CR was told.")
        if waiting == "limit":
            value = 0 if text.lower() == "none" else int(text) if text.isdigit() else None
            if value is None:
                return [Reply("Send a number, or *none* for zero. 0 = cancel.")], state
            ctx = back[1]
            rule, _ = self._rule(ctx.get("class_id"), ctx["key"])
            note = self._save_rule(ctx.get("class_id"), ctx["key"], {"max": value, "cr": bool(rule.get("cr"))})
            return self.show(who, *back, note=note)
        if waiting == "block_number":
            number = phone(text)
            self.classes.block(None, number, "admin", self._actor())
            return self.show(who, "blocks", note=f"✅ +{number} blocked everywhere.")
        if waiting == "remove":
            with self.sessions() as s:
                k = self.classes.get(s, data["id"])
                name, phones = k.name, [p.phone for p in k.people]
            if text.strip().lower() != name.lower():
                return [Reply(f"That is not the class name. Send *{name}* to remove it, or 0 to cancel.")], state
            for number in phones:
                if self.notify:
                    try:
                        self.notify(number, f"The admin removed the class {name}. Its LMS login and files are deleted.")
                    except Exception:
                        pass
            self.classes.remove(data["id"], self._actor())
            return self.show(who, "list", {}, note=f"🗑️ {name} is removed.")
        return self.show(who, "main")
