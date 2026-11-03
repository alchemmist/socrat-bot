import asyncio
import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test.db"

import pytest
from sqlalchemy import delete, select

from socrat_bot.db import init_schema, session
from datetime import datetime, timezone

from socrat_bot.logic import (
    apply_action,
    claim_due_broadcasts,
    consume_link,
    create_action,
    ensure_user,
    issue_link_code,
    mark_vpn,
    parse_broadcast_command,
    schedule_broadcast,
    vpn_audience,
)
from socrat_bot.models import Action, Audit, Broadcast, LinkCode, SudoRequest, User


@pytest.fixture(autouse=True)
def fresh_schema():
    init_schema()
    with session() as db:
        db.execute(delete(SudoRequest))
        db.execute(delete(Broadcast))
        db.execute(delete(Audit))
        db.execute(delete(Action))
        db.execute(delete(LinkCode))
        db.execute(delete(User))
    yield


def test_create_action_is_idempotent():
    with session() as db:
        first = create_action(
            db,
            type_="alert.silence",
            payload={"alertname": "Disk"},
            chat_id=10,
            idempotency_key="silence-disk",
        )
        second = create_action(
            db,
            type_="alert.silence",
            payload={"alertname": "Other"},
            chat_id=11,
            idempotency_key="silence-disk",
        )
        rows = list(db.scalars(select(Action)))
    assert first.id == second.id
    assert second.payload == {"alertname": "Disk"}
    assert len(rows) == 1


def test_apply_action_replay_does_not_dispatch_twice(monkeypatch):
    calls = 0

    async def fake_dispatch(db, action):
        nonlocal calls
        calls += 1
        return {"status": "silenced"}

    monkeypatch.setattr("socrat_bot.logic._dispatch", fake_dispatch)

    async def scenario():
        with session() as db:
            action = create_action(
                db,
                type_="alert.silence",
                payload={},
                chat_id=10,
                idempotency_key="silence-once",
            )
            first = await apply_action(db, action, "owner")
            second = await apply_action(db, action, "owner")
        return first, second

    first, second = asyncio.run(scenario())
    assert calls == 1
    assert first["status"] == "silenced"
    assert second["replayed"] is True
    assert second["status"] == "silenced"


def test_link_code_is_single_use():
    with session() as db:
        code = issue_link_code(db, "panel-client-1")
        assert consume_link(db, 501, code) == "linked"
        linked = db.scalar(select(User).where(User.telegram_id == 501))
        assert linked is not None
        assert linked.panel_client_id == "panel-client-1"
        assert consume_link(db, 502, code) == "invalid"
        assert db.scalar(select(User).where(User.telegram_id == 502)) is None


def test_vpn_audience_is_the_opt_in_list():
    with session() as db:
        db.add(User(telegram_id=1, role="vpn", panel_client_id="panel-a", uses_vpn=True, alerts_enabled=False))
        db.add(User(telegram_id=2, role="vpn", panel_client_id=None, uses_vpn=False, alerts_enabled=False))
        db.add(User(telegram_id=3, role="owner", panel_client_id="panel-b", uses_vpn=True, alerts_enabled=True))
        db.flush()
        audience = {user.telegram_id for user in vpn_audience(db)}
    assert audience == {1, 3}


def test_mark_vpn_does_not_demote_the_owner():
    with session() as db:
        ensure_user(db, 891074228)
        owner = mark_vpn(db, 891074228)
        guest = mark_vpn(db, 42)
        assert owner.role == "owner" and owner.uses_vpn is True
        assert guest.role == "vpn" and guest.uses_vpn is True


def test_scheduled_broadcast_is_claimed_when_due():
    parsed = parse_broadcast_command("2026-04-02 09:00 Maintenance tonight")
    assert parsed is not None
    send_at, text = parsed
    assert text == "Maintenance tonight"
    assert send_at.tzinfo is not None
    with session() as db:
        row = schedule_broadcast(db, 10, send_at, text)
        early = claim_due_broadcasts(db, now=datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert early == []
        due = claim_due_broadcasts(db, now=datetime(2026, 4, 2, 12, tzinfo=timezone.utc))
        assert [item.id for item in due] == [row.id]
        assert row.status == "sending"
    assert parse_broadcast_command("tomorrow hello") is None
