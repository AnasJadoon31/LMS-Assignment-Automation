"""Turn a report into a .docx laid out like the submitted Lab 5 report, then a PDF.

Every size, colour and spacing below was measured from that report (see
lms_worker/theme/README.md, "Report layout"); colours come from the theme's tokens.json.
Sizes are in Word units: twips (1/20 pt) for spacing and widths, half-points for font
sizes, eighths of a point for borders, EMU for images.

Cover section: page border, logo, university, campus, department over a short gold rule,
the navy title band, the details table. Body section: header and footer, then per
exercise the banner, the Task and Result box, the agent's sections, and after a page
break the Code, the Jupyter Notebook Screenshots and the Output.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor, Twips
from pygments.lexers import get_lexer_for_filename
from pygments.token import Comment, Keyword, Name, Operator, String
from pygments.util import ClassNotFound

from .content import Exercise, Report

THEME = resources.files("lms_worker") / "theme"

# Page: A4, measured margins
PAGE_W, PAGE_H = 11906, 16838
MARGIN_SIDE = 1220
TEXT_W = PAGE_W - 2 * MARGIN_SIDE  # 9466
SERIF, SANS, MONO = "Cambria", "Calibri", "Consolas"
# Code colours of the submitted report (not in the theme tokens)
CODE_COMMENT, CODE_STRING = "2E7D4F", "B3261E"
COVER_LOGO = (Emu(2209800), Emu(1990725))
HEADER_LOGO = (Emu(323850), Emu(295275))
SHOT_W = Emu(5867400)
FIGURE_W = Emu(4953000)
WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
# Code starts on a new page when everything before it fills less than this much of one page
# (the report's exercise 1); after a longer opening it follows on (its exercise 2)
BODY_H_PT = (PAGE_H - 1500 - 1200) / 20
CODE_BREAK_FILL = 0.9


@dataclass(frozen=True)
class Student:
    name: str
    enrollment: str
    klass: str
    university: str = "Bahria University"
    campus: str = "Karachi Campus"
    department: str = "Department of Computer Science"
    semester: str = ""  # "Semester 05"; worked out from the class when empty

    @property
    def semester_label(self) -> str:
        if self.semester:
            return self.semester
        m = re.search(r"(\d+)\s*-?\s*[A-Z]?\s*$", self.klass) or re.search(r"\b(\d+)", self.klass)
        return f"Semester {int(m[1]):02d}" if m else ""


@dataclass(frozen=True)
class CourseInfo:
    name: str  # as on the sheet, e.g. "Artificial Intelligence"
    code: str = ""


@dataclass(frozen=True)
class Theme:
    colors: dict[str, str]  # hex without '#'
    version: int
    logo: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Theme:
        root = Path(root) if root else Path(str(THEME))
        tokens = json.loads((root / "tokens.json").read_text())
        meta = json.loads((root / "theme.json").read_text())
        colors = {t["name"]: _hex(t["value"]) for t in tokens["color"]["tokens"]}
        missing = {"navy", "sky", "sky-soft", "gold", "ink", "muted", "rule", "paper"} - colors.keys()
        if missing:
            raise ValueError(f"Theme tokens are missing {sorted(missing)}")
        return cls(colors=colors, version=int(meta.get("theme_version", 1)), logo=root / meta["logo"])


def _hex(value) -> str:
    v = (value["light"] if isinstance(value, dict) else value).lstrip("#")
    return (v if len(v) == 6 else "".join(c * 2 for c in v)).upper()


# --- XML helpers; element order follows the WordprocessingML schema ---

P_ORDER = ["w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr", "w:widowControl", "w:numPr",
           "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
           "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
           "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
           "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
           "w:sectPr", "w:pPrChange"]
R_ORDER = ["w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps", "w:strike", "w:dstrike",
           "w:outline", "w:shadow", "w:emboss", "w:imprint", "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden",
           "w:color", "w:spacing", "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect",
           "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout",
           "w:specVanish", "w:oMath"]
TC_ORDER = ["w:cnfStyle", "w:tcW", "w:gridSpan", "w:hMerge", "w:vMerge", "w:tcBorders", "w:shd", "w:noWrap", "w:tcMar",
            "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark"]
TBL_ORDER = ["w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual", "w:tblStyleRowBandSize", "w:tblStyleColBandSize",
             "w:tblW", "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar",
             "w:tblLook"]


def _put(parent, el, order: list[str]) -> None:
    """Insert el into a properties element at its schema position, replacing any same tag."""
    tag = el.tag
    for old in parent.findall(tag):
        parent.remove(old)
    name = "w:" + tag.split("}")[1]
    after = order[order.index(name) + 1:] if name in order else []
    parent.insert_element_before(el, *after)


def _el(tag: str, **attrs) -> OxmlElement:
    el = OxmlElement(tag)
    for k, v in attrs.items():
        el.set(qn(f"w:{k}"), str(v))
    return el


def _borders(tag: str, sides: dict[str, tuple | None]) -> OxmlElement:
    """sides: name -> (color, size in eighths, space) or None for no line."""
    box = OxmlElement(tag)
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        if side not in sides:
            continue
        spec = sides[side]
        if spec is None:
            box.append(_el(f"w:{side}", val="none", sz=0, space=0, color="FFFFFF"))
        else:
            color, size, space = (spec + (0,))[:3]
            box.append(_el(f"w:{side}", val="single", sz=size, space=space, color=color))
    return box


def _box(color: str, size: int, space: int = 0) -> dict:
    return {s: (color, size, space) for s in ("top", "left", "bottom", "right")}


def _shd(fill: str) -> OxmlElement:
    return _el("w:shd", val="clear", color="auto", fill=fill)


class Doc:
    """The document being built, with the report's paragraph and table primitives."""

    def __init__(self, theme: Theme):
        self.t = theme
        self.c = theme.colors
        self.doc = Document()
        self._defaults()
        self._compat()
        self.figures = self.tables = self.shots = 0

    def _compat(self) -> None:
        """Word 2013+ layout rules, as in the submitted report; LibreOffice spaces lines differently under the
        template's Word 2010 mode."""
        settings = self.doc.settings.element
        for old in settings.findall(qn("w:compat")):
            settings.remove(old)
        compat = OxmlElement("w:compat")
        compat.append(_el("w:compatSetting", name="compatibilityMode", uri="http://schemas.microsoft.com/office/word", val=15))
        settings.insert_element_before(compat, "w:docVars", "w:rsids", "m:mathPr", "w:attachedSchema", "w:themeFontLang",
                                       "w:clrSchemeMapping", "w:doNotIncludeSubdocsInStats", "w:doNotAutoCompressPictures",
                                       "w:forceUpgrade", "w:captions", "w:readModeInkLockDown", "w:smartTagType",
                                       "sl:schemaLibrary", "w:shapeDefaults", "w:doNotEmbedSmartTags",
                                       "w:decimalSymbol", "w:listSeparator")

    def _defaults(self) -> None:
        styles = self.doc.styles.element
        defaults = styles.find(qn("w:docDefaults"))
        if defaults is not None:
            styles.remove(defaults)
        defaults = OxmlElement("w:docDefaults")
        rpr_default, rpr = OxmlElement("w:rPrDefault"), OxmlElement("w:rPr")
        fonts = _el("w:rFonts", ascii=SANS, hAnsi=SANS, eastAsia=SANS, cs=SANS)
        for el in (fonts, _el("w:color", val=self.c["ink"]), _el("w:sz", val=22), _el("w:szCs", val=22)):
            rpr.append(el)
        rpr_default.append(rpr)
        defaults.append(rpr_default)
        defaults.append(OxmlElement("w:pPrDefault"))
        styles.insert(0, defaults)
        for name, size in (("Normal", None), ("Heading 1", 34), ("Heading 2", 26)):
            st = self.doc.styles[name]
            pf = st.paragraph_format
            pf.space_before = pf.space_after = Pt(0)
            st.font.name = SERIF if size else SANS
            st.font.color.rgb = RGBColor.from_string(self.c["navy"] if size else self.c["ink"])
            st.font.size = Pt(size / 2) if size else Pt(11)
            st.font.bold = bool(size)
            st.font.italic = False
            r_fonts = st.element.get_or_add_rPr().get_or_add_rFonts()
            for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
                r_fonts.attrib.pop(qn(f"w:{attr}"), None)

    # --- paragraphs and runs ---

    def para(self, container=None, *, before=0, after=0, line: int | None = None, jc: str | None = None,
             ind: tuple[int, int] | None = None, keep_next=False, page_break=False, style: str | None = None,
             borders: dict | None = None, fill: str | None = None):
        container = container if container is not None else self.doc
        p = container.add_paragraph(style=style)
        ppr = p._p.get_or_add_pPr()
        if keep_next:
            _put(ppr, OxmlElement("w:keepNext"), P_ORDER)
        if page_break:
            _put(ppr, OxmlElement("w:pageBreakBefore"), P_ORDER)
        if borders:
            _put(ppr, _borders("w:pBdr", borders), P_ORDER)
        if fill:
            _put(ppr, _shd(fill), P_ORDER)
        spacing = _el("w:spacing", before=before, after=after)
        if line is not None:  # no lineRule, exactly as the submitted report writes it
            spacing.set(qn("w:line"), str(line))
        _put(ppr, spacing, P_ORDER)
        if ind:
            _put(ppr, _el("w:ind", left=ind[0], right=ind[1]), P_ORDER)
        if jc:
            _put(ppr, _el("w:jc", val=jc), P_ORDER)
        return p

    def run(self, p, text: str, *, font=SANS, size=22, color: str | None = None, bold=False, italic=False,
            caps=False, spacing: int | None = None):
        r = p.add_run(text)
        rpr = r._r.get_or_add_rPr()
        _put(rpr, _el("w:rFonts", ascii=font, hAnsi=font, eastAsia=font, cs=font), R_ORDER)
        if bold:
            _put(rpr, OxmlElement("w:b"), R_ORDER)
            _put(rpr, OxmlElement("w:bCs"), R_ORDER)
        if italic:
            _put(rpr, OxmlElement("w:i"), R_ORDER)
            _put(rpr, OxmlElement("w:iCs"), R_ORDER)
        if caps:
            _put(rpr, OxmlElement("w:caps"), R_ORDER)
        _put(rpr, _el("w:color", val=color or self.c["ink"]), R_ORDER)
        if spacing:
            _put(rpr, _el("w:spacing", val=spacing), R_ORDER)
        _put(rpr, _el("w:sz", val=size), R_ORDER)
        _put(rpr, _el("w:szCs", val=size), R_ORDER)
        return r

    def rich(self, p, text: str, *, size=22, strong_size: int | None = None):
        """Text with **strong** parts in bold navy."""
        for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
            if not part:
                continue
            if i % 2:
                self.run(p, part, size=strong_size or size, color=self.c["navy"], bold=True)
            else:
                self.run(p, part, size=size)

    def spacer(self, after: int) -> None:
        """A 1 pt line: the gap is the space after it."""
        p = self.para(after=after, line=20)
        r = self.run(p, "", size=2)
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        r._r.append(t)
        mark = OxmlElement("w:rPr")
        mark.append(_el("w:sz", val=2))
        mark.append(_el("w:szCs", val=2))
        _put(p._p.get_or_add_pPr(), mark, P_ORDER)

    def picture(self, p, path: Path, width: int, height: int | None = None) -> None:
        if height is None:
            from docx.image.image import Image
            img = Image.from_file(str(path))
            height = int(width * img.px_height / img.px_width)
        p.add_run().add_picture(str(path), width=Emu(width), height=Emu(height))

    # --- tables ---

    def table(self, widths: list[int], rows: int, *, center=False):
        t = self.doc.add_table(rows=rows, cols=len(widths))
        t.autofit = False
        tbl_pr = t._tbl.tblPr
        _put(tbl_pr, _el("w:tblW", type="dxa", w=sum(widths)), TBL_ORDER)
        if center:
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
        _put(tbl_pr, _borders("w:tblBorders", {s: None for s in ("top", "left", "bottom", "right", "insideH", "insideV")}), TBL_ORDER)
        _put(tbl_pr, _el("w:tblLayout", type="fixed"), TBL_ORDER)
        for col, w in zip(t._tbl.tblGrid.findall(qn("w:gridCol")), widths):
            col.set(qn("w:w"), str(w))
        for row in t.rows:
            for cell, w in zip(row.cells, widths):
                cell.width = Twips(w)
        return t

    def cell(self, cell, *, fill: str | None = None, borders: dict | None = None, margins=(0, 0, 0, 0), v_center=True):
        tc_pr = cell._tc.get_or_add_tcPr()
        if borders:
            _put(tc_pr, _borders("w:tcBorders", borders), TC_ORDER)
        if fill:
            _put(tc_pr, _shd(fill), TC_ORDER)
        mar = OxmlElement("w:tcMar")
        for side, v in zip(("top", "left", "bottom", "right"), margins):
            mar.append(_el(f"w:{side}", w=v, type="dxa"))
        _put(tc_pr, mar, TC_ORDER)
        if v_center:
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        cell._first = True
        return cell

    def cell_para(self, cell, **kw):
        """The cell's first paragraph is reused, later calls add one."""
        if getattr(cell, "_first", False):
            cell._first = False
            p = cell.paragraphs[0]
            p._p.getparent().remove(p._p)
        return self.para(cell, **kw)

    @staticmethod
    def row_props(row, *, cant_split=True, header=False, height: int | None = None):
        tr_pr = row._tr.get_or_add_trPr()
        if cant_split:
            tr_pr.append(OxmlElement("w:cantSplit"))
        if height:
            tr_pr.append(_el("w:trHeight", val=height, hRule="atLeast"))
        if header:
            tr_pr.append(OxmlElement("w:tblHeader"))

    def caption(self, kind: str, text: str | None) -> None:
        if kind == "Figure":
            self.figures += 1
            n = self.figures
        elif kind == "Table":
            self.tables += 1
            n = self.tables
        else:
            self.shots += 1
            n = self.shots
        p = self.para(before=80, after=160, jc="center")
        label = f"{kind} {n}: {text}" if text else f"{kind} {n}"
        self.run(p, label if label.endswith(".") else label + ".", size=18, color=self.c["muted"], italic=True)


