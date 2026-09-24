"""store administrator-approved Telegram access

Revision ID: 0011_telegram_access
Revises: 0010_buyer_profiles
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_telegram_access"
down_revision = "0010_buyer_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_access",
        sa.Column("telegram_user_id", sa.String(32), primary_key=True),
        sa.Column("telegram_chat_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("telegram_access")
