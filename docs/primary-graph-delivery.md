# Primary graph delivery

Newly received metric alerts with `spi_graphs` create an immutable PostgreSQL
snapshot and durable render intent. The original alert is delivered immediately;
Chromium/Grafana capacity never holds up the text notification. The graph worker
renders one request at a time per bot process. After a valid PNG is stored, the
ordinary fenced delivery outbox attaches it using `editMessageMedia` on the same
message ID. If rendering finishes before the initial send, `sendPhoto` is used.
There is no extra graph reply, alert replay, changed card key or recipient change.

This fixes three independent ways ordinary pod cards stayed text-only:

1. A short foreground render failed once during a burst, without a durable retry.
2. Existing text fallbacks were never eligible for a later photo upgrade.
3. Caption length included HTML markup and long href URLs, although Telegram's
   caption limit applies **after entity parsing**. Pod cards with >900 raw units
   can have <200 visible UTF-16 units. Original HTML/links are not shortened.

The supported Bot API contract explicitly allows replacing a text message with
media: <https://core.telegram.org/bots/api#editmessagemedia>.

## Bounds and fences

- At most 128 pending render intents, 12 attempts and 10 minutes per intent.
- Immutable observation time; duplicate requests never reset attempts or age.
- 45-second owner lease; another process can recover it after restart. Every
  completion checks owner, lease, incident generation, status and desired capture.
- Temporary gateway/transport errors back off (2–30 seconds). Permanent 4xx,
  invalid/oversized images and exhausted budgets retain the full text fallback.
- Fixed credential-isolated gateway; redirects and ambient proxies disabled.
  Per-attempt total transport deadline 20s, streamed PNG limit 2MiB, checksum and
  render-digest validation. Existing aggregate PNG budget remains 256MiB.
- Queue operations have PostgreSQL transaction-local lock/statement timeouts.
  Stored/logged failure codes are bounded identifiers, never upstream exceptions,
  URLs, credentials or response bodies.
- Original full captions/buttons/links survive. The conservative 768-visible-unit
  initial/upgrade budget and 1024-unit photo-update limit remain. Truly oversized
  content stays full text; unknown/malformed HTML uses the conservative raw bound.
- Migration 0018 adds only `graph_jobs`. It does not enqueue historical snapshots,
  replay alerts, clear queues, reset incidents, or change existing PNGs/delivery
  fences. Fresh natural input can reuse an immutable prior snapshot; no background
  database scan turns historical text messages into photos.

`graph_jobs.state`, `attempts`, `last_code` and timestamps distinguish pending,
ready, expired, failed and superseded intents without inspecting private payloads.
The existing Telegram dispatcher handles photo edits, receipts and safe retries;
the render worker never calls Telegram directly. No exactly-once claim is made
for an ambiguous Telegram transport acceptance.

Tests in `tests/test_primary_graphs.py` run against disposable SQLite/PostgreSQL.
They cover transient/permanent errors, a synthetic six-card resolved burst,
same-ID conversion, HTML captions, stale generations/owners, restart recovery,
queue/storage/age/attempt limits and preservation of complete text. The additive
migration test verifies existing PNG bytes survive and historical queues stay empty.
Checks are declared in `qg.yaml`; generated workflows must not be edited manually.


The gateway itself must allow 15 seconds for Telegram rendering; increasing only
client retries cannot repair its former hard 4-second server deadline. Gateway
errors are recorded as bounded known enum codes (e.g. `RENDER_FAILED`) and remain
available when an intent expires; raw response text is never stored or logged.
The stateless gateway can be released independently using the full SPI image's
explicit `alert-graphs` target, without replacing stateful SPI workers.
