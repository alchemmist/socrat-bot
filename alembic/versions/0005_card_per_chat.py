"""one alert card per chat

Revision ID: 0005_card_per_chat
Revises: 0004_user_username
Create Date: 2026-03-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_card_per_chat"
down_revision: str | None = "0004_user_username"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_cards_next",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("key", "chat_id", name="telegram_cards_pkey"),
    )
    op.execute(
        "INSERT INTO telegram_cards_next (key, chat_id, message_id, generation, status) "
        "SELECT key, chat_id, message_id, generation, status FROM telegram_cards"
    )
    op.drop_table("telegram_cards")
    op.rename_table("telegram_cards_next", "telegram_cards")


def downgrade() -> None:
    op.create_table(
        "telegram_cards_prev",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("key", name="telegram_cards_pkey"),
    )
    op.execute(
        "INSERT INTO telegram_cards_prev (key, chat_id, message_id, generation, status) "
        "SELECT key, chat_id, message_id, generation, status FROM telegram_cards"
    )
    op.drop_table("telegram_cards")
    op.rename_table("telegram_cards_prev", "telegram_cards")
