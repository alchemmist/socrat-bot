import asyncio
import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test.db"

import pytest
from sqlalchemy import delete, select

from socrat_bot.db import init_schema, session
from socrat_bot.logic import (
    apply_action,
    consume_link,
    create_action,
    issue_link_code,
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


def test_vpn_audience_excludes_users_without_panel_client_id():
    with session() as db:
        db.add(User(telegram_id=1, role="vpn", panel_client_id="panel-a", alerts_enabled=False))
        db.add(User(telegram_id=2, role="vpn", panel_client_id=None, alerts_enabled=False))
        db.add(User(telegram_id=3, role="owner", panel_client_id="panel-b", alerts_enabled=True))
        db.flush()
        audience = {user.telegram_id for user in vpn_audience(db)}
    assert audience == {1}
