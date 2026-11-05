"""Owner-only editable mailing history, bounded HTML panels and read-only paging."""

from html import escape

from sqlalchemy import func, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from socrat_bot.db import session
from socrat_bot.logic import as_utc, broadcast_zone, is_owner
from socrat_bot.models import Broadcast, BroadcastCopy

# Conservative raw-HTML UTF-16 budget also fits Telegram's post-entity limit.
PANEL_LIMIT = 3900
TEXT_BUDGET = 850


def units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def excerpt(value: str) -> str:
    """Escape first, truncate without splitting an entity or an astral character."""
    if units(escape(value, quote=False)) <= TEXT_BUDGET:
        return escape(value, quote=False)
    result, used = [], 0
    for char in value:
        escaped = escape(char, quote=False)
        if used + units(escaped) > TEXT_BUDGET - 1:
            break
        result.append(escaped)
        used += units(escaped)
    return "".join(result) + "…"


def editable_mailings(owner_id: int) -> list[tuple]:
    # Receipt time, not UUID order, determines actual delivery chronology. Multiple
    # recipients produce one history entry; edits never change its original timestamp.
    delivered_at = func.max(BroadcastCopy.sent_at)
    with session() as db:
        return list(db.execute(
            select(Broadcast.id, Broadcast.text, delivered_at)
            .join(BroadcastCopy, BroadcastCopy.broadcast_id == Broadcast.id)
            .where(Broadcast.chat_id == owner_id, Broadcast.status == "sent")
            .group_by(Broadcast.id, Broadcast.text)
            .order_by(delivered_at.desc(), Broadcast.id.desc())
        ))


def panel(owner_id: int, *, show_all: bool = False, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = editable_mailings(owner_id)
    if not rows:
        return (
            ("<b>Editable mailings</b>\n\nNo editable mailings yet.\n"
             "New mailings will appear here after delivery."),
            None,
        )
    total = len(rows)
    selected = rows if show_all else rows[:3]
    blocks = []
    zone = broadcast_zone()
    for number, (identity, text, sent_at) in enumerate(selected, 1):
        date = as_utc(sent_at).astimezone(zone).strftime("%d %b %Y · %H:%M")
        blocks.append(
            f"<b>{number}.</b> <code>{identity}</code>\n"
            f"<i>{escape(date)} · {escape(zone.key)}</i>\n"
            f"<pre>{excerpt(text)}</pre>"
        )
    footer = (
        "\n\n<b>Edit a mailing</b>\n<code>/mailing_edit UUID NEW_TEXT</code>\n"
        "Or reply to its delivery summary with <code>/mailing_edit NEW_TEXT</code>."
    )
    # Show all expands short lists in-place. Large histories get bounded pages,
    # rather than an invalid >4096-character edit or a flood of owner messages.
    pages: list[list[str]] = [[]]
    for block in blocks:
        candidate = "\n\n".join(pages[-1] + [block])
        if pages[-1] and units(candidate + footer) > PANEL_LIMIT - 200:
            pages.append([])
        pages[-1].append(block)
    page = min(max(page, 0), len(pages) - 1)
    caption = f"All {total} mailings" if show_all else f"Latest {len(selected)} of {total}"
    if len(pages) > 1:
        caption += f" · Page {page + 1}/{len(pages)}"
    text = "<b>Editable mailings</b>\n" + caption + "\n\n" + "\n\n".join(pages[page]) + footer
    buttons = []
    if show_all:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("◀ Previous", callback_data=f"ml:all:{page - 1}"))
        if page + 1 < len(pages):
            navigation.append(InlineKeyboardButton("Next ▶", callback_data=f"ml:all:{page + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons.append([InlineKeyboardButton("Latest 3", callback_data="ml:recent")])
    elif total > 3:
        buttons.append([InlineKeyboardButton("Show all", callback_data="ml:all:0")])
    assert units(text) <= PANEL_LIMIT
    return text, InlineKeyboardMarkup(buttons) if buttons else None


async def on_list_callback(query) -> bool:
    if not query.data.startswith("ml:"):
        return False
    message = query.message
    if message is None or message.chat_id != query.from_user.id:
        await query.answer("Owner private chat only.")
        return True
    with session() as db:
        allowed = is_owner(db, query.from_user.id)
    if not allowed:
        await query.answer("Owner only.")
        return True
    if query.data == "ml:recent":
        show_all, page = False, 0
    else:
        parts = query.data.split(":")
        if len(parts) != 3 or parts[1] != "all" or not parts[2].isascii() or not parts[2].isdigit() or len(parts[2]) > 8:
            await query.answer("Invalid page.")
            return True
        show_all, page = True, int(parts[2])
    text, markup = panel(query.from_user.id, show_all=show_all, page=page)
    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    return True
