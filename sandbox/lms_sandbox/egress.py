"""The solver sandbox's only way out: an HTTPS proxy that allows a short list of hosts.

Solver containers sit on an internal Docker network with no route out; this proxy is on that
network and the default one. It answers CONNECT for the allowed hosts and refuses the rest,
so code from an assignment cannot reach anything but Claude, claude.ai (the theme artifact)
and PyPI. Plain HTTP is refused.

Allowed by default: Anthropic's and Claude's own hosts and PyPI. EGRESS_ALLOW (comma
separated, "*.example.com" for subdomains) replaces the list.
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_ALLOW = ("anthropic.com", "*.anthropic.com", "claude.ai", "*.claude.ai", "claude.com", "*.claude.com",
                 "claudeusercontent.com", "*.claudeusercontent.com",  # artifact files
                 "pypi.org", "files.pythonhosted.org")


def allowed(host: str, rules: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for rule in rules:
        rule = rule.lower().strip()
        if rule.startswith("*.") and host.endswith(rule[1:]):
            return True
        if host == rule:
            return True
    return False


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter, rules: tuple[str, ...]) -> None:
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=30)
        method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
        if method.upper() != "CONNECT":
            client_w.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        host, _, port = target.rpartition(":")
        if not port.isdigit() or int(port) != 443 or not allowed(host, rules):
            log.info("Refused %s", target)
            client_w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=20)
        client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_w.drain()
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, OSError):
        pass
    finally:
        try:
            await client_w.drain()
            client_w.close()
        except Exception:
            pass


async def serve(host: str = "0.0.0.0", port: int = 3128, rules: tuple[str, ...] | None = None) -> None:
    env = os.environ.get("EGRESS_ALLOW", "")
    rules = rules or (tuple(r for r in env.split(",") if r.strip()) if env else DEFAULT_ALLOW)
    server = await asyncio.start_server(lambda r, w: _handle(r, w, rules), host, port)
    log.info("Egress proxy on %s:%s allowing %s", host, port, ", ".join(rules))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(port=int(os.environ.get("EGRESS_PORT", "3128"))))
