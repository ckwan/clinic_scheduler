"""Scheduling rules. The agent never touches the database directly; it goes through here.

Guardrails enforced in this layer, not in prompts:
- booking, cancelling, rescheduling, and listing require a verified conversation
- an appointment can only be changed by the patient it belongs to
- a slot can only hold one active appointment
- booking with the same idempotency key twice returns the first appointment
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Appointment, Conversation, Patient, Provider, Slot
from app.services.conversations import as_utc, escalate, log_audit, utcnow
from app.services.errors import (
    AppointmentNotFound,
    InvalidRequest,
    NotVerified,
    SlotUnavailable,
)

APPOINTMENT_SPECIALTY = {
    "therapy_session": "therapy",
    "checkup": "primary_care",
    "annual_wellness_visit": "primary_care",
}
APPOINTMENT_TYPES = tuple(APPOINTMENT_SPECIALTY)

MAX_VERIFICATION_ATTEMPTS = 2
MAX_AVAILABILITY_RESULTS = 10


@dataclass
class VerificationResult:
    verified: bool
    attempts_remaining: int
    escalated: bool


@dataclass
class AvailableSlot:
    slot_id: UUID
    provider_name: str
    starts_at: datetime
    ends_at: datetime


# ---------------------------------------------------------------- verification


def _same_text(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def verify_identity(
    session: Session,
    conversation: Conversation,
    first_name: str,
    last_name: str,
    date_of_birth: date,
) -> VerificationResult:
    """Name and date of birth must match the patient record on file for the sender's phone number.

    Failures never say which field was wrong. After MAX_VERIFICATION_ATTEMPTS failures the
    conversation is escalated to a human.
    """
    if conversation.verified:
        return VerificationResult(True, MAX_VERIFICATION_ATTEMPTS, False)
    if conversation.state == "escalated":
        return VerificationResult(False, 0, True)

    patient = session.get(Patient, conversation.patient_id) if conversation.patient_id else None
    matched = (
        patient is not None
        and _same_text(patient.first_name, first_name)
        and _same_text(patient.last_name, last_name)
        and patient.date_of_birth == date_of_birth
    )

    if matched:
        conversation.verified = True
        conversation.state = "verified"
        log_audit(session, conversation.id, "agent", "verification_passed")
        session.flush()
        return VerificationResult(True, MAX_VERIFICATION_ATTEMPTS - conversation.verification_attempts, False)

    conversation.verification_attempts += 1
    log_audit(session, conversation.id, "agent", "verification_failed")
    escalated = False
    if conversation.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
        escalate(session, conversation, "verification_failed")
        escalated = True
    session.flush()
    remaining = max(0, MAX_VERIFICATION_ATTEMPTS - conversation.verification_attempts)
    return VerificationResult(False, remaining, escalated)


def _require_verified(conversation: Conversation) -> None:
    if not conversation.verified or conversation.patient_id is None:
        raise NotVerified("Identity must be verified first.")


def _validate_type(appointment_type: str) -> str:
    if appointment_type not in APPOINTMENT_SPECIALTY:
        raise InvalidRequest(f"Unknown appointment type: {appointment_type}")
    return APPOINTMENT_SPECIALTY[appointment_type]


# ---------------------------------------------------------------- availability


def check_availability(
    session: Session,
    conversation: Conversation,
    appointment_type: str,
    earliest: datetime | None = None,
    latest: datetime | None = None,
    limit: int = 5,
    now: datetime | None = None,
) -> list[AvailableSlot]:
    """Open future slots for the right kind of provider. Not patient data, so no verification."""
    specialty = _validate_type(appointment_type)
    now = now or utcnow()
    start = max(as_utc(earliest), now) if earliest else now
    limit = max(1, min(limit, MAX_AVAILABILITY_RESULTS))

    query = (
        select(Slot, Provider)
        .join(Provider, Slot.provider_id == Provider.id)
        .where(
            Provider.clinic_id == conversation.clinic_id,
            Provider.specialty == specialty,
            Slot.status == "open",
            Slot.starts_at > start,
        )
        .order_by(Slot.starts_at)
        .limit(limit)
    )
    if latest:
        query = query.where(Slot.starts_at <= as_utc(latest))

    return [
        AvailableSlot(slot.id, provider.name, as_utc(slot.starts_at), as_utc(slot.ends_at))
        for slot, provider in session.execute(query)
    ]


def _open_slot_for(
    session: Session,
    conversation: Conversation,
    slot_id: UUID,
    appointment_type: str,
    now: datetime,
) -> Slot:
    specialty = _validate_type(appointment_type)
    slot = session.get(Slot, slot_id, with_for_update=True)
    provider = session.get(Provider, slot.provider_id) if slot else None
    if (
        slot is None
        or provider is None
        or provider.clinic_id != conversation.clinic_id
        or provider.specialty != specialty
    ):
        raise SlotUnavailable("That time is not available for this appointment type.")
    if slot.status != "open" or as_utc(slot.starts_at) <= now:
        raise SlotUnavailable("That time is no longer available.")
    return slot


# ---------------------------------------------------------------- booking


def book_appointment(
    session: Session,
    conversation: Conversation,
    slot_id: UUID,
    appointment_type: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> Appointment:
    _require_verified(conversation)
    _validate_type(appointment_type)
    now = now or utcnow()

    existing = session.scalar(select(Appointment).where(Appointment.idempotency_key == idempotency_key))
    if existing:
        if existing.patient_id != conversation.patient_id:
            raise InvalidRequest("Idempotency key already used.")
        return existing

    slot = _open_slot_for(session, conversation, slot_id, appointment_type, now)
    appointment = Appointment(
        patient_id=conversation.patient_id,
        slot_id=slot.id,
        appointment_type=appointment_type,
        idempotency_key=idempotency_key,
    )
    try:
        with session.begin_nested():
            slot.status = "booked"
            session.add(appointment)
            session.flush()
    except IntegrityError as exc:
        raise SlotUnavailable("That time was just taken.") from exc

    conversation.outcome = "booked"
    log_audit(
        session,
        conversation.id,
        "agent",
        "appointment_booked",
        {"appointment_id": str(appointment.id), "slot_id": str(slot.id), "type": appointment_type},
    )
    session.flush()
    return appointment


def _owned_appointment(session: Session, conversation: Conversation, appointment_id: UUID) -> Appointment:
    """Same error for 'missing' and 'belongs to someone else' so existence is not leaked."""
    appointment = session.get(Appointment, appointment_id)
    if appointment is None or appointment.patient_id != conversation.patient_id:
        raise AppointmentNotFound("No matching appointment found.")
    return appointment


def cancel_appointment(
    session: Session, conversation: Conversation, appointment_id: UUID
) -> Appointment:
    _require_verified(conversation)
    appointment = _owned_appointment(session, conversation, appointment_id)
    if appointment.status == "cancelled":
        return appointment

    slot = session.get(Slot, appointment.slot_id)
    appointment.status = "cancelled"
    if slot:
        slot.status = "open"
    conversation.outcome = "cancelled"
    log_audit(
        session,
        conversation.id,
        "agent",
        "appointment_cancelled",
        {"appointment_id": str(appointment.id)},
    )
    session.flush()
    return appointment


def reschedule_appointment(
    session: Session,
    conversation: Conversation,
    appointment_id: UUID,
    new_slot_id: UUID,
    now: datetime | None = None,
) -> Appointment:
    """Move the appointment to a new slot. The appointment id stays the same, because
    NoteLoop and the claims project reference it."""
    _require_verified(conversation)
    now = now or utcnow()
    appointment = _owned_appointment(session, conversation, appointment_id)
    if appointment.status != "booked":
        raise InvalidRequest("Only booked appointments can be rescheduled.")
    if appointment.slot_id == new_slot_id:
        return appointment

    new_slot = _open_slot_for(session, conversation, new_slot_id, appointment.appointment_type, now)
    old_slot = session.get(Slot, appointment.slot_id)
    old_slot_id = appointment.slot_id
    try:
        with session.begin_nested():
            new_slot.status = "booked"
            if old_slot:
                old_slot.status = "open"
            appointment.slot_id = new_slot.id
            session.flush()
    except IntegrityError as exc:
        raise SlotUnavailable("That time was just taken.") from exc

    conversation.outcome = "rescheduled"
    log_audit(
        session,
        conversation.id,
        "agent",
        "appointment_rescheduled",
        {
            "appointment_id": str(appointment.id),
            "from_slot_id": str(old_slot_id),
            "to_slot_id": str(new_slot.id),
        },
    )
    session.flush()
    return appointment


def confirm_attendance(
    session: Session,
    conversation: Conversation,
    appointment_id: UUID,
    now: datetime | None = None,
) -> Appointment:
    """The patient replied CONFIRM to a reminder.

    Needs ownership (the sender's phone must belong to the appointment's patient) but not full
    verification: it changes nothing sensitive and reveals nothing. Safe to repeat."""
    appointment = _owned_appointment(session, conversation, appointment_id)
    if appointment.status != "booked":
        raise InvalidRequest("Only booked appointments can be confirmed.")
    if appointment.confirmed_at is None:
        appointment.confirmed_at = now or utcnow()
        log_audit(
            session,
            conversation.id,
            "agent",
            "appointment_confirmed",
            {"appointment_id": str(appointment.id)},
        )
        session.flush()
    return appointment


def list_upcoming_appointments(
    session: Session, conversation: Conversation, now: datetime | None = None
) -> list[tuple[Appointment, Slot, Provider]]:
    _require_verified(conversation)
    now = now or utcnow()
    rows = session.execute(
        select(Appointment, Slot, Provider)
        .join(Slot, Appointment.slot_id == Slot.id)
        .join(Provider, Slot.provider_id == Provider.id)
        .where(
            Appointment.patient_id == conversation.patient_id,
            Appointment.status == "booked",
            Slot.starts_at > now,
        )
        .order_by(Slot.starts_at)
    )
    return [(a, s, p) for a, s, p in rows]
