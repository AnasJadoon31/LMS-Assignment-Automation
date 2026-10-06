<!-- Exported from the Anas Assignment Theme design system (https://claude.ai/artifact/F2Je3LZvKN92ysXVL7jM2R), version 1, on 2026-10-06.
     Student details removed; the renderer reads them from the env file. -->

# Anas Assignment Theme

One layout for every Bahria University assignment and lab report by the student. Every assignment uses this theme unchanged: same header, same details, same section order, same colours.

## Student details

These go on every assignment, exactly as written. The values live in the git-ignored env file, never in the repo.

| Field | Value |
| --- | --- |
| Name | `STUDENT_NAME` in the env file |
| Enrollment | `CMS_ENROLLMENT` in the env file |
| Class | `STUDENT_CLASS` in the env file |
| University | Bahria University, Karachi Campus |

Course name and code change per assignment and are taken from the assignment sheet.

## Report layout

Every report is laid out like the submitted Lab 5 report ("Greedy and A* Search Algorithm",
3 October 2026). The renderer (`lms_worker/solver/render.py`) reproduces it from the same
Word settings; a rebuild of that report with the renderer matches it page for page.
This replaces the single-page structure first drawn in the theme artifact.

Page: A4, 1220 twips left and right; Cambria headings, Calibri 11 pt body, Consolas 8.5 pt code.

**Cover page** (its own section, no header or footer, a 2.25 pt `navy` page border):

1. The logo, centred, 2.42 inch wide.
2. "BAHRIA UNIVERSITY" in Cambria 26 pt bold `navy`, letter-spaced; "KARACHI CAMPUS" in 12 pt `muted`,
   wide-spaced; the department in 13 pt over a short 3 pt `gold` rule.
3. The title band: a full-width `navy` box between 4.5 pt `gold` rules holding "LAB REPORT 05"
   (11 pt bold `gold`, spaced), the title (Cambria 30 pt bold white) and "course | Semester 05" in `sky`.
4. The details table, centred: Submitted by, Enrollment, Class, Course, Date; labels uppercase on `navy`,
   values on `sky`, the name in bold.

**Every other page**: a header with the small logo, "Bahria University Karachi Campus" (Cambria bold
`navy`) and "course | Lab 05" on the right over a `navy` rule; a footer under a `gold` rule with
"name | enrollment | class" and "Page N" in bold `navy`. Numbering starts at 1 after the cover.

**Each exercise**, from a new page:

1. Banner: a `navy` cell with "EXERCISE" and the number ("01"), a `sky` cell with the title
   (Cambria 17 pt bold `navy`) and a short uppercase tag, a 3 pt `gold` rule under both.
2. Task and Result box: TASK on `navy`, RESULT on `gold`; the answer itself in bold `navy`.
3. The sections that explain the work ("Given Graph", "How It Works", ...): Cambria 13 pt `navy`
   headings over a `rule` hairline, 11 pt paragraphs, and:
   - grid figures: 0.75 inch square cells, dark `navy` cells for the path, `sky` for the rest;
   - drawn figures (graphs) in the theme colours: path nodes `navy` with a `gold` ring, other nodes
     `sky`, path edges thick `navy`, other edges grey;
   - data tables: an uppercase `navy` header row, rows alternating white and `sky-soft`, the first
     column bold, the answer row on `sky` in bold `navy`;
   - every figure and table captioned in 9 pt italic `muted`: "Figure 1: ...", "Table 1: ...",
     numbered through the whole report.
   A lead-in sentence ending with a colon stays on the page of what follows it.
4. Code: from a new page when the exercise's opening fills less than 90% of a page, otherwise
   straight on. A `navy` bar with the file name, then the program on `sky-soft` in Consolas:
   docstrings and comments green italic, keywords bold `navy`, strings red, the rest `ink`.
5. Jupyter Notebook Screenshots: one or two sentences, then the notebook (the first exercise only)
   and each cell except the last, framed in `rule`, captioned "Screenshot N: Cell 1, ...".
6. Output: the last cell after running, with its output below it, as a screenshot.

Nothing else is added: no introduction, conclusion or contents page unless the sheet asks.

## Colour

Colours are sampled from the Bahria University logo. `navy` carries the title band, banners, headings and table headers; `sky` fills the details values, banner titles and answer rows; `gold` draws the rules around the title band, under banners and in the footer, and fills the RESULT label; `ink` on `paper` is the body. Code sits on `sky-soft` with a `rule` hairline. The report adds two code colours of its own: green `#2E7D4F` for docstrings and comments, red `#B3261E` for strings. `flame` is not used in reports.

## Type

Headings are serif, body is sans, code is mono. On screen: Source Serif 4, Source Sans 3 and JetBrains Mono. In Word and PDF: Cambria for headings, Calibri 11 pt for body, Consolas 8.5 pt for code (LibreOffice on the server falls back to the metric-compatible Caladea and Carlito, and DejaVu Sans Mono).

## Writing voice

Written as a 5th semester BS(CS) student explaining his own work: simple English, first person where it is natural ("I treated the table as a grid"), no filler.

Code follows the same voice:

- Plain Python that a 5th semester student would write; no clever one-liners.
- Explanation goes in multiline (triple-quoted) comments above each block, the way the student would explain it in a viva.
- A short dry run sits in a multiline comment at the end of each program.
- The Output block is always the real output of running the code, never typed by hand.

## Using this theme in other formats

- **Claude Doc:** logo image, H1 title, the details table, then H2 per exercise and H3 for Code and Output. Colours and fonts are not available there, so only the structure applies.
- **Word or PDF:** the report layout above.
- **HTML:** use the tokens as CSS variables.
