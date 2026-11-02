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
    text: str
    actions: list[dict] = Field(default_factory=list)


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


def keyboard(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data)] for label, data in rows]
    )


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    arg = context.args[0] if context.args else ""
    with session() as db:
        ensure_owner(db, user.id)
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
    await query.answer()
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
            await context.bot.edit_message_text(text, chat_id=action.chat_id, message_id=action.message_id)


def _result_text(action: Action, result: dict) -> str:
    if action.type == "alert.silence":
        return "Alert silenced for one hour." if result.get("status") == "silenced" else f"Silence was not set: {result}"
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
    buttons: list[tuple[str, str]] = []
    with session() as db:
        for item in body.actions:
            row = create_action(
                db,
                type_=item["type"],
                payload=item.get("payload") or {},
                chat_id=body.chat_id,
                idempotency_key=item.get("idempotency_key") or str(uuid.uuid4()),
            )
            buttons.append((item.get("label") or row.type, f"act:{row.id}"))
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        markup = keyboard(buttons)
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"chat_id": body.chat_id, "text": body.text}
            if markup:
                payload["reply_markup"] = markup.to_dict()
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            data = response.json()
            if not data.get("ok"):
                raise HTTPException(status_code=502, detail=data)
            message_id = data["result"]["message_id"]
            for _, data_cb in buttons:
                action = db.get(Action, uuid.UUID(data_cb.removeprefix("act:")))
                if action:
                    action.message_id = message_id
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
        markup = keyboard([("Send", f"act:{send.id}"), ("Cancel", f"act:{cancel.id}")])
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
