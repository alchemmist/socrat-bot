# Interest-targeted mailings

Owner-only private flow: `/mailing` → **VPN / Alerts / LLM / Cluster** → send text → native
body preview → day/hour or **Now**. `/mailing TEXT` is an optional shortcut, but
still requires explicit group selection. No group silently defaults for the new
interactive flow. **Cancel** cancels an unfinished group/text draft; starting a
new `/mailing` cancels only earlier group/text drafts, not already scheduled work.
Only one awaiting-text draft per owner is admitted under the parent user lock.
All composition state lives in PostgreSQL, not PTB context/user_data. Commands
aren't captured as bodies. Ordinary private text is consumed only when an owner
has an awaiting-text draft. Body and native UTF-16 entities are validated/persisted
before preview, and loaded from durable state at delivery. Photos/captions aren't
accepted as mailing text by this flow.

`Broadcast.audience_group` stores one of `vpn`, `alerts`, `llm`, `cluster` after selection;
NULL is only an unselected/cancelled interactive draft. Migration0022 backfills
EVERY existing mailing to `vpn`, including sent and scheduled rows. New interest
LLM is opt-in: no historical member inference and no key provisioning. All manual,
scheduled and legacy approval sends resolve the stored group; unknown/NULL groups
refuse sending, never fall back to VPN or all users. Alerts mailings preserve the
owner-approval boundary (selected interest AND permanent approval); VPN/LLM/Cluster use
selected interests. Audiences use current membership at SEND time, matching prior
VPN scheduling behavior, not membership when drafting. Counts shown on scheduling
are explicitly current counts, not frozen audience guarantees. Internal plaintext
`POST /v1/broadcasts` retains its VPN default for compatibility. Internal audience
API includes `cluster` and `llm` alongside unchanged `vpn` and approved `alerts` lists.

Group is shown on time selection, immediate/scheduled summaries and original-time
ordered history. In-place edits remain receipt-only: editing doesn't recompute the
audience or retarget a mailing; original confirmed copies alone are edited, never
new/replacement sends, even if a recipient subsequently changes interests.

## Rolling-process safety and rollback

New interactive rows use **group_choosing** and **group_scheduled**, deliberately
NOT legacy choosing/scheduled. Previous VPN-only callback workers/tickers cannot
admit them or mistakenly send an LLM/Alerts mailing to VPN users during rollout
(or rollback). The new ticker supports both statuses and uses an atomic status
claim; new callback scheduling claims the exact prior state and owner. Legacy
existing choosing/scheduled/preview rows remain supported and have VPN defaults.
A group button is one-shot: stale or duplicate clicks can't retarget the admitted
group. Scheduled and Now admissions cannot both claim one chooser state.

Downgrade0022 refuses while composing/new interactive rows or active non-VPN rows
remain, to avoid old code reinterpreting them. Finish/cancel them deliberately or
restore the private pre-migration backup before old schema/code. A schema downgrade
inherently discards group metadata on completed/cancelled history; retain backup.
Never merely rewrite an LLM group to VPN to get past the guard.

Acceptance: isolated SQLite+PostgreSQL scenarios cover group-first formatted text,
LLM toggles, narrow multi-interest audiences, Alerts approval, restart-independent
composition, stale/unauthorized selectors, cancellation/restart, Now deduplication,
scheduled current-membership resolution, populated old-mailing VPN backfill and
legacy-worker status isolation. Real Telegram acceptance must target only the
known owner through a fresh isolated DB and delete only returned test-message IDs.

## Catch up new subscribers

In the owner's private chat, `/resend 5` offers the same three groups. Selecting a
button starts delivery; no new text is needed. The window is the group's last N
sent owner mailings with saved receipts, ordered by first receipt. Each current
eligible member receives only missing copies, oldest first, using the current
body, formatting and photo. Legacy mailings without receipts are not replayed.
A timeout or interrupted send leaves a durable reservation: later runs skip it
rather than risking a duplicate. The summary reports pending/unknown attempts
separately from Telegram rejections, which can be retried with `/resend`.
