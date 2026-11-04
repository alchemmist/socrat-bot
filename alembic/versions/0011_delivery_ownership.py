"""Durable initial audience, content revisions and shared recipient ownership."""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0011_delivery_ownership"
down_revision = "0010_escalation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "card_deliveries",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("message_generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("delivered_revision", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("spi_url", sa.Text(), nullable=True),
        sa.Column("spi_label", sa.String(32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "buttons", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False
        ),
        sa.Column("link_revision", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "INSERT INTO card_deliveries (key, chat_id, generation, message_generation, revision, delivered_revision, text, spi_url, spi_label, note, buttons, link_revision) SELECT key, chat_id, generation, generation, 1, 1, text, CASE WHEN desired_link_revision >= link_revision THEN COALESCE(desired_spi_url, spi_url) ELSE spi_url END, spi_label, note, '[]', CASE WHEN desired_link_revision > link_revision THEN desired_link_revision ELSE link_revision END FROM telegram_cards"
    )

    # A desired fence may already be newer than applied state when upgrading from
    # upstream 0009. Recover that accepted work rather than waiting for another caller.
    connection = op.get_bind()
    cards = sa.table(
        "telegram_cards",
        sa.column("key"),
        sa.column("chat_id"),
        sa.column("generation"),
        sa.column("message_id"),
        sa.column("spi_url"),
        sa.column("link_revision"),
        sa.column("desired_spi_url"),
        sa.column("desired_link_revision"),
    )
    outbox = sa.table(
        "alert_outbox",
        sa.column("id", sa.Uuid()),
        sa.column("payload", sa.JSON().with_variant(JSONB, "postgresql")),
        sa.column("done", sa.Boolean()),
        sa.column("attempts", sa.Integer()),
        sa.column("available_at", sa.DateTime(timezone=True)),
    )
    for row in connection.execute(sa.select(cards)).mappings():
        if (
            row["message_id"]
            and row["desired_spi_url"]
            and (
                row["desired_link_revision"] > row["link_revision"]
                or row["desired_spi_url"] != row["spi_url"]
            )
        ):
            connection.execute(
                sa.insert(outbox).values(
                    id=uuid.uuid4(),
                    payload={
                        "key": row["key"],
                        "chat_id": row["chat_id"],
                        "generation": row["generation"],
                        "kind": "spi",
                        "message_id": row["message_id"],
                        "revision": 1,
                    },
                    done=False,
                    attempts=0,
                    available_at=datetime.now(UTC),
                )
            )


def downgrade():
    op.drop_table("card_deliveries")
