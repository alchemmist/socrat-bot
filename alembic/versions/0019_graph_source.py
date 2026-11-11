"""Nullable capture provenance; legacy images deliberately remain unattributed."""

import sqlalchemy as sa

from alembic import op

revision = "0019_graph_source"
down_revision = "0018_graph_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("graph_snapshots", sa.Column("chart_url", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("graph_snapshots", "chart_url")
