"""Finite durable graph retries; additive, without replaying historical alerts."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018_graph_jobs"
down_revision = "0017_primary_graph"
branch_labels = None
depends_on = None


def upgrade():
    """Add only a separate render-intent queue; existing rows and fences are untouched."""
    op.create_table(
        "graph_jobs",
        sa.Column(
            "id", sa.String(64), sa.ForeignKey("graph_snapshots.id"), primary_key=True
        ),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer, nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_code", sa.String(32), nullable=True),
    )
    op.create_index("ix_graph_jobs_due", "graph_jobs", ["state", "available_at"])


def downgrade():
    """Remove only the new queue on an explicitly requested schema rollback."""
    op.drop_index("ix_graph_jobs_due", table_name="graph_jobs")
    op.drop_table("graph_jobs")
