# fb_bot Deployment Notes

## Why the previous deploy did not become active

The main problem was the **process model**. The original `Procfile` only started:

```text
worker: python tg_slip_bot.py
```

That means the deployment did **not** start a public web process for the Facebook webhook. Facebook Messenger webhooks require an HTTP endpoint that stays online and responds to `GET /webhook` verification and `POST /webhook` events. Because there was no web process, `fb_to_tg_bot.py` could not receive webhook calls, so the Facebook-to-Telegram automation appeared dead.

A second issue was dependency and configuration readiness. The project did not clearly include all runtime packages needed for image analysis and OpenAI-based vision parsing, and the code path was not structured for deployment-safe environment variable loading and persistent storage.

## What was fixed

### `fb_to_tg_bot.py`

The Facebook bot was rewritten to be deployment-safe and webhook-ready.

| Area | Fix |
|---|---|
| Webhook runtime | Added a Flask app with `/`, `/healthz`, and `/webhook` routes |
| Verification | Added Facebook webhook verification using `FB_VERIFY_TOKEN` |
| Security | Added optional `X-Hub-Signature-256` validation using `FB_APP_SECRET` |
| Admin approval detection | Detects approval by text keywords such as `DONE` and also by the known approval image using image hashing |
| Slip handling | Stores pending user screenshots in SQLite until page admin approval arrives |
| Vision parsing | Uses OpenAI vision when available to extract amount, sender, receiver, datetime, and reference ID |
| Telegram forwarding | Sends the approved receipt screenshot and parsed details to the correct Telegram group |
| Reliability | Added duplicate webhook protection via processed message tracking |
| Operability | Added health check output and structured logging |

### `tg_slip_bot.py`

The Telegram bot was also corrected and hardened.

| Area | Fix |
|---|---|
| Secrets | Removed hard-coded operational assumptions and moved runtime configuration to environment variables |
| Storage | Uses SQLite for transaction and duplicate tracking |
| Input parsing | Accepts text, photos, and image documents |
| Vision parsing | Uses OpenAI vision to read Thai bank slip screenshots when available |
| Duplicate tracking | Detects repeated slip/reference IDs and records duplicate alerts |
| Commands | Keeps `/summary`, `/list`, `/check`, and `/balance` support |

## Deployment files updated

### `Procfile`

```text
web: gunicorn --bind 0.0.0.0:$PORT fb_to_tg_bot:app
worker: python tg_slip_bot.py
```

This makes the repository ready for platforms that support a web process and a worker process.

### `requirements.txt`

The dependency list now includes the packages needed for both bots, including:

- `flask`
- `gunicorn`
- `requests`
- `python-telegram-bot==13.15`
- `Pillow`
- `openai`

### `.env.example`

An example environment file was added so deployment variables can be filled consistently.

## Required environment variables

| Variable | Required | Purpose |
|---|---|---|
| `FB_PAGE_ID` | Recommended | Used to identify page-originated messages more reliably |
| `FB_APP_SECRET` | Recommended | Verifies Facebook webhook signatures |
| `FB_PAGE_ACCESS_TOKEN` | Yes | Reads page assets and sends Messenger replies |
| `FB_VERIFY_TOKEN` | Yes | Facebook webhook verification |
| `TG_BOT_TOKEN` | Yes | Telegram bot API token |
| `TG_BAHT_GROUP` | Yes | Telegram target group for Thai Baht receipts |
| `TG_KYAT_GROUP` | Optional | Telegram target group for Kyat receipts |
| `TG_SPECIAL_USER` | Optional | User treated as outgoing transfer source in `tg_slip_bot.py` |
| `OPENAI_API_KEY` | Optional but recommended | Enables vision extraction from screenshots |
| `OPENAI_VISION_MODEL` | Optional | Defaults to `gpt-4.1-mini` |
| `APPROVAL_IMAGE_PATH` | Optional | Path to the `DONE` approval reference image |
| `PORT` | Platform-provided | Web server bind port |
| `LOG_LEVEL` | Optional | Logging level |

## Recommended deployment model

Use a platform that supports **both**:

1. a persistent public **web** service for Facebook webhook delivery, and  
2. a persistent **worker** process for the Telegram polling bot.

If the platform only runs one process, then the Facebook webhook bot and the Telegram polling bot should be split into separate services.

## Facebook webhook setup checklist

1. Deploy the repository.
2. Ensure the web process is live.
3. Set the webhook callback URL to:

```text
https://YOUR-DOMAIN/webhook
```

4. Set the verify token to the same value as `FB_VERIFY_TOKEN`.
5. Subscribe the Facebook app to the Page messaging events.
6. Confirm that `GET /healthz` returns `status: ok`.

## GitHub push checklist

```bash
git add .
git commit -m "Fix Facebook to Telegram bot and deployment configuration"
git remote add origin <your-github-repo-url>
git push -u origin main
```

If the remote already exists, use:

```bash
git push origin main
```

## Validation already performed

The repaired Python files were compiled successfully with `python3.11 -m py_compile`, which confirms there are no syntax errors in the current repository version.
