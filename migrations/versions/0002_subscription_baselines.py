"""add subscription discovery baselines

Revision ID: 0002_subscription_baselines
Revises: 0001_initial_persistence
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_subscription_baselines"
down_revision = "0001_initial_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_baselines",
        sa.Column(
            "subscription_id",
            sa.String(36),
            sa.ForeignKey("subscriptions.id"),
            primary_key=True,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(36),
            sa.ForeignKey("catalogue_snapshots.id"),
            nullable=False,
        ),
        sa.Column("established_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("subscription_baselines")
