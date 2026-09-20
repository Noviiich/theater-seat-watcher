"""persist checkout request snapshot for recovery

Revision ID: 0005_recovery_request_snapshot
Revises: 0004_checkout_stages
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_recovery_request_snapshot"
down_revision = "0004_checkout_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checkout_intents", sa.Column("expected_total_minor", sa.Integer()))
    op.add_column(
        "checkout_intents",
        sa.Column("currency", sa.String(3), nullable=False, server_default="RUB"),
    )
    op.add_column(
        "checkout_intents",
        sa.Column(
            "expected_hold_ttl_seconds",
            sa.Integer(),
            nullable=False,
            server_default="1200",
        ),
    )
    op.execute(
        "UPDATE checkout_intents "
        "SET expected_total_minor = reserved_total_minor "
        "WHERE expected_total_minor IS NULL"
    )
    with op.batch_alter_table("checkout_intents") as batch:
        batch.alter_column("expected_total_minor", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    op.drop_column("checkout_intents", "expected_hold_ttl_seconds")
    op.drop_column("checkout_intents", "currency")
    op.drop_column("checkout_intents", "expected_total_minor")
