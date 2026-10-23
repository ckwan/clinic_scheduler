"""HTTP for the booking feature.

public_router  /public/booking/...  for patients. No login: the link is the credential, and the
                                    date of birth is checked on every request that shows or books.
staff_router   /api/...             for the dashboard (same bearer token as the rest of /api).

Opening a link (GET) never changes anything, because phones and carriers often fetch links in
the background to make a preview. Booking is a POST.
"""

import logging
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.graph import format_time
from app.api.dashboard import require_token
from app.booking import messages, outreach, ratelimit, service
from app.channels.sms import SmsSender, get_sms_sender
from app.config import settings
from app.db import get_session
from app.models import Clinic, Conversation, Patient
from app.services import optout
from app.services.conversations import add_message, log_audit, utcnow
from app.services.errors import SlotUnavailable

log = logging.getLogger(__name__)

public_router = APIRouter(prefix="/public/booking", dependencies=[Depends(ratelimit.limit)])
staff_router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

PROBLEM_TEXT = {
    "invalid": "This link isn't valid. Please text us and we'll help you book.",
    "expired": "This link has expired. Please text us and we'll send a new one.",
    "used": "This link was already used to book a visit.",
    "locked": "We couldn't verify your details, so we've asked our staff to contact you.",
    "needs_staff": "A member of our staff is already helping you by text. Please reply to them.",
}


def _problem(code: str) -> HTTPException:
    status = {"locked": 423, "needs_staff": 409}.get(code, 410)
    return HTTPException(status_code=status, detail={"code": code, "message": PROBLEM_TEXT[code]})


def _open_link(session: Session, token: str):
    link = service.find_link(session, token)
    if link is None:
        raise _problem("invalid")
    return link


class Identity(BaseModel):
    date_of_birth: date


class BookRequest(Identity):
    slot_id: UUID


@public_router.get("/{token}")
def open_link(token: str, session: Session = Depends(get_session)) -> dict:
    """Before the date of birth: only a first name, the clinic and the visit type."""
    link = service.find_link(session, token)
    status = service.link_status(link, utcnow())
    if status != "ok":
        return {"status": status, "message": PROBLEM_TEXT[status]}
    info = service.link_info(session, link)
    return {
        "status": "ok",
        "first_name": info.first_name,
        "clinic": info.clinic_name,
        "appointment_type": info.appointment_type,
        "appointment_name": info.appointment_name,
    }


@public_router.post("/{token}/slots")
def slots(token: str, body: Identity, session: Session = Depends(get_session)) -> dict:
    link = _open_link(session, token)
    try:
        found = service.list_slots(session, link, body.date_of_birth)
    except service.WrongDetails as exc:
        session.commit()  # keep the failed attempt
        raise HTTPException(
            status_code=403,
            detail={"code": "wrong_details", "message": exc.message, "attempts_remaining": exc.attempts_remaining},
        )
    except service.LinkProblem as exc:
        session.commit()
        raise _problem(exc.code)
    session.commit()
    patient = session.get(Patient, link.patient_id)
    clinic = session.get(Clinic, patient.clinic_id)
    return {
        "timezone": clinic.timezone,
        "slots": [
            {"slot_id": str(s.slot_id), "starts_at": s.starts_at.isoformat(), "provider": s.provider_name}
            for s in found
        ],
    }


@public_router.post("/{token}/book")
def book(
    token: str,
    body: BookRequest,
    session: Session = Depends(get_session),
    sms: SmsSender = Depends(get_sms_sender),
) -> dict:
    link = _open_link(session, token)
    try:
        booked = service.book(session, link, body.date_of_birth, body.slot_id)
    except service.WrongDetails as exc:
        session.commit()
        raise HTTPException(
            status_code=403,
            detail={"code": "wrong_details", "message": exc.message, "attempts_remaining": exc.attempts_remaining},
        )
    except service.LinkProblem as exc:
        session.commit()
        raise _problem(exc.code)
    except SlotUnavailable:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "slot_taken", "message": "Sorry, that time was just taken. Please pick another."},
        )
    session.commit()

    label = f"{format_time(booked.starts_at.isoformat(), ZoneInfo(booked.timezone))} with {booked.provider_name}"
    if not booked.already:
        _after_booking(session, sms, link, booked, label)
    return {
        "appointment_id": str(booked.appointment.id),
        "starts_at": booked.starts_at.isoformat(),
        "provider": booked.provider_name,
        "clinic": booked.clinic_name,
        "timezone": booked.timezone,
        "label": label,
    }


def _after_booking(session: Session, sms: SmsSender, link, booked, label: str) -> None:
    """Confirmation text and reminder. The booking is already saved, so a failure here is logged
    and never turns a successful booking into an error for the patient."""
    conversation = session.get(Conversation, link.conversation_id)
    patient = session.get(Patient, link.patient_id)
    try:
        if not optout.is_opted_out(session, patient.phone_number):
            body = messages.CONFIRMED_TEXT.format(label=label)
            sid = sms.send(patient.phone_number, body)
            message = add_message(session, conversation, "outbound", body)
            message.twilio_sid, message.delivery_status = sid, "queued"
        session.commit()
    except Exception:
        log.exception("confirmation text failed for appointment %s", booked.appointment.id)
        session.rollback()
        log_audit(session, conversation.id, "system", "confirmation_text_failed", None)
        session.commit()
    if settings.reply_mode == "temporal":
        _start_reminder(str(booked.appointment.id))


def _start_reminder(appointment_id: str) -> None:
    """Same reminder workflow the SMS booking path starts. Best effort."""
    import asyncio

    async def go() -> None:
        from temporalio.common import WorkflowIDReusePolicy

        from app.workflows.client import connect
        from app.workflows.models import ReminderInput

        client = await connect()
        await client.start_workflow(
            "AppointmentReminderWorkflow",
            ReminderInput(appointment_id),
            id=f"reminder-{appointment_id}",
            task_queue=settings.temporal_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )

    try:
        asyncio.run(go())
    except Exception:
        log.exception("could not start the reminder workflow for %s", appointment_id)


# ---------------------------------------------------------------- staff


class StaffLink(BaseModel):
    staff_name: str = Field(default="staff", min_length=1, max_length=100)


@staff_router.post("/patients/{patient_id}/booking-link", name="send_booking_link")
def send_booking_link_to_patient(
    patient_id: UUID,
    body: StaffLink,
    session: Session = Depends(get_session),
    sms: SmsSender = Depends(get_sms_sender),
) -> dict:
    """The dashboard's "Send booking link" button."""
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    try:
        link = outreach.send_booking_link(session, sms, patient, actor=f"staff:{body.staff_name}")
    except outreach.NotSent as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    return {"conversation_id": str(link.conversation_id), "link_id": str(link.id)}
