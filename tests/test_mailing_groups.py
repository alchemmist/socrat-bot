"""Group-first durable mailing, narrow audiences, old VPN defaults and restart safety."""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select, text
from telegram import MessageEntity
from test_interests import interests_store as interests_store  # noqa: PLC0414

from socrat_bot import app
from socrat_bot import db as database
from socrat_bot.logic import ensure_user, mailing_audience
from socrat_bot.mailing_compose import group_callback, receive_text
from socrat_bot.models import Base, Broadcast


def message(body, actor=1, entities=None):
    return NS(
        chat_id=actor,
        text=body,
        entities=entities or [],
        reply_text=AsyncMock(return_value=NS(message_id=99)),
    )


def update(msg, actor=1):
    return NS(message=msg, effective_user=NS(id=actor))


def callback(identity, group, actor=1, chat=None):
    msg = message("", actor if chat is None else chat)
    return NS(
        data=f"mg:{identity}:{group}",
        from_user=NS(id=actor),
        message=msg,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def draft(body="/mailing"):
    msg = message(body)
    asyncio.run(app.on_mailing(update(msg), NS()))
    with database.session() as db:
        identity = db.scalar(
            select(Broadcast.id).where(Broadcast.status == "selecting_group")
        )
    return identity, msg


def seed_groups():
    with database.session() as db:
        for identity, groups in [
            (2, ["vpn"]),
            (3, ["llm"]),
            (4, ["alerts"]),
            (5, ["alerts"]),
            (6, ["vpn", "alerts", "llm"]),
        ]:
            user = ensure_user(db, identity)
            for group in groups:
                user.set_interest(group, True)
            user.alerts_approved = identity in (4, 6)


@pytest.mark.parametrize("topic", ["llm", "cluster"])
def test_interest_and_disjoint_audiences(interests_store, topic):
    seed_groups()
    with database.session() as db:
        assert [u.telegram_id for u in mailing_audience(db, "vpn")] == [2, 6]
        assert [u.telegram_id for u in mailing_audience(db, "llm")] == [3, 6]
        assert [u.telegram_id for u in mailing_audience(db, "alerts")] == [1, 4, 6]
        with pytest.raises(ValueError):
            mailing_audience(db, None)
        with pytest.raises(ValueError):
            mailing_audience(db, "typo")
    from test_interests import click, query

    click(query(f"interest:{topic}:on"))
    with database.session() as db:
        user = ensure_user(db, 2)
        assert user in mailing_audience(db, topic)
        if topic == "cluster":
            assert [u.telegram_id for u in mailing_audience(db, topic)] == [2]
        assert user.interested_in(topic) and user.interested_in_vpn
        assert not user.alerts_active
    click(query(f"interest:{topic}:off"))
    with database.session() as db:
        assert ensure_user(db, 2) not in mailing_audience(db, topic)


def test_group_then_native_text_now_and_no_retarget_or_duplicate(
    interests_store, monkeypatch
):
    seed_groups()
    identity, msg = draft()
    labels = [
        row[0].text
        for row in msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
    ]
    assert labels == ["VPN", "Alerts", "LLM", "Cluster", "Cancel"]
    q = callback(identity, "llm")
    asyncio.run(group_callback(q, NS()))
    with database.session() as db:
        row = db.get(Broadcast, identity)
        assert row.status == "awaiting_text" and row.audience_group == "llm"
    # Same/stale selector cannot switch LLM to VPN even before text is entered.
    asyncio.run(group_callback(callback(identity, "vpn"), NS()))
    body = "😀 hello\nsecret link  "
    entities = [MessageEntity("text_link", 9, 11, url="https://example.org/hidden")]
    msg = message(body, entities=entities)
    # No transient context/user_data survives between these independent calls.
    asyncio.run(receive_text(update(msg), NS()))
    assert msg.reply_text.call_args_list[0].args == (body,)
    assert msg.reply_text.call_args_list[0].kwargs["parse_mode"] is None
    assert (
        msg.reply_text.call_args_list[0].kwargs["entities"][0].url
        == "https://example.org/hidden"
    )
    with database.session() as db:
        row = db.get(Broadcast, identity)
        assert (
            row.status == "group_choosing"
            and row.text == body
            and row.audience_group == "llm"
        )
        assert row.entities[0]["offset"] == 9
    asyncio.run(receive_text(update(message("duplicate body")), NS()))
    delivered = AsyncMock(return_value={"sent": 2, "failed": []})
    monkeypatch.setattr(app, "deliver_broadcast", delivered)
    q.data = f"m:n:{identity}"
    asyncio.run(app._on_mailing_callback(q, q.data))
    delivered.assert_awaited_once_with(body, [3, 6], identity)
    asyncio.run(app._on_mailing_callback(q, q.data))
    assert delivered.await_count == 1
    assert "Group: LLM" in q.edit_message_text.call_args.args[0]


def test_composition_permissions_cancel_and_restart(interests_store):
    identity, _ = draft()
    for q in [
        callback(identity, "llm", actor=2),
        callback(identity, "llm", chat=-10),
        callback(identity, "invalid"),
    ]:
        asyncio.run(group_callback(q, NS()))
        q.edit_message_text.assert_not_awaited()
    asyncio.run(receive_text(update(message("not yet")), NS()))
    with database.session() as db:
        assert db.get(Broadcast, identity).status == "selecting_group"
    asyncio.run(group_callback(callback(identity, "llm"), NS()))
    asyncio.run(receive_text(update(message("other owner", actor=2), actor=2), NS()))
    replacement, _ = draft()
    with database.session() as db:
        assert db.get(Broadcast, identity).status == "cancelled"
        assert db.get(Broadcast, replacement).status == "selecting_group"
    asyncio.run(group_callback(callback(replacement, "cancel"), NS()))
    asyncio.run(receive_text(update(message("must not be captured")), NS()))
    with database.session() as db:
        assert db.get(Broadcast, replacement).status == "cancelled"
    for actor, chat in [(2, 2), (1, -50)]:
        msg = message("/mailing", actor=chat)
        asyncio.run(app.on_mailing(update(msg, actor=actor), NS()))
        assert "only" in msg.reply_text.call_args.args[0]


def test_scheduled_mailing_uses_saved_group_and_current_membership(
    interests_store, monkeypatch
):
    seed_groups()
    identity, _ = draft("/mailing scheduled message")
    asyncio.run(group_callback(callback(identity, "llm"), NS()))
    with database.session() as db:
        row = db.get(Broadcast, identity)
        row.status = "group_scheduled"
        row.send_at = datetime.now(UTC) - timedelta(minutes=1)
        ensure_user(db, 3).set_interest("llm", False)
        ensure_user(db, 7).set_interest("llm", True)
    delivered = AsyncMock(return_value={"sent": 2, "failed": []})
    monkeypatch.setattr(app, "deliver_broadcast", delivered)
    original_sleep = asyncio.sleep
    ticks = 0

    async def sleep(delay):
        nonlocal ticks
        if delay == 15:
            ticks += 1
            if ticks > 1:
                raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app._broadcast_loop())
    delivered.assert_awaited_once_with("scheduled message", [6, 7], identity)
    with database.session() as db:
        assert db.get(Broadcast, identity).audience_group == "llm"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_populated_group_migration_keeps_every_legacy_mailing_vpn(tmp_path, backend):
    url = f"sqlite+pysqlite:///{tmp_path / 'groups_test.db'}"
    if backend == "postgres":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("requires disposable PostgreSQL")
        assert "test" in url
        cleanup = create_engine(url)
        Base.metadata.drop_all(cleanup)
        with cleanup.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        cleanup.dispose()
    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=str(Path("src").resolve()))
    command = [sys.executable, "-m", "alembic"]

    def migrate(action, target):
        subprocess.run(
            command + [action, target], env=env, check=True, capture_output=True
        )

    migrate("upgrade", "0021_interest_contract")
    engine = create_engine(url)
    ids = [uuid.uuid4().hex for _ in range(3)]
    with engine.begin() as conn:
        for identity, status in zip(ids, ["sent", "scheduled", "choosing"]):
            conn.execute(
                text(
                    "INSERT INTO broadcasts (id,text,status,sent,failed,entities,chat_id) VALUES (:id,'legacy',:status,0,'[]','[]',1)"
                ),
                {"id": identity, "status": status},
            )
    migrate("upgrade", "head")
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT status,audience_group,text FROM broadcasts ORDER BY status")
        ).all() == [
            ("choosing", "vpn", "legacy"),
            ("scheduled", "vpn", "legacy"),
            ("sent", "vpn", "legacy"),
        ]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE broadcasts SET audience_group='llm' WHERE status='scheduled'")
        )
    with pytest.raises(subprocess.CalledProcessError):
        migrate("downgrade", "0021_interest_contract")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE broadcasts SET status='cancelled' WHERE audience_group='llm'")
        )
    migrate("downgrade", "0021_interest_contract")
    migrate("upgrade", "head")
    engine.dispose()


def test_group_statuses_are_invisible_to_legacy_vpn_only_workers(interests_store):
    identity, _ = draft("/mailing never retarget this")
    asyncio.run(group_callback(callback(identity, "llm"), NS()))
    with database.session() as db:
        row = db.get(Broadcast, identity)
        assert row.status == "group_choosing"
        # Pre-group callback handlers admitted only 'choosing'.
        assert (
            db.scalar(
                select(Broadcast.id).where(
                    Broadcast.id == identity, Broadcast.status == "choosing"
                )
            )
            is None
        )
        row.status = "group_scheduled"
        row.send_at = datetime.now(UTC) - timedelta(minutes=1)
    with database.session() as db:
        # Pre-group ticker claimed only 'scheduled'. New ticker supports both.
        assert (
            db.scalar(
                select(Broadcast.id).where(
                    Broadcast.id == identity, Broadcast.status == "scheduled"
                )
            )
            is None
        )
        from socrat_bot.logic import claim_due_broadcasts

        due = claim_due_broadcasts(db)
        assert [row.id for row in due] == [identity]