class Renderer:
    def __init__(self, theme: Theme | None = None):
        self.t = theme or Theme.load()

    def docx(self, report: Report, student: Student, course: CourseInfo, on: date, dest: Path,
             files_dir: Path | None = None) -> Path:
        """files_dir holds the figures and screenshots the report names."""
        d = Doc(self.t)
        c = self.t.colors
        doc = d.doc
        course_line = " ".join(filter(None, [course.code or report.course_code, course.name]))
        doc.core_properties.title = f"{report.label}: {report.title}"
        doc.core_properties.author = student.name

        self._cover(d, report, student, course_line, on)

        body = doc.add_section(WD_SECTION.NEW_PAGE)
        self._page(body, top=1500, bottom=1200, header=560, footer=520)
        sect_pr = body._sectPr
        for borders in sect_pr.findall(qn("w:pgBorders")):  # the border is the cover's only
            sect_pr.remove(borders)
        pg_num = sect_pr.find(qn("w:pgNumType"))
        if pg_num is None:
            pg_num = OxmlElement("w:pgNumType")
            sect_pr.append(pg_num)
        pg_num.set(qn("w:start"), "1")
        self._header_footer(d, body, student, course_line, report)

        for i, ex in enumerate(report.exercises, 1):
            if i > 1:
                d.para(after=0, line=20, page_break=True)
            self._exercise(d, i, ex, files_dir)

        dest.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(dest))
        return dest

    # --- pages ---

    @staticmethod
    def _page(section, *, top: int, bottom: int, header: int = 708, footer: int = 708) -> None:
        section.page_width, section.page_height = Twips(PAGE_W), Twips(PAGE_H)
        section.left_margin = section.right_margin = Twips(MARGIN_SIDE)
        section.top_margin, section.bottom_margin = Twips(top), Twips(bottom)
        section.header_distance, section.footer_distance = Twips(header), Twips(footer)

    def _cover(self, d: Doc, report: Report, student: Student, course_line: str, on: date) -> None:
        c = self.t.colors
        cover = d.doc.sections[0]
        self._page(cover, top=1500, bottom=1300)
        borders = OxmlElement("w:pgBorders")
        for k, v in (("display", "allPages"), ("offsetFrom", "page"), ("zOrder", "front")):
            borders.set(qn(f"w:{k}"), v)
        for side in ("top", "left", "bottom", "right"):
            borders.append(_el(f"w:{side}", val="single", color=c["navy"], sz=18, space=24))
        pg_mar = cover._sectPr.find(qn("w:pgMar"))
        pg_mar.addnext(borders)

        p = d.para(before=500, after=260, jc="center")
        d.picture(p, self.t.logo, *COVER_LOGO)
        p = d.para(after=40, jc="center")
        d.run(p, student.university.upper(), font=SERIF, size=52, color=c["navy"], bold=True, spacing=60)
        p = d.para(after=60, jc="center")
        d.run(p, student.campus.upper(), size=24, color=c["muted"], spacing=120)
        p = d.para(after=200, jc="center", ind=(2900, 2900), borders={"bottom": (c["gold"], 24, 14)})
        d.run(p, student.department, size=26)
        d.spacer(700)

        band = d.table([TEXT_W], 1)
        cell = d.cell(band.rows[0].cells[0], fill=c["navy"], margins=(380, 300, 380, 300),
                      borders={"top": (c["gold"], 36), "left": None, "bottom": (c["gold"], 36), "right": None})
        kind = "Lab Report" if report.kind == "lab" else "Assignment"
        p = d.cell_para(cell, after=120, jc="center")
        d.run(p, f"{kind} {report.number:02d}".upper(), size=22, color=c["gold"], bold=True, spacing=120)
        p = d.cell_para(cell, after=120, jc="center")
        d.run(p, report.title, font=SERIF, size=60, color=c["paper"], bold=True)
        p = d.cell_para(cell, jc="center")
        d.run(p, "   |   ".join(filter(None, [course_line, student.semester_label])), size=23, color=c["sky"])
        d.spacer(900)

        rows = [("Submitted by", student.name, True), ("Enrollment", student.enrollment, False),
                ("Class", student.klass, False), ("Course", course_line, False), ("Date", f"{on.day} {on:%B %Y}", False)]
        details = d.table([2500, 4300], len(rows), center=True)
        edge = {"top": (c["paper"], 6), "left": None, "bottom": (c["paper"], 6), "right": None}
        for row, (label, value, strong) in zip(details.rows, rows):
            d.row_props(row, height=520)
            left = d.cell(row.cells[0], fill=c["navy"], borders=edge, margins=(60, 240, 60, 120))
            d.run(d.cell_para(left), label, size=18, color=c["paper"], bold=True, caps=True, spacing=30)
            right = d.cell(row.cells[1], fill=c["sky"], borders=edge, margins=(60, 260, 60, 120))
            d.run(d.cell_para(right), value, size=24, bold=strong)

    def _header_footer(self, d: Doc, section, student: Student, course_line: str, report: Report) -> None:
        c = self.t.colors
        section.header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False
        hp = section.header.paragraphs[0]
        ppr = hp._p.get_or_add_pPr()
        for st in ppr.findall(qn("w:pStyle")):  # the template's Header style brings its own tab stops
            ppr.remove(st)
        _put(ppr, _borders("w:pBdr", {"bottom": (c["navy"], 12, 6)}), P_ORDER)
        hp.paragraph_format.tab_stops.add_tab_stop(Twips(TEXT_W), WD_TAB_ALIGNMENT.RIGHT)
        _put(ppr, _el("w:spacing", after=0), P_ORDER)
        d.picture(hp, self.t.logo, *HEADER_LOGO)
        d.run(hp, f"   {student.university} {student.campus}", font=SERIF, size=20, color=c["navy"], bold=True)
        d.run(hp, f"\t{course_line}  |  {report.label}", size=18, color=c["muted"])

        fp = section.footer.paragraphs[0]
        ppr = fp._p.get_or_add_pPr()
        for st in ppr.findall(qn("w:pStyle")):
            ppr.remove(st)
        _put(ppr, _borders("w:pBdr", {"top": (c["gold"], 18, 6)}), P_ORDER)
        fp.paragraph_format.tab_stops.add_tab_stop(Twips(TEXT_W), WD_TAB_ALIGNMENT.RIGHT)
        _put(ppr, _el("w:spacing", before=0, after=0), P_ORDER)
        d.run(fp, f"{student.name} | {student.enrollment} | {student.klass}", size=18, color=c["muted"])
        r = d.run(fp, "\tPage ", size=18, color=c["navy"], bold=True)
        r = d.run(fp, "", size=18, color=c["navy"], bold=True)
        for el in (_el("w:fldChar", fldCharType="begin"), None, _el("w:fldChar", fldCharType="separate"),
                   "1", _el("w:fldChar", fldCharType="end")):
            if el is None:
                el = OxmlElement("w:instrText")
                el.set(qn("xml:space"), "preserve")
                el.text = "PAGE"
            elif isinstance(el, str):
                t = OxmlElement("w:t")
                t.text = el
                el = t
            r._r.append(el)

    # --- an exercise ---

    def _exercise(self, d: Doc, n: int, ex: Exercise, files: Path | None) -> None:
        c = self.t.colors
        banner = d.table([1500, TEXT_W - 1500], 1)
        d.row_props(banner.rows[0])
        edge = {"top": None, "left": None, "bottom": (c["gold"], 24), "right": None}
        left = d.cell(banner.rows[0].cells[0], fill=c["navy"], borders=edge, margins=(90, 140, 90, 140))
        d.run(d.cell_para(left, jc="center"), "EXERCISE", size=15, color=c["sky"], bold=True, spacing=40)
        d.run(d.cell_para(left, jc="center"), f"{n:02d}", font=SERIF, size=44, color=c["paper"], bold=True)
        right = d.cell(banner.rows[0].cells[1], fill=c["sky"], borders=edge, margins=(120, 260, 120, 140))
        d.run(d.cell_para(right, after=20, style="Heading 1"), ex.title, font=SERIF, size=34, color=c["navy"], bold=True)
        if ex.tag:
            d.run(d.cell_para(right), ex.tag, size=18, color=c["muted"], caps=True, spacing=20)
        d.spacer(160)

        box = d.table([1500, TEXT_W - 1500], 2)
        hair = _box(c["rule"], 4)
        for row, (label, fill, ink, text) in zip(box.rows, (("TASK", c["navy"], c["paper"], ex.task),
                                                          ("RESULT", c["gold"], c["ink"], ex.result))):
            d.row_props(row)
            lc = d.cell(row.cells[0], fill=fill, borders=hair, margins=(90, 140, 90, 140))
            d.run(d.cell_para(lc, jc="center"), label, size=17, color=ink, bold=True, spacing=30)
            rc = d.cell(row.cells[1], borders=hair, margins=(110, 260, 110, 160))
            p = d.cell_para(rc, line=290)
            if label == "TASK":
                d.run(p, text.replace("**", ""))
            else:
                d.rich(p, text, strong_size=24)

        for sec in ex.sections:
            self._heading(d, sec["heading"])
            for b in sec["blocks"]:
                self._block(d, b, files)

        if ex.code:
            if self._opening_height(ex, files) < CODE_BREAK_FILL * BODY_H_PT:
                d.para(after=0, line=20, page_break=True)
            self._heading(d, "Code")
            self._code(d, ex.code_file or "program", ex.code)
            if ex.screenshots:
                self._heading(d, "Jupyter Notebook Screenshots")
                intro = ex.notebook_intro or self._intro(ex)
                p = d.para(after=140, line=300)
                d.rich(p, intro)
                for shot in ex.screenshots:
                    self._shot(d, shot, files)
            if ex.output_shot or ex.output_text is not None:
                self._heading(d, "Output")
                if ex.output_shot:
                    self._shot(d, ex.output_shot, files)
                else:
                    self._code(d, "Output", ex.output_text or "(no output)", highlight=False)

    @staticmethod
    def _opening_height(ex: Exercise, files: Path | None) -> float:
        """Rough height in points of the banner, the Task and Result box and the sections."""
        def lines(text: str, per_line: int) -> int:
            return sum(max(1, -(-len(part) // per_line)) for part in text.replace("**", "").split("\n"))
        h = 57 + 9  # banner and the gap under it
        h += sum(lines(t, 71) * 14.5 + 11 for t in (ex.task, ex.result))
        for sec in ex.sections:
            h += 37
            for b in sec["blocks"]:
                if b["type"] == "paragraph":
                    h += lines(b["text"], 105) * 16.5 + 7
                elif b["type"] == "grid":
                    h += len(b["rows"]) * 40 + 23
                elif b["type"] == "table":
                    h += 20 + len(b["rows"]) * 18.5 + 23
                elif b["type"] == "figure":
                    path = (files / "figures" / b["file"]) if files else None
                    if path and path.is_file():
                        from docx.image.image import Image
                        img = Image.from_file(str(path))
                        h += FIGURE_W / 12700 * img.px_height / img.px_width + 23
        return h

    @staticmethod
    def _intro(ex: Exercise) -> str:
        n = len(ex.cells)
        if n <= 1:
            return "I ran the same code in Jupyter Notebook."
        names = [x for x in ex.cells if x]
        listed = (", ".join(names[:-1]) + f", and {names[-1]}") if len(names) == n and n > 1 else ""
        return f"I ran the same code in Jupyter Notebook. The code is divided into {WORDS.get(n, str(n))} cells" + (
            f": {listed}." if listed else ".")

    def _heading(self, d: Doc, text: str) -> None:
        p = d.para(before=300, after=110, keep_next=True, style="Heading 2",
                   borders={"bottom": (self.t.colors["rule"], 6, 3)})
        d.run(p, text, font=SERIF, size=26, color=self.t.colors["navy"], bold=True)

    def _block(self, d: Doc, b: dict, files: Path | None) -> None:
        c = self.t.colors
        kind = b["type"]
        if kind == "paragraph":  # a lead-in ending with a colon stays with what follows
            d.rich(d.para(after=140, line=300, keep_next=b["text"].rstrip().endswith(":")), b["text"])
        elif kind == "grid":
            rows = b["rows"]
            cols = max(len(r) for r in rows)
            grid = d.table([1500] * cols, len(rows), center=True)
            frame = _box(c["paper"], 12)
            for row, cells in zip(grid.rows, rows):
                d.row_props(row)
                for cell, item in zip(row.cells, cells + [{"label": "", "note": "", "dark": False}] * (cols - len(cells))):
                    dark = item.get("dark")
                    tc = d.cell(cell, fill=c["navy"] if dark else c["sky"], borders=frame, margins=(110, 80, 110, 80))
                    d.run(d.cell_para(tc, jc="center"), item["label"], font=SERIF, size=34,
                          color=c["paper"] if dark else c["navy"], bold=True)
                    if item.get("note"):
                        d.run(d.cell_para(tc, jc="center"), item["note"], size=18, color=c["sky"] if dark else c["muted"])
            d.caption("Figure", b.get("caption"))
        elif kind == "figure":
            path = (files / "figures" / b["file"]) if files else None
            if path and path.is_file():
                d.picture(d.para(jc="center"), path, FIGURE_W)
                d.caption("Figure", b.get("caption"))
        elif kind == "table":
            self._data_table(d, b)
            d.caption("Table", b.get("caption"))

    def _data_table(self, d: Doc, b: dict) -> None:
        c = self.t.colors
        header, rows, hl = b["header"], b["rows"], set(b.get("highlight") or [])
        cols = max(len(header), max(len(r) for r in rows))
        weights = []
        for i in range(cols):
            texts = [r[i] for r in rows if i < len(r)]
            longest = max([len(t) for t in texts] + [len(header[i]) * 1.2 if i < len(header) else 0, 3])
            weights.append(min(longest, 40) + 4)
        widths = [max(800, round(8500 * w / sum(weights) / 100) * 100) for w in weights]
        widths[-1] += 8500 - sum(widths)
        t = d.table(widths, len(rows) + (1 if header else 0), center=True)
        all_rows = list(t.rows)
        if header:
            hrow = all_rows.pop(0)
            d.row_props(hrow, header=True)
            for cell, text in zip(hrow.cells, header):
                tc = d.cell(cell, fill=c["navy"], borders=_box(c["navy"], 4), margins=(90, 140, 90, 140))
                d.run(d.cell_para(tc, jc="center", keep_next=True), text, size=17, color=c["paper"], bold=True, caps=True, spacing=20)
        for i, (row, values) in enumerate(zip(all_rows, rows)):
            d.row_props(row)
            hot = i in hl
            fill = c["sky"] if hot else (c["paper"] if i % 2 == 0 else c["sky-soft"])
            for j, (cell, text) in enumerate(zip(row.cells, values)):
                tc = d.cell(cell, fill=fill, borders=_box(c["rule"], 4), margins=(90, 140, 90, 140))
                d.run(d.cell_para(tc, jc="center", keep_next=True), text, size=21,
                      color=c["navy"] if hot else c["ink"], bold=hot or j == 0)

    def _code(self, d: Doc, filename: str, source: str, *, highlight: bool = True) -> None:
        c = self.t.colors
        p = d.para(line=280, ind=(140, 140), keep_next=True, fill=c["navy"], borders=_box(c["navy"], 4, 6))
        d.run(p, filename, font=MONO, size=16, color=c["paper"], bold=True)
        frame = _box(c["rule"], 4, 6)
        for line in self._code_lines(filename, source, highlight):
            p = d.para(line=244, ind=(140, 140), fill=c["sky-soft"], borders=frame)
            if not line:
                d.run(p, " ", font=MONO, size=17)
            for text, color, bold, italic in line:
                d.run(p, text, font=MONO, size=17, color=color, bold=bold, italic=italic)

    def _code_lines(self, filename: str, source: str, highlight: bool) -> list[list[tuple]]:
        ink, navy = self.t.colors["ink"], self.t.colors["navy"]
        source = source.expandtabs(4).rstrip("\n")
        try:
            lexer = get_lexer_for_filename(filename, stripnl=False, ensurenl=False) if highlight else None
        except ClassNotFound:
            lexer = None
        tokens = lexer.get_tokens(source) if lexer else [(None, source)]
        lines: list[list[tuple]] = [[]]
        for tok, value in tokens:
            if tok is None:
                style = (ink, False, False)
            elif tok in String.Doc or tok in Comment:
                style = (CODE_COMMENT, False, True)
            elif tok in String:
                style = (CODE_STRING, False, False)
            elif tok in Keyword or tok in Operator.Word or tok in Name.Builtin.Pseudo:
                style = (navy, True, False)
            else:
                style = (ink, False, False)
            for k, piece in enumerate(value.split("\n")):
                if k:
                    lines.append([])
                if piece:
                    lines[-1].append((piece, *style))
        return lines

    def _shot(self, d: Doc, shot, files: Path | None) -> None:
        path = (files / "shots" / shot.file) if files else None
        if not (path and path.is_file()):
            return
        p = d.para(before=60, after=0, jc="center", keep_next=True, borders=_box(self.t.colors["rule"], 6, 3))
        d.picture(p, path, SHOT_W)
        d.caption("Screenshot", shot.caption)


def to_pdf(docx_path: Path, timeout: int = 180) -> Path:
    """LibreOffice headless; a private profile per call so parallel runs never collide."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice (soffice) is not installed")
    with tempfile.TemporaryDirectory(prefix="lo-") as profile:
        result = subprocess.run(
            [soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
             "--convert-to", "pdf", "--outdir", str(docx_path.parent), str(docx_path)],
            capture_output=True, text=True, timeout=timeout,
        )
    pdf = docx_path.with_suffix(".pdf")
    if result.returncode != 0 or not pdf.exists():
        raise RuntimeError(f"PDF export failed: {(result.stderr or result.stdout).strip()[:500]}")
    return pdf
