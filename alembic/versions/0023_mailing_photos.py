"""Track the desired photo and confirmed edits per original mailing copy."""
import sqlalchemy as sa
from alembic import op

revision = "0023_mailing_photos"
down_revision = "0022_mailing_groups"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("broadcasts", sa.Column("photo_id", sa.Text(), nullable=True))
    op.add_column("broadcast_copies", sa.Column("applied_photo_id", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("broadcast_copies", "applied_photo_id")
    op.drop_column("broadcasts", "photo_id")
