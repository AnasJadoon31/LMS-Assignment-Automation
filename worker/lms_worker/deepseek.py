"""Solving with DeepSeek instead of Claude: one switch on the setup page.

The solver stays Claude Code; it is pointed at DeepSeek's Anthropic-compatible API with your
DeepSeek API key, as DeepSeek documents (api-docs.deepseek.com, Claude Code integration).
Runs are billed per token to your DeepSeek balance. DeepSeek cannot read the claude.ai theme
artifact, so it needs a saved theme (the default one on the Courses page, or a course's own),
and it is billed per token, so none of the service's own limits (Claude's plan pauses, the daily
run cap) hold it up: your DeepSeek balance is the limit.

The key is saved encrypted on the setup page (or DEEPSEEK_API_KEY); both are read on every
run, so a change needs no restart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .kv import KeyValue, SecretBox

KEY_KEY = "deepseek_key"
PROVIDER_KEY = "solver_provider"  # "deepseek", or absent for Claude
BASE_URL = "https://api.deepseek.com"
KEY = re.compile(r"sk-[A-Za-z0-9_-]{16,200}")
MODEL = "deepseek-flash"
STRONG = "deepseek-v4-pro"  # what "Redo with Opus" gets

# As in DeepSeek's Claude Code guide; Opus names (Redo with Opus) go to the stronger model
ENV = {
    "ANTHROPIC_BASE_URL": f"{BASE_URL}/anthropic",
    "ANTHROPIC_MODEL": f"{MODEL}[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": STRONG,
    "ANTHROPIC_DEFAULT_SONNET_MODEL": f"{MODEL}[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
    "CLAUDE_CODE_SUBAGENT_MODEL": MODEL,
    "CLAUDE_CODE_EFFORT_LEVEL": "high",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
}


class DeepSeekError(Exception):
    pass


@dataclass(frozen=True)
class Balance:
    available: bool
    text: str  # e.g. "12.40 USD"


class DeepSeek:
    def __init__(self, kv: KeyValue | None, box: SecretBox | None, env_key: str | None = None, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.kv, self.box, self.env_key, self.transport = kv, box, env_key, transport

    @property
    def editable(self) -> bool:
        return self.kv is not None and self.box is not None

    def _saved(self) -> str | None:
        raw = self.kv.get(KEY_KEY) if self.editable else None
        try:
            return self.box.decrypt(raw) if raw else None
        except ValueError:
            return None

    def key(self) -> str | None:
        return self._saved() or self.env_key

    def source(self) -> str | None:
        """page, env, or None."""
        return "page" if self._saved() else "env" if self.env_key else None

    def active(self) -> bool:
        """DeepSeek solves: the switch is on and there is a key."""
        return bool(self.kv and self.kv.get(PROVIDER_KEY) == "deepseek" and self.key())

    def env(self) -> dict[str, str]:
        """What a solver run needs to go to DeepSeek."""
        return {**ENV, "ANTHROPIC_AUTH_TOKEN": self.key() or ""}

    def balance(self, key: str | None = None) -> Balance:
        key = key or self.key()
        if not key:
            raise DeepSeekError("No DeepSeek API key")
        try:
            with httpx.Client(transport=self.transport, timeout=10) as c:
                r = c.get(f"{BASE_URL}/user/balance", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as e:
            raise DeepSeekError(f"DeepSeek did not answer: {e}") from e
        if r.status_code == 401:
            raise DeepSeekError("DeepSeek does not accept this API key")
        if r.status_code != 200:
            raise DeepSeekError(f"DeepSeek answered {r.status_code}")
        d = r.json()
        infos = d.get("balance_infos") or []
        text = ", ".join(f"{i.get('total_balance')} {i.get('currency')}" for i in infos) or "unknown"
        return Balance(bool(d.get("is_available")), text)

    def save_key(self, key: str) -> Balance:
        key = "".join(key.split())
        if not KEY.fullmatch(key):
            raise DeepSeekError("That does not look like a DeepSeek API key (sk-…)")
        if not self.editable:
            raise DeepSeekError("Saving needs DATABASE_URL and APP_SECRET_KEY; set DEEPSEEK_API_KEY instead")
        balance = self.balance(key)  # a wrong key is refused here, not on the next solve
        self.kv.set(KEY_KEY, self.box.encrypt(key))
        return balance

    def remove_key(self) -> None:
        if self.kv:
            self.kv.delete(KEY_KEY)
            if not self.key():
                self.kv.delete(PROVIDER_KEY)  # nothing left to solve with: back to Claude

    def use(self, provider: str) -> None:
        if provider not in ("claude", "deepseek"):
            raise DeepSeekError("Choose claude or deepseek")
        if not self.kv:
            raise DeepSeekError("Switching needs DATABASE_URL")
        if provider == "deepseek" and not self.key():
            raise DeepSeekError("Save a DeepSeek API key first")
        if provider == "deepseek":
            self.kv.set(PROVIDER_KEY, "deepseek")
        else:
            self.kv.delete(PROVIDER_KEY)
