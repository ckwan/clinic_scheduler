"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _pk():
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _created_at():
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "clinics",
        _pk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("phone_number", sa.String(32), nullable=False, unique=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        _created_at(),
    )

    op.create_table(
        "providers",
        _pk(),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("specialty", sa.String(100), nullable=False),
        _created_at(),
    )

    op.create_table(
        "patients",
        _pk(),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("phone_number", sa.String(32), nullable=False),
        sa.Column("sms_opt_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created_at(),
        sa.UniqueConstraint("clinic_id", "phone_number", name="uq_patient_clinic_phone"),
    )

    op.create_table(
        "slots",
        _pk(),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.UniqueConstraint("provider_id", "starts_at", name="uq_slot_provider_start"),
    )
    op.create_index("ix_slots_status_starts_at", "slots", ["status", "starts_at"])

    op.create_table(
        "appointments",
        _pk(),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("slot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("slots.id"), nullable=False),
        sa.Column("appointment_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="booked"),
        sa.Column("idempotency_key", sa.String(100), nullable=False, unique=True),
        _created_at(),
    )
    # A slot can have at most one active appointment, which blocks double booking.
    op.create_index(
        "uq_active_appointment_per_slot",
        "appointments",
        ["slot_id"],
        unique=True,
        postgresql_where=sa.text("status = 'booked'"),
    )

    op.create_table(
        "conversations",
        _pk(),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("patients.id"), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False, server_default="sms"),
        sa.Column("phone_number", sa.String(32), nullable=False),
        sa.Column("state", sa.String(40), nullable=False, server_default="started"),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("outcome", sa.String(40), nullable=True),
        sa.Column("langsmith_trace_id", sa.String(100), nullable=True),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_conversations_phone", "conversations", ["clinic_id", "phone_number"])

    op.create_table(
        "messages",
        _pk(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("twilio_sid", sa.String(64), nullable=True, unique=True),
        _created_at(),
    )
    op.create_index("ix_messages_conversation", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "escalations",
        _pk(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("assigned_to", sa.String(100), nullable=True),
        _created_at(),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "audit_log",
        _pk(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("actor", sa.String(40), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        _created_at(),
    )

    op.create_table(
        "eval_runs",
        _pk(),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("git_sha", sa.String(40), nullable=True),
        sa.Column("total_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_cases", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
    )

    op.create_table(
        "eval_results",
        _pk(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("eval_runs.id"), nullable=False),
        sa.Column("case_id", sa.String(100), nullable=False),
        sa.Column("expected", postgresql.JSONB(), nullable=False),
        sa.Column("actual", postgresql.JSONB(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failure_category", sa.String(60), nullable=True),
        sa.Column("turns", sa.Integer(), nullable=True),
        sa.Column("latency_seconds", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    for table in (
        "eval_results",
        "eval_runs",
        "audit_log",
        "escalations",
        "messages",
        "conversations",
        "appointments",
        "slots",
        "patients",
        "providers",
        "clinics",
    ):
        op.drop_table(table)
