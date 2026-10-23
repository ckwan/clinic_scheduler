from xml.sax.saxutils import escape

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dashboard import router as dashboard_router
from app.booking.api import public_router as booking_public_router
from app.booking.api import staff_router as booking_staff_router
from app.agent.llm import LLM
from app.agent.providers import make_llm
from app.agent.runner import handle_inbound_message
from app.db import get_session
from app.channels.twilio_security import verified_form
from app.config import settings
from app.models import Clinic
from app.services import optout
from app.services.conversations import get_or_create_conversation
from app.services.delivery import record_delivery_status

app = FastAPI(title="Clinic Scheduler", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.include_router(dashboard_router)
app.include_router(booking_staff_router)
app.include_router(booking_public_router)


def get_llm() -> LLM:
    return make_llm()  # provider comes from LLM_PROVIDER


def twiml(reply: str | None) -> Response:
    body = f"<Message>{escape(reply)}</Message>" if reply else ""
    return Response(content=f"<Response>{body}</Response>", media_type="application/xml")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def get_temporal_client():
    from app.workflows.client import connect

    return await connect()


def _field(params: dict[str, str], name: str) -> str:
    value = params.get(name)
    if not value:
        raise HTTPException(status_code=400, detail=f"Missing {name}")
    return value


@app.post("/webhooks/twilio/sms")
async def twilio_sms_webhook(
    params: dict[str, str] = Depends(verified_form),
    session: Session = Depends(get_session),
    llm: LLM = Depends(get_llm),
) -> Response:
    """Inbound SMS from Twilio (signature checked by verified_form).

    STOP, START and HELP are handled here and never reach the agent. A number that has
    opted out is ignored until it texts START.

    REPLY_MODE=twiml: run the agent now and reply in the response (simple, no Temporal).
    REPLY_MODE=temporal: hand the message to the conversation's workflow and return at once.
    The workflow sends the reply, so retries, timers and reminders are durable.
    """
    sender, to, body = _field(params, "From"), _field(params, "To"), params.get("Body", "")
    sid = params.get("MessageSid")

    clinic = session.scalar(select(Clinic).where(Clinic.phone_number == to))
    if clinic is None:
        return twiml(None)

    word = optout.keyword(body)
    if word == "stop":
        optout.opt_out(session, sender)
        session.commit()
        return twiml(None)  # Twilio sends its own unsubscribe confirmation; a second one is noise
    if word == "start":
        optout.opt_in(session, sender)
        session.commit()
        return twiml(optout.START_REPLY)
    if word == "help":
        return twiml(optout.HELP_REPLY)
    if optout.is_opted_out(session, sender):
        return twiml(None)

    if settings.reply_mode == "temporal":
        from app.workflows.client import deliver_inbound

        conversation = get_or_create_conversation(session, clinic.id, sender)
        session.commit()  # the workflow's activities read this row
        client = await get_temporal_client()
        await deliver_inbound(client, str(conversation.id), str(clinic.id), body, sid)
        return twiml(None)

    reply = await run_in_threadpool(handle_inbound_message, session, clinic, sender, body, llm)
    session.commit()
    return twiml(reply)


@app.post("/webhooks/twilio/status")
def twilio_status_callback(
    params: dict[str, str] = Depends(verified_form), session: Session = Depends(get_session)
) -> Response:
    """Twilio reports whether a text we sent was delivered."""
    record_delivery_status(
        session,
        _field(params, "MessageSid"),
        _field(params, "MessageStatus"),
        params.get("ErrorCode"),
        params.get("To"),
    )
    session.commit()
    return Response(status_code=204)
