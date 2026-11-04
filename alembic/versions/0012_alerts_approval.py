"""Keep owner approval when a subscriber opts out of delivery."""

import sqlalchemy as sa

from alembic import op

revision = "0012_alerts_approval"
down_revision = "0011_delivery_ownership"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("alerts_approved", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Older versions recorded approvals with the target Telegram ID as audit actor.
    # Recover opted-out users as well, without granting access based on VPN role.
    op.execute(
        "UPDATE users SET alerts_approved = TRUE WHERE alerts_enabled = TRUE "
        "OR role = 'owner' OR EXISTS (SELECT 1 FROM audit "
        "WHERE audit.event = 'alerts.approve' "
        "AND audit.actor = CAST(users.telegram_id AS VARCHAR(128)))"
    )


def downgrade():
    op.drop_column("users", "alerts_approved")
