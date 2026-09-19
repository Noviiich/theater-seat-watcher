"""record changing checkout stages

Revision ID: 0004_checkout_stages
Revises: 0003_booking_planning
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_checkout_stages"
down_revision = "0003_booking_planning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checkout_intents",
        sa.Column("remote_stage", sa.String(64), nullable=False, server_default="planned"),
    )
    op.add_column("checkout_intents", sa.Column("write_started_at", sa.DateTime(timezone=True)))
    op.add_column("checkout_intents", sa.Column("write_completed_at", sa.DateTime(timezone=True)))
    op.add_column("checkout_intents", sa.Column("last_error_code", sa.String(64)))
    op.add_column("checkout_intents", sa.Column("updated_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("checkout_intents", "updated_at")
    op.drop_column("checkout_intents", "last_error_code")
    op.drop_column("checkout_intents", "write_completed_at")
    op.drop_column("checkout_intents", "write_started_at")
    op.drop_column("checkout_intents", "remote_stage")
