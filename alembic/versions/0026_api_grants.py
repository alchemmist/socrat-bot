"""Individually revocable API grants (no plaintext bearer storage)."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0026_api_grants"
down_revision = "0025_pressed_operation"
branch_labels = depends_on = None


def upgrade():
    j = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "api_grants",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("scopes", j, nullable=False),
        sa.Column("chats", j, nullable=False),
        sa.Column("expires", sa.BigInteger(), nullable=False),
    )


def downgrade():
    op.drop_table("api_grants")
