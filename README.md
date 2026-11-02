# socrat-bot

Control plane for the Socrat homelab cluster. One image, three processes.

## Processes

- `python -m socrat_bot` — Telegram bot and HTTP API for the control plane. This command is the image default; the process listens on port 8080.
- `python -m uvicorn socrat_bot.broker:api --host 0.0.0.0 --port 8080` — sudo broker. It accepts a client request, asks the owner in Telegram, and returns permission to run.
- `python -m socrat_bot.updater` — checks and updates the 3X-UI panel image.

## Environment variables

Secrets stay out of git. The repository lists names only; values come from the process environment or a secret store.

Bot:

- `DATABASE_URL`
- `PORT`
- `ALEMBIC_INI`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_OWNER_ID`
- `INTERNAL_TOKEN`
- `ALERTMANAGER_URL`
- `BROADCAST_PAUSE_SECONDS`
- `XUI_UPDATER_URL`

Broker:

- `DATABASE_URL`
- `INTERNAL_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_ID`
- `SUDO_TTL_MINUTES`

Updater:

- `PORT`
- `XUI_PANEL_PORT`
- `XUI_HEALTH_PATH`
- `KUBERNETES_SERVICE_HOST`
- `KUBERNETES_SERVICE_PORT`
- `XUI_UPGRADE_TIMEOUT_SECONDS`
- `XUI_POLL_SECONDS`

`socrat-sudo` client:

- `BROKER_URL`
- `INTERNAL_TOKEN`
- `SUDO_WAIT_SECONDS`

Image publish in GitHub Actions uses the secrets `HARBOR_USERNAME`, `HARBOR_PASSWORD`, and `HOMELAB_DEPLOY_KEY`.

## socrat-sudo

The `socrat-sudo` command sits beside `sudo`. It sends the arguments to the broker and invokes `sudo` with the same arguments only after confirmation. sudoers is not tightened yet: plain `sudo` is still available. The client adds Telegram confirmation and does not replace the privilege policy.
