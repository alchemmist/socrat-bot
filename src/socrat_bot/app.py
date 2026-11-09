import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timezone, datetime

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field
from sqlalchemy import select, update as sql_update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from socrat_bot import media
from socrat_bot.cards import (
    Card,
    Link,
    add_link,
    add_note,
    build_alert,
    select_links,
)
from socrat_bot.cards import replace_spi_link as replace_spi_link
from socrat_bot.commands import COMMANDS, command_menu_loop, sync_owner_menu
from socrat_bot.db import init_schema, session
from socrat_bot.delivery import (
    annotate_card,
    delivered_copies,
    dispatch_jobs,
    publish_spi,
    spi_acknowledgements,
    submit_card,
)
from socrat_bot.escalation import escalate, marker, outbox_loop
from socrat_bot.grafana_rules import start_rule_refresh
from socrat_bot.logic import (
    alert_levels,
    apply_action,
    approve_alerts,
    audience,
    begin_mailing,
    broadcast_zone,
    card_messages,
    claim_due_broadcasts,
    consume_link,
    create_action,
    as_utc,
    deliver_broadcast,
    ensure_user,
    format_vpn_clients,
    get_card,
    is_owner,
    list_cards,
    mailing_dates,
    mailing_hours,
    mailing_slot,
    mark_vpn,
    owner_ids,
    owners,
    vpn_audience,
)
from socrat_bot.models import (
    Action,
    Broadcast,
    Incident,
    TelegramCard,
    User,
)

from socrat_bot.structured_logging import OperationalEvents, configure_logging

log = configure_logging()

ptb: Application | None = None
_updates: set[asyncio.Task] = set()


class Href(BaseModel):
    label: str
    url: str


class CardIn(BaseModel):
    key: str
    title: str
    where: str = ""
    summary: str = ""
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    links: list[str] | None = None
    hrefs: list[Href] = Field(default_factory=list)


class OutMessage(BaseModel):
    chat_id: int
    text: str = ""
    alerts: list[dict] = Field(default_factory=list)
    alert: dict | None = None
    card: CardIn | None = None
    actions: list[dict] = Field(default_factory=list)
    parse_mode: str | None = None
    images: list[media.ImageIn] | None = Field(default=None, max_length=3)


class PatchMessage(BaseModel):
    text: str


class LinkIn(BaseModel):
    panel_client_id: str
    ttl_seconds: int = 900


class BroadcastIn(BaseModel):
    text: str
    chat_id: int



def _saved_spi(key: str) -> tuple[str, str]:
    with session() as db:
        rows=db.scalars(select(TelegramCard).where(TelegramCard.key == key)).all()
        desired=max(rows,key=lambda r:r.desired_link_revision or 0,default=None)
        if desired is not None and desired.desired_spi_url:
            return desired.desired_spi_url,"SPI"
        row=next((r for r in rows if r.spi_url),None)
        return (row.spi_url,row.spi_label or "SPI")if row else ("","")


def _saved_note(key: str) -> str:
    with session() as db:
        row = db.scalar(select(TelegramCard).where(TelegramCard.key == key, TelegramCard.note.is_not(None)))
        return row.note or "" if row is not None else ""


