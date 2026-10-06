# Run your own copy

Each student runs their own copy of this app, on their own machine, with their own
CMS login, their own WhatsApp bot number and (from step 3) their own Claude Pro token.
Nobody holds anyone else's password, and every submission is reviewed by the student
whose name is on it. Run it only for your own account.

## What you need

- A machine that stays on, with Docker: a VPS (one in Pakistan gives the LMS a local
  address), a home PC, or a laptop that is usually on. About 2 GB of free RAM and 5 GB of disk.
- Your CMS enrollment and password. Institute and role default to Karachi Campus and
  Student; set `CMS_INSTITUTE` and `CMS_ROLE` if yours differ (they must match the CMS
  login form's dropdowns exactly).
- For WhatsApp: a second phone number for the bot, on a phone with WhatsApp. Evolution
  API links it the way WhatsApp Web does, which is not an official WhatsApp API, so keep
  your own number out of it. Or skip WhatsApp and use a Discord webhook.

## What runs

| Service | What it is | Reachable from |
| --- | --- | --- |
| `worker` | Logs in to CMS, polls the LMS, downloads question files and papers, sends notifications | inside the stack only |
| `api` | Answers your WhatsApp messages through the numbered menu (the web UI joins it later) | inside the stack only |
| `postgres` | Jobs, audit trail, encrypted LMS session; Evolution's tables in their own schema | inside the stack only |
| `evolution-api` | WhatsApp gateway. The worker creates and links its instance | inside the stack only |
| `redis` | Evolution API's cache | inside the stack only |

Nothing publishes a port. You link WhatsApp from the worker (now) or the web UI (later).

## Option A: any machine with Docker

```sh
git clone https://github.com/<you>/LMS-Assignment-Automation.git
cd LMS-Assignment-Automation
scripts/init-env.sh            # creates .env with random secrets, readable only by you
nano .env                      # fill in CMS_ENROLLMENT, CMS_PASSWORD, WHATSAPP_NUMBER
docker compose up -d --build
docker compose logs -f worker  # watch the first poll
```

`.env` holds your CMS password. It is git-ignored; never commit or share it.

## Option B: Coolify

1. Fork or push this repo to your GitHub, then in Coolify add it as a **Docker Compose** resource.
2. In the resource's **Environment Variables**, set `CMS_ENROLLMENT`, `CMS_PASSWORD`,
   `WHATSAPP_NUMBER` and three secrets: `POSTGRES_PASSWORD`, `APP_SECRET_KEY` and
   `EVOLUTION_API_KEY`. Make each secret 48 random letters and digits, for example from
   `openssl rand -hex 24`.
3. Deploy. No domain is needed for any service.

Commands below run in the worker's terminal: `docker compose exec worker <command>`, or
the **Terminal** tab of the worker container in Coolify.

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

Now send `menu` to the bot's number from your own WhatsApp. It answers only `WHATSAPP_NUMBER`.
If it stays silent, check `docker compose logs api` for "Evolution webhook points at".

`WHATSAPP_NUMBER` is your own number: the bot sends to it. Open WhatsApp on the bot phone
at least every two weeks; WhatsApp logs out linked devices when the phone has been away
longer than that. `lms-worker whatsapp unlink` removes the link.

Discord instead of, or as well as, WhatsApp: create a webhook (channel settings,
**Integrations**, **Webhooks**, **New Webhook**, **Copy Webhook URL**), set
`DISCORD_WEBHOOK_URL`, restart, and run `lms-worker notify-test`. Discord is one-way:
alerts and files only.

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
