"""All alert-card writers publish desired revisions, then use the same durable dispatcher."""

import asyncio
import html
import time
import uuid
from dataclasses import replace

from sqlalchemy import select

from socrat_bot.cards import (
    add_link,
    add_note,
    mute_actions,
    render_card,
    replace_note,
    replace_spi_link,
)
from socrat_bot.db import session
from socrat_bot.escalation import (
    _deliver_work,
    delivery_state,
    enqueue_card,
    marker,
    prepare_work,
    route,
)
from socrat_bot.logic import create_action
from socrat_bot.models import AlertOutbox, CardDelivery, Incident, TelegramCard


def submit_card(card, custom_actions=None, images=None, *, metadata=False):
    with session() as db:
        recipients = route(db, card.key, card.firing)
        incident = db.get(Incident, card.key)
        from socrat_bot.media import publish

        publish(db, incident, images)
        card = replace(card, status=incident.status)
        jobs = []
        for chat_id in recipients:
            state = delivery_state(db, card.key, chat_id, incident.generation)
            # Route resets new-generation content; annotations intentionally survive.
            text = render_card(card)
            if state.spi_url:
                text = add_link(text, state.spi_url, state.spi_label or "SPI")
            if state.note:
                text = add_note(text, state.note)
            if card.firing and state.text and "<i>" in state.text:
                text += "\n\n" + state.text[state.text.rfind("<i>") :]
            incoming = custom_actions or mute_actions(
                card, incident.generation, chat_id
            )
            specs = []
            for item in incoming if card.firing else []:
                payload = dict(item.get("payload") or {})
                if item["type"] == "alert.silence":
                    payload.update(card=text, key=card.key)
                action = create_action(
                    db,
                    type_=item["type"],
                    payload=payload,
                    chat_id=chat_id,
                    idempotency_key=item.get("idempotency_key") or str(uuid.uuid4()),
                    refresh=True,
                )
                specs.append(
                    {
                        "text": item.get("label") or action.type,
                        "callback_data": f"act:{action.id}",
                        "row": int(item.get("row") or 0),
                    }
                )
            state.buttons = specs
            jobs.append(enqueue_card(db, incident, chat_id, text=text))
        if metadata:
            import hashlib

            snapshot = {"generation": incident.generation, "revisions": {
                slot: hashlib.sha256(image.decoded()).hexdigest()
                for slot, image in enumerate(images or [])}}
            return recipients, jobs, snapshot
        return recipients, jobs


def annotate_card(
    key,
    *,
    url=None,
    label="SPI",
    note=None,
    mute=None,
    replacement=None,
    chat_id=None,
    expected_generation=None,
):
    """Desired-state mutation commits before HTTP and cannot lose a concurrent writer."""
    with session() as db:
        incident = db.scalar(
            select(Incident).where(Incident.key == key).with_for_update()
        )
        if not incident:
            raise LookupError("card not found")
        if (
            expected_generation is not None
            and incident.generation != expected_generation
        ):
            raise ValueError("stale card generation")
        states = list(
            db.scalars(
                select(CardDelivery)
                .where(
                    CardDelivery.key == key,
                    CardDelivery.generation == incident.generation,
                )
                .order_by(CardDelivery.chat_id)
                .with_for_update()
            )
        )
        # Lazy adoption also supports existing cards created before migration/tests.
        if not states:
            for card in db.scalars(
                select(TelegramCard).where(
                    TelegramCard.key == key,
                    TelegramCard.generation == incident.generation,
                )
            ):
                states.append(
                    delivery_state(db, key, card.chat_id, incident.generation)
                )
        if not states:
            raise LookupError("card not found")
        if not any(s.text for s in states):
            raise ValueError("card text is not stored yet")
        jobs = []
        for state in states:
            if chat_id is not None and state.chat_id != chat_id:
                continue
            if not state.text:
                continue
            text = state.text
            if replacement is not None:
                text = html.escape(replacement)
                if state.spi_url:
                    text = add_link(text, state.spi_url, state.spi_label or "SPI")
                if state.note:
                    text = add_note(text, state.note)
                if "<i>" in state.text:
                    text += "\n\n" + state.text[state.text.rfind("<i>") :]
            if url:
                applied = db.get(TelegramCard, (key, state.chat_id))
                if (
                    applied
                    and applied.desired_link_revision > 0
                    and applied.desired_spi_url != url
                ):
                    raise ValueError("revision required for this card")
                if applied and applied.desired_link_revision == 0:
                    applied.desired_spi_url = url
                text = replace_spi_link(
                    text, state.spi_url, state.spi_label, url, label
                )
                state.spi_url, state.spi_label = url, label
            if note is not None:
                text = replace_note(text, state.note, note)
                state.note = note
            if mute:
                if "<i>" in text:
                    text = text[: text.rfind("<i>")].rstrip()
                text += "\n\n" + mute
            jobs.append(enqueue_card(db, incident, state.chat_id, text=text))
            card = db.get(TelegramCard, (key, state.chat_id))
            if card and card.generation == incident.generation:
                card.text = marker(text, incident)
                card.note = state.note
        return [
            s.chat_id for s in states if chat_id is None or s.chat_id == chat_id
        ], jobs


