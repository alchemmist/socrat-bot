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
    approve_alerts,
    card_messages,
    ensure_user,
    format_vpn_clients,
    issue_link_code,
    mark_vpn,
    open_card,
    mailing_hours,
    schedule_broadcast,
    vpn_audience,
)
from socrat_bot.models import Action, Audit, Broadcast, LinkCode, SudoRequest, TelegramCard, User


@pytest.fixture(autouse=True)
def fresh_schema():
    init_schema()
    with session() as db:
        db.execute(delete(SudoRequest))
        db.execute(delete(Broadcast))
        db.execute(delete(Audit))
        db.execute(delete(Action))
        db.execute(delete(LinkCode))
        db.execute(delete(TelegramCard))
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
        people = vpn_audience(db)
        audience = {user.telegram_id for user in people}
        listing = format_vpn_clients(people)
    assert audience == {1, 3}
    assert listing == "VPN clients: 2\n\u2022 1\n\u2022 3"


def test_vpn_client_list_prefers_username():
    shown = format_vpn_clients(
        [
            User(telegram_id=3, role="vpn", uses_vpn=True, alerts_enabled=False, username="ada"),
            User(telegram_id=1, role="vpn", uses_vpn=True, alerts_enabled=False),
        ]
    )
    assert shown == "VPN clients: 2\n\u2022 1\n\u2022 @ada"


def test_open_card_keeps_the_message_until_telegram_accepts():
    with session() as db:
        row = open_card(db, "Node/pod/ns/name", 1, True)
        row.message_id = 10
        row.status = "resolved"
        db.flush()
        again = open_card(db, "Node/pod/ns/name", 1, True)
        other = open_card(db, "Node/pod/ns/name", 2, True)
        assert again.message_id == 10
        assert again.status == "resolved"
        assert other.message_id is None
        assert other.chat_id == 2
        other.message_id = 11
        db.flush()
        assert card_messages(db, "Node/pod/ns/name") == [(1, 10), (2, 11)]


def test_approve_alerts_keeps_the_role():
    with session() as db:
        ensure_user(db, 891074228)
        guest = ensure_user(db, 42)
        approved = approve_alerts(db, 42, "ada")
        assert guest.role == "user"
        assert approved.alerts_enabled is True
        assert approved.username == "ada"


def test_mark_vpn_does_not_demote_the_owner():
    with session() as db:
        ensure_user(db, 891074228)
        owner = mark_vpn(db, 891074228)
        guest = mark_vpn(db, 42)
        assert owner.role == "owner" and owner.uses_vpn is True
        assert guest.role == "vpn" and guest.uses_vpn is True


def test_mailing_hours_skip_the_past_on_the_same_day():
    from datetime import date

    from socrat_bot.logic import broadcast_zone

    morning = datetime(2026, 4, 2, 7, 30, tzinfo=broadcast_zone())
    assert mailing_hours(date(2026, 4, 2), morning)[0] == 8
    assert mailing_hours(date(2026, 4, 2), morning)[-1] == 22
    afternoon = datetime(2026, 4, 2, 15, 10, tzinfo=broadcast_zone())
    assert mailing_hours(date(2026, 4, 2), afternoon) == list(range(16, 23))
    assert mailing_hours(date(2026, 4, 3), afternoon) == list(range(8, 23))
    late = datetime(2026, 4, 2, 22, 5, tzinfo=broadcast_zone())
    assert mailing_hours(date(2026, 4, 2), late) == []


def test_scheduled_broadcast_is_claimed_when_due():
    send_at = datetime(2026, 4, 2, 6, tzinfo=timezone.utc)
    with session() as db:
        row = schedule_broadcast(db, 10, send_at, "Maintenance tonight")
        early = claim_due_broadcasts(db, now=datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert early == []
        due = claim_due_broadcasts(db, now=datetime(2026, 4, 2, 12, tzinfo=timezone.utc))
        assert [item.id for item in due] == [row.id]
        assert row.status == "sending"
