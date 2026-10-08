"""A real terminal for Operating Systems lab work, with screenshots of what it shows.

`lms-terminal` drives one tmux session running an interactive bash, so every command, every
program and every nano screen is the real thing; a screenshot is the session's screen as tmux
holds it (text, colours, cursor), drawn to a PNG. Nothing in a screenshot is typed by hand.

    lms-terminal start --dir ~/lab05            # a session in that folder (made if missing)
    lms-terminal nano fcfs.c --from /work/code/fcfs.c --shot fcfs_nano
                                                # write the file in nano (pasted, then ^O ^X) and
                                                # screenshot it in the editor before saving
    lms-terminal run "gcc fcfs.c -o fcfs" --shot fcfs_compile
    lms-terminal run "./fcfs" --shot fcfs_run   # waits for the prompt to come back
    lms-terminal type "5" --enter               # answer a program that is waiting for input
    lms-terminal keys C-c                       # tmux key names: Enter, C-c, C-d, Up, ...
    lms-terminal shot after_input               # the screen as it is now
    lms-terminal text                           # the screen as text, to read the real output
    lms-terminal clear
    lms-terminal stop

Screenshots go to /work/out/terminal/<name>.png. The prompt is bash's own user@host:folder$,
so it reads as the container's user and host name.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SESSION = "lab"
WORK = Path(os.environ.get("LMS_WORK", "/work"))
SHOTS = WORK / "out" / "terminal"
RC = Path("/tmp/lms-terminal.rc")
SIZE = Path("/tmp/lms-terminal.size")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_SIZE, PAD = 17, 12
MAX_ROWS, NANO_CHROME, MAX_PAGES = 46, 5, 6  # nano keeps 5 rows for its title, status and help lines

# Ubuntu's terminal colours
BG, FG = (48, 10, 36), (255, 255, 255)
BASE = [(46, 52, 54), (204, 0, 0), (78, 154, 6), (196, 160, 0), (52, 101, 164), (117, 80, 123), (6, 152, 154), (211, 215, 207),
        (85, 87, 83), (239, 41, 41), (138, 226, 52), (252, 233, 79), (114, 159, 207), (173, 127, 168), (52, 226, 226), (238, 238, 236)]


def tmux(*args: str, check: bool = True, text: str | None = None) -> str:
    r = subprocess.run(["tmux", "-f", "/dev/null", *args], capture_output=True, text=True, input=text)
    if check and r.returncode:
        raise SystemExit(f"tmux {' '.join(args[:2])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def running() -> bool:
    return subprocess.run(["tmux", "-f", "/dev/null", "has-session", "-t", SESSION], capture_output=True).returncode == 0


def need_session() -> None:
    if not running():
        raise SystemExit("No terminal session. Run: lms-terminal start --dir <folder>")


def size() -> tuple[int, int]:
    cols, rows = SIZE.read_text().split()
    return int(cols), int(rows)


def resize(cols: int, rows: int) -> None:
    tmux("resize-window", "-t", SESSION, "-x", str(cols), "-y", str(rows))


def settle(timeout: float = 5.0) -> str:
    """Wait until the screen stops changing; returns it."""
    last, same, end = None, 0, time.monotonic() + timeout
    while time.monotonic() < end:
        now = tmux("capture-pane", "-p", "-t", SESSION)
        same = same + 1 if now == last else 0
        if same >= 2:
            break
        last = now
        time.sleep(0.15)
    return last or ""


def wait_for_prompt(timeout: float) -> bool:
    """True once the shell is back at its prompt: nothing but bash is running in the pane."""
    end = time.monotonic() + timeout
    time.sleep(0.3)
    while time.monotonic() < end:
        if tmux("display-message", "-p", "-t", SESSION, "#{pane_current_command}").strip() == "bash":
            settle(2)
            return True
        time.sleep(0.2)
    return False


# --- drawing the screen ---

def _colour(n: int) -> tuple[int, int, int]:
    if n < 16:
        return BASE[n]
    if n < 232:
        n -= 16
        return tuple(0 if v == 0 else 55 + 40 * v for v in (n // 36, n // 6 % 6, n % 6))
    return (8 + 10 * (n - 232),) * 3


def parse(screen: str, cols: int, rows: int) -> list[list[tuple[str, tuple, tuple, bool]]]:
    """tmux's screen with escape codes -> rows of cells (character, foreground, background, bold)."""
    grid = []
    fg, bg, bold, reverse = None, None, False, False
    for line in screen.split("\n")[:rows]:
        cells, i = [], 0
        while i < len(line) and len(cells) < cols:
            if line[i] == "\x1b":
                m = re.match(r"\x1b\[([0-9;]*)m", line[i:])
                if m:
                    codes = [int(c) if c else 0 for c in m.group(1).split(";")] if m.group(1) else [0]
                    k = 0
                    while k < len(codes):
                        c = codes[k]
                        if c == 0:
                            fg, bg, bold, reverse = None, None, False, False
                        elif c == 1:
                            bold = True
                        elif c == 22:
                            bold = False
                        elif c == 7:
                            reverse = True
                        elif c == 27:
                            reverse = False
                        elif 30 <= c <= 37:
                            fg = BASE[c - 30]
                        elif 90 <= c <= 97:
                            fg = BASE[c - 82]
                        elif c == 39:
                            fg = None
                        elif 40 <= c <= 47:
                            bg = BASE[c - 40]
                        elif 100 <= c <= 107:
                            bg = BASE[c - 92]
                        elif c == 49:
                            bg = None
                        elif c in (38, 48) and k + 2 < len(codes) and codes[k + 1] == 5:
                            fg, bg = (_colour(codes[k + 2]), bg) if c == 38 else (fg, _colour(codes[k + 2]))
                            k += 2
                        elif c in (38, 48) and k + 4 < len(codes) and codes[k + 1] == 2:
                            rgb = tuple(codes[k + 2:k + 5])
                            fg, bg = (rgb, bg) if c == 38 else (fg, rgb)
                            k += 4
                        k += 1
                    i += m.end()
                    continue
                m = re.match(r"\x1b(\[[0-9;?]*[A-Za-z]|\][^\x07]*\x07|[()][A-Za-z0-9]|.)", line[i:])
                i += m.end() if m else 1
                continue
            f, b = (fg or FG), (bg or BG)
            if reverse:
                f, b = b, f
            if bold and fg is not None and fg in BASE[:8]:
                f = BASE[BASE.index(fg) + 8] if not reverse else f
            cells.append((line[i], f, b, bold))
            i += 1
        cells += [(" ", FG, BG, False)] * (cols - len(cells))
        grid.append(cells)
    grid += [[(" ", FG, BG, False)] * cols for _ in range(rows - len(grid))]
    return grid


