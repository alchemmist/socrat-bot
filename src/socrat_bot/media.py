"""Bounded inline images; independent durable delivery, never URL fetching."""

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import uuid
from datetime import timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.orm import defer

from socrat_bot.db import session
from socrat_bot.models import (
    AlertImage,
    CardDelivery,
    ImageDelivery,
    Incident,
    TelegramCard,
    User,
    utcnow,
)

MAX_BYTES = 4 * 1024 * 1024


class ImageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    data_base64: str = Field(max_length=4 * ((MAX_BYTES + 2) // 3), repr=False)
    mime_type: Literal["image/png", "image/jpeg"]
    filename: str | None = Field(default=None, max_length=100)

    def decoded(self):
        return base64.b64decode(self.data_base64, validate=True)

    @model_validator(mode="after")
    def validate_image(self):
        if self.mime_type not in ("image/png", "image/jpeg"):
            raise ValueError("images require image/png or image/jpeg")
        try:
            raw = self.decoded()
        except (ValueError, binascii.Error):
            raise ValueError("image data must be strict base64") from None
        signature = (
            b"\x89PNG\r\n\x1a\n" if self.mime_type == "image/png" else b"\xff\xd8\xff"
        )
        if base64.b64encode(raw).decode() != self.data_base64:
            raise ValueError("image data must use canonical base64")
        if not raw.startswith(signature) or len(raw) > MAX_BYTES:
            raise ValueError("image signature mismatch or decoded size exceeds 4 MiB")
        if self.filename and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.filename):
            raise ValueError(
                "filename must contain only ASCII letters, digits, dot, dash or underscore"
            )
        return self


def publish(db, incident, images):
    # Caller holds incident lock; absent/empty is deliberately not deletion.
    for slot, image in enumerate(images or []):
        raw = image.decoded()
        identity = (incident.key, incident.generation, slot)
        row = db.get(AlertImage, identity)
        if row is None:
            row = AlertImage(
                key=incident.key, generation=incident.generation, slot=slot
            )
            db.add(row)
        row.data = raw
        row.revision = hashlib.sha256(raw).hexdigest()
        row.mime_type = image.mime_type
        row.filename = image.filename or (
            "image.png" if image.mime_type == "image/png" else "image.jpg"
        )


def _claim(key=None):
    from socrat_bot.escalation import insert_once

    now = utcnow()
    with session() as db:
        # Only original admitted intents qualify, including escalation intents.
        pairs = db.execute(
            select(AlertImage, CardDelivery)
            .options(defer(AlertImage.data))
            .where(AlertImage.key == key if key is not None else True)
            .join(
                CardDelivery,
                (CardDelivery.key == AlertImage.key)
                & (CardDelivery.generation == AlertImage.generation),
            )
            .join(
                Incident,
                (Incident.key == AlertImage.key)
                & (Incident.generation == AlertImage.generation),
            )
        ).all()
        for image, state in pairs:
            card = db.get(TelegramCard, (image.key, state.chat_id))
            if (
                not card
                or not card.message_id
                or card.generation != image.generation
                or state.message_generation != image.generation
            ):
                continue
            person = db.scalar(select(User).where(User.telegram_id == state.chat_id))
            if not person or not person.alerts_enabled:
                continue
            identity = (image.key, image.generation, state.chat_id, image.slot)
            insert_once(
                db,
                ImageDelivery,
                {
                    "key": image.key,
                    "generation": image.generation,
                    "chat_id": state.chat_id,
                    "slot": image.slot,
                    "available_at": now,
                },
            )
            receipt = db.get(ImageDelivery, identity)
            if (
                receipt.revision == image.revision
                or receipt.available_at.replace(tzinfo=now.tzinfo) > now
            ):
                continue
            owner = uuid.uuid4()
            won = db.execute(
                update(CardDelivery)
                .where(
                    CardDelivery.key == image.key,
                    CardDelivery.chat_id == state.chat_id,
                    CardDelivery.generation == image.generation,
                    or_(
                        CardDelivery.lease_until.is_(None),
                        CardDelivery.lease_until <= now,
                    ),
                )
                .values(lease_owner=owner, lease_until=now + timedelta(seconds=60))
                .execution_options(synchronize_session=False)
            ).rowcount
            if not won:
                continue
            # Snapshot immutable original reply ID, never consult a newer card after HTTP.
            return {
                "identity": identity,
                "owner": owner,
                "revision": image.revision,
                "data": image.data,
                "mime": image.mime_type,
                "filename": image.filename,
                "photo": receipt.message_id,
                "reply": receipt.reply_id or card.message_id,
            }
    return None


def _eligible(work):
    key, generation, chat, _ = work["identity"]
    with session() as db:
        state = db.get(CardDelivery, (key, chat))
        incident = db.get(Incident, key)
        user = db.scalar(select(User).where(User.telegram_id == chat))
        return bool(
            state
            and incident
            and user
            and user.alerts_enabled
            and incident.generation == generation == state.generation
            and state.lease_owner == work["owner"]
            and state.lease_until.replace(tzinfo=utcnow().tzinfo) > utcnow()
        )


def _finish(work, message_id):
    key, _generation, chat, _ = work["identity"]
    with session() as db:
        state = db.scalar(
            select(CardDelivery)
            .where(CardDelivery.key == key, CardDelivery.chat_id == chat)
            .with_for_update()
        )
        if not state or state.lease_owner != work["owner"]:
            return
        receipt = db.get(ImageDelivery, work["identity"])
        if message_id:
            receipt.message_id = message_id
            receipt.reply_id = work["reply"]
            receipt.revision = work["revision"]
        receipt.available_at = utcnow() + timedelta(seconds=30 if not message_id else 0)
        state.lease_owner = state.lease_until = None


async def drain_one(*, key=None):
    work = await asyncio.to_thread(_claim, key)
    if not work:
        return False
    if not await asyncio.to_thread(_eligible, work):
        await asyncio.to_thread(_finish, work, None)
        return True
    chat = work["identity"][2]
    fields = {"chat_id": str(chat)}
    if work["photo"]:
        method = "editMessageMedia"
        fields.update(
            message_id=str(work["photo"]),
            media=json.dumps({"type": "photo", "media": "attach://photo"}),
        )
    else:
        method = "sendPhoto"
        fields["reply_parameters"] = json.dumps(
            {"message_id": work["reply"], "allow_sending_without_reply": False}
        )
    message_id = None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
                data=fields,
                files={"photo": (work["filename"], work["data"], work["mime"])},
            )
            result = response.json()
        if result.get("ok"):
            message_id = work["photo"] or result["result"]["message_id"]
        elif work["photo"] and "message is not modified" in result.get(
            "description", ""
        ):
            message_id = work["photo"]
    except Exception:  # noqa: BLE001 - ambiguous acceptance retains finite lease
        # Unknown acceptance: retain finite lease; no exactly-once claim.
        return True
    await asyncio.to_thread(_finish, work, message_id)
    return True


async def drain(limit=12, *, key=None):
    for _ in range(limit):
        if not await drain_one(key=key):
            break


def status(key, recipients):
    with session() as db:
        incident = db.get(Incident, key)
        images = db.scalars(
            select(AlertImage)
            .options(defer(AlertImage.data))
            .where(AlertImage.key == key, AlertImage.generation == incident.generation)
        ).all()
        result = []
        for chat in recipients:
            for image in images:
                receipt = db.get(
                    ImageDelivery, (key, incident.generation, chat, image.slot)
                )
                sent = bool(receipt and receipt.revision == image.revision)
                result.append(
                    {
                        "chat_id": chat,
                        "slot": image.slot,
                        "status": "sent" if sent else "pending",
                        "message_id": receipt.message_id if receipt else None,
                    }
                )
        return result


async def outbox_loop():
    """Independent worker: failed media cannot hold up the text retry queue."""
    while True:
        try:
            if not await drain_one():
                await asyncio.sleep(1)
        except Exception:  # noqa: BLE001 - keep durable worker alive on DB outages
            await asyncio.sleep(2)
