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

from socrat_bot.cards import Card, Link, build_alert, mute_actions, render_card, select_links
from socrat_bot.db import init_schema, session
from socrat_bot.grafana_rules import start_rule_refresh
from socrat_bot.logic import (
    apply_action,
    broadcast_zone,
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
    schedule_broadcast,
    vpn_audience,
)
from socrat_bot.models import Action, Broadcast, TelegramCard

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


class PatchMessage(BaseModel):
    text: str


class LinkIn(BaseModel):
    panel_client_id: str
    ttl_seconds: int = 900


class BroadcastIn(BaseModel):
    text: str
    chat_id: int


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
        person = ensure_user(db, user.id)
        already = bool(person.uses_vpn)
        print(f"telegram_start id={user.id} role={person.role}", flush=True)
        if arg.startswith("link_"):
            outcome = consume_link(db, user.id, arg.removeprefix("link_"))
            text = "Link saved." if outcome == "linked" else "The code is invalid or already used."
        else:
            text = "Socrat is online."
    if arg.startswith("link_"):
        await update.message.reply_text(text)
        return
    if already:
        await update.message.reply_text("Socrat is online.\nYou are on the VPN list.")
        return
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("I use VPN", callback_data="vpn:yes")]]),
    )


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


async def on_clients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    with session() as db:
        if not is_owner(db, user.id):
            text = "This command is available to the owner only."
        else:
            text = format_vpn_clients(vpn_audience(db))
    await message.reply_text(text)


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


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if await _on_mailing_callback(query, query.data):
        return
    if query.data == "vpn:yes":
        try:
            await query.answer()
        except Exception:
            pass
        with session() as db:
            mark_vpn(db, query.from_user.id)
        try:
            await query.edit_message_text("Socrat is online.\nYou are on the VPN list.")
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
        if not is_owner(db, query.from_user.id):
            await query.edit_message_text("This action is available to the owner only.")
            return
        action = db.get(Action, action_id)
        if action is None:
            return
        result = await apply_action(db, action, actor)
        text = _result_text(action, result)
        if action.message_id:
            try:
                await context.bot.edit_message_text(
                    text,
                    chat_id=action.chat_id,
                    message_id=action.message_id,
                    parse_mode="HTML" if action.payload.get("card") else None,
                )
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
    parse_mode = "HTML" if card is not None or body.parse_mode == "HTML" else body.parse_mode

    def prepare() -> tuple[int | None, list[tuple[str, str, int]], list[uuid.UUID], str | None]:
        with session() as db:
            stored = open_card(db, card.key, body.chat_id, card.firing) if card is not None else None
            if card is not None and card.firing and not body.actions:
                incoming = mute_actions(card, stored.generation if stored else 1)
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
            existing = stored.message_id if stored is not None and stored.message_id else None
            for item in incoming:
                row = create_action(
                    db,
                    type_=item["type"],
                    payload=dict(item.get("payload") or {}),
                    chat_id=body.chat_id,
                    idempotency_key=item.get("idempotency_key") or str(uuid.uuid4()),
                    refresh=card is not None,
                )
                action_ids.append(row.id)
                if existing is None and row.message_id:
                    existing = row.message_id
                if card is None or card.firing:
                    buttons.append((item.get("label") or row.type, f"act:{row.id}", int(item.get("row") or 0)))
            return existing, buttons, action_ids, stored.key if stored is not None else None

    existing, buttons, action_ids, card_key = await asyncio.to_thread(prepare)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    markup = keyboard(buttons)
    async with httpx.AsyncClient(timeout=20) as client:
        if existing is not None:
            edit = {
                "chat_id": body.chat_id,
                "message_id": existing,
                "text": text,
                "reply_markup": markup.to_dict() if markup else {"inline_keyboard": []},
            }
            if parse_mode:
                edit["parse_mode"] = parse_mode
            response = await client.post(f"https://api.telegram.org/bot{token}/editMessageText", json=edit)
            data = response.json()
            if not data.get("ok") and "message is not modified" not in str(data):
                raise HTTPException(status_code=502, detail=data)
            message_id = existing
            replayed = True
        else:
            payload = {"chat_id": body.chat_id, "text": text}
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
            if card_key is not None:
                stored = db.get(TelegramCard, card_key)
                if stored is not None:
                    stored.message_id = message_id

    await asyncio.to_thread(persist)
    body_out = {"id": message_id, "chat_id": body.chat_id}
    if replayed:
        body_out["replayed"] = True
    return body_out


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
