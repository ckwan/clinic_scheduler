from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Conversation, Escalation, Message, Patient


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Some backends return naive datetimes. Treat naive values as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def log_audit(
    session: Session,
    conversation_id: UUID | None,
    actor: str,
    action: str,
    detail: dict | None = None,
) -> AuditLog:
    entry = AuditLog(conversation_id=conversation_id, actor=actor, action=action, detail=detail)
    session.add(entry)
    return entry


def get_or_create_conversation(
    session: Session, clinic_id: UUID, phone_number: str, channel: str = "sms"
) -> Conversation:
    """Return the open conversation for this phone number, or start a new one."""
    conversation = session.scalars(
        select(Conversation)
        .where(
            Conversation.clinic_id == clinic_id,
            Conversation.phone_number == phone_number,
            Conversation.state != "closed",
        )
        .order_by(Conversation.created_at.desc())
    ).first()
    if conversation:
        return conversation

    patient = session.scalar(
        select(Patient).where(Patient.clinic_id == clinic_id, Patient.phone_number == phone_number)
    )
    conversation = Conversation(
        clinic_id=clinic_id,
        patient_id=patient.id if patient else None,
        phone_number=phone_number,
        channel=channel,
    )
    session.add(conversation)
    session.flush()
    return conversation


def add_message(
    session: Session,
    conversation: Conversation,
    direction: str,
    body: str,
    twilio_sid: str | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation.id, direction=direction, body=body, twilio_sid=twilio_sid
    )
    session.add(message)
    session.flush()
    return message


def escalate(session: Session, conversation: Conversation, reason: str) -> Escalation:
    """Hand the conversation to a human. Safe to call twice: returns the open escalation."""
    existing = session.scalar(
        select(Escalation).where(
            Escalation.conversation_id == conversation.id, Escalation.status == "open"
        )
    )
    if existing:
        return existing

    escalation = Escalation(conversation_id=conversation.id, reason=reason)
    session.add(escalation)
    conversation.state = "escalated"
    conversation.outcome = "escalated"
    log_audit(session, conversation.id, "agent", "escalated", {"reason": reason})
    session.flush()
    return escalation
