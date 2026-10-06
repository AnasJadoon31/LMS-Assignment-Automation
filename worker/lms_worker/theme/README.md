<!-- Exported from the Anas Assignment Theme design system (https://claude.ai/artifact/F2Je3LZvKN92ysXVL7jM2R), version 5, on 2026-10-06.
     Student details removed; the renderer reads them from the env file. -->

# Anas Assignment Theme

One layout for every Bahria University assignment and lab report by the student. Every report uses it unchanged, whatever the course: the same cover page, the same header and footer, the same exercise banner, the same order inside each exercise and the same colours. It is taken from the Lab 5 report "Greedy and A* Search Algorithm" (CSL-411 Artificial Intelligence, 3 October 2026), and the report generator rebuilds that report page for page.

## Student details

These go on every report, exactly as written. The values live in the git-ignored env file, never in the repo.

| Field | Value |
| --- | --- |
| Name | `STUDENT_NAME` in the env file |
| Enrollment | `CMS_ENROLLMENT` in the env file |
| Class | `STUDENT_CLASS` in the env file |
| University | Bahria University, Karachi Campus |
| Department | Department of Computer Science |

The course code and name are taken from the assignment sheet. The semester comes from the class (BS(CS) 3-A is Semester 03).

## The page

A4 portrait. Margins 0.85 inch left and right, 1.04 inch top, 0.83 inch bottom (0.9 inch on the cover). Headings in Cambria, text in Calibri, code in Consolas; where those are missing, Caladea, Carlito and DejaVu Sans Mono keep the same line breaks.

## Cover page

The first page, with no header or footer and a 2.25 pt `navy` border around the page, 24 pt in from the edge. Top to bottom, centred:

1. **Logo.** The Bahria University logo, 2.42 inch wide. Never redrawn or recoloured.
2. **University and campus.** `cover-university` in `navy` ("BAHRIA UNIVERSITY"), then `cover-campus` in `muted` ("KARACHI CAMPUS").
3. **Department.** `cover-department` in `ink`, over a short 3 pt `gold` rule.
4. **Title band.** A full-width `navy` box between two 4.5 pt `gold` rules, holding `band-label` in `gold` ("LAB REPORT 05" or "ASSIGNMENT 02"), the title in `band-title` white ("Greedy and A* Search Algorithm", without "Lab 5:"), and `band-caption` in `sky` ("CSL-411 Artificial Intelligence | Semester 05").
5. **Details table.** Centred, 4.7 inch wide, five rows: Submitted by, Enrollment, Class, Course, Date. Labels in `details-label` on `navy`, values in `details-value` on `sky`, the name in bold, thin white lines between rows.

## Every other page

- **Header:** the small logo, then "Bahria University Karachi Campus" in `header-title`, and on the right "CSL-411 Artificial Intelligence | Lab 05" in `header-course`, over a 1.5 pt `navy` rule.
- **Footer:** under a 2.25 pt `gold` rule, "Name | Enrollment | Class" in `muted` on the left, "Page 1" in bold `navy` on the right. Pages count from 1 after the cover.

## Each exercise

Each exercise starts on a new page.

1. **Banner.** A `navy` cell with `banner-label` ("EXERCISE") over `banner-number` ("01"), beside a `sky` cell with `banner-title` ("Greedy Best First Search") over `banner-tag` ("GREEDY SEARCH"); a 3 pt `gold` rule under both.
2. **Task and Result box.** Two rows framed in `rule`: TASK on `navy` with the question quoted from the sheet; RESULT on `gold` with the answer in one sentence, the answer itself in `box-answer` `navy` ("S → A → B → F → G").
3. **Sections that explain the work**, each under a `section-heading` with a `rule` hairline: "Given Graph", "How It Works", or whatever the question needs. Inside them:
   - paragraphs in `body`;
   - **grid figures** for tables of values: square cells, `navy` with white labels for the cells on the answer path, `sky` with `navy` labels for the rest, the value under each label;
   - **drawn figures** for graphs and trees (see Figures);
   - **data tables**: a `table-header` row on `navy`, rows alternating `paper` and `sky-soft`, the first column bold, the answer row bold `navy` on `sky`, all centred;
   - a caption under every figure and table in `caption`: "Figure 1: ...", "Table 1: ...", numbered through the whole report.
   A sentence that leads into a table and ends with a colon stays on the same page as the table.
4. **Code.** A `navy` bar with the file name in `code-file` ("exercise1_greedy.py"), then the whole program on `sky-soft` framed in `rule`: docstrings and comments in `code-comment` italic, keywords bold `navy`, strings `code-string`, everything else `ink`. The code starts on a new page when the exercise's opening leaves room on its first page, and follows straight on after a longer opening.
5. **Jupyter Notebook Screenshots.** One or two sentences on how the notebook is split ("The code is divided into three cells: the data, the search function, and the cell that runs it."), then screenshots framed in `rule`, each captioned "Screenshot N: ...": the notebook open in Jupyter Notebook (first exercise only), then every cell but the last ("Cell 1, heuristic values and the graph.").
6. **Output.** The last cell after running, with its output below it ("Screenshot 4: Cell 3 after running, with the output of Exercise 1 below the cell.").

Programs that are not Python keep steps 4 and 6, with the program's real output in a code block headed "Output". Written questions without a program end after step 3.

Nothing else is added: no introduction, conclusion or contents page unless the sheet asks.

## The notebook

One notebook per report, named after the course initials, the lab and the student: "AI_Lab_05_First_Last". It opens with a title cell ("# Lab 5: Greedy and A* Search Algorithm", then "**Name** | Enrollment | Class | CSL-411 Artificial Intelligence"), then a heading cell per exercise ("## Exercise 1: Greedy Best First Search") and its code cells, run top to bottom so the cells count [1], [2], [3] through the notebook. Screenshots are of Jupyter Notebook in a 1180 × 640 window at twice the pixel density, with the last cell selected after running.

## Figures

Graphs and trees are drawn, never pasted from the sheet: nodes are circles with bold letters; nodes on the answer path are `navy` with a `gold` ring and white letters, the others `sky` with a `navy` ring and `navy` letters; edges on the path are thick `navy` arrows with bold `navy` costs, the rest thin `edge` arrows with `ink` costs; heuristic values (h=6) sit in `muted` beside each node. A graph is about 5.4 inch wide in the report.

## Colour

Colours are sampled from the Bahria University logo. `navy` carries the title band, banners, headings, table headers and the page frame; `sky` and `sky-soft` are the light fills; `gold` draws the rules and fills the RESULT label; `ink` on `paper` is the text. `code-comment` and `code-string` colour code only, and `edge` the edges of figures. `gold` is never text on paper, and `flame` belongs to the logo only.

## Writing voice

Written as a 5th semester BS(CS) student explaining his own work: simple English, first person where it is natural ("I treated the table as a grid"), no filler.

Code follows the same voice:

- Plain Python that a 5th semester student would write; no clever one-liners.
- Explanation goes in multiline (triple-quoted) comments above each block, the way the student would explain it in a viva.
- A short dry run sits in a multiline comment at the end of each program.
- The Output is always the real output of running the code, never typed by hand.

## Using this theme in other formats

- **Word and PDF:** everything above; `lms_worker/solver/render.py` builds it.
- **Claude Doc:** the logo, the title, the details table, then a heading per exercise with Task, Result, the sections, Code and Output. Colours and fonts are not available there, so only the order applies.
- **HTML:** the tokens as CSS variables and the layout above; the components here are the reference pages.
