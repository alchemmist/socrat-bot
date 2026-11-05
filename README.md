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

A later workflow can attach a GitHub issue to every copy of a card:

```
POST /v1/cards/links
X-Internal-Token: ...
{"alert": {"labels": {"alertname": "TargetDown", "namespace": "lms"}}, "url": "https://github.com/org/repo/issues/4"}
```

The link label is `SPI`. The same alert sent again keeps the link. `key` may be sent instead of `alert`. A card that has not been rendered since this field existed answers `409` until the next alert update stores its text.

## VPN list and scheduled broadcast

`/start` stores the Telegram user. The reply is `Socrat is online.` with `I use VPN` and `Subscribe to alerts` until each one is done. Pressing the VPN button sets `uses_vpn` and does not change an owner into a regular user. A normal user becomes role `vpn`. The broadcast audience is everyone with `uses_vpn`, including an owner who opted in. A panel link code is separate and does not by itself add the person to this list.

`Subscribe to alerts` asks an owner to approve. After approval, `alerts_approved` and `alerts_enabled` are set and that chat receives cards according to its receive level (see below). Mute still creates one Alertmanager silence and updates every admin's copy of that card: the buttons go away and the message says how long it is muted. The owner is subscribed from the start.

The owner lists those Telegram accounts with `/clients`. Each known username is shown as `@username`. Someone who only pressed `/start` is not included.

The owner starts a mailing with the text:

```
/mailing Maintenance tonight
```

The bot then offers the next seven days and Now. After a day, it offers every hour from 08:00 through 22:00 in `Europe/Moscow`. Hours that already passed today are hidden. Now sends immediately. A scheduled mailing is sent when that time arrives. The check runs about every 15 seconds. `BROADCAST_TZ` overrides the zone.

## socrat-sudo

The `socrat-sudo` command sits beside `sudo`. It sends the arguments to the broker and invokes `sudo` with the same arguments only after confirmation. sudoers is not tightened yet: plain `sudo` is still available. The client adds Telegram confirmation and does not replace the privilege policy.


## Alert levels and escalation

`ALERT_LEVELS=10,20` configures ascending, unique positive tiers. Lower numbers
mean higher authority; an incident starts at the largest configured level and
Escalate advances exactly one tier toward the smallest. Keep configured levels
stable while incidents are open. The owner starts with admin 10 / receive 20;
migration 0010 gives all other existing users admin 20 / receive 20. Newly
approved users start at the largest (lowest-authority) configured tier.
`alerts_approved` stores permanent owner approval; `alerts_enabled` controls current delivery. There is no new role.

In private Telegram chat:

* `/alerts` shows English approval/admin/receive settings and receive-level buttons.
* `/alerts level N` changes receive mode, subject to `receive_level >= admin_level`.
* `/alerts off` (or `disable`) stops new cards and short notifications. `/alerts on`
  (or `enable`) restores delivery for already-approved subscribers without another
  owner request. First-time subscribers still require owner approval.
* `/unsubscribe` and the **Unsubscribe** button stop alert delivery without changing
  approval, VPN opt-in, admin tier, or receive tier. `/start` → **Subscribe to alerts**
  restores delivery without replaying existing incidents. Old approval buttons do
  not undo a later unsubscribe.
* Owner-only `/alertlevel TELEGRAM_ID N` assigns an existing user's admin tier;
  an incompatible receive tier is raised to that admin tier. This does not approve
  the subscriber or replay incidents.

A new firing generation routes only to approved users with receive level at least
its stage. The original recipient snapshot and initial delivery work commit atomically
with incident creation, before Telegram I/O. Failed first sends and a crash before
HTTP remain retryable after webhook retry or restart, without admitting later
subscribers. Ordinary repeats and resolves update **already delivered current-generation
copies** and retain originally admitted pending delivery intents; disabled users
receive edits to delivered copies but no new main cards. Joining
or changing receive mode never replays an existing firing incident. A successful
escalation explicitly snapshots eligible approved subscribers and admits every
missing copy at the destination (including someone approved mid-incident); no
other event admits a new recipient. With no eligible recipients, the API returns
`copies: []`, without falling back to the supplied `chat_id`.

