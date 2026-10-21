"""Plain dataclasses passed between workflows and activities (they must be serializable)."""

from dataclasses import dataclass, field


@dataclass
class InboundMessage:
    body: str
    sid: str | None = None  # Twilio MessageSid, used to drop duplicate webhook deliveries


@dataclass
class ConversationInput:
    conversation_id: str
    clinic_id: str
    nudge_after_s: int = 24 * 3600
    close_pending_after_s: int = 48 * 3600
    close_idle_after_s: int = 24 * 3600
    close_human_after_s: int = 3 * 24 * 3600


@dataclass
class TurnResult:
    reply: str | None
    to_number: str
    phase: str
    escalated: bool
    new_appointment_ids: list[str] = field(default_factory=list)
    duplicate: bool = False
    reply_message_id: str | None = None  # so the sent text's Twilio id can be stored on it


@dataclass
class ReminderInput:
    appointment_id: str
    remind_before_s: int = 24 * 3600
    confirm_wait_s: int = 12 * 3600


@dataclass
class AppointmentInfo:
    status: str
    starts_at_iso: str
    confirmed: bool


@dataclass
class ReminderPlan:
    """What prepare_reminder decided. sent=False means the patient was mid conversation."""

    to_number: str
    body: str
    message_id: str | None = None
