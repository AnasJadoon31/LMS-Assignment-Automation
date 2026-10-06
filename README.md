# LMS Assignment Automation

Watches the Bahria LMS, solves new assignments with Claude Code (headless, on your
Pro plan) in a sandbox, renders them on the assignment theme, and uploads them after
approval. The design is in the tech spec; this repo is being built in the spec's order.

| Step | Status |
| --- | --- |
| 1. Spike: CMS login, LMS mapping, `list_assignments()` | Done, except upload (see below) |
| 2. Watcher: poller, fingerprints, database, notifier | Done: WhatsApp and Discord, lab papers, marks, reminders |
| 3. Solver and renderer | Next |
| 4. Submitter | Not started; upload form mapped |
| 5. Web UI and auto mode | Not started |

## Run it

Each student runs their own copy: see **[SELF_HOSTING.md](SELF_HOSTING.md)**. In short:

```sh
scripts/init-env.sh                 # .env with random secrets; then fill in your CMS login and WhatsApp number
docker compose up -d --build        # worker, api, PostgreSQL, Evolution API (WhatsApp), Redis; no public ports
docker compose exec worker lms-worker whatsapp link --number <bot number>   # or without --number for a QR code
docker compose exec worker lms-worker notify-test
```

The same `docker-compose.yml` deploys on Coolify as a Docker Compose resource.

## How WhatsApp is wired

Evolution API runs inside the stack and is reachable only by the worker, at
`EVOLUTION_API_URL` (default `http://evolution-api:8080`) with `EVOLUTION_API_KEY`, both from the
environment. The worker owns the WhatsApp instance: it creates it on first use, links it by QR code
or by phone-number pairing code, and sends through it to `WHATSAPP_NUMBER`. The web UI will offer
the same QR and phone-number linking on its WhatsApp page, through the same code
(`lms_worker/whatsapp.py`). Your replies reach the app through an Evolution webhook to the api
service, also inside the stack: the api registers it at start, with a secret header derived from
`APP_SECRET_KEY`, and answers only `WHATSAPP_NUMBER`.

## The WhatsApp menu

Send `menu` to the bot. Numbered screens, reply with a digit, `0` goes back:

```
LMS BOT
1. Assignments: 2 remaining, 1 due soon
2. Lab papers: 1 upcoming, next Tue 20 Oct, 2:00 PM
3. Waiting for your approval (1)
4. Due or starting in the next 48 hours (2)
5. By course
6. Course modes (auto, approve, notify)
7. Check the LMS now
8. System status
```

Assignments list: remaining, due in 48 hours, waiting for your approval, queued or being solved,
stopped, submitted, missed (closed with no submission), marked and all. Lab papers list by exam
window: upcoming, open now, starting in 48 hours, finished, submitted, missed, marked and all. By course
gives the same screens for one course (papers only for labs), plus its mode. Picking an item shows
its deadline, status, marks, teacher's note and service state, and offers what applies: the
question file or paper, the solved PDF, DOCX, notebook and receipt, Approve, Reject, Redo with a
note, and History. Approve, Reject and switching a course to auto ask 1. Yes / 2. No first.


## What the watcher does

- **Finds work.** Each poll reads every course of the current semester. An assignment that is open and not yet
  submitted gets a job; its question file is downloaded to `/data/{course}/{assignment}/{revision}/`
  and sent to you with the course, title, deadline and the teacher's note.
- **Leaves your work alone.** Anything already closed, or already showing a submission the service did not
  make, is recorded but gets no job. If you submit by hand while a job is open, the job stops.
- **Tracks changes.** A changed title, deadline or question file creates a new revision and a "changed" message.
- **Lab papers.** Only lab courses get papers (`Papers.php`), each with a date, start and end time.
  You get one message when a paper is scheduled, reminders 24 hours and 1 hour before it starts, and
  the paper file the moment it opens: the worker polls at the start time, then every minute until
  the file appears. The LMS never shows the submitted file (`k=NA`); the row colour is the status:
  green submitted, white not submitted, red missed (the exam ended with no submission). A paper
  that turns green or red gets one message, and either stops its reminders. Papers are never solved.
- **Status, marks, reminders.** Every poll records what the LMS shows for each assignment and paper
  (submitted, open or closed, marks). "Marks posted" arrives when marks first appear, and a reminder
  24 hours and 2 hours before any assignment deadline with nothing submitted.
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
| `lms-worker api` | Answers WhatsApp messages through the menu; the api container's command |
| `lms-worker poll` | Runs one poll now and prints what it found |
| `lms-worker status` | Last poll, pauses, login state and recent jobs |
| `lms-worker resume` | Clears the polling pause after a changed LMS page |
| `lms-worker db-upgrade` | Creates or migrates the tables |
| `lms-worker notify-test` | Sends a test message to each configured channel (WhatsApp, Discord) and says which worked |
| `lms-worker whatsapp link [--number N]` | Links the bot's WhatsApp: a QR code in the terminal, or with `--number` an 8-character pairing code |
| `lms-worker whatsapp status` / `unlink` | Shows which number is linked and where messages go / removes the link |
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
