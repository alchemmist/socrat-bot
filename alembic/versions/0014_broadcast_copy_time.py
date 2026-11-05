"""Order editable mailing history by original delivery receipt time."""
import sqlalchemy as sa

from alembic import op

revision = "0014_broadcast_copy_time"
down_revision = "0013_broadcast_copies"
branch_labels = None
depends_on = None


def upgrade():
    # Batch rebuild supports populated SQLite, where ALTER ADD a timestamp with
    # a nonconstant default is not supported. Existing IDs/text remain untouched.
    with op.batch_alter_table("broadcast_copies") as batch:
        batch.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    with op.batch_alter_table("broadcast_copies") as batch:
        batch.drop_column("sent_at")