def draw(path: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    cols, rows = size()
    screen = tmux("capture-pane", "-e", "-p", "-t", SESSION)
    grid = parse(screen.rstrip("\n"), cols, rows)
    font, bold = ImageFont.truetype(FONT, FONT_SIZE), ImageFont.truetype(FONT_BOLD, FONT_SIZE)
    cw = round(font.getlength("M"))
    ascent, descent = font.getmetrics()
    ch = ascent + descent + 2
    img = Image.new("RGB", (cols * cw + 2 * PAD, rows * ch + 2 * PAD), BG)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(grid):
        for x, (char, fg, bg, is_bold) in enumerate(row):
            left, top = PAD + x * cw, PAD + y * ch
            if bg != BG:
                d.rectangle([left, top, left + cw - 1, top + ch - 1], fill=bg)
            if char != " ":
                d.text((left, top + 1), char, font=bold if is_bold else font, fill=fg)
    cx, cy, shown = tmux("display-message", "-p", "-t", SESSION, "#{cursor_x} #{cursor_y} #{cursor_flag}").split()
    if shown == "1" and int(cx) < cols and int(cy) < rows:
        left, top = PAD + int(cx) * cw, PAD + int(cy) * ch
        char, fg, bg, is_bold = grid[int(cy)][int(cx)]
        d.rectangle([left, top, left + cw - 1, top + ch - 1], fill=FG)
        if char != " ":
            d.text((left, top + 1), char, font=bold if is_bold else font, fill=BG)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def shot(name: str) -> Path:
    settle(3)
    path = draw(SHOTS / (name if name.endswith(".png") else f"{name}.png"))
    print(f"screenshot: {path}")
    return path


# --- commands ---

def cmd_start(a) -> int:
    if running():
        tmux("kill-session", "-t", SESSION)
    folder = Path(a.dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    RC.write_text(
        "PS1='\\[\\e[1;32m\\]\\u@\\h\\[\\e[0m\\]:\\[\\e[1;34m\\]\\w\\[\\e[0m\\]\\$ '\n"
        "unset PROMPT_COMMAND HISTFILE\n"
        "alias ls='ls --color=auto' grep='grep --color=auto'\n"
        f"cd {str(folder)!r}\n"
    )
    SIZE.write_text(f"{a.cols} {a.rows}")
    env = {**os.environ, "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", SESSION, "-x", str(a.cols), "-y", str(a.rows),
                    f"bash --rcfile {RC} -i"], check=True, env=env)
    tmux("set-option", "-t", SESSION, "status", "off")
    tmux("set-option", "-g", "window-size", "manual", check=False)
    resize(a.cols, a.rows)
    wait_for_prompt(10)
    print(settle(2).rstrip())
    return 0


def cmd_run(a) -> int:
    need_session()
    tmux("send-keys", "-t", SESSION, "-l", a.command)
    tmux("send-keys", "-t", SESSION, "Enter")
    back = wait_for_prompt(a.wait)
    print(settle(2).rstrip())
    if a.shot:
        shot(a.shot)
    if not back:
        print(f"[still running after {a.wait:g} s: it may be waiting for input (lms-terminal type / keys), or needs --wait]",
              file=sys.stderr)
    return 0


def cmd_type(a) -> int:
    need_session()
    tmux("send-keys", "-t", SESSION, "-l", a.text)
    if a.enter:
        tmux("send-keys", "-t", SESSION, "Enter")
    print(settle(3).rstrip())
    if a.shot:
        shot(a.shot)
    return 0


def cmd_keys(a) -> int:
    need_session()
    tmux("send-keys", "-t", SESSION, *a.keys)
    print(settle(3).rstrip())
    return 0


def cmd_shot(a) -> int:
    need_session()
    shot(a.name)
    return 0


def cmd_text(a) -> int:
    need_session()
    print(settle(2).rstrip())
    return 0


def cmd_clear(a) -> int:
    need_session()
    tmux("send-keys", "-t", SESSION, "-l", "clear")
    tmux("send-keys", "-t", SESSION, "Enter")
    wait_for_prompt(5)
    tmux("clear-history", "-t", SESSION)
    return 0


def cmd_stop(a) -> int:
    if running():
        tmux("kill-session", "-t", SESSION)
    return 0


def cmd_nano(a) -> int:
    """Open nano on the file. With --from, the file is written in nano: its text is pasted in,
    screenshotted in the editor, then saved with ^O and closed with ^X."""
    need_session()
    cols, rows = size()
    name = a.shot or Path(a.file).stem + "_nano"
    source = Path(a.source).read_text() if a.source else None
    cwd = Path(tmux("display-message", "-p", "-t", SESSION, "#{pane_current_path}").strip())
    target = (cwd / a.file) if not os.path.isabs(a.file) else Path(a.file)
    if source is not None and target.exists():
        target.unlink()  # nano writes it afresh
    lines = (source if source is not None else target.read_text() if target.exists() else "").count("\n") + 1
    tall = min(max(rows, lines + NANO_CHROME), MAX_ROWS)
    if tall != rows:
        SIZE.write_text(f"{cols} {tall}")
        resize(cols, tall)
    try:
        tmux("send-keys", "-t", SESSION, "-l", f"nano {a.file}")
        tmux("send-keys", "-t", SESSION, "Enter")
        time.sleep(0.6)
        settle(3)
        if source is not None:
            tmux("load-buffer", "-b", "lms", "-", text=source.rstrip("\n"))
            tmux("paste-buffer", "-p", "-b", "lms", "-t", SESSION)
            settle(5)
            tmux("send-keys", "-t", SESSION, "M-\\")  # back to the first line
            settle(2)
        page = tall - NANO_CHROME
        for n in range(min(MAX_PAGES, max(1, -(-lines // page)))):
            if n:
                tmux("send-keys", "-t", SESSION, "C-v")  # next page
            shot(name if n == 0 else f"{name}_{n + 1}")
        if source is not None:
            tmux("send-keys", "-t", SESSION, "C-o")
            time.sleep(0.3)
            tmux("send-keys", "-t", SESSION, "Enter")
            time.sleep(0.3)
        tmux("send-keys", "-t", SESSION, "C-x")
        if not wait_for_prompt(5):  # nano asks to save a changed buffer: no
            tmux("send-keys", "-t", SESSION, "n")
            wait_for_prompt(5)
    finally:
        if tall != rows:
            SIZE.write_text(f"{cols} {rows}")
            resize(cols, rows)
            tmux("send-keys", "-t", SESSION, "-l", "clear")
            tmux("send-keys", "-t", SESSION, "Enter")
            wait_for_prompt(5)
    if source is not None:
        saved = target.read_text() if target.exists() else None
        if saved is None or saved.rstrip() != source.rstrip():
            print(f"nano did not save {target} as given; check the file and write it again", file=sys.stderr)
            return 1
        print(f"written in nano: {target} ({lines} lines)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lms-terminal", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="start the session in a folder")
    s.add_argument("--dir", default="~/lab")
    s.add_argument("--cols", type=int, default=100)
    s.add_argument("--rows", type=int, default=28)
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("run", help="type a command, press Enter, wait for the prompt")
    s.add_argument("command")
    s.add_argument("--wait", type=float, default=60)
    s.add_argument("--shot")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("nano", help="open a file in nano; with --from, write it there")
    s.add_argument("file")
    s.add_argument("--from", dest="source")
    s.add_argument("--shot")
    s.set_defaults(fn=cmd_nano)
    s = sub.add_parser("type", help="type text into whatever is running")
    s.add_argument("text")
    s.add_argument("--enter", action="store_true")
    s.add_argument("--shot")
    s.set_defaults(fn=cmd_type)
    s = sub.add_parser("keys", help="send keys by their tmux names")
    s.add_argument("keys", nargs="+")
    s.set_defaults(fn=cmd_keys)
    s = sub.add_parser("shot", help="screenshot the screen")
    s.add_argument("name")
    s.set_defaults(fn=cmd_shot)
    sub.add_parser("text", help="print the screen").set_defaults(fn=cmd_text)
    sub.add_parser("clear", help="clear the screen").set_defaults(fn=cmd_clear)
    sub.add_parser("stop", help="end the session").set_defaults(fn=cmd_stop)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
