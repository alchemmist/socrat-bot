"""store telegram usernames

Revision ID: 0004_user_username
Revises: 0003_vpn_broadcast
Create Date: 2026-03-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_user_username"
down_revision: str | None = "0003_vpn_broadcast"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "username")
