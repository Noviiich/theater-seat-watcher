"""add singleton runtime lock and durable worker diagnostics

Revision ID: 0009_runtime_diagnostics
Revises: 0008_booking_orchestration
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_runtime_diagnostics"
down_revision = "0008_booking_orchestration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_locks",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "runtime_workers",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_duration_ms", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_workers")
    op.drop_table("runtime_locks")
