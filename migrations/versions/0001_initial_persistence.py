"""initial persistence

Revision ID: 0001_initial_persistence
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "buyers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("telegram_user_id", sa.String(32), nullable=False, unique=True),
        sa.Column("telegram_chat_id", sa.String(32), nullable=False),
        sa.Column("profile_ref", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("buyer_id", sa.String(36), sa.ForeignKey("buyers.id"), nullable=False),
        sa.Column("theatre_alias", sa.String(255), nullable=False),
        sa.Column("ticket_count", sa.Integer, nullable=False),
        sa.Column("seat_profile_id", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("theatre_alias", sa.String(255), nullable=False),
        sa.Column("provider_session_id", sa.String(128), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("hall_id", sa.String(128), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "theatre_alias", "provider_session_id"),
    )
    op.create_table(
        "catalogue_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("theatre_alias", sa.String(255), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("complete", sa.Boolean, nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.UniqueConstraint("theatre_alias", "fingerprint"),
    )
    op.create_table(
        "discovery_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(36),
            sa.ForeignKey("catalogue_snapshots.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("discovered_session_ids", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("buyer_id", sa.String(36), sa.ForeignKey("buyers.id"), nullable=False),
        sa.Column(
            "subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id"), nullable=False
        ),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column(
            "discovery_batch_id",
            sa.String(36),
            sa.ForeignKey("discovery_batches.id"),
            nullable=False,
        ),
        sa.Column("tracking_state", sa.String(64), nullable=False),
        sa.Column("current_cycle_no", sa.Integer, nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("watch_until", sa.DateTime(timezone=True)),
        sa.Column("stop_reason", sa.String(255)),
        sa.UniqueConstraint("buyer_id", "session_id"),
    )


def downgrade() -> None:
    for table in (
        "candidates",
        "discovery_batches",
        "catalogue_snapshots",
        "sessions",
        "subscriptions",
        "buyers",
    ):
        op.drop_table(table)
