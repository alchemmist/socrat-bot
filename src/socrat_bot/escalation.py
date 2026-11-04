"""Incident transitions and durable Telegram work. No network I/O inside transactions."""

import asyncio
import hashlib
import os
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import BigInteger, and_, cast, or_, select, update
from sqlalchemy.orm import Session

from socrat_bot.db import session
from socrat_bot.logic import alert_levels, audit, create_action
from socrat_bot.models import (
    Action,
    AlertOutbox,
    CardDelivery,
    Incident,
    TelegramCard,
    User,
    utcnow,
)

Work = tuple[uuid.UUID, str, dict, dict]


def token(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def button(
    db: Session, incident: Incident, chat_id: int
) -> tuple[str, str, int] | None:
    user = db.scalar(select(User).where(User.telegram_id == chat_id))
    levels = alert_levels()
    if (
        incident.status != "firing"
        or incident.level == levels[0]
        or not user
        or not user.alerts_enabled
        or user.admin_level > incident.level
    ):
        return None
    return (
        "Escalate",
        f"esc:{token(incident.key)}:{incident.generation}:{incident.level}",
        1,
    )


def marker(text: str | None, incident: Incident) -> str:
    from socrat_bot.cards import render_escalation

    return render_escalation(text, incident.level, incident.history)


def insert_once(db, model, values):
    """ON CONFLICT avoids the missing-row FOR UPDATE gap on both supported DBs."""
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = (
        insert(model)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(*model.__table__.primary_key.columns)
    )
    return db.execute(statement).first() is not None


def delivery_state(db, key, chat_id, generation):
    card = db.get(TelegramCard, (key, chat_id))
    insert_once(
        db,
        CardDelivery,
        {
            "key": key,
            "chat_id": chat_id,
            "generation": generation,
            "message_generation": card.generation if card and card.message_id else 0,
            "revision": 0,
            "delivered_revision": 0,
            "buttons": [],
            "text": card.text if card else None,
            "spi_url": (
                (card.desired_spi_url or card.spi_url)
                if card.desired_link_revision >= card.link_revision
                else card.spi_url
            )
            if card
            else None,
            "link_revision": max(card.desired_link_revision, card.link_revision)
            if card
            else 0,
            "spi_label": card.spi_label if card else None,
            "note": card.note if card else None,
        },
    )
    return db.scalar(
        select(CardDelivery)
        .where(CardDelivery.key == key, CardDelivery.chat_id == chat_id)
        .with_for_update()
    )


def enqueue_card(db, incident, chat_id, text=None, source=None):
    state = delivery_state(db, incident.key, chat_id, incident.generation)
    if state.generation != incident.generation:
        state.generation = incident.generation
        state.text = None
        state.buttons = []
    if text is not None:
        state.text = text
    state.revision += 1
    row = AlertOutbox(
        payload={
            "key": incident.key,
            "generation": incident.generation,
            "chat_id": chat_id,
            "kind": "card",
            "revision": state.revision,
            "source": source or chat_id,
        }
    )
    db.add(row)
    db.flush()
    return row.id


def route(db: Session, key: str, firing: bool) -> list[int]:
    """Atomically create one incident and snapshot durable initial recipient intents."""
    if (
        "/spi/" in key
        and db.scalar(
            select(TelegramCard.chat_id)
            .where(TelegramCard.key == key, TelegramCard.status == "resolved")
            .limit(1)
        )
        is not None
    ):
        firing = False
    inserted = insert_once(
        db,
        Incident,
        {
            "key": key,
            "level": alert_levels()[-1],
            "generation": 1,
            "history": [],
            "status": "firing" if firing else "resolved",
        },
    )
    incident = db.scalar(select(Incident).where(Incident.key == key).with_for_update())
    if "/spi/" in key and incident.status == "resolved":
        firing = False  # episode keys are immutable terminal identities
    fresh = inserted or (firing and incident.status == "resolved")
    if fresh and not inserted:
        incident.generation += 1
        incident.level = alert_levels()[-1]
        incident.history = []
    incident.status = "firing" if firing else "resolved"
    if fresh and firing:
        people = list(
            db.scalars(
                select(User)
                .where(
                    User.alerts_enabled.is_(True), User.receive_level >= incident.level
                )
                .order_by(User.telegram_id)
            )
        )
        for user in people:
            state = delivery_state(db, key, user.telegram_id, incident.generation)
            state.generation = incident.generation
            state.text = None
            # The initial intent and incident commit together, before any HTTP.
            enqueue_card(db, incident, user.telegram_id)
        return [u.telegram_id for u in people]
    copies = list(
        db.scalars(
            select(TelegramCard).where(
                TelegramCard.key == key,
                TelegramCard.generation == incident.generation,
                TelegramCard.message_id.is_not(None),
            )
        )
    )
    recipients = {c.chat_id for c in copies}
    recipients.update(
        db.scalars(
            select(CardDelivery.chat_id).where(
                CardDelivery.key == key, CardDelivery.generation == incident.generation
            )
        )
    )
    return sorted(recipients)


def escalate(
    db: Session,
    actor: int,
    chat_id: int,
    message_id: int,
    digest: str,
    generation: int,
    expected: int,
) -> str:
    user = db.scalar(select(User).where(User.telegram_id == actor))
    if actor != chat_id or not user or not user.alerts_enabled:
        return "Not authorized"
    # The callback digest is compact enough for Telegram's 64-byte limit.
    incident = next(
        (i for i in db.scalars(select(Incident)) if token(i.key) == digest), None
    )
    if not incident:
        return "Stale incident"
    card = db.get(TelegramCard, (incident.key, chat_id))
    if (
        not card
        or card.message_id != message_id
        or card.generation != generation
        or incident.generation != generation
        or incident.status != "firing"
        or incident.level != expected
        or user.admin_level > expected
    ):
        return "Stale or unauthorized escalation"
    levels = alert_levels()
    if expected not in levels or expected == levels[0]:
        return "Already at final level"
    destination = levels[levels.index(expected) - 1]
    users = list(db.scalars(select(User).where(User.alerts_enabled.is_(True))))
    if not any(u.admin_level == destination for u in users):
        return "Next tier has no enabled subscribers"
    history = incident.history + [
        {
            "from": expected,
            "level": destination,
            "actor": actor,
            "at": utcnow().isoformat(),
        }
    ]
    changed = db.execute(
        update(Incident)
        .where(
            Incident.key == incident.key,
            Incident.generation == generation,
            Incident.level == expected,
            Incident.status == "firing",
        )
        .values(level=destination, history=history),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        return "Already escalated"
    db.refresh(incident)
    copies = {
        c.chat_id: c
        for c in db.scalars(
            select(TelegramCard).where(
                TelegramCard.key == incident.key,
                TelegramCard.generation == generation,
                TelegramCard.message_id.is_not(None),
            )
        )
    }
    template = next(iter(copies.values()))
    for user in users:
        existing = copies.get(user.telegram_id)
        if not existing and user.receive_level < destination:
            continue
        state = delivery_state(db, incident.key, user.telegram_id, generation)
        if state.generation != generation:
            state.generation = generation
            state.text = None
            state.buttons = []
        if not existing and not state.text:
            source_state = delivery_state(
                db, incident.key, template.chat_id, generation
            )
            state.text = source_state.text or template.text
            state.link_revision = source_state.link_revision
            state.spi_url, state.spi_label, state.note = (
                source_state.spi_url,
                source_state.spi_label,
                source_state.note,
            )
            cloned = []
            for spec in source_state.buttons:
                original = db.get(
                    Action, uuid.UUID(spec["callback_data"].removeprefix("act:"))
                )
                if original is None:
                    continue
                minutes = original.payload.get("minutes", 60)
                identity = (
                    f"{incident.key}:g{generation}:{minutes}:{user.telegram_id}"[:128]
                    if original.type == "alert.silence"
                    else f"clone:{original.id}:g{generation}:{user.telegram_id}"
                )
                action = create_action(
                    db,
                    type_=original.type,
                    payload=dict(original.payload),
                    chat_id=user.telegram_id,
                    idempotency_key=identity,
                )
                cloned.append({**spec, "callback_data": f"act:{action.id}"})
            state.buttons = cloned

        enqueue_card(db, incident, user.telegram_id, source=template.chat_id)
        if existing and user.admin_level == destination and user.telegram_id != actor:
            db.add(
                AlertOutbox(
                    payload={
                        "key": incident.key,
                        "generation": generation,
                        "chat_id": user.telegram_id,
                        "kind": "notice",
                        "message_id": existing.message_id,
                        "level": destination,
                    }
                )
            )
    # Disabled recipients still retain and receive edits to their already-delivered copies.
    enabled = {u.telegram_id for u in users}
    for chat in copies.keys() - enabled:
        enqueue_card(db, incident, chat)
    audit(
        db,
        str(actor),
        "alert.escalated",
        {
            "key": incident.key,
            "generation": generation,
            "from": expected,
            "to": destination,
        },
    )
    return "Escalated"


def prepare_work(work_id: uuid.UUID | None = None) -> Work | None:
    with session() as db:
        now = utcnow()
        query = (
            select(AlertOutbox)
            .outerjoin(
                CardDelivery,
                and_(
                    CardDelivery.key == AlertOutbox.payload["key"].as_string(),
                    CardDelivery.chat_id
                    == cast(AlertOutbox.payload["chat_id"].as_string(), BigInteger),
                ),
            )
            .where(
                AlertOutbox.done.is_(False),
                AlertOutbox.available_at <= now,
                or_(
                    CardDelivery.lease_until.is_(None), CardDelivery.lease_until <= now
                ),
            )
        )
        if work_id is not None:
            query = query.where(AlertOutbox.id == work_id)
        row = db.scalar(
            query.order_by(AlertOutbox.available_at)
            .limit(1)
            .with_for_update(skip_locked=True, of=AlertOutbox)
        )
        if not row:
            return None
        p = dict(row.payload)
        state = delivery_state(db, p["key"], p["chat_id"], p["generation"])
        owner = uuid.uuid4()
        claimed = db.execute(
            update(CardDelivery)
            .where(
                CardDelivery.key == state.key,
                CardDelivery.chat_id == state.chat_id,
                or_(
                    CardDelivery.lease_until.is_(None), CardDelivery.lease_until <= now
                ),
            )
            .values(lease_owner=owner, lease_until=now + timedelta(seconds=60)),
            execution_options={"synchronize_session": False},
        )
        if not claimed.rowcount:
            return None
        p["_owner"] = str(owner)
        p["_revision"] = state.revision
        p["_spi_url"], p["_spi_label"], p["_link_revision"], p["_note"] = (
            state.spi_url,
            state.spi_label,
            state.link_revision,
            state.note,
        )
        row.available_at = now + timedelta(seconds=60)
        row.attempts += 1

        def discard():
            row.done = True
            db.execute(
                update(CardDelivery)
                .where(
                    CardDelivery.key == state.key, CardDelivery.chat_id == state.chat_id
                )
                .values(lease_owner=None, lease_until=None)
            )

        if p["kind"] == "retire":
            return (
                row.id,
                "editMessageText",
                {
                    "chat_id": p["chat_id"],
                    "message_id": p["message_id"],
                    "text": p["text"],
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": []},
                },
                p,
            )
        incident = db.get(Incident, p["key"])
        card = db.get(TelegramCard, (p["key"], p["chat_id"]), populate_existing=True)
        user = db.scalar(select(User).where(User.telegram_id == p["chat_id"]))
        if (
            not incident
            or (incident.generation != p["generation"] and p["kind"] != "spi")
            or state.generation != p["generation"]
        ):
            discard()
            return None
        existing = (
            card and state.message_generation == p["generation"] and card.message_id
        )
        if p["kind"] == "spi" and (not existing or card.message_id != p["message_id"]):
            discard()
            return None
        if incident.status != "firing" and (not existing or p["kind"] == "notice"):
            discard()
            return None
        p["_level"], p["_status"] = incident.level, incident.status
        if not existing and (not user or not user.alerts_enabled):
            discard()
            return None
        if p["kind"] == "notice":
            if not user or not user.alerts_enabled:
                discard()
                return None
            bot_id = os.environ.get("TELEGRAM_BOT_TOKEN", "").split(":", 1)[0]
            text = f'Escalated to level {p["level"]}. <a href="tg://openmessage?user_id={bot_id}&amp;message_id={p["message_id"]}">Open alert</a>'
            return (
                row.id,
                "sendMessage",
                {"chat_id": p["chat_id"], "text": text, "parse_mode": "HTML"},
                p,
            )
        if state.text is None:
            # Initial intent exists but content has not been supplied yet (legacy route callers).
            db.execute(
                update(CardDelivery)
                .where(
                    CardDelivery.key == state.key, CardDelivery.chat_id == state.chat_id
                )
                .values(lease_owner=None, lease_until=None)
            )
            row.available_at = now + timedelta(seconds=1)
            return None
        # Rehydrate a desired fence accepted by an older binary/migration before any HTTP.
        if card and (
            card.desired_link_revision > state.link_revision
            or (
                card.desired_link_revision == state.link_revision
                and card.desired_spi_url
                and card.desired_spi_url != state.spi_url
            )
        ):
            discard()
            return None
        text = (
            marker(state.text, incident)
            if incident.generation == p["generation"]
            else state.text
        )
        if state.spi_url:
            from socrat_bot.cards import replace_spi_link

            text = replace_spi_link(
                text,
                card.spi_url if card else None,
                card.spi_label if card else None,
                state.spi_url,
                state.spi_label or "SPI",
            )
        if incident.status == "resolved" and not text.startswith("[Resolved] "):
            text = "[Resolved] " + text
        if (
            p["kind"] != "spi"
            and existing
            and state.delivered_revision >= state.revision
            and card.text == text
            and card.status == incident.status
        ):
            discard()
            return None
        if p["kind"] == "spi":
            # Always confirm retry via idempotent edit; desired state is not delivery acknowledgement.
            return (
                row.id,
                "editMessageText",
                {
                    "chat_id": p["chat_id"],
                    "message_id": card.message_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                p,
            )
        rows = []
        # Preserve the existing mute policy: a muted card has no buttons.
        if incident.status == "firing" and "<i>" not in text:
            if not existing:
                source_actions = list(
                    db.scalars(
                        select(Action).where(
                            Action.chat_id == p.get("source"),
                            Action.type == "alert.silence",
                            Action.status == "pending",
                        )
                    )
                )
                for source_action in source_actions:
                    if (
                        source_action.payload.get("key") == incident.key
                        and f":g{incident.generation}:" in source_action.idempotency_key
                    ):
                        minutes = source_action.payload.get("minutes", 60)
                        create_action(
                            db,
                            type_="alert.silence",
                            payload={**source_action.payload, "card": text},
                            chat_id=p["chat_id"],
                            idempotency_key=f"{incident.key}:g{incident.generation}:{minutes}:{p['chat_id']}"[
                                :128
                            ],
                        )
            actions = list(
                db.scalars(
                    select(Action).where(
                        Action.chat_id == p["chat_id"],
                        Action.type == "alert.silence",
                        Action.status == "pending",
                    )
                )
            )
            mute = [
                {
                    "text": "Mute 1h"
                    if a.payload.get("minutes", 60) == 60
                    else f"Mute {a.payload.get('minutes', 60)}m",
                    "callback_data": f"act:{a.id}",
                }
                for a in sorted(
                    actions, key=lambda a: int(a.payload.get("minutes", 60))
                )
                if a.payload.get("key") == incident.key
                and f":g{incident.generation}:" in a.idempotency_key
            ]
            if state.buttons:
                grouped = {}
                for spec in state.buttons:
                    grouped.setdefault(spec.get("row", 0), []).append(
                        {"text": spec["text"], "callback_data": spec["callback_data"]}
                    )
                rows = [grouped[n] for n in sorted(grouped)]
            elif mute:
                rows.append(mute)
            esc = button(db, incident, p["chat_id"])
            if esc:
                rows.append([{"text": esc[0], "callback_data": esc[1]}])
        body = {
            "chat_id": p["chat_id"],
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": rows},
        }
        if existing:
            body["message_id"] = card.message_id
        return row.id, "editMessageText" if existing else "sendMessage", body, p


def renew_lease(p):
    with session() as db:
        return (
            db.execute(
                update(CardDelivery)
                .where(
                    CardDelivery.key == p["key"],
                    CardDelivery.chat_id == p["chat_id"],
                    CardDelivery.lease_owner == uuid.UUID(p["_owner"]),
                )
                .values(lease_until=utcnow() + timedelta(seconds=60))
            ).rowcount
            == 1
        )


def authorize_http(work: Work) -> bool:
    """Fence a superseded SPI snapshot again immediately before starting HTTP."""
    id_, _method, _body, p = work
    with session() as db:
        state = db.scalar(
            select(CardDelivery)
            .where(CardDelivery.key == p["key"], CardDelivery.chat_id == p["chat_id"])
            .with_for_update()
        )
        if state.lease_owner != uuid.UUID(p["_owner"]):
            return False
        card = db.get(TelegramCard, (p["key"], p["chat_id"]))
        stale_state = state.link_revision > p.get("_link_revision", 0) or (
            state.link_revision == p.get("_link_revision", 0)
            and state.spi_url
            and state.spi_url != p.get("_spi_url")
        )
        stale_card = card and (
            card.desired_link_revision > p.get("_link_revision", 0)
            or (
                card.desired_link_revision == p.get("_link_revision", 0)
                and card.desired_spi_url
                and card.desired_spi_url != p.get("_spi_url")
            )
        )
        if p["kind"] in {"card", "spi"} and (stale_state or stale_card):
            db.get(AlertOutbox, id_).done = True
            state.lease_owner = state.lease_until = None
            return False
        return True


async def drain_one() -> bool:
    work = await asyncio.to_thread(prepare_work)
    return await _deliver_work(work) if work else False


async def _deliver_work(work: Work) -> bool:
    id_, method, body, p = work

    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            if not await asyncio.to_thread(renew_lease, p):
                return

    ticker = asyncio.create_task(heartbeat())
    definite_rejection = False
    try:
        if not await asyncio.to_thread(renew_lease, p) or not await asyncio.to_thread(
            authorize_http, work
        ):
            return False
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
                json=body,
            )
            data = response.json()
            if not data.get("ok") and "message is not modified" not in str(data):
                definite_rejection = True
                raise RuntimeError("Telegram delivery rejected")

        return await asyncio.to_thread(finish_work, work, data)
    except Exception:  # noqa: BLE001 - durable retry boundary; never log token-bearing HTTP errors
        # Definite rejection is safe to retry immediately. Ambiguous transport failures
        # retain their lease: Telegram may already have accepted the send.
        if definite_rejection:

            def release():
                with session() as db:
                    result = db.execute(
                        update(CardDelivery)
                        .where(
                            CardDelivery.key == p["key"],
                            CardDelivery.chat_id == p["chat_id"],
                            CardDelivery.lease_owner == uuid.UUID(p["_owner"]),
                        )
                        .values(lease_owner=None, lease_until=None)
                    )
                    if result.rowcount:
                        db.get(AlertOutbox, id_).available_at = utcnow()

            await asyncio.to_thread(release)
        return False
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)


def finish_work(work: Work, data: dict) -> bool:
    id_, _method, body, p = work
    with session() as db:
        state = db.scalar(
            select(CardDelivery)
            .where(
                CardDelivery.key == p["key"],
                CardDelivery.chat_id == p["chat_id"],
            )
            .with_for_update()
        )
        if state.lease_owner != uuid.UUID(p["_owner"]):
            return False
        row = db.get(AlertOutbox, id_)
        row.done = True
        state.lease_owner = state.lease_until = None
        if p["kind"] not in {"card", "spi"}:
            return True
        incident = db.get(Incident, p["key"])
        message_id = body.get("message_id") or data["result"]["message_id"]
        if (
            incident.generation != p["generation"] and p["kind"] != "spi"
        ) or state.generation != p["generation"]:
            retired = (
                body["text"]
                if body["text"].startswith("[Resolved] ")
                else "[Resolved] " + body["text"]
            )
            db.add(
                AlertOutbox(
                    payload={
                        "key": p["key"],
                        "generation": p["generation"],
                        "chat_id": p["chat_id"],
                        "kind": "retire",
                        "message_id": message_id,
                        "text": retired,
                    }
                )
            )
            return True
        card = db.get(TelegramCard, (p["key"], p["chat_id"]))
        if not card:
            card = TelegramCard(key=p["key"], chat_id=p["chat_id"])
            db.add(card)
            db.flush()  # server/Python defaults initialize the upstream SPI fences
        if p["kind"] == "spi" and card.message_id != p["message_id"]:
            return True
        card.generation, card.status, card.message_id = (
            state.generation,
            incident.status if incident.generation == p["generation"] else card.status,
            message_id,
        )
        # Applied SPI metadata describes the actual accepted HTTP snapshot, never newer desired state.
        card.text = body["text"]
        card.spi_url, card.spi_label, card.note = (
            p["_spi_url"],
            p["_spi_label"],
            p["_note"],
        )
        card.link_revision = p["_link_revision"]
        if state.link_revision >= card.desired_link_revision:
            card.desired_link_revision, card.desired_spi_url = (
                state.link_revision,
                state.spi_url,
            )
        state.message_generation = p["generation"]
        state.delivered_revision = p["_revision"]
        if (
            state.revision != p["_revision"]
            or incident.level != p["_level"]
            or incident.status != p["_status"]
        ):
            db.add(
                AlertOutbox(
                    payload={
                        "key": p["key"],
                        "generation": state.generation,
                        "chat_id": p["chat_id"],
                        "kind": "spi" if p["kind"] == "spi" else "card",
                        "message_id": message_id,
                    }
                )
            )
            # Status/stage changes from legacy callers also need a distinct revision.
            if state.revision == p["_revision"]:
                state.revision += 1
        for spec in state.buttons:
            action = db.get(
                Action, uuid.UUID(spec["callback_data"].removeprefix("act:"))
            )
            if action:
                action.message_id = message_id
        for action in db.scalars(
            select(Action).where(
                Action.chat_id == p["chat_id"],
                Action.type == "alert.silence",
                Action.status == "pending",
            )
        ):
            if (
                action.payload.get("key") == incident.key
                and f":g{incident.generation}:" in action.idempotency_key
            ):
                action.message_id = message_id
        return True


async def outbox_loop() -> None:
    while True:
        try:
            if not await drain_one():
                await asyncio.sleep(1)
        except Exception:  # noqa: BLE001 - recover database outages without killing the worker
            await asyncio.sleep(2)
