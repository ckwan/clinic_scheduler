"""SMS channel boundary.

The agent and workflows only talk to SmsSender. Real sends go through Twilio;
tests and evals use the in memory mock so they cost nothing and are repeatable.
"""

from dataclasses import dataclass, field
from uuid import uuid4
from typing import Protocol

from langsmith import traceable

from app.config import settings


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> str:
        """Send a message and return a provider message id."""


@dataclass
class MockSmsSender:
    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, to: str, body: str) -> str:
        self.sent.append((to, body))
        # Unique across instances and restarts: sids are stored in a unique database column.
        return f"mock-{uuid4().hex[:16]}"


def _send_inputs(inputs: dict) -> dict:
    """Trace the send without the phone number or message text."""
    to = inputs.get("to", "")
    return {"to": "***" + to[-4:], "characters": len(inputs.get("body", ""))}


class TwilioSmsSender:
    def __init__(self) -> None:
        from twilio.rest import Client

        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    @traceable(name="twilio_send_sms", run_type="tool", process_inputs=_send_inputs)
    def send(self, to: str, body: str) -> str:
        extra = {}
        if settings.public_base_url:  # lets Twilio report delivered / failed back to us
            extra["status_callback"] = settings.public_base_url.rstrip("/") + "/webhooks/twilio/status"
        message = self._client.messages.create(
            to=to, from_=settings.twilio_from_number, body=body, **extra
        )
        return message.sid


def get_sms_sender() -> SmsSender:
    return MockSmsSender() if settings.sms_mock else TwilioSmsSender()
