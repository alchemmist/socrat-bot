"""create control-plane tables

Revision ID: 0001_init
Revises:
Create Date: 2026-03-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
uuid_type = sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("panel_client_id", sa.String(length=128), nullable=True),
        sa.Column("alerts_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)
    op.create_table(
        "link_codes",
        sa.Column("code", sa.String(length=64), primary_key=True),
        sa.Column("panel_client_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "actions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", json_type, nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_actions_idempotency_key"),
    )
    op.create_table(
        "audit",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("event", sa.String(length=128), nullable=False),
        sa.Column("detail", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "broadcasts",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sent", sa.Integer(), nullable=False),
        sa.Column("failed", json_type, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "sudo_requests",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("user_name", sa.String(length=128), nullable=False),
        sa.Column("node", sa.String(length=128), nullable=False),
        sa.Column("argv", json_type, nullable=False),
        sa.Column("argv_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_id", uuid_type, sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sudo_requests")
    op.drop_table("broadcasts")
    op.drop_table("audit")
    op.drop_table("actions")
    op.drop_table("link_codes")
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")
