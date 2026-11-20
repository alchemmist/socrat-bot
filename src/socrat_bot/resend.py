"""Owner-initiated catch-up using original mailing identities, never a new broadcast."""

import asyncio
import os
import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .db import session
from .logic import is_owner, mailing_audience
from .mailing_compose import GROUPS, group_label
from .models import Broadcast, BroadcastCopy, ResendClaim


from .telegram_reply import reply_text


def allowed(user, chat):
    with session() as db:
        return user == chat and is_owner(db, user)


async def start(update, context):
    message = update.message
    if not allowed(update.effective_user.id, message.chat_id):
        await reply_text(
            message, "Команда доступна только владельцу бота в личном чате."
        )
        return
    args = context.args or []
    if not args:
        problem = "Не указано, сколько последних рассылок проверить."
    elif len(args) != 1:
        problem = (
            "После /resend нужно указать только одно число. Группу ты выберешь кнопкой."
        )
    elif not re.fullmatch(r"[1-9][0-9]{0,8}", args[0]):
        problem = "Количество рассылок нужно записать целым числом от 1 до 999999999 без ведущих нулей."
    else:
        problem = None
    if problem:
        await reply_text(
            message,
            f"{problem}\n\nНапример, отправь /resend 5 и выбери группу. "
            "Бот проверит последние пять рассылок этой группы и дошлёт каждому только пропущенные. "
            "Сейчас ничего не отправлено.",
        )
        return
    n = args[0]
    await reply_text(
        message,
        f"Сколько последних рассылок проверить: {n}. Выбери группу, чтобы начать досылку пропущенных сообщений.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(group_label(g), callback_data=f"rs:{n}:{g}")]
                for g in GROUPS
            ]
        ),
    )


async def catch_up(owner, group, n, bot):
    with session() as db:
        ids = list(
            db.scalars(
                select(Broadcast.id)
                .join(BroadcastCopy)
                .where(
                    Broadcast.chat_id == owner,
                    Broadcast.status == "sent",
                    Broadcast.audience_group == group,
                )
                .group_by(Broadcast.id)
                .order_by(func.min(BroadcastCopy.sent_at).desc(), Broadcast.id.desc())
                .limit(n)
            )
        )
    sent = failed = uncertain = 0
    for identity in reversed(ids):
        with session() as db:
            row = db.get(Broadcast, identity)
            known = set(
                db.scalars(
                    select(BroadcastCopy.chat_id).where(
                        BroadcastCopy.broadcast_id == identity
                    )
                )
            )
            known.update(
                item["accepted_untracked"]["chat_id"]
                for item in row.failed or []
                if isinstance(item, dict) and "accepted_untracked" in item
            )
            recipients = [
                u.telegram_id
                for u in mailing_audience(db, group)
                if u.telegram_id not in known
            ]
        for chat in recipients:
            key = identity, chat
            try:
                with session() as db:
                    if db.get(BroadcastCopy, key):
                        continue
                    db.add(ResendClaim(broadcast_id=identity, chat_id=chat))
            except IntegrityError:  # concurrent or unresolved attempt
                uncertain += 1
                continue
            with session() as db:
                row = db.get(Broadcast, identity)
                text, photo, entities = row.text, row.photo_id, row.entities or []
            native = [MessageEntity.de_json(item, bot) for item in entities]
            try:
                if photo:
                    msg = await bot.send_photo(
                        chat,
                        photo,
                        caption=text,
                        caption_entities=native,
                        parse_mode=None,
                    )
                else:
                    msg = await bot.send_message(
                        chat, text, entities=native, parse_mode=None
                    )
            except (BadRequest, Forbidden, RetryAfter):
                with session() as db:
                    db.delete(db.get(ResendClaim, key))
                failed += 1
            except TelegramError:  # Telegram may have accepted the message: keep claim.
                uncertain += 1
            else:
                with session() as db:
                    db.add(
                        BroadcastCopy(
                            broadcast_id=identity,
                            chat_id=chat,
                            message_id=msg.message_id,
                            applied_text=text,
                            applied_entities=entities,
                            applied_photo_id=photo,
                        )
                    )
                    # Keep the reservation: even a stale concurrent snapshot cannot send again.
                sent += 1
            await asyncio.sleep(float(os.getenv("BROADCAST_PAUSE_SECONDS", "0.4")))
    return f"{group.upper()} · {len(ids)} tracked mailings. Delivered: {sent}; failed: {failed}; pending/unknown: {uncertain}."


async def run_callback(query, bot):
    parts = query.data.split(":")
    if not (
        allowed(query.from_user.id, query.message.chat_id)
        and len(parts) == 3
        and re.fullmatch(r"[1-9][0-9]{0,8}", parts[1])
        and parts[2] in GROUPS
    ):
        await query.answer("Unavailable.")
        return
    await query.answer()
    await query.edit_message_text(f"Checking {parts[2].upper()} mailing history…")
    await query.edit_message_text(
        await catch_up(query.from_user.id, parts[2], int(parts[1]), bot)
    )