def schedule_action_event(event: dict) -> None:
    url = os.environ.get("ACTION_WEBHOOK_URL", "").strip()
    if not url:
        return

    async def send() -> None:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("ACTION_WEBHOOK_TOKEN", "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(url, json=event, headers=headers)
        except Exception:
            log.error("action webhook failed")

    try:
        asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        pass


def require_internal(token: str | None) -> None:
    expected = os.environ.get("INTERNAL_TOKEN", "")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def keyboard(rows: list[tuple[str, str, int]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    grouped: dict[int, list[InlineKeyboardButton]] = {}
    for label, data, row in rows:
        grouped.setdefault(row, []).append(InlineKeyboardButton(label, callback_data=data))
    return InlineKeyboardMarkup([grouped[index] for index in sorted(grouped)])


def card_from_request(body: OutMessage) -> Card | None:
    if body.alert is not None:
        return build_alert(body.alert)
    if len(body.alerts) == 1:
        item = body.alerts[0]
        if "labels" not in item:
            item = {
                "status": item.get("status"),
                "name": item.get("name"),
                "summary": item.get("summary") or "",
                "labels": {},
                "annotations": {"summary": item.get("summary") or ""},
            }
        return build_alert(item)
    if body.card is None:
        return None
    raw = body.card
    labels = dict(raw.labels)
    if raw.links:
        links = select_links(labels, raw.links)
    else:
        links = []
    links = [*links, *[Link(item.label, item.url) for item in raw.hrefs]]
    status = "resolved" if raw.status == "resolved" else "firing"
    return Card(key=raw.key[:120], title=raw.title, where=raw.where, summary=raw.summary, status=status, links=links)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    arg = context.args[0] if context.args else ""
    with session() as db:
        person = ensure_user(db, user.id, user.username)
        uses_vpn = bool(person.uses_vpn)
        alerts = bool(person.alerts_enabled)
        owner = person.role == "owner"
        log.info("telegram start")
        if arg.startswith("link_"):
            outcome = consume_link(db, user.id, arg.removeprefix("link_"))
            text = "Link saved." if outcome == "linked" else "The code is invalid or already used."
        else:
            text = _start_text(uses_vpn, alerts)
    if arg.startswith("link_"):
        await update.message.reply_text(text)
        return
    keyboard = _start_keyboard(uses_vpn, alerts)
    await update.message.reply_text(text, reply_markup=keyboard)
    if owner and not await sync_owner_menu(context.bot, user.id):
        log.info("telegram commands failed")


def _date_keyboard(broadcast_id: uuid.UUID, now: datetime) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for day in mailing_dates(now):
        current.append(
            InlineKeyboardButton(day.strftime("%d %b"), callback_data=f"m:d:{broadcast_id}:{day.strftime('%Y%m%d')}")
        )
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([InlineKeyboardButton("Now", callback_data=f"m:n:{broadcast_id}")])
    return InlineKeyboardMarkup(rows)


def _hour_keyboard(broadcast_id: uuid.UUID, day, hours: list[int]) -> InlineKeyboardMarkup:
    stamp = day.strftime("%Y%m%d")
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for hour in hours:
        current.append(
            InlineKeyboardButton(
                f"{hour:02d}:00",
                callback_data=f"m:h:{broadcast_id}:{stamp}:{hour:02d}",
            )
        )
        if len(current) == 5:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return InlineKeyboardMarkup(rows)


async def _remember_usernames(telegram_ids: list[int]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not telegram_ids:
        return
    found: list[tuple[int, str]] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for telegram_id in telegram_ids:
            response = await client.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": telegram_id},
            )
            username = (response.json().get("result") or {}).get("username")
            if username:
                found.append((telegram_id, username))
    if not found:
        return
    with session() as db:
        for telegram_id, username in found:
            person = db.scalar(select(User).where(User.telegram_id == telegram_id))
            if person is not None:
                person.username = username


def _start_text(uses_vpn: bool, alerts: bool) -> str:
    lines = ["Socrat is online."]
    if uses_vpn:
        lines.append("You are on the VPN list.")
    if alerts:
        lines.append("You are subscribed to alerts.")
    return "\n".join(lines)


def _start_keyboard(uses_vpn: bool, alerts: bool) -> InlineKeyboardMarkup | None:
    buttons = []
    if not uses_vpn:
        buttons.append(InlineKeyboardButton("I use VPN", callback_data="vpn:yes"))
    if not alerts:
        buttons.append(InlineKeyboardButton("Subscribe to alerts", callback_data="al:yes"))
    else:
        buttons.append(InlineKeyboardButton("Unsubscribe", callback_data="al:no"))
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons])


def _person_label(user) -> str:
    username = getattr(user, "username", None)
    return f"@{username}" if username else str(user.id)


async def on_clients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    with session() as db:
        owner = is_owner(db, user.id)
        people = vpn_audience(db) if owner else []
        missing = [person.telegram_id for person in people if not person.username]
    if not owner:
        await message.reply_text("This command is available to the owner only.")
        return
    if missing:
        await _remember_usernames(missing)
        with session() as db:
            people = vpn_audience(db)
    await message.reply_text(format_vpn_clients(people))


async def on_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    if message.chat_id != user.id:
        await message.reply_text("This command is available in private chat only.")
        return
    from .entities import command_body
    try:
        text, entities = command_body(message)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    with session() as db:
        if not is_owner(db, user.id):
            await message.reply_text("This command is available to the owner only.")
            return
        if not text:
            await message.reply_text("Usage: /mailing message text")
            return
        row = begin_mailing(db, user.id, text, entities)
        broadcast_id = row.id
    from telegram import MessageEntity
    await message.reply_text(text, entities=[MessageEntity.de_json(item, getattr(context, "bot", None)) for item in entities], parse_mode=None)
    response = await message.reply_text(
        "Pick a day. Time is Europe/Moscow.",
        reply_markup=_date_keyboard(broadcast_id, datetime.now(broadcast_zone())),
    )

    with session() as db:
        db.get(Broadcast, broadcast_id).message_id = response.message_id


async def on_mailing_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from sqlalchemy import select
    from .mailing import edit_mailing

    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    with session() as db:
        allowed = is_owner(db, user.id)
    if not allowed or message.chat_id != user.id:
        await message.reply_text("This command is available to the owner in private chat only.")
        return
    raw = (message.text or "").split(maxsplit=1)
    value = raw[1] if len(raw) > 1 else ""
    broadcast_id = None
    if message.reply_to_message:
        with session() as db:
            broadcast_id = db.scalar(select(Broadcast.id).where(
                Broadcast.chat_id == user.id,
                Broadcast.message_id == message.reply_to_message.message_id,
                Broadcast.status == "sent",
            ))
    body_tokens = 1 if broadcast_id is not None else 2
    if broadcast_id is None and value:
        parts = value.split(maxsplit=1)
        try:
            broadcast_id = uuid.UUID(parts[0])
            value = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass
    if broadcast_id is None or not value:
        from .mailing_list import panel
        text, markup = panel(user.id)
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        return
    from .entities import command_body
    try:
        value, entities = command_body(message, body_tokens)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    result = await edit_mailing(broadcast_id, user.id, value, entities)
    await message.reply_text(result[:4096])


async def _on_mailing_callback(query, data: str) -> bool:
    if not data.startswith("m:"):
        return False
    try:
        await query.answer()
    except Exception:
        pass
    parts = data.split(":")
    if len(parts) < 3:
        return True
    kind, raw_id = parts[1], parts[2]
    try:
        broadcast_id = uuid.UUID(raw_id)
    except ValueError:
        return True
    notice = None
    pending_text = None
    pending_ids: list[int] = []
    with session() as db:
        if not is_owner(db, query.from_user.id) or query.message is None or query.message.chat_id != query.from_user.id:
            notice = "This command is available to the owner only."
        else:
            row = db.get(Broadcast, broadcast_id)
            if row is None or row.status != "choosing":
                return True
            now = datetime.now(broadcast_zone())
            if kind == "d" and len(parts) == 4:
                day = datetime.strptime(parts[3], "%Y%m%d").date()
                hours = mailing_hours(day, now)
                label = day.strftime("%d %b")
                if not hours:
                    await query.edit_message_text(
                        f"{label} has no hours left. Pick another day.",
                        reply_markup=_date_keyboard(broadcast_id, now),
                    )
                else:
                    await query.edit_message_text(
                        f"{label}. Pick an hour, Europe/Moscow.",
                        reply_markup=_hour_keyboard(broadcast_id, day, hours),
                    )
                return True
            if kind == "h" and len(parts) == 5:
                day = datetime.strptime(parts[3], "%Y%m%d").date()
                hour = int(parts[4])
                if hour not in mailing_hours(day, now):
                    await query.edit_message_text(
                        "That hour has already passed. Pick another day.",
                        reply_markup=_date_keyboard(broadcast_id, now),
                    )
                    return True
                send_at = mailing_slot(day, hour)
                row.send_at = send_at
                row.status = "scheduled"
                count = len(vpn_audience(db))
                local = send_at.astimezone(broadcast_zone()).strftime("%Y-%m-%d %H:%M")
                notice = f"Scheduled for {local} {broadcast_zone().key}. VPN recipients now: {count}.\nMailing {broadcast_id}\nAfter delivery: /mailing_edit {broadcast_id} NEW_TEXT"
            elif kind == "n":
                claimed = db.execute(sql_update(Broadcast).where(
                    Broadcast.id == broadcast_id, Broadcast.status == "choosing",
                    Broadcast.chat_id == query.from_user.id,
                ).values(status="sending")).rowcount
                if not claimed:
                    return True
                pending_text = row.text
                pending_ids = [user.telegram_id for user in vpn_audience(db)]
            else:
                return True
    if pending_text is not None:
        result = await deliver_broadcast(pending_text, pending_ids, broadcast_id)
        with session() as db:
            stored = db.get(Broadcast, broadcast_id)
            if stored is not None:
                stored.status = "sent"
                stored.sent = result["sent"]
                stored.failed = result["failed"] + [{"accepted_untracked": item} for item in result.get("untracked", [])]
        notice = _delivery_notice("Sent now", result) + f"\nMailing {broadcast_id}\nReply: /mailing_edit NEW_TEXT"
    if notice:
        await query.edit_message_text(notice)
    return True


async def _request_alerts(query) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    user = query.from_user
    if query.message is None:
        return
    if query.message.chat_id != user.id:
        await query.message.reply_text("Alert settings are available in private chat only.")
        return
    with session() as db:
        person = ensure_user(db, user.id, user.username)
        approved = person.alerts_approved
        if approved:
            person.alerts_enabled = True
            text = _start_text(bool(person.uses_vpn), True)
            markup = _start_keyboard(bool(person.uses_vpn), True)
        targets = [(owner.telegram_id, owner.username) for owner in owners(db)] if not approved else []
    if approved:
        await query.edit_message_text(text, reply_markup=markup)
        return
    if not targets:
        await query.message.reply_text("No owner is available to approve this.")
        return
    label = _person_label(user)
    for telegram_id, _username in targets:
        await query.get_bot().send_message(
            telegram_id,
            f"{label} wants alert notifications.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Approve", callback_data=f"al:ok:{user.id}")]]
            ),
        )
    await query.message.reply_text("Request sent. An owner has to approve it.")


