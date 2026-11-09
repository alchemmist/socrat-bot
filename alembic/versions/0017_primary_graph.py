"""Native primary graph messages; additive, preserves all existing delivery fences."""

import sqlalchemy as sa

from alembic import op

revision = "0017_primary_graph"
down_revision = "0016_mailing_entities"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "graph_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("png", sa.LargeBinary, nullable=True),
        sa.Column("render_digest", sa.String(64), nullable=True),
    )
    with op.batch_alter_table("telegram_cards") as batch:
        batch.add_column(
            sa.Column(
                "message_kind", sa.String(8), nullable=False, server_default="text"
            )
        )
        batch.add_column(sa.Column("graph_capture", sa.String(64), nullable=True))
    with op.batch_alter_table("incidents") as batch:
        batch.add_column(sa.Column("graph_capture", sa.String(64), nullable=True))
        batch.add_column(sa.Column("graph_generation", sa.Integer, nullable=True))


def downgrade():
    with op.batch_alter_table("incidents") as batch:
        batch.drop_column("graph_generation")
        batch.drop_column("graph_capture")
    with op.batch_alter_table("telegram_cards") as batch:
        batch.drop_column("graph_capture")
        batch.drop_column("message_kind")
    op.drop_table("graph_snapshots")
