"""Delivery status callbacks from Twilio for messages we sent."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Message
from app.services.conversations import log_audit
from app.services.optout import opt_out

# Callbacks can arrive out of order ("delivered" before "sent"), so a status never moves backward.
RANK = {"accepted": 0, "queued": 0, "sending": 1, "sent": 2, "delivered": 3, "undelivered": 3, "failed": 3, "read": 4}
FAILED = {"failed", "undelivered"}
# Twilio error 21610: the recipient replied STOP to our number. (From Twilio's error docs; worth
# confirming against the current list when you test live.)
RECIPIENT_OPTED_OUT = "21610"


def record_delivery_status(
    session: Session, sid: str, status: str, error_code: str | None = None, to_number: str | None = None
) -> bool:
    """Returns True if a message of ours was updated. Unknown ids are ignored."""
    message = session.scalar(select(Message).where(Message.twilio_sid == sid))
    if message is None or message.direction != "outbound":
        return False

    status = status.lower()
    if RANK.get(status, 0) >= RANK.get(message.delivery_status or "", -1):
        changed = message.delivery_status != status
        message.delivery_status = status
        if error_code:
            message.delivery_error_code = error_code
        if changed and status in FAILED:
            log_audit(
                session, message.conversation_id, "twilio", "sms_delivery_failed",
                {"message_id": str(message.id), "status": status, "error_code": error_code},
            )
    if error_code == RECIPIENT_OPTED_OUT and to_number:
        opt_out(session, to_number, source="twilio_error")
    session.flush()
    return True
