"""Persist only confirmed original Telegram copies and edit ownership."""
import sqlalchemy as sa

from alembic import op

revision = "0013_broadcast_copies"
down_revision = "0012_alerts_approval"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("broadcasts", sa.Column("edit_token", sa.String(36), nullable=True))
    op.add_column("broadcasts", sa.Column("edit_until", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "broadcast_copies",
        sa.Column("broadcast_id", sa.Uuid(), sa.ForeignKey("broadcasts.id"), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("applied_text", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_table("broadcast_copies")
    op.drop_column("broadcasts", "edit_until")
    op.drop_column("broadcasts", "edit_token")
