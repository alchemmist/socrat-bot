"""Reserve catch-up deliveries across workers and restarts."""

import sqlalchemy as sa
from alembic import op

revision = "0024_resend_claims"
down_revision = "0023_mailing_photos"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "resend_claims",
        sa.Column(
            "broadcast_id", sa.Uuid(), sa.ForeignKey("broadcasts.id"), primary_key=True
        ),
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
    )


def downgrade():
    op.drop_table("resend_claims")