async def dispatch_jobs(jobs, *, wait_seconds=25):
    """Synchronous API compatibility; background recovery owns any unfinished work."""
    deadline = time.monotonic() + wait_seconds
    remaining = set(jobs)
    while remaining:
        progress = False
        for id_ in list(remaining):
            work = await asyncio.to_thread(prepare_work, id_)
            if work:
                progress = True
                if not await _deliver_work(work):
                    # Do not obscure errors or spin on ambiguous Telegram timeouts.
                    remaining.discard(id_)
                    continue
            with session() as db:
                row = db.get(AlertOutbox, id_)
                if row.done:
                    remaining.discard(id_)
        if remaining and not progress:
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.05)


def delivered_copies(key, recipients, jobs=None):
    with session() as db:
        incident = db.get(Incident, key)
        result = []
        requested = {}
        if jobs is not None:
            for row in db.scalars(select(AlertOutbox).where(AlertOutbox.id.in_(jobs))):
                requested[row.payload["chat_id"]] = max(
                    requested.get(row.payload["chat_id"], 0),
                    row.payload.get("revision", 0),
                )
        for chat_id in recipients:
            card = db.get(TelegramCard, (key, chat_id))
            state = db.get(CardDelivery, (key, chat_id))
            if (
                card
                and card.message_id
                and card.generation == incident.generation
                and state.message_generation == incident.generation
                and state.delivered_revision >= requested.get(chat_id, state.revision)
            ):
                result.append({"chat_id": chat_id, "message_id": card.message_id})
        return result


