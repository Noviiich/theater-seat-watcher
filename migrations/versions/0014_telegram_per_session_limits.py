"""apply Telegram ticket and price limits independently to each live session

Revision ID: 0014_telegram_per_session_limits
Revises: 0013_enable_live_renewals
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0014_telegram_per_session_limits"
down_revision = "0013_enable_live_renewals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    subscriptions = connection.execute(sa.text("SELECT id, config FROM subscriptions")).all()
    for subscription_id, raw_config in subscriptions:
        config = json.loads(raw_config)
        order_limit = config.get("max_order_total")
        if (
            config.get("booking_mode") != "live"
            or not isinstance(order_limit, int)
            or config.get("max_sessions_per_batch") != 1
            or config.get("max_active_orders") != 1
            or config.get("max_batch_total") != order_limit
            or config.get("max_active_total") != order_limit
        ):
            continue
        for name in (
            "max_sessions_per_batch",
            "max_active_orders",
            "max_batch_total",
            "max_active_total",
        ):
            config[name] = None
        connection.execute(
            sa.text("UPDATE subscriptions SET config = :config WHERE id = :id"),
            {"config": json.dumps(config), "id": subscription_id},
        )
        connection.execute(
            sa.text(
                "UPDATE candidates SET tracking_state = 'queued', next_run_at = NULL, "
                "stop_reason = NULL WHERE subscription_id = :subscription_id "
                "AND booking_mode = 'live' "
                "AND tracking_state IN ('stopped', 'skipped_limit') "
                "AND stop_reason = 'max_sessions_per_batch'"
            ),
            {"subscription_id": subscription_id},
        )


def downgrade() -> None:
    # A blanket reversal could discard a later user choice and stop active cycles.
    pass
