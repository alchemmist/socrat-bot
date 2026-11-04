"""Independent incidents and durable escalation delivery."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0010_escalation"
down_revision = "0009_spi_desired_revision"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("admin_level", sa.Integer(), nullable=False, server_default="20"),
    )
    op.add_column(
        "users",
        sa.Column("receive_level", sa.Integer(), nullable=False, server_default="20"),
    )
    op.execute("UPDATE users SET admin_level = 10 WHERE role = 'owner'")
    json_type = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "incidents",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("history", json_type, nullable=False),
    )
    op.create_table(
        "alert_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_alert_outbox_due", "alert_outbox", ["done", "available_at"])
    op.execute(
        "INSERT INTO incidents (key, generation, level, status, history) "
        "SELECT key, MAX(generation), 20, "
        "CASE WHEN key LIKE '%/spi/%' AND SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) > 0 THEN 'resolved' "
        "WHEN SUM(CASE WHEN status = 'firing' THEN 1 ELSE 0 END) > 0 "
        "THEN 'firing' ELSE 'resolved' END, '[]' FROM telegram_cards GROUP BY key"
    )


def downgrade():
    op.drop_table("alert_outbox")
    op.drop_table("incidents")
    op.drop_column("users", "receive_level")
    op.drop_column("users", "admin_level")
