"""All alert-card writers publish desired revisions, then use the same durable dispatcher."""

import asyncio
import html
import time
import uuid

from sqlalchemy import select

from socrat_bot.cards import add_link, add_note, mute_actions, render_card, replace_note
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


def submit_card(card, custom_actions=None):
    with session() as db:
        recipients = route(db, card.key, card.firing)
        incident = db.get(Incident, card.key)
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
                state.spi_url, state.spi_label = url, label
                text = add_link(text, url, label)
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
                card.spi_url, card.spi_label, card.note = (
                    state.spi_url,
                    state.spi_label,
                    state.note,
                )
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
