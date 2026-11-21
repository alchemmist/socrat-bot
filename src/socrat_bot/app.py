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
from telegram.error import BadRequest

from socrat_bot.mute_transport import mute_minutes
from socrat_bot.telegram_reply import accepted_start, reply_text

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from socrat_bot import callback_trace, media
from socrat_bot.cards import (
    Card,
    Link,
    build_alert,
    select_links,
)
from socrat_bot.cards import add_link as add_link
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
from socrat_bot.escalation import escalate, outbox_loop
from socrat_bot.grafana_rules import start_rule_refresh
from socrat_bot.logic import (
    alert_levels,
    apply_action,
    approve_alerts,
    audience,
    broadcast_zone,
    claim_due_broadcasts,
    consume_link,
    create_action,
    as_utc,
    deliver_broadcast,
    ensure_user,
    format_interest_users,
    get_card,
    interest_users,
    is_owner,
    list_cards,
    mailing_audience,
    mailing_dates,
    mailing_hours,
    mailing_slot,
    mark_vpn,
    owner_ids,
    owners,
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
    operation = accepted_start()
    if update.effective_chat.id != user.id:
        await reply_text(update.message, "Open /start in private chat to choose your interests.", operation=operation)
        return
    arg = context.args[0] if context.args else ""
    with session() as db:
        person = ensure_user(db, user.id, user.username)
        interests = {row.topic for row in person.interests if row.enabled}
        approved = bool(person.alerts_approved)
        owner = person.role == "owner"
        if arg.startswith("link_"):
            outcome = consume_link(db, user.id, arg.removeprefix("link_"))
            text = "Link saved." if outcome == "linked" else "The code is invalid or already used."
        else:
            text = _start_text(interests, approved)
    if arg.startswith("link_"):
        await reply_text(update.message, text, operation=operation)
        return
    keyboard = _start_keyboard(interests, approved)
    await reply_text(update.message, text, operation=operation, reply_markup=keyboard)
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


def _start_text(interests: set[str], approved: bool) -> str:
    del interests, approved
    return "\n".join([
        "Welcome to Socrat Bot!",
        "",
        "I'm interested in…",
        "Choose any number of interests. Tap again to turn one off.",
    ])


def _start_keyboard(interests: set[str], approved: bool) -> InlineKeyboardMarkup:
    from .interests import INTERESTS

    rows = []
    for item in INTERESTS:
        enabled = item.key in interests
        marker = "☑" if enabled else "☐"
        suffix = " · pending approval" if item.approval_required and enabled and not approved else ""
        # Explicit target state makes repeated delivery of the same callback safe.
        rows.append([InlineKeyboardButton(f"{marker} {item.label}{suffix}", callback_data=f"interest:{item.key}:{'off' if enabled else 'on'}")])
    return InlineKeyboardMarkup(rows)


def _person_label(user) -> str:
    username = getattr(user, "username", None)
    return f"@{username}" if username else str(user.id)


def _users_keyboard() -> InlineKeyboardMarkup:
    from .interests import INTERESTS

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(item.label, callback_data=f"users:{item.key}")]
        for item in INTERESTS
    ])


async def on_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    with session() as db:
        owner = is_owner(db, user.id)
    if not owner:
        await message.reply_text("This command is available to the owner only.")
        return
    await message.reply_text("Users", reply_markup=_users_keyboard())


async def _on_users_topic(query) -> None:
    from .interests import BY_KEY

    topic = query.data.removeprefix("users:")
    item = BY_KEY.get(topic)
    if item is None:
        await query.answer("Unknown interest")
        return
    with session() as db:
        if not is_owner(db, query.from_user.id):
            await query.answer("Owner only")
            return
        people = interest_users(db, topic)
        missing = [person.telegram_id for person in people if not person.username]
    await query.answer()
    if missing:
        await _remember_usernames(missing)
        with session() as db:
            people = interest_users(db, topic)
    text = format_interest_users(item.label, people)
    try:
        await query.edit_message_text(text)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def on_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .mailing_compose import start
    await start(update, context)


