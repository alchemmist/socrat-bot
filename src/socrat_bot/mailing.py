"""Receipt-only in-place edits. Never derives an audience or sends replacements."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select, update

from .db import session
from .models import Broadcast, BroadcastCopy


def failure(data):
    # Telegram descriptions are useful; never expose transport exceptions/URLs.
    description = str(data.get("description", "Telegram rejected the edit"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    return description.replace(token, "[redacted]")[:300] if token else description[:300]


async def edit_mailing(broadcast_id, owner_id, text):
    if not text or len(text.encode("utf-16-le")) // 2 > 4096:
        return "Text must contain 1–4096 UTF-16 units."
    token = str(uuid.uuid4())
    now = datetime.now(UTC)
    with session() as db:
        claimed = db.execute(update(Broadcast).where(
            Broadcast.id == broadcast_id, Broadcast.chat_id == owner_id,
            Broadcast.status == "sent",
            or_(Broadcast.edit_token.is_(None), Broadcast.edit_until < now),
        ).values(edit_token=token, edit_until=now + timedelta(minutes=5))).rowcount
        if not claimed:
            return "Mailing unavailable, not sent, not yours, or another edit is running."
        copies = list(db.scalars(select(BroadcastCopy).where(BroadcastCopy.broadcast_id == broadcast_id)))
        row = db.get(Broadcast, broadcast_id)
        if copies:
            row.text = text
    updated = skipped = 0
    errors = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for copy in copies:
                with session() as db:
                    owned = db.execute(update(Broadcast).where(
                        Broadcast.id == broadcast_id, Broadcast.edit_token == token,
                        Broadcast.edit_until > datetime.now(UTC),
                    ).values(edit_until=datetime.now(UTC) + timedelta(minutes=5))).rowcount
                if not owned:
                    return "Edit lease lost; retry to reconcile remaining copies."
                if copy.applied_text == text:
                    skipped += 1
                    continue
                error = None
                try:
                    response = await client.post(
                        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/editMessageText",
                        json={"chat_id": copy.chat_id, "message_id": copy.message_id, "text": text},
                    )
                    data = response.json()
                    if not (response.status_code == 200 and data.get("ok")):
                        error = failure(data)
                        if "message is not modified" in error.lower():
                            error = None
                except (httpx.HTTPError, ValueError, KeyError):
                    error = "Transport error (delivery outcome unknown); retry."
                with session() as db:
                    owned = db.execute(update(Broadcast).where(
                        Broadcast.id == broadcast_id, Broadcast.edit_token == token,
                    ).values(edit_until=datetime.now(UTC) + timedelta(minutes=5))).rowcount
                    if not owned:
                        return "Edit lease lost; retry to reconcile remaining copies."
                    stored = db.get(BroadcastCopy, (broadcast_id, copy.chat_id))
                    stored.error = error
                    if error is None:
                        stored.applied_text = text
                if error:
                    errors.append(f"{copy.chat_id}: {error}")
                else:
                    updated += 1
    finally:
        with session() as db:
            db.execute(update(Broadcast).where(Broadcast.id == broadcast_id, Broadcast.edit_token == token)
                       .values(edit_token=None, edit_until=None))
    if not copies:
        return "Unavailable: no original message IDs were saved for this mailing (older releases cannot be edited)."
    return f"Updated {updated}; failed {len(errors)}; skipped {skipped}." + ("\n" + "\n".join(errors) if errors else "")
