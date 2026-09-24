"""enable recurring bookings for existing live subscriptions

Revision ID: 0013_enable_live_renewals
Revises: 0012_soft_delete_subscriptions
"""

import json
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision = "0013_enable_live_renewals"
down_revision = "0012_soft_delete_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    subscriptions = connection.execute(sa.text("SELECT id, config FROM subscriptions")).all()
    for subscription_id, raw_config in subscriptions:
        config = json.loads(raw_config)
        if config.get("booking_mode") != "live" or config.get("max_cycles_per_session") != 1:
            continue
        interval = config.get("renewal_interval_seconds", 1200)
        if not isinstance(interval, int) or interval <= 0:
            interval = 1200
        config["max_cycles_per_session"] = None
        connection.execute(
            sa.text("UPDATE subscriptions SET config = :config WHERE id = :id"),
            {"config": json.dumps(config), "id": subscription_id},
        )
        stopped_orders = connection.execute(
            sa.text(
                "SELECT candidates.id, orders.held_at "
                "FROM candidates "
                "JOIN renewal_cycles ON renewal_cycles.candidate_id = candidates.id "
                "AND renewal_cycles.cycle_no = candidates.current_cycle_no "
                "JOIN checkout_intents ON checkout_intents.renewal_cycle_id = renewal_cycles.id "
                "JOIN orders ON orders.checkout_intent_id = checkout_intents.id "
                "WHERE candidates.subscription_id = :subscription_id "
                "AND candidates.booking_mode = 'live' "
                "AND candidates.tracking_state = 'stopped' "
                "AND candidates.stop_reason = 'max_cycles_per_session' "
                "AND candidates.current_cycle_no = 1 "
                "AND orders.held_at IS NOT NULL"
            ),
            {"subscription_id": subscription_id},
        ).all()
        for candidate_id, held_at in stopped_orders:
            due_at = datetime.fromisoformat(held_at) + timedelta(seconds=interval)
            connection.execute(
                sa.text(
                    "UPDATE candidates SET tracking_state = 'renewal_waiting', "
                    "next_run_at = :due_at, stop_reason = NULL WHERE id = :id"
                ),
                {"due_at": due_at.isoformat(sep=" "), "id": candidate_id},
            )


def downgrade() -> None:
    # The old one-cycle policy was a temporary product restriction. Restoring it
    # would silently stop subscriptions that users have since allowed to renew.
    pass