The full-width Escalate button is the second row below Mute. It requires an enabled,
approved subscriber, their own delivered current-generation card, and admin level
no greater than the incident stage. Callbacks carry expected stage and generation;
a transactional database compare-and-swap makes concurrent taps advance only once.
An empty next **assigned admin tier** rejects the transition (no automatic skipping).
After advancement, lower-authority copies lose the button; the final tier has none.
Repeats retain stage/history, resolved removes buttons, and the next firing starts
a new generation at the maximum tier. Old callbacks cannot affect it.

Every transition stores an actor/timestamp audit and durable delivery outbox in
one transaction. Initial delivery, ordinary webhook updates, escalation, SPI,
notes, mute, and PATCH of known alert cards all publish desired content revisions
and use the same dispatcher. A durable recipient/card lease serializes HTTP and
successful-message persistence across independent workers; ownership is renewed
every 10 seconds with a 60-second lease and fenced completion. No database session
or transaction is retained during Telegram HTTP. Revisions, not just stage/status,
detect newer alert text or annotations received in flight; the worker preserves
that desired state and durably repairs the old snapshot. Two normal successful
jobs for a new recipient cannot both send a main card.

`POST /v1/messages` still dispatches synchronously and returns `copies`/message
IDs for completed copies (partial success is retained). With admitted recipients
but no completed delivery, it returns 502 and retains durable retry work rather
than returning a misleading empty success. An ambiguous transport failure retains
its lease until expiry; a definite Telegram rejection can be retried immediately.
The background worker retries after failures and restarts. New
recipients receive a full card with the escalation marker, existing recipients
whose assigned admin tier equals the destination receive a short separate
message linking their existing card, except the actor. New main-card recipients
never receive that extra message. All delivered copies receive the stage/history
marker. SPI links, notes and existing mute footers are preserved; mute is still
independent Alertmanager silence and the existing no-buttons-on-muted-card policy
is retained. Delivery is **at least once**: a Telegram timeout after accepting a
send can cause a duplicate on retry (Telegram has no idempotency key). A process
crash after Telegram acceptance but before successful persistence, or loss/expiry
of ownership during an uncertain request, has the same at-least-once caveat.

`GET /v1/audience` includes `admin_level` and `receive_level` for each person;
card listing/item metadata includes `generation`, `level`, `escalation_history`.

### Escalation tests

Run `.venv/bin/pytest -q`. For real PostgreSQL concurrency/retry coverage, point
`TEST_POSTGRES_URL` at a **disposable test database** (its URL must contain `test`)
and run the same command. Those tests drop/recreate application tables there;
never point them at production. PostgreSQL-specific tests are skipped otherwise.
No new lint dependency is required; `python -m compileall -q src tests alembic`
is also supported.


### Schema startup and rollout

Both bot and sudo broker call `init_schema`. They serialize PostgreSQL upgrades
using the same session advisory lock on a dedicated migration connection, supplied
to Alembic; the lock is acquired before version-table inspection. CLI Alembic uses
the same lock. Migration 0011 adds durable recipient ownership/content state and
backfills existing cards; 0007→0011 (including upstream SPI migrations 0008/0009) and downgrade/upgrade are supported.

Older binaries do not honor the new recipient leases or incident state and must
not overlap with new bot workers. The current homelab component Deployment template
uses `strategy: Recreate` and `replicaCount: 1`; normal GitOps rollout preserves
that requirement without ad hoc scaling/self-heal overrides. Concurrent startup
of the separate bot/broker Deployments is supported by migration ownership.


### SPI episode and link revision integration

`spi_episode` retains the upstream authoritative digest-based immutable card key.
A terminal resolved episode never re-fires or replaces its message, even if a
late firing webhook arrives; a distinct episode digest starts a distinct incident.
Episode cards use the same approved audience rules (never a fallback `chat_id`).

