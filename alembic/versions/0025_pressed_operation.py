"""Recipient whose next visible edit must precede any pending graph upload."""
import sqlalchemy as sa
from alembic import op

revision = '0025_pressed_operation'
down_revision = '0024_resend_claims'
branch_labels = depends_on = None


def upgrade():
    op.add_column('card_deliveries', sa.Column('pressed_operation', sa.String(32)))


def downgrade():
    op.drop_column('card_deliveries', 'pressed_operation')
