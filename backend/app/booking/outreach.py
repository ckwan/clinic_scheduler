"""Texting patients a booking link, and finding the patients who are due.

Used by the staff button, the Temporal campaign, and the tests. Every send passes the same guards:
opted out, a human owns the conversation, or already texted recently means no text.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.booking import messages, service
from app.channels.sms import SmsSender
from app.config import settings
from app.models import Appointment, AuditLog, BookingLink, Clinic, Conversation, Patient, Slot
from app.services import optout
from app.services.conversations import add_message, as_utc, log_audit, utcnow

WELLNESS = "annual_wellness_visit"
DUE_AFTER_DAYS = 365
COOLDOWN = timedelta(hours=24)


class NotSent(Exception):
    """A guard stopped the text. `reason` is stable; str() is safe to show staff."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def is_due(patient: Patient, today: date) -> bool:
    last = patient.last_wellness_visit_on
    return last is None or (today - last).days >= DUE_AFTER_DAYS


def _has_upcoming_visit(session: Session, patient: Patient, now: datetime) -> bool:
    return (
        session.scalar(
            select(Appointment.id)
            .join(Slot, Appointment.slot_id == Slot.id)
            .where(
                Appointment.patient_id == patient.id,
                Appointment.status == "booked",
                Slot.starts_at >= now,
            )
        )
        is not None
    )


def _has_live_link(session: Session, patient: Patient, now: datetime) -> bool:
    return (
        session.scalar(
            select(BookingLink.id).where(
                BookingLink.patient_id == patient.id,
                BookingLink.used_at.is_(None),
                BookingLink.revoked_at.is_(None),
                BookingLink.expires_at > now,
            )
        )
        is not None
    )


def find_due_patients(session: Session, now: datetime | None = None, limit: int = 200) -> list[Patient]:
    """Patients a campaign should text: due for a yearly visit, nothing booked, no live link."""
    now = now or utcnow()
    cutoff = now.date() - timedelta(days=DUE_AFTER_DAYS)
    candidates = session.scalars(
        select(Patient)
        .where(or_(Patient.last_wellness_visit_on.is_(None), Patient.last_wellness_visit_on <= cutoff))
        .order_by(Patient.last_wellness_visit_on.asc().nulls_first(), Patient.created_at)
    )
    due = []
    for patient in candidates:
        if optout.is_opted_out(session, patient.phone_number):
            continue
        if _has_upcoming_visit(session, patient, now) or _has_live_link(session, patient, now):
            continue
        due.append(patient)
        if len(due) >= limit:
            break
    return due


def send_booking_link(
    session: Session,
    sms: SmsSender,
    patient: Patient,
    *,
    kind: str = "initial",
    actor: str = "system",
    now: datetime | None = None,
    appointment_type: str = WELLNESS,
) -> BookingLink:
    """Create a link and text it. Raises NotSent (nothing created, nothing sent) if a guard stops it."""
    now = now or utcnow()
    if optout.is_opted_out(session, patient.phone_number):
        raise NotSent("opted_out", "This patient opted out of texts")

    conversation = session.scalar(
        select(Conversation).where(
            Conversation.clinic_id == patient.clinic_id,
            Conversation.phone_number == patient.phone_number,
            Conversation.state != "closed",
        )
    )
    if conversation is not None:
        if conversation.state == "escalated":
            raise NotSent("escalated", "A staff member already owns this conversation")
        recent = session.scalar(
            select(AuditLog.id).where(
                AuditLog.conversation_id == conversation.id,
                AuditLog.action == "outreach_sent",
                AuditLog.created_at >= now - COOLDOWN,
            )
        )
        if recent:
            raise NotSent("cooldown", "This patient was already texted in the last 24 hours")

    clinic = session.get(Clinic, patient.clinic_id)
    link, token = service.create_link(session, patient, appointment_type, now)
    template = messages.NUDGE_TEXT if kind == "nudge" else messages.LINK_TEXT
    body = template.format(
        first_name=patient.first_name,
        clinic=clinic.name,
        url=service.link_url(token),
        days=settings.booking_link_ttl_days,
    )

    conversation = session.get(Conversation, link.conversation_id)
    sid = sms.send(patient.phone_number, body)  # raises before the text is recorded
    message = add_message(session, conversation, "outbound", _redact(body, token))
    message.twilio_sid, message.delivery_status = sid, "queued"
    log_audit(
        session,
        conversation.id,
        actor,
        "outreach_sent",
        {"message_id": str(message.id), "kind": f"booking_link_{kind}", "link_id": str(link.id)},
    )
    session.flush()
    return link


def _redact(body: str, token: str) -> str:
    """The transcript is read by staff and sent to dashboards, so it keeps the text but not the
    secret: anyone who could read it could otherwise open the patient's link."""
    return body.replace(token, "…")


def link_outcome(session: Session, patient: Patient, now: datetime | None = None) -> str:
    """What happened to the patient's most recent link: booked, pending, expired, none, opted_out."""
    now = now or utcnow()
    if optout.is_opted_out(session, patient.phone_number):
        return "opted_out"
    link = session.scalar(
        select(BookingLink).where(BookingLink.patient_id == patient.id).order_by(BookingLink.created_at.desc())
    )
    if link is None:
        return "none"
    if link.used_at is not None:
        return "booked"
    if link.revoked_at is not None:
        return "revoked"
    return "expired" if as_utc(link.expires_at) <= now else "pending"