`POST /v1/cards/links` preserves `expected_copies` identity validation and monotonic
`revision` semantics. The accepted `desired_link_revision`/`desired_spi_url` fence
commits **before HTTP**, even when all Telegram edits fail. Stale revisions or a
same-revision different URL receive 409 without an edit. `link_revision` and
`spi_url` describe applied successful HTTP snapshots, not desired state. Each
retry re-edits the copy, including same-URL cached successes; `updated` acknowledges
only delivered applied desired revisions and `pending` must be retried even on
HTTP 200. The shared dispatcher checks the SPI fence again immediately before
HTTP, preserves newer accepted targets during in-flight older edits, and repairs
through durable work. Notes, mute and ordinary alert updates render the latest
desired SPI target rather than reintroducing an older applied anchor.

Migration 0011 preserves populated upstream 0009 desired/applied fences, including
historical applied revisions, and enqueues recovery for accepted-but-unapplied
SPI links. CI runs the PostgreSQL concurrency/migration/SPI suite on a PostgreSQL
17 service on every release before the unchanged image publication/deploy chain.

### Persistent alert approval migration

Migration `0012_alerts_approval` separates one-time owner approval from current
delivery. It preserves existing enabled subscribers, owners, and previously
approved opted-out users recoverable from `alerts.approve` audit entries. VPN
opt-in alone never grants alert access. Existing delivered cards still receive
state/resolution edits after opt-out; no new cards or escalation notices are sent.

### Logging scope

The bot, sudo broker and x-ui updater emit bounded JSON operational events through
`socrat-bot`: successful application startup/shutdown and HTTP completion with a
static component (`bot`, `broker`, `updater`), numeric status and duration in ms.
HTTP events deliberately omit URL/path/query, method, client address, headers,
body and exception details. Failed requests still emit a status (500 if no
response was started); failures are not swallowed. Existing reviewed bot events
retain their specific event names and bounded numeric fields.

The Python entrypoints disable Uvicorn's raw access log, replacing it with these
owned request events. Uvicorn lifecycle/error output and other dependency logs
are **not** converted to application JSON: collector envelopes may normalize
those vendor lines, but that is distinct from source JSON. A plain vendor startup
line is not evidence of an application logger configuration failure. This is not
an all-lines JSON guarantee, nor a redaction guarantee for vendor exception logs.

Deterministic local verification: run
`python -m pytest tests/test_structured_logging.py -q`. The actual-entrypoints test
starts each real `python -m` Uvicorn process with isolated temporary SQLite and no
Telegram credentials, requests only health, verifies owned lifecycle/request JSON
and absence of a query canary, then terminates the processes. It makes no Telegram
or Kubernetes changes and does not print captured log bodies.

### Telegram command autocomplete

`src/socrat_bot/commands.py` is the canonical command registry: command name,
English description, handler binding, and owner-only menu visibility. Bot startup
registers handlers from that registry and publishes complete lists with Telegram
`setMyCommands`; BotFather edits are no longer necessary and will be overwritten
in managed scopes on the next startup.

Default and all-private-chat menus contain `/start`, `/alerts`, `/unsubscribe`.
Owner private-chat scopes add `/clients`, `/alertlevel`, `/mailing`. Configured
owner IDs and persisted owner users are included; an owner's `/start` also refreshes
that personal scope after bootstrap. Language-independent, English, and Russian
scope entries use the same English UI descriptions. Other manually configured
language/scope overrides are not managed. Menu visibility is not authorization;
handlers still check their existing permissions.

Transient Telegram menu failures do not block webhooks or delivery: a background
startup task retries every 60 seconds until all managed scopes succeed. The task
is cancelled on shutdown; failures are logged without exception text or tokens.

### Editing sent mailings

Owners can privately use `/mailing_edit UUID NEW_TEXT`, or reply to a delivery
summary with `/mailing_edit NEW_TEXT`. `/mailing_edit` lists owned sent IDs.
Text is plain Unicode (up to 4096 UTF-16 units). Only the original successfully
sent copies with saved Telegram message IDs are edited; the current VPN audience
is never consulted and no replacement messages are sent. Partial edit failures
are reported per copy; repeat the same command to retry failed copies while
skipping copies already updated. Deleted or otherwise uneditable messages remain
failures. Mailings sent by older releases have no saved recipient message IDs:
Telegram Bot API cannot reconstruct those copies, so they cannot be edited.
