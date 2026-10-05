# LMS Assignment Automation

Watches the Bahria LMS, solves new assignments with Claude Code (headless, on your
Pro plan) in a sandbox, renders them on the assignment theme, and uploads them after
approval. The design is in the tech spec; this repo is being built in the spec's order.

| Step | Status |
| --- | --- |
| 1. Spike: CMS login, LMS mapping, `list_assignments()` | Done, except upload (see below) |
| 2. Watcher: poller, fingerprints, database, notifier | Done: WhatsApp and Discord |
| 3. Solver and renderer | Next |
| 4. Submitter | Not started; upload form mapped |
| 5. Web UI and auto mode | Not started |

## Deploy the watcher on Coolify

1. Add this repo in Coolify as a **Docker Compose** resource (`docker-compose.yml` at the root).
2. Set the environment variables, as secrets where marked:
   - `POSTGRES_PASSWORD` (secret)
   - `APP_SECRET_KEY` (secret): any random string of 32 or more characters. It encrypts the stored LMS session.
   - `CMS_ENROLLMENT` and `CMS_PASSWORD` (secret). Institute and role default to Karachi Campus and Student.
   - WhatsApp: `EVOLUTION_API_URL`, `EVOLUTION_API_KEY` (secret), `EVOLUTION_INSTANCE`, `WHATSAPP_NUMBER`
   - Discord, optional: `DISCORD_WEBHOOK_URL` (secret). In Discord: channel settings, Integrations, Webhooks, New Webhook, Copy Webhook URL.
3. Deploy. The worker runs its migrations, then polls every 10 minutes from 08:00 to 23:00 PKT and every 30 minutes overnight.
   Nothing in this step needs a public domain.

With no notification channel set, messages go to the worker's log.

## What the watcher does

- **Finds work.** Each poll reads every course of the current semester. An assignment that is open and not yet
  submitted gets a job; its question file is downloaded to `/data/{course}/{assignment}/{revision}/`
  and sent to you with the course, title, deadline and the teacher's note.
- **Leaves your work alone.** Anything already closed, or already showing a submission the service did not
  make, is recorded but gets no job. If you submit by hand while a job is open, the job stops.
- **Tracks changes.** A changed title, deadline or question file creates a new revision and a "changed" message.
- **Keeps going.** A changed LMS page pauses polling and alerts you (`lms-worker resume` after checking).
  A lost session logs in again through CMS; 2 rejected logins or a CAPTCHA pause that and alert you.
  An unreachable LMS backs off 5, 15, then 60 minutes and alerts after 3 failed polls.
- **Never loses a message.** Messages go through an outbox in PostgreSQL, one row per channel, retried until
  delivered. WhatsApp and Discord deliver independently. Discord webhooks are one-way, so approvals will
  happen on WhatsApp and the web UI.

## Worker setup

```sh
cd worker
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env   # fill in CMS_ENROLLMENT and CMS_PASSWORD
pytest                 # set TEST_DATABASE_URL to a scratch PostgreSQL database to run the watcher tests too
```

`.env`, `state/` and `data/` are git-ignored. Without `DATABASE_URL`, `state/` holds the LMS session cookie,
the failed-login counter and any spike recordings. With it, they live in PostgreSQL and the session is encrypted.

## Commands

| Command | What it does |
| --- | --- |
| `lms-worker run` | Polls on schedule until stopped; the worker container's command |
| `lms-worker poll` | Runs one poll now and prints what it found |
| `lms-worker status` | Last poll, pauses, login state and recent jobs |
| `lms-worker resume` | Clears the polling pause after a changed LMS page |
| `lms-worker db-upgrade` | Creates or migrates the tables |
| `lms-worker probe` | Reads the CMS login form and reports its fields and any CAPTCHA. Submits nothing |
| `lms-worker login` | Logs in through CMS (enrollment, password, institute, role), follows the LMS link, saves the session |
| `lms-worker check` | Says whether the saved LMS session still works and shows the failed-login count |
| `lms-worker list` | Lists this semester's courses and assignments, with submitted and open state |
| `lms-worker paste-session <PHPSESSID>` | Saves a session copied from your own browser and clears the re-login pause |
| `lms-worker retry` | Clears the re-login pause (the Retry button) |
| `lms-worker record` | Opens a browser and records LMS traffic to `state/spike/` for mapping (needs a display) |

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