async def on_resend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .resend import start
    await start(update, context)


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
    photo = getattr(message, "photo", None)
    raw = (message.text or getattr(message, "caption", None) or "").split(maxsplit=1)
    value = raw[1] if len(raw) > 1 else ""
    broadcast_id = None
    if message.reply_to_message:
        with session() as db:
            broadcast_id = db.scalar(select(Broadcast.id).where(
                Broadcast.chat_id == user.id,
                Broadcast.message_id == message.reply_to_message.message_id,
                Broadcast.status == "sent",
            ))
            if broadcast_id is None:
                from .models import BroadcastCopy
                broadcast_id = db.scalar(select(Broadcast.id).join(BroadcastCopy).where(
                    Broadcast.chat_id == user.id, Broadcast.status == "sent",
                    BroadcastCopy.chat_id == message.chat_id,
                    BroadcastCopy.message_id == message.reply_to_message.message_id,
                ))
    body_tokens = 1 if broadcast_id is not None else 2
    if broadcast_id is None and value:
        parts = value.split(maxsplit=1)
        try:
            broadcast_id = uuid.UUID(parts[0])
            value = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass
    if photo and broadcast_id is None:
        await message.reply_text("Reply to the delivered mailing or its delivery summary, or use /mailing_edit UUID in the photo caption.")
        return
    if broadcast_id is None or (not value and not photo):
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
    if photo:
        if not value:
            value = None  # Keep the current mailing text and its formatting.
        await message.reply_text("Updating the original mailing messages…")
    result = await edit_mailing(broadcast_id, user.id, value, entities, **({"photo_id": photo[-1].file_id} if photo else {}))
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
            if row is None or row.status not in {"choosing", "group_choosing"} or row.chat_id != query.from_user.id:
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
                if not db.execute(sql_update(Broadcast).where(
                    Broadcast.id == broadcast_id, Broadcast.status == row.status,
                    Broadcast.chat_id == query.from_user.id,
                ).values(status="group_scheduled" if row.status == "group_choosing" else "scheduled", send_at=send_at)).rowcount:
                    return True
                count = len(mailing_audience(db, row.audience_group))
                local = send_at.astimezone(broadcast_zone()).strftime("%Y-%m-%d %H:%M")
                notice = f"Scheduled for {local} {broadcast_zone().key}. Group: {row.audience_group.upper()}. Recipients now: {count}.\nMailing {broadcast_id}\nAfter delivery: /mailing_edit {broadcast_id} NEW_TEXT"
            elif kind == "n":
                claimed = db.execute(sql_update(Broadcast).where(
                    Broadcast.id == broadcast_id, Broadcast.status == row.status,
                    Broadcast.chat_id == query.from_user.id,
                ).values(status="sending")).rowcount
                if not claimed:
                    return True
                pending_text = row.text
                pending_ids = [user.telegram_id for user in mailing_audience(db, row.audience_group)]
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
        notice = _delivery_notice("Sent now", result) + f"\nGroup: {row.audience_group.upper()}\nMailing {broadcast_id}\nReply: /mailing_edit NEW_TEXT"
    if notice:
        await query.edit_message_text(notice)
    return True


async def _edit_interests(query, text, markup):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


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
        already_selected = person.interested_in_alerts
        person.interested_in_alerts = True
        approved = person.alerts_approved
        text = _start_text({row.topic for row in person.interests if row.enabled}, bool(approved))
        markup = _start_keyboard({row.topic for row in person.interests if row.enabled}, bool(approved))
        if approved:
            person.interested_in_alerts = True
            text = _start_text({row.topic for row in person.interests if row.enabled}, True)
            markup = _start_keyboard({row.topic for row in person.interests if row.enabled}, True)
        targets = [(owner.telegram_id, owner.username) for owner in owners(db)] if not approved and not already_selected else []
    if approved:
        await _edit_interests(query, text, markup)
        return
    await _edit_interests(query, text, markup)
    if already_selected:
        return
    if not targets:
        await query.message.reply_text("Interest saved. No owner is available to approve alerts yet.")
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
        active = person.alerts_active
    if already_approved:
        await query.edit_message_text(f"Already approved {label}.")
        return
    await query.edit_message_text(f"Approved {label}.")
    schedule_action_event({"event": "alerts.approved", "telegram_id": person.telegram_id, "username": person.username})
    try:
        await query.get_bot().send_message(telegram_id, "Your Alerts interest is approved." + (" Alert notifications are on." if active else " Alerts remain off; enable them in /start when you want."))
    except Exception:
        pass


