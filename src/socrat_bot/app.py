import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from socrat_bot.cards import Card, Link, add_link, add_note, build_alert, mute_actions, render_card, replace_note, select_links
from socrat_bot.db import init_schema, session
from socrat_bot.grafana_rules import start_rule_refresh
from socrat_bot.logic import (
    alert_recipients,
    audience,
    get_card,
    list_cards,
    apply_action,
    approve_alerts,
    broadcast_zone,
    card_messages,
    claim_due_broadcasts,
    consume_link,
    create_action,
    deliver_broadcast,
    ensure_user,
    format_vpn_clients,
    is_owner,
    begin_mailing,
    mailing_dates,
    mailing_hours,
    mailing_slot,
    mark_vpn,
    open_card,
    owners,
    schedule_broadcast,
    vpn_audience,
)
from socrat_bot.models import Action, Broadcast, TelegramCard, User

ptb: Application | None = None
_updates: set[asyncio.Task] = set()
_card_locks: dict[str, asyncio.Lock] = {}


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
        row = db.scalar(select(TelegramCard).where(TelegramCard.key == key, TelegramCard.spi_url.is_not(None)))
        if row is None or not row.spi_url:
            return "", ""
        return row.spi_url, row.spi_label or "SPI"


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
        except Exception as exc:
            print(f"action webhook {exc}", flush=True)

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
    already = False
    with session() as db:
        person = ensure_user(db, user.id, user.username)
        uses_vpn = bool(person.uses_vpn)
        alerts = bool(person.alerts_enabled)
        print(f"telegram_start id={user.id} role={person.role}", flush=True)
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
    text = " ".join(context.args or []).strip()
    with session() as db:
        if not is_owner(db, user.id):
            await message.reply_text("This command is available to the owner only.")
            return
        if not text:
            await message.reply_text("Usage: /mailing message text")
            return
        row = begin_mailing(db, user.id, text)
        broadcast_id = row.id
    await message.reply_text(
        "Pick a day. Time is Europe/Moscow.",
        reply_markup=_date_keyboard(broadcast_id, datetime.now(broadcast_zone())),
    )


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
        if not is_owner(db, query.from_user.id):
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
                notice = f"Scheduled for {local} {broadcast_zone().key}. VPN recipients now: {count}."
            elif kind == "n":
                pending_text = row.text
                pending_ids = [user.telegram_id for user in vpn_audience(db)]
                row.status = "sending"
            else:
                return True
    if pending_text is not None:
        result = await deliver_broadcast(pending_text, pending_ids)
        with session() as db:
            stored = db.get(Broadcast, broadcast_id)
            if stored is not None:
                stored.status = "sent"
                stored.sent = result["sent"]
                stored.failed = result["failed"]
        notice = _delivery_notice("Sent now", result)
    if notice:
        await query.edit_message_text(notice)
    return True


