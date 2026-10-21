"""SMS opt-out (STOP / START) handling. Carriers and US rules expect these keywords to work.

Matching is on the whole message, so "please stop texting me about parking" is NOT an opt-out
but "STOP" is. A bare "CANCEL" is also a Twilio opt-out keyword; we deliberately do not treat it
as one here because for a scheduler it means "cancel my appointment" (and never does anything
without identity verification). Reminder texts therefore never tell patients to reply CANCEL.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SmsOptOut

STOP_WORDS = {"stop", "stopall", "unsubscribe", "quit", "end"}
START_WORDS = {"start", "unstop"}
HELP_WORDS = {"help", "info"}

HELP_REPLY = (
    "Clinic Scheduler appointment texts. Reply with what you need, such as booking or changing an "
    "appointment. Reply STOP to stop receiving texts, START to resume."
)
STOP_REPLY = "You're unsubscribed and won't get more texts from us. Reply START to resume."
START_REPLY = "You're subscribed again. How can I help with your appointment?"


def keyword(body: str) -> str | None:
    """Return 'stop', 'start', 'help', or None."""
    word = re.sub(r"[^a-z]", "", body.strip().lower())
    if word in STOP_WORDS:
        return "stop"
    if word in START_WORDS:
        return "start"
    if word in HELP_WORDS:
        return "help"
    return None


def is_opted_out(session: Session, phone_number: str) -> bool:
    return session.scalar(select(SmsOptOut.id).where(SmsOptOut.phone_number == phone_number)) is not None


def opt_out(session: Session, phone_number: str, source: str = "keyword") -> None:
    if not is_opted_out(session, phone_number):
        session.add(SmsOptOut(phone_number=phone_number, source=source))
        session.flush()


def opt_in(session: Session, phone_number: str) -> None:
    row = session.scalar(select(SmsOptOut).where(SmsOptOut.phone_number == phone_number))
    if row:
        session.delete(row)
        session.flush()