async def _approve_alerts(query) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        telegram_id = int(query.data.removeprefix("al:ok:"))
    except ValueError:
        return
    with session() as db:
        if not is_owner(db, query.from_user.id):
            await query.edit_message_text("This action is available to the owner only.")
            return
        existing = db.scalar(select(User).where(User.telegram_id == telegram_id))
        already_approved = bool(existing and existing.alerts_approved)
        person = approve_alerts(db, telegram_id)
        label = f"@{person.username}" if person.username else str(person.telegram_id)
    if already_approved:
        await query.edit_message_text(f"Already approved {label}.")
        return
    await query.edit_message_text(f"Approved {label}.")
    schedule_action_event({"event": "alerts.approved", "telegram_id": person.telegram_id, "username": person.username})
    try:
        await query.get_bot().send_message(telegram_id, "You are subscribed to alerts.")
    except Exception:
        pass


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    from .mailing_list import on_list_callback
    if await on_list_callback(query):
        return
    if query.data.startswith("esc:"):
        try:
            _, digest, generation, level = query.data.split(":")
            with session() as db:
                result = escalate(db, query.from_user.id, query.message.chat_id, query.message.message_id, digest, int(generation), int(level))
            await query.answer(result)
        except (ValueError, AttributeError):
            await query.answer("Invalid escalation")
        return
    if query.data.startswith("rl:"):
        with session() as db:
            person = ensure_user(db, query.from_user.id)
            try:
                level = int(query.data.split(":")[1])
            except ValueError:
                level = -1
            allowed = query.message.chat_id == query.from_user.id and person.alerts_enabled and level in alert_levels() and level >= person.admin_level
            if allowed:
                person.receive_level = level
                settings_text = f"Alerts: enabled\nAdmin level: {person.admin_level}\nReceive level: {person.receive_level}"
                settings_rows = [[InlineKeyboardButton(f"Receive {n}", callback_data=f"rl:{n}") for n in alert_levels() if n >= person.admin_level], [InlineKeyboardButton("Unsubscribe", callback_data="al:no")]]
        await query.answer(f"Receive level: {level}" if allowed else "Not allowed")
        if allowed:
            await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(settings_rows))
        return
    if await _on_mailing_callback(query, query.data):
        return
    if query.data == "al:no":
        await query.answer()
        if query.message is None or query.message.chat_id != query.from_user.id:
            return
        with session() as db:
            person = ensure_user(db, query.from_user.id, query.from_user.username)
            person.alerts_enabled = False
            markup = _start_keyboard(bool(person.uses_vpn), False)
        await query.edit_message_text("You are unsubscribed from alerts. You can subscribe again at any time.", reply_markup=markup)
        return
    if query.data == "al:yes":
        await _request_alerts(query)
        return
    if query.data.startswith("al:ok:"):
        await _approve_alerts(query)
        return
    if query.data == "vpn:yes":
        try:
            await query.answer()
        except Exception:
            pass
        with session() as db:
            person = mark_vpn(db, query.from_user.id, query.from_user.username)
            text = _start_text(True, bool(person.alerts_enabled))
            keyboard = _start_keyboard(True, bool(person.alerts_enabled))
            schedule_action_event({"event": "vpn.opt_in", "telegram_id": person.telegram_id, "username": person.username})
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            pass
        return
    if not query.data.startswith("act:"):
        return
    try:
        await query.answer()
    except Exception:
        pass
    actor = str(query.from_user.id)
    mute_generation = None
    action_id = uuid.UUID(query.data.removeprefix("act:"))
    from sqlalchemy import update as sql_update
    legacy = None
    with session() as db:
        candidate = db.get(Action, action_id)
        if candidate and candidate.type == "broadcast.send":
            if not is_owner(db, query.from_user.id):
                return
            if candidate.status != "pending" or (candidate.expires_at and as_utc(candidate.expires_at) < datetime.now(timezone.utc)):
                return
            claimed = db.execute(sql_update(Action).where(Action.id == action_id, Action.status == "pending").values(status="running")).rowcount
            if not claimed:
                return
            row = db.get(Broadcast, uuid.UUID(candidate.payload["broadcast_id"]))
            if row is None or row.status != "preview":
                candidate.status = "failed"
                return
            row.status = "sending"
            legacy = (row.id, row.text, [person.telegram_id for person in vpn_audience(db)])
    if legacy:
        broadcast_id, body, recipients = legacy
        result = await deliver_broadcast(body, recipients, broadcast_id)
        with session() as db:
            row = db.get(Broadcast, broadcast_id)
            row.status, row.sent, row.failed = "sent", result["sent"], result["failed"] + [{"accepted_untracked": item} for item in result.get("untracked", [])]
            action = db.get(Action, action_id)
            action.status, action.result = "done", result
        await query.edit_message_text(_delivery_notice("Broadcast", result) + f"\nMailing {broadcast_id}\nReply: /mailing_edit NEW_TEXT")
        return
    with session() as db:
        action = db.get(Action, action_id)
        if action is None:
            return
        person = ensure_user(db, query.from_user.id, query.from_user.username)
        allowed = person.role == "owner" or (action.type == "alert.silence" and person.alerts_enabled)
        if not allowed:
            await query.edit_message_text("This action is available to the owner only.")
            return
        if action.type == "alert.silence" and action.payload.get("key"):
            incident = db.get(Incident, action.payload["key"])
            copy = db.get(TelegramCard, (action.payload["key"], action.chat_id))
            if incident and (incident.status != "firing" or not copy or copy.generation != incident.generation or f":g{incident.generation}:" not in action.idempotency_key):
                return
            mute_generation = incident.generation if incident else None
        result = await apply_action(db, action, actor)
        text = _result_text(action, result)
        html = bool((action.payload or {}).get("card"))
        copies = [(action.chat_id, action.message_id)] if action.message_id else []
        if action.type == "alert.silence" and result.get("status") == "silenced":
            key = (action.payload or {}).get("key") or ""
            schedule_action_event({
                "event": "alert.muted",
                "telegram_id": query.from_user.id,
                "username": query.from_user.username,
                "key": key,
                "minutes": int((action.payload or {}).get("minutes") or 0),
            })
            if key:
                copies = card_messages(db, key) or copies
                spi_url, spi_label = _saved_spi(key)
                if spi_url:
                    text = add_link(text, spi_url, spi_label)
                note = _saved_note(key)
                if note:
                    text = add_note(text, note)
                incident = db.get(Incident, key)
                if incident:
                    text = marker(text, incident)
        else:
            key = ""
    if key and text:
        footer = text[text.rfind("<i>"):] if "<i>" in text else "<i>Muted</i>"
        try:
            _recipients, jobs = await asyncio.to_thread(
                annotate_card, key, mute=footer, expected_generation=mute_generation
            )
        except ValueError:
            # A silence request may finish after this incident has resolved/refired.
            # Never carry an old callback's footer/buttons into the new generation.
            return
        await dispatch_jobs(jobs)
        return
    for chat_id, message_id in copies:
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML" if html else None)
        except Exception:
            pass


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(15)
        try:
            with session() as db:
                due = [row.id for row in claim_due_broadcasts(db)]
            for broadcast_id in due:
                with session() as db:
                    row = db.get(Broadcast, broadcast_id)
                    if row is None:
                        continue
                    text = row.text
                    recipients = [user.telegram_id for user in vpn_audience(db)]
                result = await deliver_broadcast(text, recipients, broadcast_id)
                with session() as db:
                    row = db.get(Broadcast, broadcast_id)
                    if row is not None:
                        row.status = "sent"
                        row.sent = result["sent"]
                        row.failed = result["failed"] + [{"accepted_untracked": item} for item in result.get("untracked", [])]
                log.info("broadcast completed", extra={"sent": result["sent"], "failed": result["failed"]})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("broadcast loop failed")


