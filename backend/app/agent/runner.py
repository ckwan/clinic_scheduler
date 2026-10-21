"""Entry point for one inbound message."""

import copy
from dataclasses import dataclass, field
from datetime import datetime

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import GRAPH, AgentContext
from app.agent.llm import LLM
from app.models import AuditLog, Clinic, Conversation, Message
from app.services.conversations import add_message, get_or_create_conversation, utcnow


def _trace_inputs(inputs: dict) -> dict:
    """Keep traces free of database objects. Message bodies are logged because all data in this
    project is synthetic; see DESIGN.md for what changes for real PHI."""
    conversation = inputs.get("conversation")
    return {
        "conversation_id": str(conversation.id) if conversation else None,
        "message": inputs.get("body"),
        "phase": (conversation.agent_state or {}).get("phase") if conversation else None,
    }


@traceable(name="clinic_scheduler_turn", run_type="chain", process_inputs=_trace_inputs)
def _run_turn(
    session: Session,
    clinic: Clinic,
    conversation: Conversation,
    body: str,
    llm: LLM,
    now: datetime,
) -> str | None:
    run = get_current_run_tree()
    if run is not None:
        conversation.langsmith_trace_id = str(run.trace_id)

    state = copy.deepcopy(conversation.agent_state or {})
    ctx = AgentContext(
        session=session, clinic=clinic, conversation=conversation, llm=llm, now=now, agent_state=state
    )
    result = GRAPH.invoke({"message": body}, config={"configurable": {"ctx": ctx}})
    # Assign a new object so SQLAlchemy sees the change.
    conversation.agent_state = state

    parts = [result.get("prefix"), result.get("reply")]
    return " ".join(p for p in parts if p) or None


@dataclass
class TurnOutcome:
    reply: str | None
    new_appointment_ids: list[str] = field(default_factory=list)
    duplicate: bool = False
    reply_message_id: str | None = None


def process_inbound(
    session: Session,
    clinic: Clinic,
    conversation: Conversation,
    body: str,
    llm: LLM,
    now: datetime | None = None,
    twilio_sid: str | None = None,
) -> TurnOutcome:
    """Record one inbound message, run the agent, record the reply. The caller commits.

    Idempotent on twilio_sid: a redelivered message is not processed again and reports
    duplicate=True, so a retried Temporal activity never double books or double replies.
    """
    now = now or utcnow()
    if twilio_sid and session.scalar(select(Message.id).where(Message.twilio_sid == twilio_sid)):
        return TurnOutcome(reply=None, duplicate=True)

    add_message(session, conversation, "inbound", body, twilio_sid=twilio_sid)
    if conversation.state == "escalated":
        return TurnOutcome(reply=None)

    before = _booked_ids(session, conversation)
    reply = _run_turn(session, clinic, conversation, body, llm, now)
    reply_id = None
    if reply:
        reply_id = str(add_message(session, conversation, "outbound", reply).id)
    session.flush()
    new_ids = sorted(_booked_ids(session, conversation) - before)
    return TurnOutcome(reply=reply, new_appointment_ids=new_ids, reply_message_id=reply_id)


def _booked_ids(session: Session, conversation: Conversation) -> set[str]:
    """Appointment ids booked in this conversation, read from the audit log."""
    rows = session.scalars(
        select(AuditLog).where(
            AuditLog.conversation_id == conversation.id, AuditLog.action == "appointment_booked"
        )
    )
    return {r.detail["appointment_id"] for r in rows}


def handle_inbound_message(
    session: Session,
    clinic: Clinic,
    from_number: str,
    body: str,
    llm: LLM,
    now: datetime | None = None,
) -> str | None:
    """Synchronous path (REPLY_MODE=twiml): the reply goes back in the webhook response."""
    conversation = get_or_create_conversation(session, clinic.id, from_number)
    return process_inbound(session, clinic, conversation, body, llm, now).reply
