"""add candidate mode isolation and durable dry-run reports

Revision ID: 0008_booking_orchestration
Revises: 0007_payment_outbox
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_booking_orchestration"
down_revision = "0007_payment_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column("subscription_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "candidates",
        sa.Column("booking_mode", sa.String(16), nullable=False, server_default="dry_run"),
    )
    op.execute(
        "UPDATE candidates SET booking_mode = COALESCE("
        "(SELECT json_extract(subscriptions.config, '$.booking_mode') "
        "FROM subscriptions WHERE subscriptions.id = candidates.subscription_id), "
        "'dry_run')"
    )
    op.execute(
        "UPDATE candidates SET subscription_version = COALESCE("
        "(SELECT subscriptions.version FROM subscriptions "
        "WHERE subscriptions.id = candidates.subscription_id), 1)"
    )
    op.create_table(
        "dry_run_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.String(36),
            sa.ForeignKey("candidates.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("selected_seat_ids", sa.JSON(), nullable=False),
        sa.Column("total_minor", sa.Integer()),
        sa.Column("currency", sa.String(3)),
        sa.Column("explanation", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dry_run_reports")
    op.drop_column("candidates", "booking_mode")
    op.drop_column("candidates", "subscription_version")
