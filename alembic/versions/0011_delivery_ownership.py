"""Durable initial audience, content revisions and shared recipient ownership."""

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
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "INSERT INTO card_deliveries (key, chat_id, generation, message_generation, revision, delivered_revision, text, spi_url, spi_label, note, buttons) SELECT key, chat_id, generation, generation, 1, 1, text, spi_url, spi_label, note, '[]' FROM telegram_cards"
    )


def downgrade():
    op.drop_table("card_deliveries")
