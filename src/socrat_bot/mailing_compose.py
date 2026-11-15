"""Durable owner/private group-first composition, independent of PTB memory."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy import update as sql_update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest

from .db import session
from .entities import command_body, normalize
from .interests import BY_KEY
from .logic import broadcast_zone, ensure_user
from .models import Broadcast

GROUPS = ("vpn", "alerts", "llm")
COMPOSING = ("selecting_group", "awaiting_text")


def group_label(group):
    return BY_KEY[group].label if group in GROUPS else "Unknown"


def controls(identity, groups=False):
    rows = (
        [
            [
                InlineKeyboardButton(
                    group_label(key), callback_data=f"mg:{identity}:{key}"
                )
            ]
            for key in GROUPS
        ]
        if groups
        else []
    )
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"mg:{identity}:cancel")])
    return InlineKeyboardMarkup(rows)


async def edit(query, body, **kwargs):
    try:
        await query.edit_message_text(body, **kwargs)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def preview(message, context, identity):
    # Never project user text into a control message or reinterpret native entities.
    from .app import _date_keyboard

    with session() as db:
        row = db.get(Broadcast, identity)
        if not row or row.status != "group_choosing":
            return
        body, entities, group = row.text, row.entities, row.audience_group
    await message.reply_text(
        body,
        entities=[
            MessageEntity.de_json(item, getattr(context, "bot", None))
            for item in entities
        ],
        parse_mode=None,
    )
    response = await message.reply_text(
        f"Group: {group_label(group)}. Pick a day. Time is {broadcast_zone().key}.",
        reply_markup=_date_keyboard(identity, datetime.now(broadcast_zone())),
    )
    with session() as db:
        db.execute(
            sql_update(Broadcast)
            .where(Broadcast.id == identity, Broadcast.status == "group_choosing")
            .values(message_id=response.message_id)
        )


async def start(update, context):
    message, actor = update.message, update.effective_user
    if not message or not actor:
        return
    if message.chat_id != actor.id:
        await message.reply_text("This command is available in private chat only.")
        return
    # Check permission before parsing or creating durable drafts.
    with session() as db:
        allowed = ensure_user(db, actor.id).role == "owner"
    if not allowed:
        await message.reply_text("This command is available to the owner only.")
        return
    try:
        body, entities = command_body(message)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    with session() as db:
        if ensure_user(db, actor.id).role != "owner":
            return
        db.execute(
            sql_update(Broadcast)
            .where(Broadcast.chat_id == actor.id, Broadcast.status.in_(COMPOSING))
            .values(status="cancelled")
        )
        row = Broadcast(
            chat_id=actor.id,
            text=body,
            entities=entities,
            status="selecting_group",
            audience_group=None,
        )
        db.add(row)
        db.flush()
        identity = row.id
    response = await message.reply_text(
        "Choose one mailing group. Then send the message text."
        if not body
        else "Choose one mailing group for this message.",
        reply_markup=controls(identity, True),
    )
    with session() as db:
        db.execute(
            sql_update(Broadcast)
            .where(Broadcast.id == identity, Broadcast.status == "selecting_group")
            .values(message_id=response.message_id)
        )


async def group_callback(query, context):
    if not query.data.startswith("mg:"):
        return False
    await query.answer()
    try:
        _, raw, group = query.data.split(":")
        identity = uuid.UUID(raw)
    except ValueError:
        return True
    if (
        group not in (*GROUPS, "cancel")
        or not query.message
        or query.message.chat_id != query.from_user.id
    ):
        return True
    with session() as db:
        if ensure_user(db, query.from_user.id).role != "owner":
            return True
        row = db.scalar(
            select(Broadcast)
            .where(Broadcast.id == identity, Broadcast.chat_id == query.from_user.id)
            .with_for_update()
        )
        if not row or row.status not in COMPOSING:
            return True
        if group == "cancel":
            row.status = "cancelled"
            state = "cancelled"
        elif row.status != "selecting_group":
            return True  # A stale/double click cannot retarget an admitted group.
        else:
            row.audience_group = group
            state = row.status = "group_choosing" if row.text else "awaiting_text"
    if state == "cancelled":
        await edit(query, "Mailing cancelled.")
    elif state == "awaiting_text":
        await edit(
            query,
            f"Group: {group_label(group)}. Send the message text (formatting and hidden links are supported).",
            reply_markup=controls(identity),
        )
    else:
        await edit(query, f"Group: {group_label(group)} selected.")
        await preview(query.message, context, identity)
    return True


async def receive_text(update, context):
    message, actor = update.message, update.effective_user
    if (
        not message
        or not actor
        or message.chat_id != actor.id
        or not message.text
        or message.text.startswith("/")
    ):
        return
    with session() as db:
        if ensure_user(db, actor.id).role != "owner":
            return
        row = db.scalar(
            select(Broadcast)
            .where(Broadcast.chat_id == actor.id, Broadcast.status == "awaiting_text")
            .with_for_update()
        )
        if not row:
            return
        try:
            entities = normalize(message.text, getattr(message, "entities", None))
        except ValueError as exc:
            error = str(exc)
            identity = None
        else:
            row.text, row.entities, row.status = (
                message.text,
                entities,
                "group_choosing",
            )
            identity, error = row.id, None
    if error:
        await message.reply_text(error)
    else:
        await preview(message, context, identity)
