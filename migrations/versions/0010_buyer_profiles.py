"""store buyer payment profiles in SQLite

Revision ID: 0010_buyer_profiles
Revises: 0009_runtime_diagnostics
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_buyer_profiles"
down_revision = "0009_runtime_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("buyers") as batch:
        batch.add_column(sa.Column("lastname", sa.String(255)))
        batch.add_column(sa.Column("firstname", sa.String(255)))
        batch.add_column(sa.Column("middlename", sa.String(255)))
        batch.add_column(sa.Column("email", sa.String(320)))
        batch.add_column(sa.Column("phone", sa.String(32)))
        batch.add_column(
            sa.Column(
                "personal_data_consent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.drop_column("profile_ref")


def downgrade() -> None:
    with op.batch_alter_table("buyers") as batch:
        batch.add_column(sa.Column("profile_ref", sa.String(255)))
        batch.drop_column("personal_data_consent")
        batch.drop_column("phone")
        batch.drop_column("email")
        batch.drop_column("middlename")
        batch.drop_column("firstname")
        batch.drop_column("lastname")
