"""vpn flag and scheduled broadcasts

Revision ID: 0003_vpn_broadcast
Revises: 0002_telegram_cards
Create Date: 2026-03-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_vpn_broadcast"
down_revision: str | None = "0002_telegram_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("uses_vpn", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("broadcasts", sa.Column("send_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("broadcasts", "send_at")
    op.drop_column("users", "uses_vpn")
