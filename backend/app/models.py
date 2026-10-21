import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Los_Angeles", nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    specialty: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Patient(Base):
    """Synthetic patients only."""

    __tablename__ = "patients"

    id: Mapped[uuid.UUID] = _uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("clinic_id", "phone_number", name="uq_patient_clinic_phone"),)


class Slot(Base):
    __tablename__ = "slots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)  # open, booked, blocked

    __table_args__ = (UniqueConstraint("provider_id", "starts_at", name="uq_slot_provider_start"),)


class Appointment(Base):
    """The id is the shared key NoteLoop and the Claims Feedback Loop reference."""

    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"), nullable=False)
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("slots.id"), nullable=False)
    appointment_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="booked", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    # Set when the patient replies CONFIRM to a reminder.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # A slot can have at most one active appointment, which blocks double booking.
        Index(
            "uq_active_appointment_per_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("status = 'booked'"),
            sqlite_where=text("status = 'booked'"),
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), default="sms", nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(40), default="started", nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Where the agent is in the conversation (phase, offered slots, pending action).
    agent_state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    langsmith_trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # inbound, outbound
    body: Mapped[str] = mapped_column(Text, nullable=False)
    twilio_sid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # Outbound only: what Twilio last told us (queued, sent, delivered, undelivered, failed).
    delivery_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    delivery_error_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class SmsOptOut(Base):
    """A phone number that texted STOP. We never send to it until it texts START.

    Keyed by phone number only. With several clinics on separate sender numbers this would be
    keyed by (sender number, phone number), the way Twilio scopes opt-outs."""

    __tablename__ = "sms_opt_outs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # keyword, twilio_error
    created_at: Mapped[datetime] = _created_at()


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    reason: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)  # open, taken, resolved
    assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(40), nullable=False)  # agent, human, system
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    total_cases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed_cases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_runs.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(String(100), nullable=False)
    expected: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actual: Mapped[dict] = mapped_column(JSONB, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
