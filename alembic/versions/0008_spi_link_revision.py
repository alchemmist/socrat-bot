"""Fence out-of-order SPI link updates per card copy."""
from alembic import op
import sqlalchemy as sa
revision='0008_spi_link_revision'
down_revision='0007_card_note'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('telegram_cards',sa.Column('link_revision',sa.Integer(),nullable=False,server_default='0'))

def downgrade():
    op.drop_column('telegram_cards','link_revision')
