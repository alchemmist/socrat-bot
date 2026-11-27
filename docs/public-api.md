# Public API

The HTTPS base is `https://bot.socrat.alchemmist.xyz/api/v1`. Use `Authorization: Bearer TOKEN`; never put credentials in URLs. Tokens are opaque random secrets: only a hash, permissions, allowed chats and expiry are persisted. Revocation works on the next request without waiting for token expiry. An in-flight request is not cancelled. Plain message grants can optionally restrict destination chats. Other scopes are global and should be treated as trusted integrations.

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

## Manage tokens in the English-language console

1. Open the [token console](https://bot.socrat.alchemmist.xyz/) and sign in with Authentik. Membership in **either** `socrat-bot-admin` or `homelab-owner` is sufficient. There is no Basic password fallback, and client tokens cannot manage grants.
2. Enter **Token name**, **Expires in** and **Permissions**. For plain sends, select only `messages:send`. This allows delivery to any chat the bot can message. Editing requires its own permission; do not grant it by default.
3. Select **Create token** and copy the secret directly into the consumer’s secret store. The console shows it only once. To rotate, issue a replacement, switch and verify the consumer, then select **Revoke** beside the old token.
4. **Sign out** ends the console session, not the client grants. Sessions last at most five minutes.

In production SPI has `cards:read` and `cards:link`. The two n8n alert workflows each have a separate `messages:send` + `messages:advanced` grant for structured alerts; neither has `messages:edit`.

The legacy `/v1/*` handlers are disabled, including inside the cluster. `/telegram/webhook` still uses Telegram’s webhook secret, not a bearer token, and `/healthz` remains private.
