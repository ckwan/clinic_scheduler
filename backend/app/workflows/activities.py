"""Temporal activities. All side effects (database, SMS, model calls) live here, never in the
workflows, so workflow code stays deterministic and replayable."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from temporalio import activity

from app.agent import replies
from app.agent.runner import process_inbound
from app.channels.sms import SmsSender
from app.models import Appointment, Clinic, Conversation, Message, Patient, Slot
from app.services.conversations import add_message, as_utc, log_audit, utcnow
from app.services.optout import is_opted_out
from app.workflows.models import AppointmentInfo, InboundMessage, ReminderPlan, TurnResult


class Activities:
    def __init__(self, session_factory, llm, sms: SmsSender):
        self._session_factory = session_factory
        self._llm = llm
        self._sms = sms

    @activity.defn
    def process_message(self, conversation_id: str, message: InboundMessage) -> TurnResult:
        """Run the agent for one inbound message. Idempotent on the Twilio sid."""
        with self._session_factory() as session:
            conversation = session.get(Conversation, UUID(conversation_id))
            clinic = session.get(Clinic, conversation.clinic_id)
            outcome = process_inbound(
                session, clinic, conversation, message.body, self._llm, twilio_sid=message.sid
            )
            result = TurnResult(
                reply=outcome.reply,
                to_number=conversation.phone_number,
                phase=(conversation.agent_state or {}).get("phase", "idle"),
                escalated=conversation.state == "escalated",
                new_appointment_ids=outcome.new_appointment_ids,
                duplicate=outcome.duplicate,
                reply_message_id=outcome.reply_message_id,
            )
            session.commit()
            return result

    @activity.defn
    def send_sms(self, to_number: str, body: str, message_id: str | None = None) -> str:
        """Raises on failure so Temporal retries with backoff. Never texts an opted-out number.

        When message_id is given, the provider's id is stored on that message so delivery
        callbacks can be matched to it."""
        with self._session_factory() as session:
            if is_opted_out(session, to_number):
                return "skipped:opted_out"
        sid = self._sms.send(to_number, body)
        if message_id:
            try:
                with self._session_factory() as session:
                    message = session.get(Message, UUID(message_id))
                    if message is not None:
                        message.twilio_sid = sid
                        message.delivery_status = "queued"
                        session.commit()
            except Exception:  # the text is already sent: failing here would make Temporal resend it
                activity.logger.exception("could not store sid %s on message %s", sid, message_id)
        return sid

    @activity.defn
    def record_event(self, conversation_id: str, action: str, detail: dict) -> None:
        """Audit trail for things workflows do (nudge, close, failed send)."""
        with self._session_factory() as session:
            log_audit(session, UUID(conversation_id), "workflow", action, detail)
            session.commit()

    @activity.defn
    def prepare_nudge(self, conversation_id: str) -> ReminderPlan | None:
        """Text for a stalled conversation, or None if there is nothing to nudge."""
        with self._session_factory() as session:
            conversation = session.get(Conversation, UUID(conversation_id))
            phase = (conversation.agent_state or {}).get("phase", "idle")
            body = replies.NUDGES.get(phase)
            if body is None or conversation.state in ("escalated", "closed"):
                return None
            if is_opted_out(session, conversation.phone_number):
                return None
            message = add_message(session, conversation, "outbound", body)
            session.commit()
            return ReminderPlan(to_number=conversation.phone_number, body=body, message_id=str(message.id))

    @activity.defn
    def close_conversation(self, conversation_id: str, reason: str) -> None:
        with self._session_factory() as session:
            conversation = session.get(Conversation, UUID(conversation_id))
            if conversation.state != "closed":
                conversation.state = "closed"
                conversation.outcome = conversation.outcome or reason
                conversation.agent_state = {}
                log_audit(session, conversation.id, "workflow", "conversation_closed", {"reason": reason})
            session.commit()

    @activity.defn
    def get_appointment_info(self, appointment_id: str) -> AppointmentInfo:
        with self._session_factory() as session:
            appointment = session.get(Appointment, UUID(appointment_id))
            slot = session.get(Slot, appointment.slot_id)
            return AppointmentInfo(
                status=appointment.status,
                starts_at_iso=as_utc(slot.starts_at).isoformat(),
                confirmed=appointment.confirmed_at is not None,
            )

    @activity.defn
    def prepare_reminder(self, appointment_id: str) -> ReminderPlan | None:
        """Build the reminder and, if the patient is not mid conversation, arm their reply.

        The next inbound message is then read as an answer to this reminder.
        """
        with self._session_factory() as session:
            appointment = session.get(Appointment, UUID(appointment_id))
            if appointment is None or appointment.status != "booked":
                return None
            patient = session.get(Patient, appointment.patient_id)
            if is_opted_out(session, patient.phone_number):
                return None
            slot = session.get(Slot, appointment.slot_id)
            when = as_utc(slot.starts_at).strftime("%a %b %d at %I:%M %p UTC")
            label = f"{appointment.appointment_type.replace('_', ' ')} on {when}"

            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.clinic_id == patient.clinic_id,
                    Conversation.phone_number == patient.phone_number,
                    Conversation.state != "closed",
                )
            )
            if conversation is None:
                from app.services.conversations import get_or_create_conversation

                conversation = get_or_create_conversation(session, patient.clinic_id, patient.phone_number)
            phase = (conversation.agent_state or {}).get("phase", "idle")
            if phase == "idle" and conversation.state != "escalated":
                conversation.agent_state = {
                    "phase": "awaiting_reminder_response",
                    "pending": {"action": "confirm", "appointment_id": str(appointment.id), "label": label},
                }
            body = replies.reminder(label)
            message = add_message(session, conversation, "outbound", body)
            session.commit()
            return ReminderPlan(to_number=patient.phone_number, body=body, message_id=str(message.id))

    @activity.defn
    def flag_unconfirmed(self, appointment_id: str) -> bool:
        """After the wait: write an audit entry if the patient never confirmed.
        Returns True if it was flagged."""
        with self._session_factory() as session:
            appointment = session.get(Appointment, UUID(appointment_id))
            if appointment.status != "booked" or appointment.confirmed_at is not None:
                return False
            patient = session.get(Patient, appointment.patient_id)
            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.phone_number == patient.phone_number)
                .order_by(Conversation.created_at.desc())
            )
            log_audit(
                session,
                conversation.id if conversation else None,
                "workflow",
                "reminder_unconfirmed",
                {"appointment_id": appointment_id, "flagged_at": utcnow().isoformat()},
            )
            session.commit()
            return True

    def all(self):
        return [
            self.process_message, self.send_sms, self.record_event, self.prepare_nudge,
            self.close_conversation, self.get_appointment_info, self.prepare_reminder,
            self.flag_unconfirmed,
        ]
