"""add order payment snapshots and persistent outbox

Revision ID: 0007_payment_outbox
Revises: 0006_renewal_schedule
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_payment_outbox"
down_revision = "0006_renewal_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("actual_seat_ids", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "orders",
        sa.Column("currency", sa.String(3), nullable=False, server_default="RUB"),
    )
    op.add_column("orders", sa.Column("payment_url", sa.Text()))
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id")),
        sa.Column(
            "discovery_batch_id",
            sa.String(36),
            sa.ForeignKey("discovery_batches.id"),
        ),
        sa.Column("buyer_id", sa.String(36), sa.ForeignKey("buyers.id"), nullable=False),
        sa.Column("destination_chat_id", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("telegram_message_id", sa.String(64)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedup_key"),
    )
    op.create_index("ix_outbox_due", "outbox_messages", ["state", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_due", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_column("orders", "payment_url")
    op.drop_column("orders", "currency")
    op.drop_column("orders", "actual_seat_ids")
