"""Persist mailing audience, with a VPN default for every existing row."""

import sqlalchemy as sa

from alembic import op

revision = "0022_mailing_groups"
down_revision = "0021_interest_contract"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "broadcasts",
        sa.Column("audience_group", sa.String(64), nullable=True, server_default="vpn"),
    )


def downgrade():
    # New audiences cannot safely be interpreted by old, VPN-only source.
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT count(*) FROM broadcasts WHERE status IN ('selecting_group','awaiting_text','group_choosing','group_scheduled') OR (status IN ('scheduled','choosing','preview','draft','sending') AND (audience_group IS NULL OR audience_group <> 'vpn'))"
        )
    ):
        raise RuntimeError(
            "Cancel/finish non-VPN or composing mailings before downgrading"
        )
    with op.batch_alter_table("broadcasts") as batch:
        batch.drop_column("audience_group")