def _result_text(action: Action, result: dict) -> str:
    if action.type == "alert.silence":
        minutes = int((action.payload or {}).get("minutes") or 60)
        span = {15: "15 minutes", 30: "30 minutes", 60: "1 hour"}.get(minutes, f"{minutes} minutes")
        footer = f"Muted for {span}." if result.get("status") == "silenced" else f"Mute failed: {result}"
        card = (action.payload or {}).get("card")
        return f"{card}\n\n<i>{footer}</i>" if card else footer
    if action.type == "xui.upgrade":
        if result.get("status") == "updated":
            return f"3X-UI updated: {result.get('image')}"
        if result.get("status") == "rolled_back":
            return "Automation restored the working version. Further checks have to be done manually."
        return f"Upgrade: {result}"
    if action.type == "broadcast.send":
        return _delivery_notice("Broadcast", result)
    if action.type == "broadcast.cancel":
        return "Broadcast cancelled."
    if action.type in {"sudo.approve", "sudo.deny"}:
        return f"Sudo {result.get('status')}: {result.get('argv')}"
    return str(result)


def _delivery_notice(title: str, result: dict) -> str:
    sent = result.get("sent", 0)
    failed = result.get("failed") or []
    text = f"{title}. Delivered {sent}."
    if result.get("untracked"):
        details = ", ".join(f"{copy['chat_id']}/{copy['message_id']}" for copy in result["untracked"])
        text += f"\nWARNING: accepted but receipt not saved (chat/message): {details}. Do not resend."

    if not failed:
        return text
    lines = "\n".join(f"\u2022 {item}" for item in failed)
    return f"{text}\nNot accepted:\n{lines}"