async def _request_alerts(query) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    user = query.from_user
    with session() as db:
        person = ensure_user(db, user.id, user.username)
        if person.alerts_enabled:
            text = _start_text(bool(person.uses_vpn), True)
            keyboard = _start_keyboard(bool(person.uses_vpn), True)
            try:
                await query.edit_message_text(text, reply_markup=keyboard)
            except Exception:
                pass
            return
        targets = [(owner.telegram_id, owner.username) for owner in owners(db)]
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
        person = approve_alerts(db, telegram_id)
        label = f"@{person.username}" if person.username else str(person.telegram_id)
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
    if await _on_mailing_callback(query, query.data):
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
    action_id = uuid.UUID(query.data.removeprefix("act:"))
    with session() as db:
        action = db.get(Action, action_id)
        if action is None:
            return
        person = ensure_user(db, query.from_user.id, query.from_user.username)
        allowed = person.role == "owner" or (action.type == "alert.silence" and person.alerts_enabled)
        if not allowed:
            await query.edit_message_text("This action is available to the owner only.")
            return
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
        else:
            key = ""
    empty = InlineKeyboardMarkup([])
    for chat_id, message_id in copies:
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML" if html else None,
                reply_markup=empty if action.type == "alert.silence" and result.get("status") == "silenced" else None,
            )
        except Exception:
            pass
    if key and text:
        def remember_mute() -> None:
            with session() as db:
                for chat_id, _message_id in copies:
                    row = db.get(TelegramCard, (key, chat_id))
                    if row is not None:
                        row.text = text
        await asyncio.to_thread(remember_mute)


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
                result = await deliver_broadcast(text, recipients)
                with session() as db:
                    row = db.get(Broadcast, broadcast_id)
                    if row is not None:
                        row.status = "sent"
                        row.sent = result["sent"]
                        row.failed = result["failed"]
                print(f"broadcast {broadcast_id} {result.get('status')}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"broadcast loop {exc}", flush=True)


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
    print(f"telegram webhook {response.status_code}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb
    init_schema()
    start_rule_refresh()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        ptb = Application.builder().token(token).updater(None).build()
        ptb.add_handler(CommandHandler("start", on_start))
        ptb.add_handler(CommandHandler("clients", on_clients))
        ptb.add_handler(CommandHandler("mailing", on_mailing))
        ptb.add_handler(CallbackQueryHandler(on_callback))
        await ptb.initialize()
        await ptb.start()
        await _register_webhook()
    ticker = asyncio.create_task(_broadcast_loop())
    yield
    ticker.cancel()
    if ptb is not None:
        await ptb.stop()
        await ptb.shutdown()


api = FastAPI(lifespan=lifespan)


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
    except Exception as exc:
        print(f"telegram update {exc}", flush=True)


@api.post("/v1/messages")
async def post_message(body: OutMessage, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    if len(body.alerts) > 1:
        raise HTTPException(status_code=400, detail="one alert per message")
    try:
        card = card_from_request(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    text = render_card(card) if card is not None else body.text
    if not text:
        raise HTTPException(status_code=400, detail="text or alert required")
    if card is not None:
        spi_url, spi_label = await asyncio.to_thread(_saved_spi, card.key)
        if spi_url:
            text = add_link(text, spi_url, spi_label)
        note = await asyncio.to_thread(_saved_note, card.key)
        if note:
            text = add_note(text, note)
    parse_mode = "HTML" if card is not None or body.parse_mode == "HTML" else body.parse_mode

    def chats() -> list[int]:
        if card is None:
            return [body.chat_id]
        with session() as db:
            people = [user.telegram_id for user in alert_recipients(db)]
        return people or [body.chat_id]

    def prepare(chat_id: int) -> tuple[int | None, list[tuple[str, str, int]], list[uuid.UUID], str | None]:
        with session() as db:
            stored = open_card(db, card.key, chat_id, card.firing) if card is not None else None
            if card is not None and card.firing and not body.actions:
                incoming = mute_actions(card, stored.generation if stored else 1, chat_id)
            else:
                incoming = []
                for item in body.actions:
                    copied = dict(item)
                    if card is not None and copied.get("type") == "alert.silence":
                        payload = dict(copied.get("payload") or {})
                        payload.setdefault("card", text)
                        copied["payload"] = payload
                    incoming.append(copied)
            buttons: list[tuple[str, str, int]] = []
            action_ids: list[uuid.UUID] = []
            fresh = stored is not None and card is not None and card.firing and stored.status == "resolved"
            existing = None if fresh else stored.message_id if stored is not None and stored.message_id else None
            for item in incoming:
                row = create_action(
                    db,
                    type_=item["type"],
                    payload=dict(item.get("payload") or {}),
                    chat_id=chat_id,
                    idempotency_key=item.get("idempotency_key") or str(uuid.uuid4()),
                    refresh=card is not None,
                )
                action_ids.append(row.id)
                if existing is None and row.message_id:
                    existing = row.message_id
                if card is None or card.firing:
                    buttons.append((item.get("label") or row.type, f"act:{row.id}", int(item.get("row") or 0)))
            return existing, buttons, action_ids, stored.key if stored is not None else None

    async def deliver(chat_id: int) -> dict:
        existing, buttons, action_ids, card_key = await asyncio.to_thread(prepare, chat_id)
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        markup = keyboard(buttons)
        async with httpx.AsyncClient(timeout=20) as client:
            if existing is not None:
                edit = {
                    "chat_id": chat_id,
                    "message_id": existing,
                    "text": text,
                    "reply_markup": markup.to_dict() if markup else {"inline_keyboard": []},
                }
                if parse_mode:
                    edit["parse_mode"] = parse_mode
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/editMessageText",
                    json=edit,
                )
                data = response.json()
                if not data.get("ok") and "message is not modified" not in str(data):
                    raise HTTPException(status_code=502, detail=data)
                message_id = existing
                replayed = True
            else:
                payload = {"chat_id": chat_id, "text": text}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                if markup:
                    payload["reply_markup"] = markup.to_dict()
                response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
                data = response.json()
                if not data.get("ok"):
                    raise HTTPException(status_code=502, detail=data)
                message_id = data["result"]["message_id"]
                replayed = False

        def persist() -> None:
            with session() as db:
                for action_id in action_ids:
                    row = db.get(Action, action_id)
                    if row is not None:
                        row.message_id = message_id
                if card_key is not None and card is not None:
                    stored = db.get(TelegramCard, (card_key, chat_id))
                    if stored is not None:
                        if card.firing and stored.status == "resolved":
                            stored.generation += 1
                        stored.status = "firing" if card.firing else "resolved"
                        stored.message_id = message_id
                        stored.text = text

        await asyncio.to_thread(persist)
        body_out = {"id": message_id, "chat_id": chat_id}
        if replayed:
            body_out["replayed"] = True
        return body_out

    lock = _card_locks.setdefault(card.key, asyncio.Lock()) if card is not None else None
    if lock is not None:
        await lock.acquire()
    try:
        delivered: list[dict] = []
        failure: HTTPException | None = None
        for chat_id in await asyncio.to_thread(chats):
            try:
                delivered.append(await deliver(chat_id))
            except HTTPException as exc:
                failure = exc
                print(f"alert card {chat_id} {exc.detail}", flush=True)
        if not delivered:
            raise failure or HTTPException(status_code=502, detail="not delivered")
        chosen = next((item for item in delivered if item["chat_id"] == body.chat_id), delivered[0])
        result = {
            "id": chosen["id"],
            "chat_id": chosen["chat_id"],
            "copies": [
                {"chat_id": item["chat_id"], "message_id": item["id"], **({"replayed": True} if item.get("replayed") else {})}
                for item in delivered
            ],
        }
        if chosen.get("replayed"):
            result["replayed"] = True
        if card is not None:
            result["key"] = card.key
        return result
    finally:
        if lock is not None:
            lock.release()



class CardLinkIn(BaseModel):
    url: str
    label: str = "SPI"
    key: str | None = None
    alert: dict | None = None


@api.post("/v1/cards/links")
async def add_card_link(body: CardLinkIn, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    if body.key:
        key = body.key
    elif body.alert:
        key = build_alert(body.alert).key
    else:
        raise HTTPException(status_code=400, detail="key or alert required")

    def load() -> list[tuple[int, int | None, str | None]]:
        with session() as db:
            rows = db.scalars(select(TelegramCard).where(TelegramCard.key == key)).all()
            return [(row.chat_id, row.message_id, row.text) for row in rows]

    rows = await asyncio.to_thread(load)
    if not rows:
        raise HTTPException(status_code=404, detail="card not found")
    if not any(item[2] for item in rows):
        raise HTTPException(status_code=409, detail="card text is not stored yet")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    updated: list[dict] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for chat_id, message_id, current in rows:
            if not current or not message_id:
                continue
            revised = add_link(current, body.url, body.label)
            if revised != current:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/editMessageText",
                    json={"chat_id": chat_id, "message_id": message_id, "text": revised, "parse_mode": "HTML"},
                )
                data = response.json()
                if not data.get("ok") and "message is not modified" not in str(data):
                    print(f"spi link {chat_id} {data}", flush=True)
                    continue
            updated.append({"chat_id": chat_id, "message_id": message_id})

    def save() -> None:
        with session() as db:
            for chat_id, message_id, current in rows:
                row = db.get(TelegramCard, (key, chat_id))
                if row is None:
                    continue
                row.spi_url = body.url
                row.spi_label = body.label
                if row.text:
                    row.text = add_link(row.text, body.url, body.label)

    await asyncio.to_thread(save)
    return {"key": key, "updated": updated}




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
def get_cards(status: str = "firing", x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    if status not in {"firing", "resolved", "all"}:
        raise HTTPException(status_code=400, detail="status must be firing, resolved or all")
    with session() as db:
        return {"cards": list_cards(db, status)}


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

    def load() -> tuple[str | None, list[tuple[int, int | None, str | None]]]:
        with session() as db:
            rows = db.scalars(select(TelegramCard).where(TelegramCard.key == key)).all()
            previous = next((row.note for row in rows if row.note), None)
            return previous, [(row.chat_id, row.message_id, row.text) for row in rows]

    previous, rows = await asyncio.to_thread(load)
    if not rows:
        raise HTTPException(status_code=404, detail="card not found")
    if not any(item[2] for item in rows):
        raise HTTPException(status_code=409, detail="card text is not stored yet")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    updated: list[dict] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for chat_id, message_id, current in rows:
            if not current or not message_id:
                continue
            revised = replace_note(current, previous, cleaned)
            if revised != current:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/editMessageText",
                    json={"chat_id": chat_id, "message_id": message_id, "text": revised, "parse_mode": "HTML"},
                )
                data = response.json()
                if not data.get("ok") and "message is not modified" not in str(data):
                    print(f"card note {chat_id} {data}", flush=True)
                    continue
            updated.append({"chat_id": chat_id, "message_id": message_id})

    def save() -> None:
        with session() as db:
            for chat_id, _message_id, current in rows:
                row = db.get(TelegramCard, (key, chat_id))
                if row is None:
                    continue
                row.note = cleaned
                if row.text:
                    row.text = replace_note(row.text, previous, cleaned)

    await asyncio.to_thread(save)
    return {"key": key, "updated": updated}


@api.patch("/v1/messages/{message_id}")
async def patch_message(
    message_id: int,
    body: PatchMessage,
    chat_id: int,
    x_internal_token: str | None = Header(default=None),
):
    require_internal(x_internal_token)
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
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": body.chat_id, "text": preview, "reply_markup": markup.to_dict()},
            )
            data = response.json()
            if not data.get("ok"):
                raise HTTPException(status_code=502, detail=data)
            message_id = data["result"]["message_id"]
            row.message_id = message_id
            send.message_id = message_id
            cancel.message_id = message_id
        return {"id": str(row.id), "message_id": message_id}


@api.get("/v1/actions/{action_id}")
def get_action(action_id: uuid.UUID, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    with session() as db:
        row = db.get(Action, action_id)
        if row is None:
            raise HTTPException(status_code=404)
        return {"id": str(row.id), "status": row.status, "result": row.result}
