"""The sandbox's entry point: prepare the work folder, then run Claude Code on the job.

/work/job.json holds the prompts, the summary schema and the limits (written by the worker).
The question files in /work/in are rendered to page images first, so Claude sees figures in
the sheet. A job that asks for it ("text") also gets every file's text in /work/in/text, so
slides can be read as text, which costs far less than reading them as pictures. A job that
says "prepared" reuses what an earlier run in the same folder made. Claude's stream-json output goes to stdout, for the worker to read live, and to
/work/out/stream.jsonl.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WORK = Path(os.environ.get("LMS_WORK", "/work"))
OFFICE = {".doc", ".docx", ".ppt", ".pptx", ".odt", ".odp", ".rtf", ".xls", ".xlsx"}
MAX_PAGES = 40


def render_pages(inbox: Path, text: bool = False) -> list[Path]:
    """Each PDF and office file in inbox -> inbox/pages/<name>-<n>.png, and with text also
    inbox/text/<name>.txt (pdftotext; one form feed between pages)."""
    pages = inbox / "pages"
    made = []
    for f in sorted(p for p in inbox.iterdir() if p.is_file()):
        pdf = f if f.suffix.lower() == ".pdf" else None
        if f.suffix.lower() in OFFICE:
            tmp = Path(tempfile.mkdtemp(prefix="pages-"))
            subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--convert-to", "pdf",
                            "--outdir", str(tmp), str(f)], capture_output=True, timeout=180)
            converted = tmp / (f.stem + ".pdf")
            pdf = converted if converted.exists() else None
        if pdf is None:
            continue
        if text:
            (inbox / "text").mkdir(exist_ok=True)
            subprocess.run(["pdftotext", "-layout", str(pdf), str(inbox / "text" / (f.stem + ".txt"))],
                           capture_output=True, timeout=300)
        pages.mkdir(exist_ok=True)
        subprocess.run(["pdftoppm", "-r", "110", "-png", "-l", str(MAX_PAGES), str(pdf), str(pages / f.stem)],
                       capture_output=True, timeout=300)
        made += sorted(pages.glob(f"{f.stem}*.png"))
    return made


def claude_args(job: dict) -> list[str]:
    """Every Claude Code tool, the account's claude.ai connectors and skills, and no permission
    prompts (you chose full access): the container is the boundary.

    Two lighter kinds of run, to spend less of the plan: "plain" loads no connectors and no
    skills (a run that needs neither, like writing lecture notes); "lean" also swaps Claude
    Code's own long system prompt for the job's and keeps four tools (a short look by Haiku,
    which is then about a tenth of the tokens)."""
    args = ["claude", "-p", job["prompt"], "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions", "--max-turns", str(job.get("max_turns", 200))]
    lean = bool(job.get("lean"))
    if job.get("system"):
        args += ["--system-prompt" if lean else "--append-system-prompt", job["system"]]
    if lean or job.get("plain"):
        args += ["--strict-mcp-config", "--disable-slash-commands"]
    if lean:
        args += ["--tools", "Read,Glob,Grep,Bash"]
    if job.get("schema"):
        args += ["--json-schema", json.dumps(job["schema"])]
    if job.get("model"):
        args += ["--model", job["model"]]
    if job.get("effort"):
        args += ["--effort", job["effort"]]
    if job.get("resume"):
        args += ["--resume", job["resume"]]
    return args


def main() -> int:
    job = json.loads((WORK / "job.json").read_text())
    out = WORK / "out"
    out.mkdir(parents=True, exist_ok=True)
    (WORK / "code").mkdir(exist_ok=True)
    if (WORK / "in").is_dir() and not job.get("resume") and not job.get("prepared"):
        render_pages(WORK / "in", text=bool(job.get("text")))
    if not shutil.which("claude"):
        print(json.dumps({"type": "result", "is_error": True, "result": "claude is not installed in the sandbox"}), flush=True)
        return 127
    # `claude -p` counts as an SDK run, where the Artifact tool is off unless asked for
    env = {**os.environ, "CLAUDE_CODE_ARTIFACT": "1"}
    proc = subprocess.Popen(claude_args(job), cwd=WORK, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    with open(out / "stream.jsonl", "a") as log:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
    err = proc.stderr.read()
    if err:
        (out / "stderr.txt").write_text(err[-20000:])
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