async def _register_webhook() -> None:
    url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
    ip = os.environ.get("TELEGRAM_WEBHOOK_IP", "")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not (url and ip and secret and token):
        return
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={
                "url": url,
                "secret_token": secret,
                "ip_address": ip,
                "allowed_updates": ["message", "callback_query"],
            },
        )
    log.info("telegram webhook completed", extra={"status": response.status_code})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb
    alert_levels()
    init_schema()
    start_rule_refresh()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    command_ticker = None
    if token:
        ptb = Application.builder().token(token).updater(None).build()
        for command in COMMANDS:
            ptb.add_handler(CommandHandler(command.name, globals()[command.handler]))
        ptb.add_handler(CallbackQueryHandler(on_callback))
        await ptb.initialize()
        await ptb.start()
        await _register_webhook()
        menu_owners = owner_ids()
        with session() as db:
            menu_owners.update(person.telegram_id for person in owners(db))
        command_ticker = asyncio.create_task(command_menu_loop(ptb.bot, menu_owners))
    ticker = asyncio.create_task(_broadcast_loop())
    escalation_ticker = asyncio.create_task(outbox_loop())
    media_ticker = asyncio.create_task(media.outbox_loop())
    yield
    ticker.cancel()
    escalation_ticker.cancel()
    media_ticker.cancel()
    await asyncio.gather(media_ticker, return_exceptions=True)
    if command_ticker is not None:
        command_ticker.cancel()
        await asyncio.gather(command_ticker, return_exceptions=True)
    if ptb is not None:
        await ptb.stop()
        await ptb.shutdown()


