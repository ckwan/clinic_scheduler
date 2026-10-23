"""The booking page's rules. The page never touches the database or the model directly.

Booking goes through scheduling.book_appointment, the same function the SMS agent uses, so the
same guardrails apply: one booked appointment per slot, idempotency, ownership, audit log.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.booking import messages
from app.booking.tokens import hash_token, new_token
from app.config import settings
from app.models import Appointment, BookingLink, Clinic, Conversation, Patient, Provider, Slot
from app.services import scheduling
from app.services.conversations import as_utc, escalate, get_or_create_conversation, log_audit, utcnow
from app.services.errors import InvalidRequest, SchedulingError

MAX_ATTEMPTS = scheduling.MAX_VERIFICATION_ATTEMPTS  # same limit as texting
WINDOW_DAYS = 21
MAX_SLOTS = 60


class LinkProblem(SchedulingError):
    """The link cannot be used. `code` says why so the page can show the right message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WrongDetails(SchedulingError):
    code = "wrong_details"

    def __init__(self, attempts_remaining: int) -> None:
        super().__init__("Those details did not match our records.")
        self.attempts_remaining = attempts_remaining


# ---------------------------------------------------------------- creating links


def link_url(token: str) -> str:
    return f"{settings.booking_base_url.rstrip('/')}/book/{token}"


def create_link(
    session: Session, patient: Patient, appointment_type: str, now: datetime | None = None
) -> tuple[BookingLink, str]:
    """A fresh link. Older unused links for this patient stop working: one live link at a time."""
    now = now or utcnow()
    if appointment_type not in scheduling.APPOINTMENT_TYPES:
        raise InvalidRequest(f"Unknown appointment type: {appointment_type}")
    for old in session.scalars(
        select(BookingLink).where(
            BookingLink.patient_id == patient.id,
            BookingLink.used_at.is_(None),
            BookingLink.revoked_at.is_(None),
        )
    ):
        old.revoked_at = now

    conversation = get_or_create_conversation(session, patient.clinic_id, patient.phone_number)
    token = new_token()
    link = BookingLink(
        patient_id=patient.id,
        conversation_id=conversation.id,
        appointment_type=appointment_type,
        token_hash=hash_token(token),
        expires_at=now + timedelta(days=settings.booking_link_ttl_days),
    )
    session.add(link)
    session.flush()
    return link, token


# ---------------------------------------------------------------- opening a link


def find_link(session: Session, token: str) -> BookingLink | None:
    if not token or len(token) > 200:
        return None
    return session.scalar(select(BookingLink).where(BookingLink.token_hash == hash_token(token)))


def link_status(link: BookingLink | None, now: datetime) -> str:
    """ok, used, expired, or invalid (unknown or revoked)."""
    if link is None or link.revoked_at is not None:
        return "invalid"
    if link.used_at is not None:
        return "used"
    if as_utc(link.expires_at) <= now:
        return "expired"
    return "ok"


@dataclass
class LinkInfo:
    first_name: str
    clinic_name: str
    appointment_type: str
    appointment_name: str


def link_info(session: Session, link: BookingLink) -> LinkInfo:
    """What the page may show before the date of birth is entered: a first name and the clinic."""
    patient = session.get(Patient, link.patient_id)
    clinic = session.get(Clinic, patient.clinic_id)
    return LinkInfo(
        patient.first_name,
        clinic.name,
        link.appointment_type,
        messages.APPOINTMENT_NAMES.get(link.appointment_type, "Appointment"),
    )


# ---------------------------------------------------------------- identity


def _conversation_for(session: Session, patient: Patient) -> Conversation:
    return get_or_create_conversation(session, patient.clinic_id, patient.phone_number)


def authenticate(
    session: Session, link: BookingLink, date_of_birth: date, now: datetime
) -> tuple[Patient, Conversation]:
    """Check the date of birth. A link can be forwarded or opened on a shared phone, so opening it
    proves nothing: the person must know the patient's date of birth.

    Failures count against the link, not the phone. After MAX_ATTEMPTS the link is revoked and a
    human is asked to follow up, the same as repeated failures by text. The message never says
    which detail was wrong."""
    status = link_status(link, now)
    if status in ("invalid", "expired"):
        raise LinkProblem(status)
    patient = session.get(Patient, link.patient_id)
    conversation = _conversation_for(session, patient)
    if conversation.state == "escalated":
        raise LinkProblem("needs_staff")  # a person already owns this conversation

    if patient.date_of_birth != date_of_birth:
        link.failed_attempts += 1
        log_audit(session, conversation.id, "patient_link", "booking_link_verification_failed", None)
        if link.failed_attempts >= MAX_ATTEMPTS:
            link.revoked_at = now
            escalate(session, conversation, "booking_link_verification_failed", actor="patient_link")
            session.flush()
            raise LinkProblem("locked")
        session.flush()
        raise WrongDetails(MAX_ATTEMPTS - link.failed_attempts)
    return patient, conversation


# ---------------------------------------------------------------- slots and booking


def list_slots(
    session: Session, link: BookingLink, date_of_birth: date, now: datetime | None = None
) -> list[scheduling.AvailableSlot]:
    now = now or utcnow()
    if link_status(link, now) == "used":
        return []
    _, conversation = authenticate(session, link, date_of_birth, now)
    return scheduling.check_availability(
        session,
        conversation,
        link.appointment_type,
        latest=now + timedelta(days=WINDOW_DAYS),
        limit=MAX_SLOTS,
        max_results=MAX_SLOTS,
        now=now,
    )


@dataclass
class Booked:
    appointment: Appointment
    starts_at: datetime
    provider_name: str
    clinic_name: str
    timezone: str
    already: bool  # True when this was a repeat of a booking already made with this link


def book(
    session: Session, link: BookingLink, date_of_birth: date, slot_id: UUID, now: datetime | None = None
) -> Booked:
    """Book through scheduling.book_appointment. Repeating the request returns the same booking."""
    now = now or utcnow()
    if link_status(link, now) == "used" and link.appointment_id:
        patient = session.get(Patient, link.patient_id)
        if patient.date_of_birth != date_of_birth:
            raise LinkProblem("used")  # do not describe the booking to someone who fails the check
        return _describe(session, session.get(Appointment, link.appointment_id), already=True)

    patient, conversation = authenticate(session, link, date_of_birth, now)
    # book_appointment wants a verified conversation. The date of birth above is that verification
    # for this one action; the conversation goes back to how it was so texting is not unlocked.
    was_verified = conversation.verified
    conversation.verified = True
    try:
        appointment = scheduling.book_appointment(
            session, conversation, slot_id, link.appointment_type, f"link-{link.id}", now=now
        )
    finally:
        conversation.verified = was_verified

    link.used_at = now
    link.appointment_id = appointment.id
    log_audit(
        session, conversation.id, "patient_link", "booked_via_link", {"appointment_id": str(appointment.id)}
    )
    session.flush()
    return _describe(session, appointment, already=False)


def _describe(session: Session, appointment: Appointment, already: bool) -> Booked:
    slot = session.get(Slot, appointment.slot_id)
    provider = session.get(Provider, slot.provider_id)
    clinic = session.get(Clinic, provider.clinic_id)
    return Booked(appointment, as_utc(slot.starts_at), provider.name, clinic.name, clinic.timezone, already)

