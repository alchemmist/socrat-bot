"""remember card text and an attached SPI link

Revision ID: 0006_card_spi_link
Revises: 0005_card_per_chat
Create Date: 2026-03-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_card_spi_link"
down_revision: str | None = "0005_card_per_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("telegram_cards", sa.Column("text", sa.Text(), nullable=True))
    op.add_column("telegram_cards", sa.Column("spi_url", sa.Text(), nullable=True))
    op.add_column("telegram_cards", sa.Column("spi_label", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("telegram_cards", "spi_label")
    op.drop_column("telegram_cards", "spi_url")
    op.drop_column("telegram_cards", "text")