api = FastAPI(lifespan=lifespan)
api.add_middleware(OperationalEvents, component="bot")


@api.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@api.post("/telegram/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or x_telegram_bot_api_secret_token != expected or ptb is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    payload = await request.json()
    task = asyncio.create_task(_run_update(payload))
    _updates.add(task)
    task.add_done_callback(_updates.discard)
    return {"ok": True}


async def _run_update(payload: dict) -> None:
    if ptb is None:
        return
    try:
        update = Update.de_json(payload, ptb.bot)
        await ptb.process_update(update)
    except Exception:
        log.error("telegram update failed")


@api.post("/v1/messages")
async def post_message(body: OutMessage, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    if len(body.alerts) > 1:
        raise HTTPException(status_code=400, detail="one alert per message")
    try:
        card = card_from_request(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.images and card is None:
        raise HTTPException(status_code=400, detail="images require card, alert, or singleton alerts")
    if card is not None:
        from socrat_bot.primary_graphs import capture as capture_primary_graph
        raw_alert = body.alert or (body.alerts[0] if body.alerts else None)
        primary_graph = await capture_primary_graph(card.key, raw_alert)
        recipients, jobs, snapshot = await asyncio.to_thread(
            submit_card, card, body.actions, body.images, metadata=True, primary_graph=primary_graph
        )
        await dispatch_jobs(jobs)
        if body.images:
            try:
                async with asyncio.timeout(2):
                    await media.drain(limit=1, key=card.key, generation=snapshot["generation"])
            except TimeoutError:
                pass
        attachments = await asyncio.to_thread(media.status, card.key, recipients, snapshot)
        copies = await asyncio.to_thread(delivered_copies, card.key, recipients, jobs)
        if recipients and not copies:
            raise HTTPException(status_code=502, detail={"message": "delivery pending; durable retry retained", "attachments": attachments} if attachments else "delivery pending; durable retry retained")
        episode_meta = {"episode": card.key.rsplit("/spi/", 1)[1], "immutable_key": True} if "/spi/" in card.key else {}
        if attachments:
            episode_meta["attachments"] = attachments
        if not copies:
            return {"key": card.key, "copies": [], **episode_meta}
        chosen = next((c for c in copies if c["chat_id"] == body.chat_id), copies[0])
        return {"key": card.key, "id": chosen["message_id"], "chat_id": chosen["chat_id"], "copies": copies, **episode_meta}
    if not body.text:
        raise HTTPException(status_code=400, detail="text or alert required")
    buttons, action_ids, existing = [], [], None
    with session() as db:
        for item in body.actions:
            action = create_action(db, type_=item["type"], payload=dict(item.get("payload") or {}), chat_id=body.chat_id,
                idempotency_key=item.get("idempotency_key") or str(uuid.uuid4()))
            action_ids.append(action.id)
            existing = existing or action.message_id
            buttons.append((item.get("label") or action.type, f"act:{action.id}", int(item.get("row") or 0)))
    payload = {"chat_id": body.chat_id, "text": body.text}
    if body.parse_mode:
        payload["parse_mode"] = body.parse_mode
    markup = keyboard(buttons)
    if markup:
        payload["reply_markup"] = markup.to_dict()
    if existing:
        payload["message_id"] = existing
    method = "editMessageText" if existing else "sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}", json=payload)
        data = response.json()
    if not data.get("ok") and "message is not modified" not in str(data):
        raise HTTPException(status_code=502, detail="Telegram delivery rejected")
    message_id = existing or data["result"]["message_id"]
    with session() as db:
        for id_ in action_ids:
            db.get(Action, id_).message_id = message_id
    return {"id": message_id, "chat_id": body.chat_id, "copies": [{"chat_id": body.chat_id, "message_id": message_id}], **({"replayed": True} if existing else {})}


class CardLinkIn(BaseModel):
    url: str
    label: str = "SPI"
    key: str | None = None
    alert: dict | None = None
    expected_copies: list[dict] | None = None
    revision: int | None = None


@api.post("/v1/cards/links")
async def add_card_link(body: CardLinkIn, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    key = body.key or (build_alert(body.alert).key if body.alert else None)
    if not key:
        raise HTTPException(status_code=400, detail="key or alert required")

    pairs, jobs = await asyncio.to_thread(publish_spi, key, body.url, body.label, body.revision, body.expected_copies)
    await dispatch_jobs(jobs)
    return await asyncio.to_thread(spi_acknowledgements, key, pairs, jobs, body.url, body.revision)




@api.get("/v1/cards/item")
def get_card_item(key: str, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    if not key:
        raise HTTPException(status_code=400, detail="key required")
    with session() as db:
        found = get_card(db, key)
    if found is None:
        raise HTTPException(status_code=404, detail="card not found")
    return found


@api.get("/v1/cards")
def get_cards(status: str = "firing", episode: str | None = None, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    if status not in {"firing", "resolved", "all"}:
        raise HTTPException(status_code=400, detail="status must be firing, resolved or all")
    with session() as db:
        cards=list_cards(db, status)
    if episode is not None:
        import re
        if not re.fullmatch(r"[a-f0-9]{64}",episode):
            raise HTTPException(status_code=400,detail="invalid episode")
        cards=[c for c in cards if c["key"].endswith("/spi/"+episode)]
        for c in cards:
            c.update(episode=episode, immutable_key=True)
    return {"cards":cards}


@api.get("/v1/audience")
def get_audience(x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    with session() as db:
        return audience(db)


class CardNoteIn(BaseModel):
    text: str
    key: str | None = None
    alert: dict | None = None


@api.post("/v1/cards/notes")
async def add_card_note(body: CardNoteIn, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    if body.key:
        key = body.key
    elif body.alert:
        key = build_alert(body.alert).key
    else:
        raise HTTPException(status_code=400, detail="key or alert required")
    cleaned = " ".join(body.text.split())
    if not cleaned:
        raise HTTPException(status_code=400, detail="note required")
    if len(cleaned) > 240:
        raise HTTPException(status_code=400, detail="note is too long")

    try:
        recipients, jobs = await asyncio.to_thread(annotate_card, key, note=cleaned)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await dispatch_jobs(jobs)
    return {"key": key, "updated": await asyncio.to_thread(delivered_copies, key, recipients, jobs)}


@api.patch("/v1/messages/{message_id}")
async def patch_message(
    message_id: int,
    body: PatchMessage,
    chat_id: int,
    x_internal_token: str | None = Header(default=None),
):
    require_internal(x_internal_token)
    with session() as db:
        stored = db.scalar(select(TelegramCard).where(TelegramCard.chat_id == chat_id, TelegramCard.message_id == message_id))
        target = (stored.key, stored.generation) if stored else None
    if target:
        try:
            _recipients, jobs = await asyncio.to_thread(annotate_card, target[0], replacement=body.text, chat_id=chat_id, expected_generation=target[1])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await dispatch_jobs(jobs)
        completed = await asyncio.to_thread(delivered_copies, target[0], [chat_id], jobs)
        if not completed:
            raise HTTPException(status_code=502, detail="delivery pending; durable retry retained")
        return {"id": message_id}
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": body.text},
        )
        data = response.json()
        if not data.get("ok"):
            raise HTTPException(status_code=502, detail=data)
    return {"id": message_id}


@api.post("/v1/link-codes")
def link_codes(body: LinkIn, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    from socrat_bot.logic import issue_link_code

    with session() as db:
        code = issue_link_code(db, body.panel_client_id, body.ttl_seconds)
    return {"code": code, "start": f"link_{code}"}


@api.post("/v1/broadcasts")
async def broadcasts(body: BroadcastIn, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    with session() as db:
        row = Broadcast(text=body.text, status="preview", chat_id=body.chat_id)
        db.add(row)
        db.flush()
        send = create_action(
            db,
            type_="broadcast.send",
            payload={"broadcast_id": str(row.id)},
            chat_id=body.chat_id,
            idempotency_key=f"broadcast-send-{row.id}",
        )
        cancel = create_action(
            db,
            type_="broadcast.cancel",
            payload={"broadcast_id": str(row.id)},
            chat_id=body.chat_id,
            idempotency_key=f"broadcast-cancel-{row.id}",
        )
        preview = f"Broadcast draft:\n{body.text}"
        markup = keyboard([("Send", f"act:{send.id}", 0), ("Cancel", f"act:{cancel.id}", 0)])
        broadcast_id, send_id, cancel_id = row.id, send.id, cancel.id
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": body.chat_id, "text": preview, "reply_markup": markup.to_dict()},
        )
        data = response.json()
        if not data.get("ok"):
            raise HTTPException(status_code=502, detail="Telegram rejected preview")
        message_id = data["result"]["message_id"]
    with session() as db:
        db.get(Broadcast, broadcast_id).message_id = message_id
        db.get(Action, send_id).message_id = message_id
        db.get(Action, cancel_id).message_id = message_id
    return {"id": str(broadcast_id), "message_id": message_id}


@api.get("/v1/actions/{action_id}")
def get_action(action_id: uuid.UUID, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    with session() as db:
        row = db.get(Action, action_id)
        if row is None:
            raise HTTPException(status_code=404)
        return {"id": str(row.id), "status": row.status, "result": row.result}


async def on_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != update.effective_user.id:
        await update.effective_message.reply_text("Alert settings are available in private chat only.")
        return
    with session() as db:
        person = ensure_user(db, update.effective_user.id, update.effective_user.username)
        args = context.args or []
        result = ""
        if args and args[0] in {"off", "disable"}:
            person.alerts_enabled = False
        elif args and args[0] in {"on", "enable"}:
            if person.alerts_approved:
                person.alerts_enabled = True
            elif not person.alerts_enabled:
                result = "Approval required. Use Subscribe to alerts in /start.\n"
        elif len(args) == 2 and args[0] == "level":
            try:
                level = int(args[1])
            except ValueError:
                level = -1
            if person.alerts_enabled and level in alert_levels() and level >= person.admin_level:
                person.receive_level = level
            else:
                result = "Invalid or unauthorized receive level.\n"
        text = result + f"Alerts: {'enabled' if person.alerts_enabled else 'disabled'}\nAdmin level: {person.admin_level}\nReceive level: {person.receive_level}"
        rows = [[InlineKeyboardButton(f"Receive {n}", callback_data=f"rl:{n}") for n in alert_levels() if n >= person.admin_level]] if person.alerts_enabled else [[InlineKeyboardButton("Subscribe to alerts", callback_data="al:yes")]]
    if person.alerts_enabled:
        rows.append([InlineKeyboardButton("Unsubscribe", callback_data="al:no")])
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def on_alertlevel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != update.effective_user.id:
        await update.effective_message.reply_text("Use private chat.")
        return
    with session() as db:
        if not is_owner(db, update.effective_user.id):
            response = "Owner only."
        else:
            try:
                target, level = map(int, context.args or [])
                if level not in alert_levels():
                    raise ValueError()
            except (ValueError, TypeError):
                response = "Usage: /alertlevel telegram_id N"
            else:
                person = db.scalar(select(User).where(User.telegram_id == target))
                if not person:
                    response = "Unknown user."
                else:
                    person.admin_level = level
                    person.receive_level = max(level, person.receive_level)
                    from socrat_bot.logic import audit
                    audit(db, str(update.effective_user.id), "alerts.admin_level", {"telegram_id": target, "level": level})
                    response = f"Admin level set to {level}."
    await update.effective_message.reply_text(response)


async def on_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable delivery, not the owner's durable approval or VPN opt-in."""
    if update.effective_chat.id != update.effective_user.id:
        await update.effective_message.reply_text("Alert settings are available in private chat only.")
        return
    with session() as db:
        person = ensure_user(db, update.effective_user.id, update.effective_user.username)
        person.alerts_enabled = False
        markup = _start_keyboard(bool(person.uses_vpn), False)
    await update.effective_message.reply_text(
        "You are unsubscribed from alerts. You can subscribe again at any time.",
        reply_markup=markup,
    )


@api.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc: RequestValidationError):
    # Preserve legacy validation responses unless an image-bearing body is involved.
    if not (isinstance(exc.body, dict) and "images" in exc.body):
        return await request_validation_exception_handler(request, exc)
    # FastAPI otherwise echoes base64 bytes and arbitrary metadata in validation errors.
    errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})
