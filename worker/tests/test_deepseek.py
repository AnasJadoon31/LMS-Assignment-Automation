import httpx
import pytest

from lms_worker.deepseek import DeepSeek, DeepSeekError
from lms_worker.kv import FileKV, SecretBox

KEY = "k" * 48
GOOD = "sk-0123456789abcdef0123456789abcdef"


def balance_api(request):
    if request.headers["authorization"] != f"Bearer {GOOD}":
        return httpx.Response(401, json={"error": {"message": "Authentication Fails"}})
    return httpx.Response(200, json={"is_available": True, "balance_infos": [
        {"currency": "USD", "total_balance": "12.40", "granted_balance": "0.00", "topped_up_balance": "12.40"}]})


def deepseek(tmp_path, env_key=None):
    return DeepSeek(FileKV(tmp_path / "kv"), SecretBox(KEY), env_key, transport=httpx.MockTransport(balance_api))


def test_a_key_is_checked_saved_encrypted_and_switched_on(tmp_path):
    ds = deepseek(tmp_path)
    with pytest.raises(DeepSeekError, match="does not look like"):
        ds.save_key("hello")
    with pytest.raises(DeepSeekError, match="does not accept"):
        ds.save_key("sk-wrongwrongwrongwrongwrong")
    with pytest.raises(DeepSeekError, match="Save a DeepSeek API key first"):
        ds.use("deepseek")
    assert ds.save_key(f" {GOOD}\n").text == "12.40 USD"
    assert GOOD.encode() not in (tmp_path / "kv" / "deepseek_key.json").read_bytes()
    assert not ds.active()  # a key alone does not switch solving over
    ds.use("deepseek")
    assert ds.active()
    env = ds.env()
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic" and env["ANTHROPIC_AUTH_TOKEN"] == GOOD
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash[1m]" and env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-v4-pro"
    assert env["ENABLE_TOOL_SEARCH"] == "false"  # DeepSeek refuses tool search's tool_definition blocks
    ds.remove_key()
    assert not ds.active() and ds.kv.get("solver_provider") is None  # back to Claude


def test_the_pages_key_wins_over_the_environment(tmp_path):
    ds = deepseek(tmp_path, env_key="sk-fromtheenvironment0000")
    assert (ds.key(), ds.source()) == ("sk-fromtheenvironment0000", "env")
    ds.save_key(GOOD)
    assert (ds.key(), ds.source()) == (GOOD, "page")
