import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sandbox"))
from lms_sandbox.egress import DEFAULT_ALLOW, _handle, allowed  # noqa: E402


def test_only_the_listed_hosts_get_through():
    assert allowed("api.anthropic.com", DEFAULT_ALLOW)
    assert allowed("claude.ai", DEFAULT_ALLOW) and allowed("files.pythonhosted.org", DEFAULT_ALLOW)
    assert not allowed("evil.com", DEFAULT_ALLOW)
    assert not allowed("claude.ai.evil.com", DEFAULT_ALLOW)
    assert not allowed("notanthropic.com", DEFAULT_ALLOW)
    assert not allowed("lms.bahria.edu.pk", DEFAULT_ALLOW)


def ask(request: bytes) -> bytes:
    async def go():
        server = await asyncio.start_server(lambda r, w: _handle(r, w, DEFAULT_ALLOW), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(request)
            await w.drain()
            answer = await asyncio.wait_for(r.read(200), timeout=5)
            w.close()
            return answer
    return asyncio.run(go())


def test_refusals():
    assert ask(b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com:443\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert ask(b"CONNECT claude.ai:22 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")  # only port 443
    assert ask(b"GET http://claude.ai/ HTTP/1.1\r\nHost: claude.ai\r\n\r\n").startswith(b"HTTP/1.1 405")  # no plain HTTP
