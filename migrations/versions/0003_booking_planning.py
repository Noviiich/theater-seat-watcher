"""add durable booking planning tables

Revision ID: 0003_booking_planning
Revises: 0002_subscription_baselines
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_booking_planning"
down_revision = "0002_subscription_baselines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "renewal_cycles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("cycle_no", sa.Integer, nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allocation_released_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("candidate_id", "cycle_no"),
    )
    op.create_table(
        "checkout_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "renewal_cycle_id", sa.String(36), sa.ForeignKey("renewal_cycles.id"), nullable=False
        ),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("selected_seat_ids", sa.JSON, nullable=False),
        sa.Column("reserved_total_minor", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("renewal_cycle_id", "attempt_no"),
    )
    op.create_table(
        "budget_allocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("buyer_id", sa.String(36), sa.ForeignKey("buyers.id"), nullable=False),
        sa.Column(
            "discovery_batch_id",
            sa.String(36),
            sa.ForeignKey("discovery_batches.id"),
            nullable=False,
        ),
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column(
            "checkout_intent_id",
            sa.String(36),
            sa.ForeignKey("checkout_intents.id"),
            nullable=False,
        ),
        sa.Column("reserved_total_minor", sa.Integer, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("checkout_intent_id"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "checkout_intent_id",
            sa.String(36),
            sa.ForeignKey("checkout_intents.id"),
            nullable=False,
        ),
        sa.Column("provider_order_id", sa.String(255)),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("total_minor", sa.Integer, nullable=False),
        sa.Column("held_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("checkout_intent_id"),
    )


def downgrade() -> None:
    op.drop_table("orders")
    op.drop_table("budget_allocations")
    op.drop_table("checkout_intents")
    op.drop_table("renewal_cycles")
