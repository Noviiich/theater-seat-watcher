"""Persist a separate booking slot and price policy for every individual ticket."""

import sqlalchemy as sa
from alembic import op

revision = "0015_individual_ticket_orders"
down_revision = "0014_telegram_per_session_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing candidates keep slot zero and all their cycles, orders and outbox IDs.
    with op.batch_alter_table(
        "candidates",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"},
    ) as batch:
        batch.add_column(sa.Column("ticket_no", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("target_seat_id", sa.String(255), nullable=True))
        batch.drop_constraint("uq_candidates_buyer_id_session_id", type_="unique")
        batch.create_unique_constraint(
            "uq_candidate_ticket", ["buyer_id", "session_id", "ticket_no"]
        )
        batch.create_unique_constraint(
            "uq_candidate_seat", ["buyer_id", "session_id", "target_seat_id"]
        )
    op.add_column(
        "checkout_intents",
        sa.Column("price_unlimited", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM candidates WHERE ticket_no > 0"))
    if count:
        raise RuntimeError("individual ticket history requires this schema; restore a backup")
    op.drop_column("checkout_intents", "price_unlimited")
    with op.batch_alter_table("candidates") as batch:
        batch.drop_constraint("uq_candidate_ticket", type_="unique")
        batch.drop_constraint("uq_candidate_seat", type_="unique")
        batch.drop_column("ticket_no")
        batch.drop_column("target_seat_id")
        batch.create_unique_constraint(
            "uq_candidates_buyer_id_session_id", ["buyer_id", "session_id"]
        )