def _prepare_callback(action_id, actor_id, username):
    """Use a worker-owned DB session; never stall the webhook loop on row locks."""
    from sqlalchemy import update as sql_update
    legacy = None
    with session() as db:
        candidate = db.get(Action, action_id)
        if candidate and candidate.type == "broadcast.send":
            if not is_owner(db, actor_id):
                return ("rejected", ("denied", "Недостаточно прав."))
            if candidate.status != "pending" or (candidate.expires_at and as_utc(candidate.expires_at) < datetime.now(timezone.utc)):
                return ("rejected", ("stale", "Действие уже завершено или истекло."))
            claimed = db.execute(sql_update(Action).where(Action.id == action_id, Action.status == "pending").values(status="running")).rowcount
            if not claimed:
                return ("rejected", ("stale", "Действие уже выполняется."))
            row = db.get(Broadcast, uuid.UUID(candidate.payload["broadcast_id"]))
            if row is None or row.status != "preview":
                candidate.status = "failed"
                return ("rejected", ("stale", "Предпросмотр рассылки больше недоступен."))
            row.status = "sending"
            legacy = (row.id, row.text, [person.telegram_id for person in mailing_audience(db, row.audience_group)])
    if legacy:
        return ("broadcast", legacy)
    mute_generation = None
    preview = None
    preview_base = ""
    preview_minutes = 60
    preview_photo = False
    is_mute = False
    rejected = None
    with session() as db:
        action = db.get(Action, action_id)
        if action is None:
            rejected = ("missing", "Кнопка больше недоступна.")
        else:
            person = ensure_user(db, actor_id, username)
            is_mute = action.type == "alert.silence"
            if not (person.role == "owner" or (is_mute and person.alerts_active)):
                rejected = ("denied", "Недостаточно прав для этого действия.")
            elif is_mute and action.payload.get("key"):
                incident = db.get(Incident, action.payload["key"])
                copy = db.get(TelegramCard, (action.payload["key"], action.chat_id))
                if incident and (incident.status != "firing" or not copy or copy.generation != incident.generation or f":g{incident.generation}:" not in action.idempotency_key):
                    rejected = ("stale", "Алерт закрыт или карточка устарела.")
                elif incident:
                    mute_generation = incident.generation
                    preview_base = (copy.text if copy and copy.text else "") or ((action.payload or {}).get("card") or "")
                    preview_minutes = mute_minutes(action.payload)
                    preview_photo = getattr(copy, "message_kind", None) == "photo"
                    preview = _with_footer(preview_base, "<i>Requesting mute…</i>")
    if rejected:
        return ("rejected", rejected)
    return ("ready", (preview, preview_base, preview_minutes, preview_photo, is_mute, mute_generation))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if query.data.startswith("rs:"):
        from .resend import run_callback
        await run_callback(query, context.bot)
        return
    from .mailing_compose import group_callback
    if await group_callback(query, context):
        return
    from .mailing_list import on_list_callback
    if await on_list_callback(query):
        return
    if query.data.startswith("users:"):
        await _on_users_topic(query)
        return
    if query.data.startswith("interest:"):
        from .interests import BY_KEY
        parts = query.data.split(":")
        if len(parts) != 3 or parts[1] not in BY_KEY or parts[2] not in {"on", "off"}:
            await query.answer("Invalid interest")
            return
        if query.message is None or query.message.chat_id != query.from_user.id:
            await query.answer("Choose your interests in private chat only.")
            return
        topic, enabled = parts[1], parts[2] == "on"
        if topic == "alerts" and enabled:
            await _request_alerts(query)
            return
        with session() as db:
            person = ensure_user(db, query.from_user.id, query.from_user.username)
            changed = person.interested_in(topic) != enabled
            person.set_interest(topic, enabled)
            from .logic import audit
            if changed:
                audit(db, str(query.from_user.id), "interest.changed", {"topic": topic, "enabled": enabled})
            text = _start_text({row.topic for row in person.interests if row.enabled}, person.alerts_approved)
            markup = _start_keyboard({row.topic for row in person.interests if row.enabled}, person.alerts_approved)
        await query.answer()
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        if topic == "vpn" and enabled and changed:
            schedule_action_event({"event": "vpn.opt_in", "telegram_id": query.from_user.id, "username": query.from_user.username})
        return
    if query.data.startswith("esc:"):
        try:
            _, digest, generation, level = query.data.split(":")
            args = (query.from_user.id, query.message.chat_id, query.message.message_id, digest, int(generation), int(level))
        except (ValueError, AttributeError):
            await query.answer("Invalid escalation")
            return
        def transition():
            jobs = []
            with session() as db:
                result = escalate(db, *args, pressed_jobs=jobs)
            return result, jobs
        callback_trace.stage("escalate_dispatch")
        try:
            result, jobs = await asyncio.to_thread(transition)
        except Exception as exc:
            callback_trace.stage("handler_error", exception=exc)
            await query.answer("Не удалось подтвердить эскалацию. Проверьте уровень карточки.")
            return
        callback_trace.stage("escalated" if jobs else "not_escalated")
        # After commit, edit the pressed copy without waiting for a toast or
        # for the background queue of unrelated deliveries. Same fenced job.
        await asyncio.gather(query.answer(result), dispatch_jobs(jobs))
        return
    if query.data.startswith("rl:"):
        with session() as db:
            person = ensure_user(db, query.from_user.id)
            try:
                level = int(query.data.split(":")[1])
            except ValueError:
                level = -1
            allowed = query.message.chat_id == query.from_user.id and person.alerts_active and level in alert_levels() and level >= person.admin_level
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
            person.interested_in_alerts = False
            markup = _start_keyboard({row.topic for row in person.interests if row.enabled}, person.alerts_approved)
        await query.edit_message_text("You are unsubscribed from alerts. You can subscribe again at any time.", reply_markup=markup)
        return
    if query.data == "al:yes":
        await _request_alerts(query)
        return
    if query.data.startswith("al:ok:"):
        await _approve_alerts(query)
        return
    if query.data == "vpn:yes":
        if query.message is None or query.message.chat_id != query.from_user.id:
            await query.answer("Choose your interests in private chat only.")
            return
        try:
            await query.answer()
        except Exception:
            pass
        with session() as db:
            person = mark_vpn(db, query.from_user.id, query.from_user.username)
            text = _start_text({row.topic for row in person.interests if row.enabled}, person.alerts_approved)
            keyboard = _start_keyboard({row.topic for row in person.interests if row.enabled}, person.alerts_approved)
            schedule_action_event({"event": "vpn.opt_in", "telegram_id": person.telegram_id, "username": person.username})
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            pass
        return
    if not query.data.startswith("act:"):
        return
    callback_trace.stage("handler")
    async def acknowledge(message):
        try:
            await asyncio.wait_for(query.answer(message), timeout=2)
            callback_trace.stage("acknowledged")
        except Exception:
            callback_trace.stage("ack_unconfirmed")
    actor = str(query.from_user.id)
    mute_generation = None
    try:
        action_id = uuid.UUID(query.data.removeprefix("act:"))
    except ValueError:
        callback_trace.stage("invalid")
        await acknowledge("Кнопка недействительна.")
        return
    preparation = asyncio.create_task(asyncio.to_thread(_prepare_callback, action_id, query.from_user.id, query.from_user.username))
    def start_ack():
        task = asyncio.create_task(acknowledge("Запрос принят. Проверяю…"))
        _updates.add(task)
        task.add_done_callback(_updates.discard)
    done, _ = await asyncio.wait({preparation}, timeout=0.1)
    acknowledged = not done
    if acknowledged:
        start_ack()
    try:
        kind, data = await preparation
    except Exception as exc:
        callback_trace.stage("preflight_error", exception=exc)
        if not acknowledged:
            await acknowledge("Не удалось проверить действие. Попробуйте позже.")
        return
    if kind == "rejected":
        reason, message = data
        callback_trace.stage(reason)
        if not acknowledged:
            await acknowledge(message)
        return
    if not acknowledged:
        start_ack()
    if kind == "broadcast":
        broadcast_id, body, recipients = data
        result = await deliver_broadcast(body, recipients, broadcast_id)
        with session() as db:
            row = db.get(Broadcast, broadcast_id)
            row.status, row.sent, row.failed = "sent", result["sent"], result["failed"] + [{"accepted_untracked": item} for item in result.get("untracked", [])]
            action = db.get(Action, action_id)
            action.status, action.result = "done", result
        await query.edit_message_text(_delivery_notice("Broadcast", result) + f"\nMailing {broadcast_id}\nReply: /mailing_edit NEW_TEXT")
        return
    preview, preview_base, preview_minutes, preview_photo, is_mute, mute_generation = data
    # The Telegram preview used to block the Alertmanager request. Run the mute
    # with its own transaction and event loop: unrelated synchronous DB work in
    # other callbacks must not consume this client's transport timeout budget.
    if is_mute:
        def run_mute():
            with session() as worker_db:
                current = worker_db.get(Action, action_id)
                if current is None:
                    return {"status": "expired"}
                if mute_generation is not None:
                    current_incident = worker_db.get(Incident, current.payload["key"])
                    if not current_incident or current_incident.status != "firing" or current_incident.generation != mute_generation:
                        return {"status": "stale"}
                return asyncio.run(apply_action(worker_db, current, actor))
        callback_trace.stage("dispatch")
        mute_result = await asyncio.to_thread(run_mute)
        callback_trace.stage("silenced" if mute_result.get("status") == "silenced" else "not_silenced")
        if mute_result.get("status") == "stale":
            callback_trace.stage("stale")
            return
    with session() as db:
        action = db.get(Action, action_id)
        if action is None:
            return  # deleted after the initial authorization; never edit an unverified card
        result = mute_result if is_mute else await apply_action(db, action, actor)
        if preview and action.type == "alert.silence" and result.get("status") != "silenced":
            failure = _with_footer(preview_base, _muted_footer(preview_minutes, _mute_failure(result)))
        else:
            failure = None
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
                "minutes": mute_minutes(action.payload),
            })
        else:
            key = ""
    if failure:
        await _show_muted_message(query, failure, preview_photo, getattr(query.message, "reply_markup", None))
        return
    if key and text:
        footer = _muted_footer(mute_minutes(action.payload))
        try:
            _recipients, jobs = await asyncio.to_thread(
                annotate_card, key, mute=footer, expected_generation=mute_generation,
                preferred_chat=action.chat_id
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
                    recipients = [user.telegram_id for user in mailing_audience(db, row.audience_group)]
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


def _mute_failure(result: dict) -> str:
    if result.get("status") == "running":
        return "Mute request in progress."
    if result.get("status") == "expired":
        return "This mute button has expired."
    if result.get("reason") == "not_connected":
        return "Alertmanager could not be reached. Tap Mute to retry."
    if result.get("reason") == "legacy_overlap":
        return "A matching silence already exists; no additional mute was created."
    if result.get("reason") == "invalid_matchers":
        return "Mute rejected: missing alert matchers."
    return "Mute not confirmed. Tap Mute to check Alertmanager again."


async def _show_muted_message(query, text: str, photo: bool, markup=None) -> None:
    """Report the actual outcome on the pressed text or photo card."""
    try:
        if photo:
            await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        log.warning("mute feedback edit failed")


def _muted_footer(minutes: int, failed: str | None = None) -> str:
    span = {15: "15 minutes", 30: "30 minutes", 60: "1 hour"}.get(minutes, f"{minutes} minutes")
    body = failed if failed is not None else f"Muted for {span}."
    return f"<i>{body}</i>"


def _with_footer(text: str, footer: str) -> str:
    if "<i>" in text:
        text = text[: text.rfind("<i>")].rstrip()
    return f"{text}\n\n{footer}" if text else footer


def _result_text(action: Action, result: dict) -> str:
    if action.type == "alert.silence":
        minutes = mute_minutes(action.payload)
        footer = _muted_footer(minutes) if result.get("status") == "silenced" else _muted_footer(minutes, _mute_failure(result))
        card = (action.payload or {}).get("card")
        return _with_footer(card or "", footer)
    if action.type == "broadcast.send":
        return _delivery_notice("Broadcast", result)
    if action.type == "broadcast.cancel":
        return "Broadcast cancelled."
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
        # One slow callback must not delay answering the next button press.
        ptb = Application.builder().token(token).updater(None).concurrent_updates(True).build()
        for command in COMMANDS:
            ptb.add_handler(CommandHandler(command.name, globals()[command.handler]))
        ptb.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^/mailing_edit(?:\s|$)"), on_mailing_edit))
        ptb.add_handler(CallbackQueryHandler(on_callback))
        ptb.add_error_handler(_ptb_error)
        from .mailing_compose import receive_text
        ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
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
    from socrat_bot.graph_jobs import outbox_loop as graph_loop
    graph_ticker = asyncio.create_task(graph_loop())
    yield
    ticker.cancel()
    escalation_ticker.cancel()
    media_ticker.cancel()
    graph_ticker.cancel()
    await asyncio.gather(media_ticker, graph_ticker, return_exceptions=True)
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


async def _ptb_error(update, context):
    # process_update dispatches exceptions to error handlers, not its caller.
    callback_trace.stage("handler_error", exception=context.error)
    log.error("telegram update failed")


async def _run_update(payload: dict) -> None:
    if ptb is None:
        return
    token = callback_trace.start(payload)
    try:
        update = Update.de_json(payload, ptb.bot)
        await ptb.process_update(update)
    except Exception:
        callback_trace.stage("decode_or_dispatch_error")
        log.error("telegram update failed")
    finally:
        if token is not None:
            callback_trace.operation.reset(token)


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
    if episode is not None:
        import re
        if not re.fullmatch(r"[a-f0-9]{64}",episode):
            raise HTTPException(status_code=400,detail="invalid episode")
    with session() as db:
        cards=list_cards(db, status, episode)
    if episode is not None:
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
            person.interested_in_alerts = False
        elif args and args[0] in {"on", "enable"}:
            if person.alerts_approved:
                person.interested_in_alerts = True
            elif not person.alerts_active:
                result = "Approval required. Choose Alerts in /start.\n"
        elif len(args) == 2 and args[0] == "level":
            try:
                level = int(args[1])
            except ValueError:
                level = -1
            if person.alerts_active and level in alert_levels() and level >= person.admin_level:
                person.receive_level = level
            else:
                result = "Invalid or unauthorized receive level.\n"
        text = result + f"Alerts: {'enabled' if person.alerts_active else 'disabled'}\nAdmin level: {person.admin_level}\nReceive level: {person.receive_level}"
        rows = [[InlineKeyboardButton(f"Receive {n}", callback_data=f"rl:{n}") for n in alert_levels() if n >= person.admin_level]] if person.alerts_active else [[InlineKeyboardButton("Subscribe to alerts", callback_data="al:yes")]]
    if person.alerts_active:
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
        person.interested_in_alerts = False
        markup = _start_keyboard({row.topic for row in person.interests if row.enabled}, person.alerts_approved)
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
