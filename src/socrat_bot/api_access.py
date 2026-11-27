"""Public grants never substitute for webhook, management, or legacy credentials."""

import hashlib
import os
import secrets
import time
import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import session
from .models import ApiGrant

TOKEN_PREFIX = "sb1_"
TOKEN_BYTES = 32
TOKEN_LENGTH = len(TOKEN_PREFIX) + (TOKEN_BYTES * 8 + 5) // 6

SCOPES = {
    "messages:send",
    "messages:advanced",
    "messages:edit",
    "cards:read",
    "cards:link",
    "cards:note",
    "audience:read",
    "links:issue",
    "broadcasts:send",
    "actions:read",
}


@dataclass(frozen=True)
class Grant:
    scopes: frozenset
    chats: frozenset


bearer = HTTPBearer(auto_error=False)


def credential(scope):
    def check(request: Request, auth=Depends(bearer)):
        if request.scope["route"].path.startswith("/v1/"):
            if os.getenv("BOT_DISABLE_LEGACY_API") == "true":
                raise HTTPException(404)
            return request.headers.get("x-internal-token")
        if (
            not auth
            or not auth.credentials.startswith(TOKEN_PREFIX)
            or len(auth.credentials) != TOKEN_LENGTH
        ):
            raise HTTPException(401, "invalid bearer")
        digest = hashlib.sha256(auth.credentials.encode()).hexdigest()
        with session() as db:
            row = db.scalar(
                select(ApiGrant).where(
                    ApiGrant.digest == digest, ApiGrant.expires > int(time.time())
                )
            )
            if not row:
                raise HTTPException(401, "invalid bearer")
            if scope not in row.scopes:
                raise HTTPException(403, "scope required: " + scope)
            return Grant(frozenset(row.scopes), frozenset(row.chats))

    return check


basic = HTTPBasic()


def admin(credentials: HTTPBasicCredentials = Depends(basic)):
    key = os.environ.get("BOT_API_ADMIN_SECRET", "")
    if (
        len(key) < 32
        or credentials.username != "admin"
        or not secrets.compare_digest(credentials.password.encode(), key.encode())
    ):
        raise HTTPException(
            401,
            "administrator required",
            headers={"WWW-Authenticate": 'Basic realm="Bot API"'},
        )


def mutation(request: Request, _=Depends(admin)):
    # Basic auth is ambient in browsers. Reject simple cross-origin requests;
    # this app does not enable CORS. Never mint via GET or an HTML form POST.
    if request.headers.get("x-bot-admin") != "1":
        raise HTTPException(403, "management header required")


class Issue(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: set[str] = Field(min_length=1, max_length=10)
    chats: set[int] = Field(default_factory=set, max_length=100)
    days: int = Field(default=30, ge=1, le=365)


def install(app):
    from .grant_ui import page

    @app.get("/", dependencies=[Depends(admin)], include_in_schema=False)
    def home():
        return HTMLResponse(
            page(SCOPES),
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/admin/tokens", dependencies=[Depends(admin)])
    def listing(response: Response):
        response.headers["Cache-Control"] = "no-store"
        with session() as db:
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "scopes": r.scopes,
                    "chats": r.chats,
                    "expires": r.expires,
                }
                for r in db.scalars(select(ApiGrant).order_by(ApiGrant.name))
            ]

    @app.post("/admin/tokens", dependencies=[Depends(mutation)], status_code=201)
    def issue(body: Issue, response: Response):
        if body.scopes - SCOPES or (
            "messages:send" in body.scopes
            and "messages:advanced" not in body.scopes
            and not body.chats
        ):
            raise HTTPException(
                422, "choose known scopes and destination chats for plain send"
            )
        if body.chats and body.scopes != {"messages:send"}:
            raise HTTPException(
                422, "destination restrictions apply only to plain send"
            )
        secret = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
        id_ = uuid.uuid4().hex
        with session() as db:
            db.add(
                ApiGrant(
                    id=id_,
                    digest=hashlib.sha256(secret.encode()).hexdigest(),
                    name=body.name,
                    scopes=sorted(body.scopes),
                    chats=sorted(body.chats),
                    expires=int(time.time()) + 86400 * body.days,
                )
            )
        response.headers["Cache-Control"] = "no-store"
        return {"id": id_, "token": secret}

    @app.delete(
        "/admin/tokens/{id_}", dependencies=[Depends(mutation)], status_code=204
    )
    def revoke(id_: str):
        with session() as db:
            row = db.get(ApiGrant, id_)
            if not row:
                raise HTTPException(404)
            db.delete(row)
        return Response(status_code=204)
