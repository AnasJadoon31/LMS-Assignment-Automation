"""Where a solver run happens.

DockerSandbox (production): a throwaway container from the sandbox image per run, reached
through the Docker socket proxy. The work folder goes in and comes back as tar archives, so no
host paths are shared. Claude runs under your claude.ai login, kept in a
Docker volume mounted as the solver's home: a full login, unlike a setup token, can read the
theme artifact, and the volume keeps the refreshed login between runs. Log in once with
the setup page (/setup). The container's only network is the sandbox
network, whose way out is the egress proxy. Limits: 2 CPUs, 4 GB memory, 512 processes and
the run's time limit.

LocalSandbox (development): the same entry script as a subprocess, with a clean environment
and the home folder of a logged-in Claude Code.

Both stream Claude's output line by line to `on_line`; returning False from it stops the run.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

SANDBOX_UID = 10001


@dataclass
class SandboxRun:
    exit_code: int | None
    timed_out: bool = False
    stopped: bool = False  # on_line asked to stop
    error: str | None = None


class Sandbox(Protocol):
    def run(self, work: Path, env: dict[str, str], on_line: Callable[[str], bool], timeout: float) -> SandboxRun: ...
    def work_path(self, work: Path) -> str: ...  # the work folder as the run sees it


class LocalSandbox:
    """Runs sandbox/lms_sandbox/run.py with `python` (which needs the sandbox's packages) and `claude` on PATH."""

    def __init__(self, python: str, sandbox_src: Path, path: str | None = None, passthrough: tuple[str, ...] = (),
                 home: Path | None = None):
        self.python = python
        self.src = sandbox_src
        self.path = path or os.environ.get("PATH", "/usr/bin:/bin")
        self.passthrough = passthrough  # e.g. proxy settings of a development machine
        self.home = home  # where Claude Code is logged in; a fresh empty home without one

    def work_path(self, work: Path) -> str:
        return str(work)

    def run(self, work: Path, env: dict[str, str], on_line: Callable[[str], bool], timeout: float) -> SandboxRun:
        home = self.home or Path(tempfile.mkdtemp(prefix="solver-home-"))
        clean = {"PATH": self.path, "HOME": str(home), "LMS_WORK": str(work), "PYTHONPATH": str(self.src),
                 "LMS_THEME_DIR": str(work / "theme"), "LMS_FIGURES_DIR": str(work / "out" / "figures"), "MPLBACKEND": "Agg",
                 **{k: os.environ[k] for k in self.passthrough if k in os.environ}, **env}
        proc = subprocess.Popen([self.python, "-m", "lms_sandbox.run"], cwd=work, env=clean,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        timed_out = threading.Event()

        def expire():
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, expire)
        timer.start()
        stopped = False
        try:
            for line in proc.stdout:
                if not on_line(line):
                    stopped = True
                    proc.kill()
                    break
        finally:
            timer.cancel()
        return SandboxRun(exit_code=proc.wait(), timed_out=timed_out.is_set(), stopped=stopped)


def _tar_dir(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        def owned(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = info.gid = SANDBOX_UID
            info.uname = info.gname = ""
            return info
        for child in sorted(root.iterdir()):
            tar.add(child, arcname=f"work/{child.name}", filter=owned)
    return buf.getvalue()


def _safe_extract(stream, dest: Path, strip: str) -> None:
    """Untar get_archive output into dest; regular files and folders only, never outside dest."""
    data = b"".join(stream)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            name = m.name[len(strip):].lstrip("/") if m.name.startswith(strip) else None
            if not name or not (m.isfile() or m.isdir()):
                continue
            target = (dest / name).resolve()
            if root not in target.parents and target != root:
                continue
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(target, "wb") as out:
                    out.write(src.read())


class DockerSandbox:
    def __init__(self, image: str, network: str | None, login_volume: str, *, docker_host: str | None = None,
                 cpus: float = 2.0, memory: str = "4g", pids: int = 512, client=None):
        if client is None:
            import docker  # only the worker in production needs it

            client = docker.DockerClient(base_url=docker_host) if docker_host else docker.from_env()
        self.client = client
        self.image, self.network, self.login_volume = image, network, login_volume
        self.cpus, self.memory, self.pids = cpus, memory, pids

    @classmethod
    def deployed(cls, image: str, network: str | None, login_volume: str | None, login_path: str,
                 *, docker_host: str | None = None, client=None) -> DockerSandbox:
        """Coolify renames a compose file's images, volumes and networks; find them as deployed."""
        box = cls(image, network, login_volume or "", docker_host=docker_host, client=client)
        box.image = _find_image(box.client, image)
        box.network = _find_network(box.client, network) if network else None
        box.login_volume = login_volume or _own_volume(box.client, login_path) or "lms-claude-home"
        return box

    def work_path(self, work: Path) -> str:
        return "/work"

    def run(self, work: Path, env: dict[str, str], on_line: Callable[[str], bool], timeout: float) -> SandboxRun:
        container = self.client.containers.create(
            self.image, command=["python3", "-m", "lms_sandbox.run"], environment=env, working_dir="/work",
            network=self.network, network_disabled=self.network is None, user=str(SANDBOX_UID),
            volumes={self.login_volume: {"bind": "/home/solver", "mode": "rw"}},
            nano_cpus=int(self.cpus * 1e9), mem_limit=self.memory, pids_limit=self.pids,
            cap_drop=["ALL"], security_opt=["no-new-privileges"], detach=True,
        )
        try:
            container.put_archive("/", _tar_dir(work))
            container.start()
            timed_out = threading.Event()

            def expire():
                timed_out.set()
                try:
                    container.kill()
                except Exception:
                    pass

            timer = threading.Timer(timeout, expire)
            timer.start()
            stopped = False
            buf = b""
            try:
                for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=False):
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not on_line(line.decode("utf-8", "replace")):
                            stopped = True
                            break
                    if stopped:
                        container.kill()
                        break
            finally:
                timer.cancel()
            result = container.wait(timeout=60)
            # The whole folder comes back (code, theme, out), so a later run can resume the session
            stream, _ = container.get_archive("/work")
            _safe_extract(stream, work, "work")
            return SandboxRun(exit_code=result.get("StatusCode"), timed_out=timed_out.is_set(), stopped=stopped)
        except Exception as e:  # docker errors: the job retries later
            return SandboxRun(exit_code=None, error=f"{type(e).__name__}: {e}")
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass


def _find_image(client, name: str) -> str:
    """The image by its name, else the one the compose file built for the egress service."""
    try:
        client.images.get(name)
        return name
    except Exception:
        pass
    base = name.split(":")[0]
    for img in client.images.list():
        for tag in img.tags:
            if tag.split(":")[0].endswith((base, "-egress", "_egress")):
                return tag
    return name  # the run fails with Docker's own message


def _find_network(client, name: str) -> str:
    """The sandbox network by its name, else the internal network whose name ends in 'sandbox'."""
    try:
        client.networks.get(name)
        return name
    except Exception:
        pass
    try:
        for net in client.networks.list():
            if net.attrs.get("Internal") and net.name.endswith(("sandbox", name)):
                return net.name
    except Exception:
        pass
    return name


def _own_volume(client, destination: str) -> str | None:
    """The volume mounted at destination in this container (the worker's own login volume),
    else in this stack's worker or api container, found by their compose labels."""
    import socket

    def mounted(container) -> str | None:
        for m in container.attrs.get("Mounts") or []:
            if m.get("Type") == "volume" and m.get("Destination") == destination:
                return m.get("Name")
        return None

    try:
        found = mounted(client.containers.get(socket.gethostname()))
        if found:
            return found
    except Exception:
        pass
    try:
        for service in ("worker", "api"):
            for c in client.containers.list(filters={"label": f"com.docker.compose.service={service}"}):
                found = mounted(c)
                if found:
                    return found
    except Exception:
        pass
    return None
