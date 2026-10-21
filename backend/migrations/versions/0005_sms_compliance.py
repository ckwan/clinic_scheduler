"""sms opt-outs and delivery status

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_opt_outs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone_number", sa.String(32), nullable=False, unique=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("messages", sa.Column("delivery_status", sa.String(20), nullable=True))
    op.add_column("messages", sa.Column("delivery_error_code", sa.String(20), nullable=True))
    # Replaced by the sms_opt_outs table, which also covers numbers that are not patients.
    op.drop_column("patients", "sms_opt_out")


def downgrade() -> None:
    op.add_column("patients", sa.Column("sms_opt_out", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.drop_column("messages", "delivery_error_code")
    op.drop_column("messages", "delivery_status")
    op.drop_table("sms_opt_outs")
