# Native mailing formatting

`/mailing TEXT` and `/mailing_edit UUID TEXT` (or reply `/mailing_edit TEXT`)
use the message's native Bot API entities, not Markdown/HTML interpretation.
Whitespace inside the body and trailing whitespace are retained; command/UUID
and the whitespace separating them from the body are removed. Offsets are
rebased in UTF-16 units; surrogate-splitting and out-of-range entities are rejected.
Styles crossing the removed prefix are clipped (bold, italic, underline,
strikethrough, spoiler, code/pre, blockquotes). Other crossing entities are dropped:
a link/mention/custom emoji is never reassigned to only part of its original text.
Nested/overlapping native entity order and metadata are preserved, including hidden
URLs, pre languages, text-mention users and custom emoji IDs. Telegram remains the
validator of semantic entity combinations.

The owner receives the formatted body as a separate preview message, followed by
the existing scheduling/Send now controls. This keeps the full 4096 UTF-16 body
budget without shifting heading/status entities. History remains escaped, bounded
plaintext HTML preview: hidden links are deliberately not reconstructed there.
The internal HTTP broadcast API remains plaintext; no new entity API is exposed.

Desired entities and each receipt's successfully applied entities are durable JSON.
A formatting-only edit updates existing receipt message IDs; partial failures retry
only mismatched receipts, and explicit empty entities remove prior formatting.
Old rows default to empty lists. Existing ownership, edit leases and receipt-only
(no replacement send/no audience expansion) behavior is unchanged.
