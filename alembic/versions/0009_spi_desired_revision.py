"""Persist accepted desired SPI revision separately from applied copy revision."""
from alembic import op
import sqlalchemy as sa
revision='0009_spi_desired_revision'
down_revision='0008_spi_link_revision'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('telegram_cards',sa.Column('desired_link_revision',sa.Integer(),nullable=False,server_default='0'))
    op.add_column('telegram_cards',sa.Column('desired_spi_url',sa.Text(),nullable=True))

def downgrade():
    op.drop_column('telegram_cards','desired_spi_url')
    op.drop_column('telegram_cards','desired_link_revision')
