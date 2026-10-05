# LMS Assignment Automation

Watches the Bahria LMS, solves new assignments with a Claude agent in a sandbox,
renders them on the assignment theme, and uploads them after approval. The design
is in the tech spec; this repo is being built in the spec's order.

| Step | Status |
| --- | --- |
| 1. Spike: CMS login, LMS mapping, `list_assignments()` | Done, except upload (see below) |
| 2. Watcher: poller, fingerprints, database, notifier | Next |
| 3. Solver and renderer | Not started |
| 4. Submitter | Not started; upload form mapped |
| 5. Web UI and auto mode | Not started |

## Worker setup

```sh
cd worker
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env   # fill in CMS_ENROLLMENT and CMS_PASSWORD
pytest
```

`.env` and `state/` are git-ignored. `state/` holds the LMS session cookie, the
failed-login counter and any spike recordings.

## Commands

| Command | What it does |
| --- | --- |
| `python -m lms_worker probe` | Reads the CMS login form and reports its fields and any CAPTCHA. Submits nothing |
| `python -m lms_worker login` | Logs in through CMS (enrollment, password, institute, role), follows the LMS link, saves the session |
| `python -m lms_worker check` | Says whether the saved LMS session still works and shows the failed-login count |
| `python -m lms_worker list [--status]` | Lists this semester's courses and assignments |
| `python -m lms_worker paste-session <PHPSESSID>` | Saves a session copied from your own browser and clears the re-login pause |
| `python -m lms_worker retry` | Clears the re-login pause (the Retry button) |
| `python -m lms_worker record` | Opens a browser and records LMS traffic to `state/spike/` for mapping (needs a display) |

## What the spike found

- **CMS login** (`cms.bahria.edu.pk/Logins/Student/Login.aspx`, ASP.NET WebForms) takes four fields:
  enrollment, password, institute (`Karachi Campus` = option `2`) and role (`Student` = option `None`).
  It shows a reCAPTCHA to non-browser clients such as curl and httpx, and none to Chromium, headless included.
  The login stays in Playwright; the service never solves a CAPTCHA, and pauses if one appears.
- **LMS session** is the `PHPSESSID` cookie on `lms.bahria.edu.pk`. Without it, every page redirects to CMS.
  CMS's LMS link opens the LMS with a fresh session.
- **Assignments** are listed on `Student/Assignments.php?s=<semester>&oc=<course>`, with base64 ids, in one table:
  number, title, question file, your submission, marks, returned submission, action, deadline (PKT).
- **The action cell is the state.** It shows Submit (open, nothing submitted), Delete (open, already submitted)
  or Deadline Exceeded. The assignment's real id appears only on open rows, so rows are keyed by course and number.
- **Download links** carry a base64 key of `student id, today's date, kind, 0, filename`. The key changes daily,
  so it is never part of a fingerprint. Its first field is the student id, not the assignment id.
- **Upload** is a multipart POST to `includes/studentprocess.php?s=assignmentSubmissionAdd` with `assignmentID`,
  `offeredCourseID`, `ocID`, `studentID` and `assignmentSubmissionFile` (.pdf .doc .docx .xls .xlsx .zip,
  no special symbols in the name). **A submitted assignment shows Delete, not a re-upload**, so a correction
  means deleting the old file and then submitting again. Delete is a plain GET link, and the poller never follows it.
