"""soft delete Telegram subscriptions

Revision ID: 0012_soft_delete_subscriptions
Revises: 0011_telegram_access
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_soft_delete_subscriptions"
down_revision = "0011_telegram_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("deleted_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("subscriptions", "deleted_at")
