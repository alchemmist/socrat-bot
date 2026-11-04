from socrat_bot.structured_logging import OperationalEvents
import os
import shlex
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import update

from socrat_bot.db import init_schema, session
from socrat_bot.logic import argv_hash, as_utc, audit, create_action
from socrat_bot.models import SudoRequest, utcnow


class SudoIn(BaseModel):
    user: str
    node: str
    argv: list[str] = Field(min_length=1)


class ResultIn(BaseModel):
    exit_code: int


def require_internal(token: str | None) -> None:
    expected = os.environ.get("INTERNAL_TOKEN", "")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def _owner_chat() -> int:
    raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    if not raw:
        raise HTTPException(status_code=500, detail="TELEGRAM_OWNER_ID is not set")
    return int(raw)


def _telegram_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not set")
    return token


def _load_owned(request_id: uuid.UUID, user: str, node: str) -> SudoRequest:
    with session() as db:
        row = db.get(SudoRequest, request_id)
        if row is None or row.user_name != user or row.node != node:
            raise HTTPException(status_code=404, detail="not found")
        return row


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_schema()
    yield


api = FastAPI(lifespan=lifespan)
api.add_middleware(OperationalEvents, component="broker")


@api.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@api.post("/v1/sudo")
def create_sudo(body: SudoIn, x_internal_token: str | None = Header(default=None)):
    require_internal(x_internal_token)
    token = _telegram_token()
    chat_id = _owner_chat()
    minutes = int(os.environ.get("SUDO_TTL_MINUTES", "5"))
    shown = shlex.join(body.argv)
    if len(shown) > 3000:
        shown = shown[:3000] + "…"
    text = f"Sudo request\nnode: {body.node}\nuser: {body.user}\n{shown}"
    with session() as db:
        row = SudoRequest(
            user_name=body.user,
            node=body.node,
            argv=list(body.argv),
            argv_hash=argv_hash(list(body.argv)),
            status="pending",
            expires_at=utcnow() + timedelta(minutes=minutes),
        )
        db.add(row)
        db.flush()
        approve = create_action(
            db,
            type_="sudo.approve",
            payload={"request_id": str(row.id)},
            chat_id=chat_id,
            idempotency_key=f"sudo-approve-{row.id}",
        )
        deny = create_action(
            db,
            type_="sudo.deny",
            payload={"request_id": str(row.id)},
            chat_id=chat_id,
            idempotency_key=f"sudo-deny-{row.id}",
        )
        row.action_id = approve.id
        audit(
            db,
            body.user,
            "sudo.request",
            {"request_id": str(row.id), "node": body.node, "argv_hash": row.argv_hash},
        )
        markup = {
            "inline_keyboard": [
                [{"text": "Allow", "callback_data": f"act:{approve.id}"}],
                [{"text": "Deny", "callback_data": f"act:{deny.id}"}],
            ]
        }
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "reply_markup": markup},
            timeout=20,
        )
        data = response.json()
        if not data.get("ok"):
            raise HTTPException(status_code=502, detail=data)
        message_id = data["result"]["message_id"]
        approve.message_id = message_id
        deny.message_id = message_id
        request_id = str(row.id)
    return {"id": request_id, "status": "pending"}


@api.get("/v1/sudo/{request_id}")
def get_sudo(
    request_id: uuid.UUID,
    user: str = Query(),
    node: str = Query(),
    x_internal_token: str | None = Header(default=None),
):
    require_internal(x_internal_token)
    with session() as db:
        row = db.get(SudoRequest, request_id)
        if row is None or row.user_name != user or row.node != node:
            raise HTTPException(status_code=404, detail="not found")
        now = utcnow()
        if row.status in {"pending", "approved"} and as_utc(row.expires_at) <= now:
            row.status = "expired"
            return {"status": "expired", "execute": False}
        if row.status == "pending":
            return {"status": "pending"}
        if row.status == "approved":
            claimed = db.execute(
                update(SudoRequest)
                .where(
                    SudoRequest.id == request_id,
                    SudoRequest.user_name == user,
                    SudoRequest.node == node,
                    SudoRequest.status == "approved",
                )
                .values(status="consumed")
                .returning(SudoRequest.argv)
            ).first()
            if claimed is not None:
                return {"status": "approved", "execute": True, "argv": claimed[0]}
            db.refresh(row)
        if row.status == "denied":
            return {"status": "denied", "execute": False}
        if row.status == "expired":
            return {"status": "expired", "execute": False}
        if row.status == "consumed":
            return {"status": "consumed", "execute": False}
        return {"status": row.status, "execute": False}


@api.post("/v1/sudo/{request_id}/result")
def record_result(
    request_id: uuid.UUID,
    body: ResultIn,
    user: str = Query(),
    node: str = Query(),
    x_internal_token: str | None = Header(default=None),
):
    require_internal(x_internal_token)
    with session() as db:
        row = db.get(SudoRequest, request_id)
        if row is None or row.user_name != user or row.node != node:
            raise HTTPException(status_code=404, detail="not found")
        row.exit_code = body.exit_code
        return {"id": str(row.id), "exit_code": row.exit_code}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("socrat_bot.broker:api", host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
