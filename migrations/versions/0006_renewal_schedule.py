"""add persisted renewal cycle schedule

Revision ID: 0006_renewal_schedule
Revises: 0005_recovery_request_snapshot
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_renewal_schedule"
down_revision = "0005_recovery_request_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("renewal_cycles", sa.Column("due_at", sa.DateTime(timezone=True)))
    op.add_column("renewal_cycles", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("renewal_cycles", sa.Column("ended_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE renewal_cycles SET started_at = created_at WHERE started_at IS NULL")
    op.create_index("ix_candidates_next_run_at", "candidates", ["next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_candidates_next_run_at", table_name="candidates")
    op.drop_column("renewal_cycles", "ended_at")
    op.drop_column("renewal_cycles", "started_at")
    op.drop_column("renewal_cycles", "due_at")
