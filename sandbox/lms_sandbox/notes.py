"""lms-notes: study notes written in Markdown -> a clean PDF in one fixed layout.

    lms-notes notes.md [out.pdf]

Claude writes only the content; the look is here, so no time is spent on layout. Markdown with
tables, fenced code and images (paths relative to the Markdown file); maths as $...$ and
$$...$$ in LaTeX, shown as MathML. The first "# " heading is the title. Prints the page count.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font: 10.5pt/1.5 Carlito, Calibri, Arial, sans-serif; color: #1c2733; }
h1 { font-size: 22pt; margin: 0 0 4pt; color: #1f4e79; border-bottom: 2.5pt solid #1f4e79; padding-bottom: 6pt; }
h2 { font-size: 15pt; color: #1f4e79; margin: 18pt 0 6pt; border-bottom: 0.75pt solid #c9d3de; padding-bottom: 3pt; break-after: avoid; }
h3 { font-size: 12pt; color: #2b3a4b; margin: 12pt 0 4pt; break-after: avoid; }
p { margin: 0 0 6pt; } ul, ol { margin: 0 0 6pt; padding-left: 18pt; } li { margin-bottom: 2pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9.5pt; break-inside: avoid; }
th { background: #1f4e79; color: #fff; text-align: left; } th, td { border: 0.5pt solid #c9d3de; padding: 4pt 6pt; vertical-align: top; }
tr:nth-child(even) td { background: #f3f6fa; }
code { font: 9pt "DejaVu Sans Mono", Consolas, monospace; background: #f0f3f7; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f0f3f7; border-left: 2.5pt solid #1f4e79; padding: 6pt 8pt; white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; }
blockquote { margin: 6pt 0; padding: 6pt 10pt; background: #eef6fc; border-left: 2.5pt solid #3a87c8; break-inside: avoid; }
blockquote p:last-child { margin-bottom: 0; }
img { max-width: 100%; max-height: 110mm; display: block; margin: 6pt auto; }
math[display="block"] { margin: 6pt 0; }
"""
FOOTER = ('<div style="font: 8pt Arial; color: #5b6876; width: 100%; padding: 0 16mm; display: flex; justify-content: space-between">'
          '<span class="title"></span><span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>')


def _maths(text: str) -> tuple[str, list[str]]:
    """Take $$...$$ and $...$ out before Markdown sees them; they come back as MathML."""
    from latex2mathml.converter import convert

    held: list[str] = []

    def hold(m: re.Match) -> str:
        display = m.group(0).startswith("$$")
        try:
            held.append(convert(m.group(1).strip(), display="block" if display else "inline"))
        except Exception:  # an expression it cannot read is shown as written
            held.append(f"<code>{html.escape(m.group(1).strip())}</code>")
        return f"@@MATH{len(held) - 1}@@"

    parts = re.split(r"(```.*?```|`[^`\n]*`)", text, flags=re.S)  # leave code alone
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"\$\$(.+?)\$\$", hold, parts[i], flags=re.S)
        parts[i] = re.sub(r"(?<![\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])", hold, parts[i])
    return "".join(parts), held


def build(source: Path, out: Path) -> int:
    import markdown
    from playwright.sync_api import sync_playwright

    text, held = _maths(source.read_text())
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    body = re.sub(r"@@MATH(\d+)@@", lambda m: held[int(m.group(1))], body)
    title = re.search(r"^# +(.+)$", text, flags=re.M)
    page_html = (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title.group(1) if title else 'Notes')}</title>"
                 f"<style>{CSS}</style>{body}")
    staged = source.with_suffix(".html")
    staged.write_text(page_html)
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(staged.resolve().as_uri())  # as a file, so relative image paths work
            page.pdf(path=str(out), format="A4", print_background=True, display_header_footer=True,
                     header_template="<span></span>", footer_template=FOOTER,
                     margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
        finally:
            browser.close()
    info = subprocess.run(["pdfinfo", str(out)], capture_output=True, text=True).stdout
    pages = re.search(r"Pages:\s+(\d+)", info)
    return int(pages.group(1)) if pages else 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else source.parent / "out" / "notes.pdf"
    pages = build(source, out)
    print(f"{out}: {pages} page(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
