"""Settings a class has, and who decides each one.

You set a rule per setting, for every class (the defaults) and, where you want, for one class:

    switches  "on" (always on), "off" (always off), or "cr" (the CR decides; `default` is where
              their switch starts). `hide` keeps a locked switch off the CR's page.
    limits    `max`, and `cr`: whether the CR may set a lower number. They never go higher.
    mode      what the bot does at all: off, notify (notices only) or menu (notices and the menu).
              `max` is the most you allow; with `cr`, the CR may pick less.

The CR's own choices are kept separately and only count where the rule says "cr", so locking a
setting overrides them at once, and unlocking it again brings their choice back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MODES = ("off", "notify", "menu")


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    section: str
    kind: str  # switch, limit, mode
    help: str = ""


SPECS: tuple[Spec, ...] = (
    Spec("mode", "What the bot does", "Bot", "mode", "Off, notices only, or notices and the menu"),
    Spec("notice_new", "New assignments", "Notices", "switch", "A message when the teacher posts an assignment"),
    Spec("notice_file", "Question file with new assignments", "Notices", "switch"),
    Spec("notice_change", "Changed assignments and deadlines", "Notices", "switch"),
    Spec("remind_24h", "Reminder 24 hours before the deadline", "Notices", "switch"),
    Spec("remind_2h", "Reminder 2 hours before the deadline", "Notices", "switch"),
    Spec("summary_morning", "Morning summary (8:00 AM)", "Notices", "switch"),
    Spec("summary_evening", "Evening summary (9:00 PM)", "Notices", "switch"),
    Spec("menu_questions", "Menu: assignment question files", "Menu", "switch"),
    Spec("menu_lectures", "Menu: lectures", "Menu", "switch"),
    Spec("menu_outline", "Menu: course outline and plan", "Menu", "switch"),
    Spec("menu_deadlines", "Menu: deadlines", "Menu", "switch"),
    Spec("menu_leaderboard", "Menu: leaderboard", "Menu", "switch"),
    Spec("menu_private", "Menu in private chat with the bot", "Menu", "switch"),
    Spec("announce", "CR announcements through the bot", "CR", "switch",
         "The CR can send their own message to the class groups, labelled as from the CR"),
    Spec("limit_requests", "Menu requests per person per hour", "Limits", "limit"),
    Spec("limit_files", "Files per person per hour", "Limits", "limit"),
    Spec("limit_replies", "Bot replies per group per hour", "Limits", "limit"),
    Spec("limit_announce", "CR announcements per day", "Limits", "limit"),
)
BY_KEY = {s.key: s for s in SPECS}

# Where everything starts: notices only, the CR decides the rest, limits as suggested
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "mode": {"max": "notify", "cr": True},
    **{s.key: {"lock": "cr", "default": True} for s in SPECS if s.kind == "switch"},
    "announce": {"lock": "off"},
    "limit_requests": {"max": 10, "cr": True},
    "limit_files": {"max": 5, "cr": True},
    "limit_replies": {"max": 40, "cr": True},
    "limit_announce": {"max": 3, "cr": True},
}


class PolicyError(ValueError):
    pass


def _clean_rule(spec: Spec, rule: dict) -> dict:
    if not isinstance(rule, dict):
        raise PolicyError(f"{spec.label}: not a rule")
    if spec.kind == "switch":
        lock = rule.get("lock")
        if lock not in ("on", "off", "cr"):
            raise PolicyError(f"{spec.label}: choose always on, always off, or CR decides")
        out = {"lock": lock}
        if lock == "cr":
            out["default"] = bool(rule.get("default", True))
        elif rule.get("hide"):
            out["hide"] = True
        return out
    if spec.kind == "limit":
        try:
            top = int(rule.get("max"))
        except (TypeError, ValueError) as e:
            raise PolicyError(f"{spec.label}: give a number") from e
        if not 0 <= top <= 1000:
            raise PolicyError(f"{spec.label}: between 0 and 1000")
        return {"max": top, "cr": bool(rule.get("cr", False))}
    top = rule.get("max")
    if top not in MODES:
        raise PolicyError(f"{spec.label}: one of {', '.join(MODES)}")
    return {"max": top, "cr": bool(rule.get("cr", False))}


def clean_rules(rules: dict) -> dict:
    """Your rules from the page, checked. Unknown settings are refused."""
    if not isinstance(rules, dict):
        raise PolicyError("Rules must be an object")
    out = {}
    for key, rule in rules.items():
        if key not in BY_KEY:
            raise PolicyError(f"No setting named {key}")
        out[key] = _clean_rule(BY_KEY[key], rule)
    return out


def load(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Policy:
    """The rules that apply to one class: its own where you set them, else the defaults."""

    defaults: dict
    own: dict  # your rules for this class
    choices: dict  # the CR's

    def rule(self, key: str) -> dict:
        return self.own.get(key) or self.defaults.get(key) or DEFAULT_RULES[key]

    def source(self, key: str) -> str:
        return "class" if key in self.own else "default"

    def value(self, key: str) -> Any:
        spec, rule, choice = BY_KEY[key], self.rule(key), self.choices.get(key)
        if spec.kind == "switch":
            if rule["lock"] != "cr":
                return rule["lock"] == "on"
            return bool(choice) if isinstance(choice, bool) else bool(rule.get("default", True))
        if spec.kind == "limit":
            top = int(rule["max"])
            if rule.get("cr") and isinstance(choice, int) and not isinstance(choice, bool):
                return max(0, min(top, choice))
            return top
        top = MODES.index(rule["max"])
        if rule.get("cr") and choice in MODES:
            return MODES[min(top, MODES.index(choice))]
        return rule["max"]

    def on(self, key: str) -> bool:
        return bool(self.value(key))

    @property
    def notices(self) -> bool:
        return self.value("mode") in ("notify", "menu")

    @property
    def menu(self) -> bool:
        return self.value("mode") == "menu"

    def editable(self, key: str) -> bool:
        """May the CR change it?"""
        spec, rule = BY_KEY[key], self.rule(key)
        if spec.kind == "switch":
            return rule["lock"] == "cr"
        return bool(rule.get("cr"))

    def for_cr(self) -> list[dict]:
        """What the CR's page shows: every setting not hidden, its value, and whether it is locked."""
        out = []
        for spec in SPECS:
            rule = self.rule(spec.key)
            if spec.kind == "switch" and rule.get("lock") != "cr" and rule.get("hide"):
                continue
            item = {"key": spec.key, "label": spec.label, "section": spec.section, "kind": spec.kind, "help": spec.help,
                    "value": self.value(spec.key), "editable": self.editable(spec.key)}
            if spec.kind == "limit":
                item["max"] = int(rule["max"])
            if spec.kind == "mode":
                item["choices"] = list(MODES[: MODES.index(rule["max"]) + 1])
            out.append(item)
        return out

    def choose(self, key: str, value: Any) -> dict:
        """The CR sets one setting; returns their choices to keep. Refused when locked."""
        if key not in BY_KEY:
            raise PolicyError(f"No setting named {key}")
        if not self.editable(key):
            raise PolicyError(f"{BY_KEY[key].label} is set by the admin")
        spec, rule = BY_KEY[key], self.rule(key)
        if spec.kind == "switch":
            if not isinstance(value, bool):
                raise PolicyError("On or off")
        elif spec.kind == "limit":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= int(rule["max"]):
                raise PolicyError(f"{spec.label}: a number from 0 to {rule['max']}")
        elif value not in MODES[: MODES.index(rule["max"]) + 1]:
            raise PolicyError(f"{spec.label}: the admin allows up to {rule['max']}")
        return {**self.choices, key: value}


def describe_lock(key: str, rule: dict) -> str:
    """For the note a CR gets when you change a rule: "Reminders 2 hours before: now always on"."""
    spec = BY_KEY[key]
    if spec.kind == "switch":
        return {"on": "now always on", "off": "now always off", "cr": "now yours to decide"}[rule["lock"]]
    if spec.kind == "limit":
        return f"at most {rule['max']}" + ("" if rule.get("cr") else ", set by the admin")
    return f"at most {rule['max']}" + ("" if rule.get("cr") else ", set by the admin")
