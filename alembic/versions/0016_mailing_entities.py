"""Persist desired and applied native Telegram entities."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016_mailing_entities"
down_revision = "0015_alert_images"
branch_labels = None
depends_on = None


def upgrade():
    kind = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    for table, column in (("broadcasts", "entities"), ("broadcast_copies", "applied_entities")):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, kind, nullable=False, server_default="[]"))


def downgrade():
    for table, column in (("broadcast_copies", "applied_entities"), ("broadcasts", "entities")):
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
