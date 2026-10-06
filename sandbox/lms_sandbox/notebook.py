"""Build the report's notebook, run it, and screenshot it in Jupyter Notebook the way the
submitted Lab 5 report shows it.

The notebook is named like "AI_Lab_05_First_Last.ipynb" and holds a title cell (the lab title,
then the student line), a heading cell per exercise, and the exercise's code cells. It is run
top to bottom in one kernel, so the cells count [1], [2], [3] ... through the whole notebook.

Screenshots are taken in Chromium at twice the pixel density with a 1180 x 640 window: the
notebook open in Jupyter Notebook (the first one), then every code cell on its own, with its
output under it when it has one.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError

VIEWPORT = {"width": 1180, "height": 640}
# Scroll the notebook's own scrolling container so code cell i sits just under the toolbar
SCROLL_TO_CELL = """(i) => {
    const cell = document.querySelectorAll('.jp-Notebook .jp-Cell.jp-CodeCell')[i];
    let box = cell.parentElement;
    while (box && !(box.scrollHeight > box.clientHeight && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) {
        box = box.parentElement;
    }
    if (box) box.scrollTop += cell.getBoundingClientRect().top - box.getBoundingClientRect().top - 8;
}"""
SCALE = 2
SMALL_WORDS = {"and", "of", "the", "for", "in", "to", "lab", "&"}


@dataclass
class Exercise:
    heading: str  # "Exercise 1: Greedy Best First Search"
    cells: list[tuple[str, str]]  # (caption, code)


@dataclass
class CellResult:
    exercise: int  # 0-based
    number: int  # execution count, 1-based through the notebook
    caption: str
    output: str
    error: str | None = None
    shot: str | None = None  # file name under the shots folder


@dataclass
class NotebookRun:
    path: Path
    cells: list[CellResult] = field(default_factory=list)
    overview_shot: str | None = None

    @property
    def failed(self) -> list[CellResult]:
        return [c for c in self.cells if c.error]


def notebook_name(course: str, kind: str, number: int, student: str) -> str:
    """'Artificial Intelligence', lab 5, 'First Last' -> 'AI_Lab_05_First_Last'."""
    words = [w for w in re.findall(r"[A-Za-z]+", course) if w.lower() not in SMALL_WORDS]
    initials = "".join(w[0].upper() for w in words) or "Course"
    who = "_".join(re.findall(r"[A-Za-z0-9]+", student)) or "Student"
    return f"{initials}_{'Lab' if kind == 'lab' else 'Assignment'}_{number:02d}_{who}"


def build(path: Path, title: str, student_line: str, exercises: list[Exercise]) -> nbformat.NotebookNode:
    """student_line: '**Name** | enrollment | class | course'."""
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"display_name": "Python 3 (ipykernel)", "language": "python", "name": "python3"}
    nb.metadata["language_info"] = {"name": "python"}
    nb.cells.append(nbformat.v4.new_markdown_cell(f"# {title}\n\n{student_line}"))
    for ex in exercises:
        nb.cells.append(nbformat.v4.new_markdown_cell(f"## {ex.heading}"))
        for caption, code in ex.cells:
            cell = nbformat.v4.new_code_cell(code)
            cell.metadata["caption"] = caption
            nb.cells.append(cell)
    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, str(path))
    return nb


def _text(outputs) -> str:
    parts = []
    for o in outputs:
        if o.get("output_type") == "stream":
            parts.append(o.get("text", ""))
        elif o.get("output_type") in ("execute_result", "display_data"):
            parts.append(o.get("data", {}).get("text/plain", ""))
            if not str(parts[-1]).endswith("\n"):
                parts[-1] = str(parts[-1]) + "\n"
        elif o.get("output_type") == "error":
            parts.append("\n".join(o.get("traceback", [])))
    return re.sub(r"\x1b\[[0-9;]*m", "", "".join(str(p) for p in parts))


def execute(path: Path, cwd: Path, *, cell_timeout: int = 120) -> NotebookRun:
    """Run every cell in one kernel and save the outputs into the notebook."""
    nb = nbformat.read(str(path), as_version=4)
    client = NotebookClient(nb, timeout=cell_timeout, kernel_name="python3", allow_errors=True,
                            resources={"metadata": {"path": str(cwd)}})
    client.execute()
    nbformat.write(nb, str(path))
    subprocess.run([sys.executable, "-m", "jupyter", "trust", str(path)], capture_output=True, check=False)
    run = NotebookRun(path=path)
    exercise = -1
    for cell in nb.cells:
        if cell.cell_type == "markdown" and cell.source.startswith("## "):
            exercise += 1
        if cell.cell_type != "code":
            continue
        errors = [o for o in cell.get("outputs", []) if o.get("output_type") == "error"]
        run.cells.append(CellResult(
            exercise=max(exercise, 0),
            number=cell.get("execution_count") or len(run.cells) + 1,
            caption=cell.metadata.get("caption", ""),
            output=_text(cell.get("outputs", [])),
            error=f"{errors[0].get('ename')}: {errors[0].get('evalue')}" if errors else None,
        ))
    return run


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def jupyter_server(root: Path):
    """Jupyter Notebook on localhost with no token, every cell rendered (no windowing), no news popups."""
    port = _free_port()
    home = Path(tempfile.mkdtemp(prefix="jupyter-"))
    settings = home / "user-settings"
    for name, value in (
        ("@jupyterlab/notebook-extension/tracker.jupyterlab-settings", {"windowingMode": "none"}),
        ("@jupyterlab/apputils-extension/notification.jupyterlab-settings", {"checkForUpdates": False, "fetchNews": "false"}),
    ):
        f = settings / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(value))
    env = {**os.environ, "JUPYTERLAB_SETTINGS_DIR": str(settings), "JUPYTER_CONFIG_DIR": str(home / "config"),
           "JUPYTER_RUNTIME_DIR": str(home / "runtime")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "jupyter", "notebook", "--no-browser", "--ip=127.0.0.1", f"--port={port}",
         f"--ServerApp.root_dir={root}", "--IdentityProvider.token=", "--ServerApp.password=",
         "--ServerApp.disable_check_xsrf=True", "--ServerApp.open_browser=False"]
        + (["--allow-root"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"{base}/api/status", timeout=1)
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("Jupyter did not start: " + proc.stderr.read().decode()[-500:])
                time.sleep(0.5)
        else:
            raise RuntimeError("Jupyter did not answer within 60 s")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def screenshot(run: NotebookRun, shots: Path, *, browser_path: str | None = None) -> NotebookRun:
    """The notebook open in Jupyter Notebook, then each code cell with its output."""
    from playwright.sync_api import sync_playwright

    shots.mkdir(parents=True, exist_ok=True)
    root = run.path.parent
    with jupyter_server(root) as base, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=browser_path) if browser_path else pw.chromium.launch()
        # A checkpoint, so the header shows "Last Checkpoint"
        urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/contents/{run.path.name}/checkpoints", method="POST", data=b""), timeout=10)
        page = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE).new_page()
        page.goto(f"{base}/notebooks/{run.path.name}", wait_until="networkidle")
        page.wait_for_selector(".jp-Notebook .jp-CodeCell", timeout=60_000)
        # Wait for the kernel to report idle, so the toolbar shows the kernel as in the report
        try:
            page.wait_for_selector(".jp-Notebook-ExecutionIndicator[data-status='idle']", timeout=30_000)
        except Exception:
            pass
        cells = page.locator(".jp-Notebook .jp-Cell.jp-CodeCell")
        # As after running all cells: the last cell selected, the view back at the top
        cells.last.locator(".jp-InputPrompt").click()
        page.keyboard.press("Escape")
        page.evaluate("""() => { for (const sel of ['.jp-WindowedPanel-outer', '.jp-NotebookPanel-notebook', '.jp-Notebook']) {
            const el = document.querySelector(sel); if (el) el.scrollTop = 0; } window.scrollTo(0, 0); }""")
        page.wait_for_timeout(500)
        heading = page.locator(".jp-MarkdownCell h2").first
        if heading.count():
            heading.hover()
        page.wait_for_timeout(1000)
        page.screenshot(path=str(shots / "notebook.png"))
        run.overview_shot = "notebook.png"
        page.mouse.move(VIEWPORT["width"] - 2, 2)
        for i, result in enumerate(run.cells):
            name = f"cell_{result.number}.png"
            cell = cells.nth(i)
            # A window tall enough for the whole cell under the toolbar; only the notebook scrolls
            height = cell.evaluate("el => el.getBoundingClientRect().height")
            page.set_viewport_size({"width": VIEWPORT["width"], "height": max(VIEWPORT["height"], int(height) + 400)})
            page.evaluate(SCROLL_TO_CELL, i)
            page.wait_for_timeout(200)
            cell.screenshot(path=str(shots / name), animations="disabled")
            result.shot = name
        browser.close()
    return run
