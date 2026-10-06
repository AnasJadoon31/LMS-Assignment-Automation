# Run your own copy

Each student runs their own copy of this app, on their own machine, with their own
CMS login, their own WhatsApp bot number and their own Claude Pro account.
Nobody holds anyone else's password, and every submission is reviewed by the student
whose name is on it. Run it only for your own account.

## What you need

- A machine that stays on, with Docker: a VPS (one in Pakistan gives the LMS a local
  address), a home PC, or a laptop that is usually on. About 6 GB of RAM (a solver run may use
  up to 4 GB) and 20 GB of free disk (the images take about 8 GB).
- Your CMS enrollment and password. Institute and role default to Karachi Campus and
  Student; set `CMS_INSTITUTE` and `CMS_ROLE` if yours differ (they must match the CMS
  login form's dropdowns exactly).
- For WhatsApp: a second phone number for the bot, on a phone with WhatsApp. Evolution
  API links it the way WhatsApp Web does, which is not an official WhatsApp API, so keep
  your own number out of it. Or skip WhatsApp and use a Discord webhook.

## What runs

| Service | What it is | Reachable from |
| --- | --- | --- |
| `worker` | Logs in to CMS, polls the LMS, downloads question files and papers, solves assignments in solver containers, saves the reports to Google Drive, uploads what you approve, sends notifications | inside the stack only |
| `api` | Answers your WhatsApp messages through the numbered menu, and serves the setup page (`/setup`): sign Claude in, link WhatsApp, connect Google Drive | its domain (port 8000) |
| `postgres` | Jobs, audit trail, encrypted LMS session; Evolution's tables in their own schema | inside the stack only |
| `evolution-api` | WhatsApp gateway. The worker creates and links its instance | inside the stack only |
| `redis` | Evolution API's cache | inside the stack only |
| `docker-proxy` | Lets the worker start solver containers; only container and image calls | inside the stack only |
| `solver-image` | Builds the `lms-solver` image on every deploy, then idles | nothing |
| solver containers | One per assignment, from the `lms-solver` image: Claude Code, with every tool and the internet, solves it and builds the report from your theme artifact, then the container is removed | nothing; they reach the internet but not the stack's services |

Only the api gets a domain. Claude runs under your claude.ai login, kept in the
`lms-claude-home` volume: a full login can read your theme artifact, which a setup token
cannot. Sign in from the setup page; if the login ever expires, the bot tells you and
links you there.

## Option A: any machine with Docker

```sh
git clone https://github.com/<you>/LMS-Assignment-Automation.git
cd LMS-Assignment-Automation
scripts/init-env.sh            # creates .env with random secrets, readable only by you
nano .env                      # fill in CMS_ENROLLMENT, CMS_PASSWORD, WHATSAPP_NUMBER
docker compose up -d --build
docker compose exec worker env HOME=/claude-home claude auth login --claudeai   # sign Claude in: open the link, paste the code back
docker compose logs -f worker  # watch the first poll
```

Without a domain, sign Claude in from the terminal as above and link WhatsApp from the
terminal (below). With one, use the setup page instead.

`.env` holds your CMS password. It is git-ignored; never commit or share it.

## Option B: Coolify

1. Fork or push this repo to your GitHub, then in Coolify add it as a **Docker Compose** resource.
2. In the resource's **Environment Variables**, set `CMS_ENROLLMENT`, `CMS_PASSWORD`,
   `WHATSAPP_NUMBER`, `ADMIN_PASSWORD` (the setup page's password), `PUBLIC_URL` (the api's
   address, e.g. `https://lms.example.com`) and three secrets: `POSTGRES_PASSWORD`,
   `APP_SECRET_KEY` and `EVOLUTION_API_KEY`. Make each secret 48 random letters and digits,
   for example from `openssl rand -hex 24`.
3. Give the **api** service that domain (port 8000) in its **Domains** setting. No other
   service needs one.
4. Deploy. The first deploy also builds the `lms-solver` image, which takes a few minutes.
5. Open `https://<your domain>/setup`, sign in with `ADMIN_PASSWORD`, then:
   - **Sign in to Claude**: open the link it shows, sign in with your Claude account, and
     paste the code that page shows back into the setup page.
   - **WhatsApp**: link the bot phone with the QR code, or type the bot phone's number for a
     pairing code (WhatsApp, **Linked devices**, **Link a device**, **Link with phone number
     instead**). Then, under **Messages go to**, save your own number (unless you set
     `WHATSAPP_NUMBER`) and send a test message.
   - **Google Drive** (optional, see below): **Connect Google Drive**.
   - **Discord** (optional): paste a channel webhook URL.

   Then **Courses** lists this semester's courses. Pick each course's mode (**Approve**,
   **Auto** or **Notify**); open a course to see its assignments and lab papers, download
   question files and solved PDFs, and approve, reject or redo.

The worker starts solver containers through Docker, so the stack mounts
`/var/run/docker.sock` (read-only, behind `docker-proxy`). Coolify allows this.

The commands below run in the worker's terminal: `docker compose exec worker <command>`,
or the **Terminal** tab of the worker container in Coolify. You need them only without the
setup page.

## Save reports to Google Drive

Every solved report (the .docx, the .pdf and the question file) is saved to
**University Assignments / <semester> / <course> / <report name>** in your Drive. The app
asks only for Drive's `drive.file` permission: it sees only the folders and files it makes.

1. In [Google Cloud Console](https://console.cloud.google.com), create a project, then
   **APIs & Services**: enable the **Google Drive API**.
2. **OAuth consent screen** (Google Auth Platform): user type **External**, add your Gmail as
   a test user, then **Publish app** (otherwise Google ends the connection after 7 days).
   With only `drive.file` it needs no review.
3. **Credentials**, **Create credentials**, **OAuth client ID**, type **Web application**.
   Add the redirect URI the setup page shows under Google Drive,
   `https://<your domain>/setup/google/callback`.
4. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, redeploy, and press **Connect Google
   Drive** on the setup page.

If saving ever fails, the report still comes to you, and its message says why.

## Link the bot's WhatsApp

By phone-number code (easiest in a terminal):

```sh
lms-worker whatsapp link --number 923001234567   # the BOT phone's number, country code first
```

It prints an 8-character code. On the bot phone: WhatsApp, **Settings**, **Linked devices**,
**Link a device**, **Link with phone number instead**, then type the code.

By QR code:

```sh
lms-worker whatsapp link
```

It draws the QR code in the terminal (and redraws it when it rotates). Scan it from
**Linked devices**, **Link a device** on the bot phone.

Then:

```sh
lms-worker whatsapp status    # Linked: <name> (+92...), and where messages go
lms-worker notify-test        # sends a test message to each channel
```

Now send `menu` to the bot's number from your own WhatsApp. It answers only the number messages go to.
If it stays silent, check `docker compose logs api` for "Evolution webhook points at".

`WHATSAPP_NUMBER` is your own number: the bot sends to it. You can save it on the setup page
instead (**Messages go to**); a number saved there wins, and takes effect at once. Open WhatsApp on the bot phone
at least every two weeks; WhatsApp logs out linked devices when the phone has been away
longer than that. `lms-worker whatsapp unlink` removes the link.

Discord instead of, or as well as, WhatsApp: create a webhook (channel settings,
**Integrations**, **Webhooks**, **New Webhook**, **Copy Webhook URL**) and paste it into
**Discord** on the setup page; it sends a test message and takes effect at once. Or set
`DISCORD_WEBHOOK_URL` and restart. Discord is one-way: alerts and files only.

## Check it works

```sh
lms-worker poll      # one poll now: courses, assignments, what is new
lms-worker status    # last poll, pauses, login state, recent jobs
```

The first poll sends a "New courses found" message, then one message with the question
file for each open assignment you have not submitted. Closed and already-submitted
assignments are recorded and left alone.

## Look after it

| Task | How |
| --- | --- |
| Update | `git pull && docker compose up -d --build` (Coolify: redeploy) |
| Back up | `docker compose exec postgres pg_dump -U lms lms > backup.sql`; question files are in the `artifacts` volume |
| Login paused (wrong password or CAPTCHA) | fix `CMS_PASSWORD` if needed, then `lms-worker retry` |
| "The LMS page changed" alert | check the LMS still works in a browser, then `lms-worker resume` |
| Change `APP_SECRET_KEY` | allowed; the worker just logs in to CMS again |
| Use your own Evolution API elsewhere | set `EVOLUTION_API_URL` (and its key), delete the `evolution-api` and `redis` services, and set the api's `API_INTERNAL_URL` to an address that Evolution can reach |
| Discord only | leave `WHATSAPP_NUMBER` empty; you can delete the `evolution-api` and `redis` services |
