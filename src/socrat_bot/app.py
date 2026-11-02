import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from socrat_bot.db import init_schema, session
from socrat_bot.logic import apply_action, consume_link, create_action, ensure_owner, is_owner
from socrat_bot.models import Action, Broadcast

ptb: Application | None = None


class OutMessage(BaseModel):
    chat_id: int
    text: str = ""
    alerts: list[dict] = Field(default_factory=list)
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


def _esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_alerts(alerts: list[dict]) -> str:
    firing = [item for item in alerts if item.get("status") != "resolved"]
    resolved = [item for item in alerts if item.get("status") == "resolved"]
    blocks: list[str] = []

    def section(title: str, items: list[dict]) -> None:
        if not items:
            return
        lines = [f"<b>{title}</b>  {len(items)}"]
        for item in items[:8]:
            name = _esc(str(item.get("name") or "alert"))
            where = _esc(str(item.get("where") or ""))
            summary = _esc(str(item.get("summary") or ""))
            lines.append(f"<b>{name}</b>")
            if where:
                lines.append(f"<code>{where}</code>")
            if summary:
                lines.append(summary)
            links = []
            for label, url in (item.get("links") or {}).items():
                if not url:
                    continue
                links.append(f'<a href="{_esc(str(url))}">{_esc(str(label))}</a>')
            if links:
                lines.append(" · ".join(links))
        if len(items) > 8:
            lines.append(f"+{len(items) - 8} more")
        blocks.append("\n".join(lines))

    section("Firing", firing)
    section("Resolved", resolved)
    return "\n\n".join(blocks)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    arg = context.args[0] if context.args else ""
    with session() as db:
        ensure_owner(db, user.id)
        print(f"telegram_start id={user.id}", flush=True)
        if arg.startswith("link_"):
            outcome = consume_link(db, user.id, arg.removeprefix("link_"))
            text = "Link saved." if outcome == "linked" else "The code is invalid or already used."
        else:
            text = "Socrat is online."
    await update.message.reply_text(text)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or not query.data.startswith("act:"):
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
        return f"Broadcast: sent {result.get('sent', 0)}, not accepted {result.get('failed', [])}"
    if action.type == "broadcast.cancel":
        return "Broadcast cancelled."
    if action.type in {"sudo.approve", "sudo.deny"}:
        return f"Sudo {result.get('status')}: {result.get('argv')}"
    return str(result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb
    init_schema()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        ptb = Application.builder().token(token).updater(None).build()
        ptb.add_handler(CommandHandler("start", on_start))
        ptb.add_handler(CallbackQueryHandler(on_callback))
        await ptb.initialize()
        await ptb.start()
    yield
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
    update = Update.de_json(await request.json(), ptb.bot)
    await ptb.process_update(update)
    return {"ok": True}


@api.post("/v1/messages")
async def post_message(body: OutMessage, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    text = render_alerts(body.alerts) if body.alerts else body.text
    if not text:
        raise HTTPException(status_code=400, detail="text or alerts required")
    parse_mode = "HTML" if body.alerts or body.parse_mode == "HTML" else body.parse_mode
    buttons: list[tuple[str, str, int]] = []
    rows = []
    with session() as db:
        for item in body.actions:
            payload = dict(item.get("payload") or {})
            if body.alerts and item.get("type") == "alert.silence":
                payload.setdefault("card", text)
            row = create_action(
                db,
                type_=item["type"],
                payload=payload,
                chat_id=body.chat_id,
                idempotency_key=item.get("idempotency_key") or str(uuid.uuid4()),
            )
            rows.append(row)
            buttons.append((item.get("label") or row.type, f"act:{row.id}", int(item.get("row") or 0)))
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        markup = keyboard(buttons)
        existing = next((row.message_id for row in rows if row.message_id), None)
        async with httpx.AsyncClient(timeout=20) as client:
            if existing is not None:
                edit = {"chat_id": body.chat_id, "message_id": existing, "text": text}
                if parse_mode:
                    edit["parse_mode"] = parse_mode
                if markup:
                    edit["reply_markup"] = markup.to_dict()
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/editMessageText",
                    json=edit,
                )
                data = response.json()
                if not data.get("ok") and "message is not modified" not in str(data):
                    raise HTTPException(status_code=502, detail=data)
                return {"id": existing, "chat_id": body.chat_id, "replayed": True}
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
            for row in rows:
                row.message_id = message_id
    return {"id": message_id, "chat_id": body.chat_id}


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
