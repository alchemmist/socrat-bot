"""Finite durable rendering retries, separate from Telegram's delivery outbox.

No historical replay: only newly received graph intents enter this queue. A late
renderer result can publish only to its current incident generation and desired
capture. Rendering failures never block the original text alert.
"""

import asyncio
import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import UTC, timedelta

from sqlalchemy import func, or_, select, text

from socrat_bot.db import session
from socrat_bot.models import CardDelivery, GraphJob, GraphSnapshot, Incident, utcnow

MAX_STORED_BYTES = 256 * 1024 * 1024
MAX_PENDING = 128
MAX_ATTEMPTS = 12
MAX_AGE = 600
LEASE_SECONDS = 30
_lock = threading.Lock()
log = logging.getLogger(__name__)


def limits(db):
    """Keep this non-critical queue from waiting indefinitely on database locks."""
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        db.execute(text("SET LOCAL statement_timeout = '5s'"))


def aware(value):
    """SQLite test timestamps and PostgreSQL timestamps compare in the same timezone."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def enqueue(key, payload):
    """Reserve one immutable timestamp/request; duplicate inputs never reset budgets."""
    from socrat_bot.escalation import insert_once

    serialized = json.dumps([key, payload], sort_keys=True, ensure_ascii=True)
    if len(serialized.encode()) > 30000:
        return None
    identity = hashlib.sha256(serialized.encode()).hexdigest()
    with _lock, session() as db:
        limits(db)
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(6003092151369551944)"))
        snapshot = db.get(GraphSnapshot, identity)
        if (snapshot is not None and snapshot.png is not None) or db.get(
            GraphJob, identity
        ) is not None:
            return identity
        pending = db.scalar(
            select(func.count())
            .select_from(GraphJob)
            .where(GraphJob.state == "pending")
        )
        if pending >= MAX_PENDING:
            log.warning("primary graph result: QUEUE_LIMIT")
            return None
        now = utcnow()
        insert_once(db, GraphSnapshot, {"id": identity, "observed_at": now})
        observed = aware(db.get(GraphSnapshot, identity).observed_at)
        db.add(
            GraphJob(
                id=identity,
                key=key,
                payload={**payload, "observed_at": observed.isoformat()},
                created_at=now,
                available_at=now,
            )
        )
    return identity


def claim():
    """Claim one due request with an expiring owner; recover after worker restart."""
    now = utcnow()
    with session() as db:
        limits(db)
        job = db.scalar(
            select(GraphJob)
            .where(
                GraphJob.state == "pending",
                GraphJob.available_at <= now,
                or_(GraphJob.lease_until.is_(None), GraphJob.lease_until <= now),
                or_(
                    GraphJob.generation.is_not(None),
                    GraphJob.created_at <= now - timedelta(seconds=MAX_AGE),
                ),
            )
            .order_by(GraphJob.available_at, GraphJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        incident = db.get(Incident, job.key)
        if (
            aware(job.created_at) + timedelta(seconds=MAX_AGE) <= now
            or job.attempts >= MAX_ATTEMPTS
        ):
            job.state, job.last_code = "expired", "BUDGET_EXHAUSTED"
            return None
        if (
            incident is None
            or incident.generation != job.generation
            or incident.graph_generation != job.generation
            or incident.graph_capture != job.id
            or incident.status != job.payload.get("status")
        ):
            job.state, job.last_code = "superseded", "SUPERSEDED"
            return None
        job.attempts += 1
        job.lease_owner = str(uuid.uuid4())
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return job.id, job.key, job.lease_owner, dict(job.payload)


def finish(work, result):
    """Fence a result, store a bounded PNG, then dirty current delivery intents atomically."""
    from socrat_bot.escalation import enqueue_card

    identity, key, owner, _ = work
    data, digest, retryable, code = result
    with session() as db:
        limits(db)
        # Same lock order as submit_card: incident, then graph intent, then card intent.
        incident = db.scalar(
            select(Incident).where(Incident.key == key).with_for_update()
        )
        job = db.scalar(
            select(GraphJob).where(GraphJob.id == identity).with_for_update()
        )
        now = utcnow()
        if (
            job is None
            or job.state != "pending"
            or job.lease_owner != owner
            or job.lease_until is None
            or aware(job.lease_until) <= now
        ):
            return False
        job.lease_owner = job.lease_until = None
        if (
            incident is None
            or incident.generation != job.generation
            or incident.graph_generation != job.generation
            or incident.graph_capture != identity
            or incident.status != job.payload.get("status")
        ):
            job.state, job.last_code = "superseded", "SUPERSEDED"
            return False
        if aware(job.created_at) + timedelta(seconds=MAX_AGE) <= now:
            job.state, job.last_code = "expired", "BUDGET_EXHAUSTED"
            return False
        job.last_code = code
        if data is None:
            if retryable and job.attempts < MAX_ATTEMPTS:
                job.available_at = now + timedelta(seconds=min(30, 2**job.attempts))
            else:
                job.state = "failed"
            log.info("primary graph result: %s", code)
            return False
        snapshot = db.scalar(
            select(GraphSnapshot).where(GraphSnapshot.id == identity).with_for_update()
        )
        if snapshot.png is None:
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(6003092151369551945)"))
            total = db.scalar(select(func.sum(func.length(GraphSnapshot.png)))) or 0
            if total + len(data) > MAX_STORED_BYTES:
                job.state, job.last_code = "failed", "STORAGE_LIMIT"
                return False
            snapshot.png, snapshot.render_digest = data, digest
        job.state, job.last_code = "ready", "READY"
        recipients = list(
            db.scalars(
                select(CardDelivery.chat_id).where(
                    CardDelivery.key == key, CardDelivery.generation == job.generation
                )
            )
        )
        for chat_id in recipients:
            enqueue_card(db, incident, chat_id)
        return True


async def drain_one():
    """Perform at most one bounded render attempt; never send Telegram from this worker."""
    from socrat_bot.primary_graphs import fetch_png

    work = await asyncio.to_thread(claim)
    if work is None:
        return False
    result = await fetch_png(work[3])
    await asyncio.to_thread(finish, work, result)
    return True


async def outbox_loop():
    """Serialize renders per worker and leave durable leases recoverable on cancellation."""
    while True:
        try:
            if (
                os.environ.get("ALERT_GRAPHS_ENABLED", "false") == "true"
                and await drain_one()
            ):
                continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - durable retry boundary, no upstream exception text
            log.error("primary graph worker failed")
        await asyncio.sleep(1)
