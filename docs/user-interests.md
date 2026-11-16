# Independent user interests

`/start` is an English, private-chat multi-select: **Welcome to Socrat Bot!** /
**I'm interested in…**. VPN and Alerts are independent checkboxes. Toggles carry
explicit `interest:<topic>:on|off` states, not a blind inversion; repeating a
callback doesn't turn a selection back off. Registered topics come from
`src/socrat_bot/interests.py`; one row per `(user_id,topic)` persists its enabled
state in `user_interests`. Disabled rows deliberately persist, including opt-outs
while approval is pending. Future topics need registry/feature wiring, not new
boolean columns or mutually exclusive roles. Unknown callback topics are denied.

- VPN selects VPN mailings, NOT VPN authorization. Existing panel links survive
  toggles; link codes alone do not subscribe to mailings. `/users` is the owner
  command: it offers each registered interest and then lists who selected it.
- Alerts selection is distinct from durable `users.alerts_approved`. An
  unapproved selection is shown as checked/pending; it cannot receive alerts,
  use alert actions or select an unauthorized receive tier. Owner approval never
  adds an interest and never undoes an explicit opt-out. Existing receive/admin
  tiers, generation fences, original audiences, receipt-only mailing edits,
  desired/applied captions/media and retries retain their existing contracts.
- `/unsubscribe`, `/alerts off` and `al:no` clear only the Alerts interest.
  Re-enabling an approved interest requires no new approval and no backlog.
  Existing `vpn:yes`, `al:yes` and `al:ok:<id>` buttons remain compatible. An old
  first-approval button grants permission only: if its pre-migration request had
  never recorded an opt-in, the person must choose Alerts in the new screen.
- `owner` and `user` describe authority. Historical `vpn` roles normalize to
  `user`; no interest grants owner/admin rights. New owners keep the previous
  initial default of approved Alerts; explicit owner opt-outs survive `/start`.

## Two-stage rollout (do NOT squash the release boundary)

1. **0020_user_interests** expands/backfills exact current VPN and Alerts flags.
   Approval history remains separate; old enabled rows are recognized as approved
   just as old `ensure_user` did. Merely having approval or a panel link does not
   create a selected interest. No alert/card/mailing history is replayed. Temporary
   PostgreSQL triggers maintain legacy flags/interest changes both ways while old
   and new pods coexist; old-code delivery only sees selection AND approval.
   New code never maps/reads/writes the legacy boolean columns or creates vpn roles.
2. Wait for bot, sudo-broker and xui-updater to roll completely to the 0020-capable
   image. Verify pod images and no old application pods/jobs remain. Check that
   old projections agree with current interests/approval. Only then publish the
   separate **0021_interest_contract** release. It drops the temporary triggers,
   function and old `uses_vpn` / `alerts_enabled` columns, and re-normalizes vpn
   roles created by old pods during transition. The ONLY interest source of truth
   is `user_interests`. New install may apply the full chain immediately.

Take a private full pg_dump before phase1. Before phase2, confirm a current private
backup. Migrations hold the existing startup PostgreSQL advisory migration lock;
never hold a user preference transaction open during Telegram HTTP. Preference
updates lock the parent user row; existing users can change independent flags
without lost whole-JSON updates. SQLite uses the same schema and selection model;
its test-only path doesn't need PostgreSQL rolling-process compatibility triggers.

Rollback schema before deploying pre-0020 code: 0021 downgrade reconstructs the
legacy projection and compatibility triggers; 0020 downgrade reconstructs flags
and VPN roles before dropping interests. Going all the way back to the old schema
cannot represent unknown future topics or pending interest separately from
approval; retain the private pre-rollback dump. Do not use downgrade as a normal
feature toggle. Never run old source against the contracted schema.

No messages are sent by migration. Acceptance must use isolated SQLite and only
known owner test-message IDs; do not mass-edit old welcome screens or alert cards.
