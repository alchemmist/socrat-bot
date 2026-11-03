"""remember a short note attached to a card

Revision ID: 0007_card_note
Revises: 0006_card_spi_link
Create Date: 2026-03-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_card_note"
down_revision: str | None = "0006_card_spi_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("telegram_cards", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("telegram_cards", "note")
