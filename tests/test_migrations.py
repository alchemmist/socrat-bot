"""Exercise the immutable upstream 0009 schema before adding our delivery tables."""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from socrat_bot.models import AlertOutbox, CardDelivery, Incident, TelegramCard


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
            == "0011_delivery_ownership"
        )
    engine.dispose()
