"""Independent interests, approval boundary, old callbacks and populated migration."""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from telegram.error import BadRequest

from socrat_bot import app
from socrat_bot import db as database
from socrat_bot.logic import alert_recipients, approve_alerts, ensure_user, vpn_audience
from socrat_bot.models import Base, User, UserInterest


@pytest.fixture(params=["sqlite", "postgres"])
def interests_store(request, monkeypatch):
    url = "sqlite://"
    if request.param == "postgres":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("requires disposable PostgreSQL")
        assert "test" in url
    engine = create_engine(
        url,
        **(
            {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
            if url == "sqlite://"
            else {}
        ),
    )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(
        database, "_Session", sessionmaker(engine, expire_on_commit=False)
    )
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    with database.session() as db:
        ensure_user(db, 1)
        ensure_user(db, 2)
    yield request.param
    Base.metadata.drop_all(engine)
    engine.dispose()


def query(data, actor=2, chat=None, bot=None):
    return NS(
        data=data,
        from_user=NS(id=actor, username="test"),
        message=NS(chat_id=actor if chat is None else chat, reply_text=AsyncMock()),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        get_bot=lambda: bot or NS(send_message=AsyncMock()),
    )


def click(q):
    asyncio.run(app.on_callback(NS(callback_query=q), NS()))


def test_independent_flags_and_private_only(interests_store):
    click(query("interest:vpn:on"))
    q = query("interest:alerts:on")
    click(q)
    assert "awaiting owner approval" in q.edit_message_text.call_args.args[0]
    with database.session() as db:
        user = ensure_user(db, 2)
        assert user.interested_in_vpn and user.interested_in_alerts
        assert not user.alerts_active and not user.alerts_approved
        assert user.role == "user"
        assert [u.telegram_id for u in vpn_audience(db)] == [2]
        assert [u.telegram_id for u in alert_recipients(db)] == [1]
        user.panel_client_id = "keep-panel"
    click(query("interest:vpn:off", chat=-20))
    click(query("interest:unknown:on"))
    with database.session() as db:
        assert ensure_user(db, 2).interested_in_vpn
        approve_alerts(db, 2)
    click(query("interest:vpn:off"))
    with database.session() as db:
        user = ensure_user(db, 2)
        assert not user.interested_in_vpn and user.alerts_active
        assert user.panel_client_id == "keep-panel"
        assert [u.telegram_id for u in vpn_audience(db)] == []
    click(query("interest:alerts:off"))
    with database.session() as db:
        user = ensure_user(db, 2)
        assert not user.alerts_active and user.alerts_approved
    bot = NS(send_message=AsyncMock())
    click(query("interest:alerts:on", bot=bot))
    assert bot.send_message.await_count == 0
    with database.session() as db:
        user = ensure_user(db, 2)
        assert user.alerts_active and not user.interested_in_vpn
        assert len(user.interests) == 2


def test_pending_opt_out_then_old_approval_never_subscribes(interests_store):
    click(query("interest:alerts:on"))
    click(query("interest:alerts:off"))
    click(query("al:ok:2", actor=1))
    with database.session() as db:
        user = ensure_user(db, 2)
        assert (
            user.alerts_approved
            and not user.interested_in_alerts
            and not user.alerts_active
        )
    click(query("interest:alerts:on"))
    with database.session() as db:
        assert ensure_user(db, 2).alerts_active


def test_callback_retry_is_not_a_toggle_and_does_not_duplicate_requests(
    interests_store,
):
    bot = NS(send_message=AsyncMock())
    q = query("interest:alerts:on", bot=bot)
    click(q)
    q.edit_message_text.side_effect = BadRequest("Message is not modified")
    click(q)
    assert bot.send_message.await_count == 1
    with database.session() as db:
        user = ensure_user(db, 2)
        assert user.interested_in_alerts and not user.alerts_active
        assert len([r for r in user.interests if r.topic == "alerts"]) == 1
    q = query("interest:vpn:on")
    click(q)
    q.edit_message_text.side_effect = BadRequest("Message is not modified")
    click(q)
    with database.session() as db:
        assert ensure_user(db, 2).interested_in_vpn


def test_start_keeps_flags_and_renders_all_registered_topics(
    interests_store, monkeypatch
):
    from socrat_bot import interests

    monkeypatch.setattr(
        interests,
        "INTERESTS",
        (*interests.INTERESTS, interests.Interest("future", "Future feature")),
    )
    with database.session() as db:
        user = ensure_user(db, 2)
        user.interested_in_vpn = True
        user.interested_in_alerts = True
        user.set_interest("future", True)
    msg = NS(reply_text=AsyncMock())
    update = NS(
        effective_user=NS(id=2, username=None), effective_chat=NS(id=2), message=msg
    )
    asyncio.run(app.on_start(update, NS(args=[])))
    body = msg.reply_text.call_args.args[0]
    assert "Welcome to Socrat Bot!" in body and "I'm interested in" in body
    assert "awaiting owner approval" in body
    keyboard = msg.reply_text.call_args.kwargs["reply_markup"]
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
        "interest:vpn:off",
        "interest:alerts:off",
        "interest:future:off",
    ]
    with database.session() as db:
        assert not ensure_user(db, 2).alerts_approved


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_populated_interests_upgrade_and_legacy_bridge(tmp_path, backend):
    url = f"sqlite+pysqlite:///{tmp_path / 'interest_test.db'}"
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
    cmd = [sys.executable, "-m", "alembic"]

    def migrate(action, target):
        subprocess.run(cmd + [action, target], env=env, check=True, capture_output=True)

    migrate("upgrade", "0019_graph_source")
    engine = create_engine(url)
    rows = [
        (1, "owner", False, False, True),
        (2, "vpn", True, True, True),
        (3, "vpn", False, False, True),
        (4, "user", False, False, False),
    ]
    ids = {tid: uuid.uuid4().hex for tid, *_ in rows}
    with engine.begin() as conn:
        for tid, role, vpn, alerts, approved in rows:
            conn.execute(
                text(
                    "INSERT INTO users (id,telegram_id,role,panel_client_id,uses_vpn,alerts_enabled,alerts_approved,admin_level,receive_level,created_at) VALUES (:id,:tid,:role,'panel',:vpn,:alerts,:approved,20,20,CURRENT_TIMESTAMP)"
                ),
                {
                    "id": ids[tid],
                    "tid": tid,
                    "role": role,
                    "vpn": vpn,
                    "alerts": alerts,
                    "approved": approved,
                },
            )
    migrate("upgrade", "0020_user_interests")
    with sessionmaker(engine)() as db:
        users = list(db.scalars(select(User).order_by(User.telegram_id)))
        assert [
            (
                u.telegram_id,
                u.role,
                u.interested_in_vpn,
                u.alerts_active,
                u.alerts_approved,
            )
            for u in users
        ] == [
            (1, "owner", False, False, True),
            (2, "user", True, True, True),
            (3, "user", False, False, True),
            (4, "user", False, False, False),
        ]
        assert all(u.panel_client_id == "panel" for u in users)
        assert len(list(db.scalars(select(UserInterest)))) == 8
    if backend == "postgres":
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET uses_vpn=true,alerts_enabled=true,alerts_approved=true WHERE telegram_id=4"
                )
            )
        with sessionmaker(engine)() as db:
            u = db.scalar(select(User).where(User.telegram_id == 4))
            assert u.interested_in_vpn and u.alerts_active
            u.interested_in_alerts = False
            db.commit()
        with engine.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT alerts_enabled FROM users WHERE telegram_id=4")
                )
                is False
            )
        with sessionmaker(engine)() as db:
            u = db.scalar(select(User).where(User.telegram_id == 3))
            u.interested_in_alerts = True
            db.commit()
        with engine.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT alerts_enabled FROM users WHERE telegram_id=3")
                )
                is True
            )
        # An unapproved user may select interest, but OLD code must not deliver.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET alerts_enabled=false,alerts_approved=false WHERE telegram_id=4"
                )
            )
        with sessionmaker(engine)() as db:
            u = db.scalar(select(User).where(User.telegram_id == 4))
            u.interested_in_alerts = True
            db.commit()
        with engine.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT alerts_enabled FROM users WHERE telegram_id=4")
                )
                is False
            )
    migrate("downgrade", "0019_graph_source")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT uses_vpn FROM users WHERE telegram_id=2"))
        assert conn.scalar(text("SELECT alerts_enabled FROM users WHERE telegram_id=2"))
    migrate("upgrade", "head")
    assert not {"uses_vpn", "alerts_enabled"} & {
        c["name"] for c in inspect(engine).get_columns("users")
    }
    with sessionmaker(engine)() as db:
        u = db.scalar(select(User).where(User.telegram_id == 2))
        u.set_interest("future-capability", True)
        u.interested_in_alerts = False
        db.commit()
        assert u.interested_in_vpn and u.alerts_approved and not u.alerts_active
    # Contract downgrade/re-upgrade restores rollback compatibility without
    # losing independent, unknown future topic flags or silently resubscribing.
    migrate("downgrade", "0020_user_interests")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT uses_vpn FROM users WHERE telegram_id=2"))
        assert not conn.scalar(
            text("SELECT alerts_enabled FROM users WHERE telegram_id=2")
        )
    migrate("upgrade", "head")
    with sessionmaker(engine)() as db:
        u = db.scalar(select(User).where(User.telegram_id == 2))
        assert u.interested_in("future-capability")
        assert u.interested_in_vpn and u.alerts_approved and not u.alerts_active
    engine.dispose()
