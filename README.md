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

## Alert cards

`POST /v1/messages` sends one card. An Alertmanager alert is the `alert` object (`status`, `labels`, `annotations`). The bot keeps one Telegram message per object: alert name plus pod, workload, or node. A cluster alert with no object is keyed by the alert name only. Repeats edit that message. `resolved` updates the same message, adds the word Resolved, and removes the mute buttons. The next firing after that is a new message.

Links come from a provider registry in `socrat_bot.cards`, not from Prometheus annotations. The alert default is Alert, then the node dashboard and Rancher when the subject is a node, Rancher for a pod or an explicitly labeled workload, Loki logs, and Argo CD when the namespace maps to one Application. At most four of those. A trace id adds Traces (Tempo) only when the caller asks for the `traces` provider, or when that provider is included in `card.links`.

A non-alert card uses the same renderer:

```json
{
  "chat_id": 1,
  "card": {
    "key": "deploy-blog",
    "title": "blog rolled out",
    "labels": {"namespace": "blog", "trace_id": "abc"},
    "links": ["argo", "traces"],
    "hrefs": [{"label": "Diff", "url": "https://example.invalid/diff"}]
  }
}
```

`links` names providers. Unknown names are rejected. `hrefs` are extra URLs for that message only. Mute buttons are added for a firing alert when `actions` is empty.

## VPN list and scheduled broadcast

`/start` stores the Telegram user. The reply is `Socrat is online.` with `I use VPN` and `Subscribe to alerts` until each one is done. Pressing the VPN button sets `uses_vpn` and does not change an owner into a regular user. A normal user becomes role `vpn`. The broadcast audience is everyone with `uses_vpn`, including an owner who opted in. A panel link code is separate and does not by itself add the person to this list.

`Subscribe to alerts` asks an owner to approve. After approval, `alerts_enabled` is set and that chat receives its own copy of each alert card. Mute still creates one Alertmanager silence and updates every admin's copy of that card: the buttons go away and the message says how long it is muted. The owner is subscribed from the start.

The owner lists those Telegram accounts with `/clients`. Each known username is shown as `@username`. Someone who only pressed `/start` is not included.

The owner starts a mailing with the text:

```
/mailing Maintenance tonight
```

The bot then offers the next seven days and Now. After a day, it offers every hour from 08:00 through 22:00 in `Europe/Moscow`. Hours that already passed today are hidden. Now sends immediately. A scheduled mailing is sent when that time arrives. The check runs about every 15 seconds. `BROADCAST_TZ` overrides the zone.

## socrat-sudo

The `socrat-sudo` command sits beside `sudo`. It sends the arguments to the broker and invokes `sudo` with the same arguments only after confirmation. sudoers is not tightened yet: plain `sudo` is still available. The client adds Telegram confirmation and does not replace the privilege policy.
