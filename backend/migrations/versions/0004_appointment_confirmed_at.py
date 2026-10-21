"""add confirmed_at to appointments

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("appointments", "confirmed_at")
