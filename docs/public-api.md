# Public API

The HTTPS base is `https://bot.socrat.alchemmist.xyz/api/v1` once public ingress is enabled. Use `Authorization: Bearer TOKEN`; never put credentials in URLs. Tokens are opaque random secrets: only a hash, permissions, allowed chats and expiry are persisted. Revocation works on the next request without waiting for token expiry. An in-flight request is not cancelled. Plain message grants require explicit chat IDs. Other scopes are global and should be treated as trusted integrations.

| Route | Required grant |
|---|---|
| POST /messages | messages:send; cards, interactive actions, rich formatting and fan-out additionally require messages:advanced |
| PATCH /messages/{id} | messages:edit |
| GET /cards, /cards/item | cards:read |
| POST /cards/links, /cards/notes | cards:link or cards:note respectively |
| GET /audience | audience:read |
| POST /link-codes | links:issue |
| POST /broadcasts | broadcasts:send |
| GET /actions/{id} | actions:read |

Management: open `/` over HTTPS using Authentik OIDC; membership in `socrat-bot-admin` or `homelab-owner` is required. Select scopes and an expiry, copy the bearer once; issue and revoke independently for each consumer. No grant permits managing grants. For rotation, issue a replacement and revoke the old one after cutover. No Basic fallback exists. Console sessions expire within five minutes or on local logout. During migration `/v1/*` remains private and independently authenticated; `BOT_DISABLE_LEGACY_API=true` disables those handlers after migration. Never route `/v1/*` through the public ingress.