def publish_spi(key, url, label, revision, expected_copies):
    """Accept an immutable desired SPI fence before HTTP; applied metadata stays honest."""
    from fastapi import HTTPException

    from socrat_bot.escalation import insert_once
    from socrat_bot.logic import alert_levels

    with session() as db:
        rows = list(
            db.scalars(
                select(TelegramCard)
                .where(TelegramCard.key == key)
                .order_by(TelegramCard.chat_id)
            )
        )
        if not rows:
            raise HTTPException(404, "card not found")
        insert_once(
            db,
            Incident,
            {
                "key": key,
                "generation": max(r.generation for r in rows),
                "level": alert_levels()[-1],
                "status": "resolved"
                if any(r.status == "resolved" for r in rows)
                else "firing",
                "history": [],
            },
        )
        incident = db.scalar(
            select(Incident).where(Incident.key == key).with_for_update()
        )
        states = {
            r.chat_id: delivery_state(db, key, r.chat_id, r.generation) for r in rows
        }
        # A pending first send can materialize another TelegramCard while we wait
        # for ownership rows. Lock all original intents before refreshing copies.
        states.update(
            {
                state.chat_id: state
                for state in db.scalars(
                    select(CardDelivery)
                    .where(CardDelivery.key == key)
                    .order_by(CardDelivery.chat_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
        )
        rows = list(
            db.scalars(
                select(TelegramCard)
                .where(TelegramCard.key == key)
                .order_by(TelegramCard.chat_id)
                .execution_options(populate_existing=True)
            )
        )
        if not any(r.text for r in rows):
            raise HTTPException(409, "card text is not stored yet")
        if expected_copies is not None:
            if not expected_copies or len(expected_copies) > 1000:
                raise HTTPException(400, "invalid expected copies")
            expected = set()
            for copy in expected_copies:
                if (
                    type(copy.get("chat_id")) is not int
                    or type(copy.get("message_id")) is not int
                ):
                    raise HTTPException(400, "invalid expected copy")
                expected.add((copy["chat_id"], copy["message_id"]))
            if not expected <= {(r.chat_id, r.message_id) for r in rows}:
                raise HTTPException(409, "card identity changed")
        if revision is None and any(
            r.desired_link_revision > 0 and r.desired_spi_url != url for r in rows
        ):
            raise HTTPException(409, "revision required for this card")
        if revision is not None:
            if revision < 1 or label != "SPI" or not expected_copies:
                raise HTTPException(400, "revision requires expected copies")
            if any(
                max(r.link_revision, r.desired_link_revision) > revision
                or (
                    r.desired_link_revision == revision
                    and r.desired_spi_url not in (None, url)
                )
                for r in rows
            ):
                raise HTTPException(409, "stale link revision")
        jobs, pairs = [], []
        for row in rows:
            state = states[row.chat_id]
            if state.generation != row.generation:
                raise HTTPException(409, "card identity changed")
            if revision is not None:
                row.desired_link_revision, row.desired_spi_url = revision, url
            elif row.desired_link_revision == 0:
                row.desired_spi_url = (
                    url  # legacy unversioned targets still publish before HTTP
                )
            text = state.text or row.text
            if text:
                text = replace_spi_link(
                    text, state.spi_url, state.spi_label, url, label
                )
                text = replace_spi_link(text, row.spi_url, row.spi_label, url, label)
            state.text, state.spi_url, state.spi_label = text, url, label
            state.link_revision = (
                revision
                if revision is not None
                else max(
                    row.desired_link_revision, row.link_revision, state.link_revision
                )
            )
            state.revision += 1
            job = AlertOutbox(
                payload={
                    "key": key,
                    "generation": state.generation,
                    "chat_id": row.chat_id,
                    "kind": "spi",
                    "message_id": row.message_id,
                    "revision": state.revision,
                }
            )
            db.add(job)
            db.flush()
            jobs.append(job.id)
            pairs.append({"chat_id": row.chat_id, "message_id": row.message_id})
        # Original admitted recipients may still await their first main card. A
        # key-wide SPI association must survive their later successful first send.
        for state in db.scalars(
            select(CardDelivery)
            .where(
                CardDelivery.key == key, CardDelivery.generation == incident.generation
            )
            .order_by(CardDelivery.chat_id)
            .with_for_update()
        ):
            if state.chat_id in {row.chat_id for row in rows}:
                continue
            if revision is not None and state.link_revision > revision:
                raise HTTPException(409, "stale link revision")
            text = (
                replace_spi_link(state.text, state.spi_url, state.spi_label, url, label)
                if state.text
                else None
            )
            state.spi_url, state.spi_label = url, label
            if revision is not None:
                state.link_revision = revision
            enqueue_card(db, incident, state.chat_id, text=text)
        return pairs, jobs


def spi_acknowledgements(key, pairs, jobs, url, revision):
    with session() as db:
        requested = {
            j.payload["chat_id"]: j.payload["revision"]
            for j in db.scalars(select(AlertOutbox).where(AlertOutbox.id.in_(jobs)))
        }
        updated, pending = [], []
        for pair in pairs:
            row = db.get(TelegramCard, (key, pair["chat_id"]))
            state = db.get(CardDelivery, (key, pair["chat_id"]))
            applied = (
                row
                and row.message_id == pair["message_id"]
                and row.spi_url == url
                and state.spi_url == url
                and row.desired_spi_url in (None, url)
                and state.delivered_revision >= requested[pair["chat_id"]]
            )
            if revision is not None:
                applied = (
                    applied
                    and row.link_revision == revision
                    and row.desired_link_revision == revision
                    and row.desired_spi_url == url
                )
            (updated if applied else pending).append(pair)
        return {"key": key, "updated": updated, "pending": pending}
