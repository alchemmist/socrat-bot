"""Exercise the immutable upstream 0009 schema before adding our delivery tables."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from socrat_bot.models import AlertOutbox, Base, CardDelivery, Incident, TelegramCard


def test_sqlite_populated_upstream_desired_fence_upgrade_and_downgrade(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'migration_test.db'}"
    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=str(Path("src").resolve()))
    command = [sys.executable, "-m", "alembic"]

    def migrate(*args):
        subprocess.run(command + list(args), env=env, capture_output=True, check=True)

    migrate("upgrade", "0009_spi_desired_revision")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO telegram_cards (key,chat_id,message_id,generation,status,text,spi_url,spi_label,link_revision,desired_link_revision,desired_spi_url) VALUES ('pending-spi',1,10,1,'resolved','Resolved card','https://example.org/old','SPI',1,2,'https://example.org/new')"
            )
        )
        for chat, status in [(3, "firing"), (4, "resolved")]:
            connection.execute(
                text(
                    "INSERT INTO telegram_cards (key,chat_id,message_id,generation,status,text) VALUES (:key,:chat,:chat,1,:status,'Episode')"
                ),
                {"key": "mixed/spi/" + "a" * 64, "chat": chat, "status": status},
            )
    migrate("upgrade", "head")
    with Session(engine) as db:
        assert db.get(Incident, "mixed/spi/" + "a" * 64).status == "resolved"
        card = db.get(TelegramCard, ("pending-spi", 1))
        state = db.get(CardDelivery, ("pending-spi", 1))
        assert card.link_revision == 1 and card.desired_link_revision == 2
        assert card.spi_url == "https://example.org/old"
        assert state.spi_url == card.desired_spi_url == "https://example.org/new"
        assert state.link_revision == 2
        work = db.scalar(select(AlertOutbox))
        assert work.payload["kind"] == "spi" and not work.done
    migrate("downgrade", "0007_card_note")
    migrate("upgrade", "head")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0013_broadcast_copies"
        )
    engine.dispose()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_persistent_approval_backfills_current_and_historical_subscribers(tmp_path, backend):
    import uuid

    url = f"sqlite+pysqlite:///{tmp_path / 'approval_migration_test.db'}"
    if backend == "postgres":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set TEST_POSTGRES_URL to a disposable PostgreSQL database")
        assert "test" in url
        cleanup = create_engine(url)
        Base.metadata.drop_all(cleanup)
        with cleanup.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        cleanup.dispose()
    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=str(Path("src").resolve()))
    command = [sys.executable, "-m", "alembic"]
    subprocess.run(command + ["upgrade", "0011_delivery_ownership"], env=env, capture_output=True, check=True)
    engine = create_engine(url)
    with engine.begin() as connection:
        for telegram_id, role, enabled in [(1, "owner", False), (2, "vpn", True), (3, "vpn", False), (4, "vpn", False)]:
            connection.execute(text(
                "INSERT INTO users (id,telegram_id,role,uses_vpn,alerts_enabled,admin_level,receive_level,created_at) "
                "VALUES (:id,:tid,:role,true,:enabled,20,20,CURRENT_TIMESTAMP)"
            ), {"id": uuid.uuid4().hex, "tid": telegram_id, "role": role, "enabled": enabled})
        connection.execute(text(
            "INSERT INTO audit (id,actor,event,detail,created_at) VALUES (:id,'3','alerts.approve','{}',CURRENT_TIMESTAMP)"
        ), {"id": uuid.uuid4().hex})
    subprocess.run(command + ["upgrade", "head"], env=env, capture_output=True, check=True)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT telegram_id, alerts_enabled, alerts_approved FROM users ORDER BY telegram_id")).all()
        assert [(tid, bool(enabled), bool(approved)) for tid, enabled, approved in rows] == [
            (1, False, True), (2, True, True), (3, False, True), (4, False, False)
        ]
    subprocess.run(command + ["downgrade", "0011_delivery_ownership"], env=env, capture_output=True, check=True)
    subprocess.run(command + ["upgrade", "head"], env=env, capture_output=True, check=True)
    engine.dispose()
