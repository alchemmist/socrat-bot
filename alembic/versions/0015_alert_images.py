"""Durable image intent and per-generation recipient receipts."""
import sqlalchemy as sa

from alembic import op

revision = "0015_alert_images"
down_revision = "0014_broadcast_copy_time"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alert_images",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("generation", sa.Integer, primary_key=True),
        sa.Column("slot", sa.Integer, primary_key=True),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary, nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(100), nullable=False),
    )
    op.create_table(
        "image_deliveries",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("generation", sa.Integer, primary_key=True),
        sa.Column("chat_id", sa.BigInteger, primary_key=True),
        sa.Column("slot", sa.Integer, primary_key=True),
        sa.Column("revision", sa.String(64)),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("reply_id", sa.BigInteger),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("image_deliveries")
    op.drop_table("alert_images")
