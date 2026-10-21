"""Read and review API for the staff dashboard.

Phone numbers are masked in every response. If DASHBOARD_TOKEN is set, every route needs
"Authorization: Bearer <token>". Leave it empty for local demos only.
"""

import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.channels.sms import SmsSender, get_sms_sender
from app.config import settings
from app.db import get_session
from app.agent import replies
from app.models import Appointment, AuditLog, Clinic, Conversation, Escalation, Message, Patient, Provider, Slot
from app.services import optout
from app.services.conversations import add_message, as_utc, get_or_create_conversation, log_audit, utcnow

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parents[3] / "evals" / "results"
SUCCESS_OUTCOMES = {"booked", "cancelled", "rescheduled"}


def require_token(authorization: str = Header(default="")) -> None:
    if not settings.dashboard_token:
        return
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, settings.dashboard_token):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def mask_phone(number: str) -> str:
    return "***-" + number[-4:] if len(number) >= 4 else "***"


def iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def _patient_name(session: Session, conversation: Conversation) -> str | None:
    if not conversation.patient_id:
        return None
    patient = session.get(Patient, conversation.patient_id)
    return f"{patient.first_name} {patient.last_name}" if patient else None


def _last_message(session: Session, conversation_id: UUID) -> Message | None:
    return session.scalar(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at.desc())
    )


def _conversation_row(session: Session, c: Conversation) -> dict:
    last = _last_message(session, c.id)
    return {
        "id": str(c.id),
        "phone": mask_phone(c.phone_number),
        "patient": _patient_name(session, c),
        "state": c.state,
        "outcome": c.outcome,
        "verified": c.verified,
        "phase": (c.agent_state or {}).get("phase", "idle"),
        "last_message": last.body[:120] if last else None,
        "updated_at": iso(last.created_at if last else c.created_at),
    }


# ------------------------------------------------------------ overview


@router.get("/summary")
def summary(session: Session = Depends(get_session)) -> dict:
    total = session.scalar(select(func.count()).select_from(Conversation)) or 0
    by_state = dict(session.execute(select(Conversation.state, func.count()).group_by(Conversation.state)).all())
    by_outcome = dict(
        session.execute(
            select(Conversation.outcome, func.count())
            .where(Conversation.outcome.is_not(None))
            .group_by(Conversation.outcome)
        ).all()
    )
    open_escalations = session.scalar(
        select(func.count()).select_from(Escalation).where(Escalation.status != "resolved")
    ) or 0
    escalated = by_outcome.get("escalated", 0)
    completed = sum(n for k, n in by_outcome.items() if k in SUCCESS_OUTCOMES)
    return {
        "conversations": total,
        "by_state": by_state,
        "by_outcome": by_outcome,
        "open_escalations": open_escalations,
        "completion_rate": round(completed / total, 3) if total else None,
        "escalation_rate": round(escalated / total, 3) if total else None,
    }


# ------------------------------------------------------------ conversations


@router.get("/conversations")
def list_conversations(
    state: str | None = None, limit: int = 50, session: Session = Depends(get_session)
) -> list[dict]:
    query = select(Conversation).order_by(Conversation.updated_at.desc()).limit(min(limit, 200))
    if state:
        query = query.where(Conversation.state == state)
    return [_conversation_row(session, c) for c in session.scalars(query)]


@router.get("/conversations/{conversation_id}")
def conversation_detail(conversation_id: UUID, session: Session = Depends(get_session)) -> dict:
    c = session.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    audit = list(
        session.scalars(
            select(AuditLog).where(AuditLog.conversation_id == c.id).order_by(AuditLog.created_at)
        )
    )
    staff_message_ids = {a.detail.get("message_id") for a in audit if a.action == "human_reply" and a.detail}
    messages = session.scalars(
        select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at)
    )
    escalations = session.scalars(
        select(Escalation).where(Escalation.conversation_id == c.id).order_by(Escalation.created_at)
    )
    return {
        **_conversation_row(session, c),
        "langsmith_trace_id": c.langsmith_trace_id,
        "messages": [
            {
                "id": str(m.id),
                "sender": "patient" if m.direction == "inbound"
                else "staff" if str(m.id) in staff_message_ids else "agent",
                "body": m.body,
                "at": iso(m.created_at),
                "delivery": m.delivery_status,
                "delivery_error": m.delivery_error_code,
            }
            for m in messages
        ],
        "escalations": [_escalation_row(e) for e in escalations],
        "audit": [{"action": a.action, "actor": a.actor, "detail": a.detail, "at": iso(a.created_at)} for a in audit],
    }


class StaffReply(BaseModel):
    body: str = Field(min_length=1, max_length=1000)
    staff_name: str = Field(default="staff", max_length=100)


@router.post("/conversations/{conversation_id}/reply")
def staff_reply(
    conversation_id: UUID,
    reply: StaffReply,
    session: Session = Depends(get_session),
    sms: SmsSender = Depends(get_sms_sender),
) -> dict:
    """A human answers the patient. Only allowed while the conversation is escalated, so staff
    cannot talk over the agent in a conversation the agent still owns."""
    c = session.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if c.state != "escalated":
        raise HTTPException(status_code=409, detail="Take over the conversation first (it is not escalated)")

    sms.send(c.phone_number, reply.body)  # raises before we record anything if the send fails
    message = add_message(session, c, "outbound", reply.body)
    log_audit(session, c.id, f"staff:{reply.staff_name}", "human_reply", {"message_id": str(message.id)})
    session.commit()
    return {"id": str(message.id)}


# ------------------------------------------------------------ escalations


def _escalation_row(e: Escalation) -> dict:
    return {
        "id": str(e.id),
        "conversation_id": str(e.conversation_id),
        "reason": e.reason,
        "status": e.status,
        "assigned_to": e.assigned_to,
        "created_at": iso(e.created_at),
        "resolved_at": iso(e.resolved_at),
    }


@router.get("/escalations")
def list_escalations(status: str | None = None, session: Session = Depends(get_session)) -> list[dict]:
    query = select(Escalation).order_by(Escalation.created_at.desc())
    if status == "active":
        query = query.where(Escalation.status != "resolved")
    elif status:
        query = query.where(Escalation.status == status)
    rows = []
    for e in session.scalars(query):
        c = session.get(Conversation, e.conversation_id)
        rows.append({**_escalation_row(e), "phone": mask_phone(c.phone_number), "patient": _patient_name(session, c)})
    return rows


class Take(BaseModel):
    staff_name: str = Field(min_length=1, max_length=100)


def _get_escalation(session: Session, escalation_id: UUID) -> Escalation:
    e = session.get(Escalation, escalation_id)
    if e is None:
        raise HTTPException(status_code=404, detail="Escalation not found")
    return e


@router.post("/escalations/{escalation_id}/take")
def take_escalation(escalation_id: UUID, body: Take, session: Session = Depends(get_session)) -> dict:
    e = _get_escalation(session, escalation_id)
    if e.status == "resolved":
        raise HTTPException(status_code=409, detail="Already resolved")
    if e.status == "taken" and e.assigned_to != body.staff_name:
        raise HTTPException(status_code=409, detail=f"Already taken by {e.assigned_to}")
    e.status, e.assigned_to = "taken", body.staff_name
    log_audit(session, e.conversation_id, f"staff:{body.staff_name}", "escalation_taken", {"escalation_id": str(e.id)})
    session.commit()
    return _escalation_row(e)


@router.post("/escalations/{escalation_id}/resolve")
def resolve_escalation(escalation_id: UUID, session: Session = Depends(get_session)) -> dict:
    """Close the escalation and the conversation, so the patient's next text starts fresh."""
    e = _get_escalation(session, escalation_id)
    if e.status == "resolved":
        return _escalation_row(e)
    e.status, e.resolved_at = "resolved", utcnow()
    c = session.get(Conversation, e.conversation_id)
    c.state, c.agent_state = "closed", {}
    log_audit(session, c.id, f"staff:{e.assigned_to or 'staff'}", "escalation_resolved", {"escalation_id": str(e.id)})
    session.commit()
    return _escalation_row(e)


# ------------------------------------------------------------ eval results (from evals/results/*.json)


def _results_dir() -> Path:
    return Path(settings.eval_results_dir) if settings.eval_results_dir else DEFAULT_RESULTS_DIR


@router.get("/evals")
def list_eval_runs() -> list[dict]:
    runs = []
    for path in sorted(_results_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text())
            runs.append({"label": data["label"], "model": data["model"], "created_at": data["created_at"],
                         "metrics": data["metrics"]})
        except (ValueError, KeyError):
            continue  # not a run file
    runs.sort(key=lambda r: r["created_at"], reverse=True)
    return runs


@router.get("/evals/{label}")
def eval_run(label: str) -> dict:
    if not re.fullmatch(r"[\w.-]+", label):
        raise HTTPException(status_code=404, detail="Run not found")
    path = _results_dir() / f"{label}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Run not found")
    return json.loads(path.read_text())


# ------------------------------------------------------------ appointments


STATUS_FILTERS = {"all", "booked", "confirmed", "unconfirmed", "cancelled"}


def _clinic_tz(session: Session) -> ZoneInfo:
    """The dashboard shows one clinic, so day filters use the first clinic's timezone."""
    clinic = session.scalar(select(Clinic).order_by(Clinic.created_at))
    return ZoneInfo(clinic.timezone if clinic else "UTC")


@router.get("/appointments")
def list_appointments(
    day: str | None = None,
    status: str = "all",
    limit: int = 200,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Appointments, soonest first. day is YYYY-MM-DD in the clinic's timezone. status is one
    of all, booked, confirmed (booked and the patient confirmed), unconfirmed, cancelled."""
    if status not in STATUS_FILTERS:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(STATUS_FILTERS)}")

    query = (
        select(Appointment, Slot, Provider, Patient)
        .join(Slot, Appointment.slot_id == Slot.id)
        .join(Provider, Slot.provider_id == Provider.id)
        .join(Patient, Appointment.patient_id == Patient.id)
        .order_by(Slot.starts_at)
        .limit(min(limit, 500))
    )
    if day:
        try:
            start_local = datetime.fromisoformat(day).replace(tzinfo=_clinic_tz(session))
        except ValueError:
            raise HTTPException(status_code=422, detail="day must look like 2026-10-21") from None
        start = start_local.astimezone(timezone.utc)
        end = (start_local + timedelta(days=1)).astimezone(timezone.utc)
        query = query.where(Slot.starts_at >= start, Slot.starts_at < end)
    if status == "booked":
        query = query.where(Appointment.status == "booked")
    elif status == "confirmed":
        query = query.where(Appointment.status == "booked", Appointment.confirmed_at.is_not(None))
    elif status == "unconfirmed":
        query = query.where(Appointment.status == "booked", Appointment.confirmed_at.is_(None))
    elif status == "cancelled":
        query = query.where(Appointment.status == "cancelled")

    # Which conversation created each appointment: the audit entry written when it was booked.
    booked_via: dict[str, str] = {}
    for entry in session.scalars(select(AuditLog).where(AuditLog.action == "appointment_booked")):
        if entry.detail and entry.conversation_id:
            booked_via[entry.detail["appointment_id"]] = str(entry.conversation_id)

    rows = []
    for appointment, slot, provider, patient in session.execute(query):
        rows.append(
            {
                "id": str(appointment.id),
                "patient": f"{patient.first_name} {patient.last_name}",
                "phone": mask_phone(patient.phone_number),
                "type": appointment.appointment_type,
                "provider": provider.name,
                "starts_at": iso(slot.starts_at),
                "status": appointment.status,
                "confirmed": appointment.confirmed_at is not None,
                "confirmed_at": iso(appointment.confirmed_at),
                "conversation_id": booked_via.get(str(appointment.id)),  # None if booked another way
            }
        )
    return rows


# ------------------------------------------------------------ patients and outreach


@router.get("/patients")
def list_patients(session: Session = Depends(get_session)) -> list[dict]:
    now = utcnow()
    rows = []
    for patient in session.scalars(select(Patient).order_by(Patient.last_name, Patient.first_name)):
        upcoming = session.execute(
            select(Appointment, Slot)
            .join(Slot, Appointment.slot_id == Slot.id)
            .where(
                Appointment.patient_id == patient.id,
                Appointment.status == "booked",
                Slot.starts_at >= now,
            )
            .order_by(Slot.starts_at)
        ).first()
        last_outreach = session.scalar(
            select(AuditLog)
            .join(Conversation, AuditLog.conversation_id == Conversation.id)
            .where(Conversation.phone_number == patient.phone_number, AuditLog.action == "outreach_sent")
            .order_by(AuditLog.created_at.desc())
        )
        rows.append(
            {
                "id": str(patient.id),
                "name": f"{patient.first_name} {patient.last_name}",
                "phone": mask_phone(patient.phone_number),
                "opted_out": optout.is_opted_out(session, patient.phone_number),
                "next_appointment": iso(upcoming[1].starts_at) if upcoming else None,
                "last_outreach_at": iso(last_outreach.created_at) if last_outreach else None,
            }
        )
    return rows


class Outreach(BaseModel):
    staff_name: str = Field(default="staff", min_length=1, max_length=100)


OUTREACH_COOLDOWN = timedelta(hours=24)


@router.post("/patients/{patient_id}/outreach")
def text_patient(
    patient_id: UUID,
    body: Outreach,
    session: Session = Depends(get_session),
    sms: SmsSender = Depends(get_sms_sender),
) -> dict:
    """Send the first text of a conversation. Guards, in order: the patient must exist, must not
    have opted out, must not be in a conversation a human owns, and must not have been texted
    in the last 24 hours (so a double click or two staff members cannot text them twice)."""
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    if optout.is_opted_out(session, patient.phone_number):
        raise HTTPException(status_code=409, detail="This patient opted out of texts")

    conversation = get_or_create_conversation(session, patient.clinic_id, patient.phone_number)
    if conversation.state == "escalated":
        raise HTTPException(status_code=409, detail="A staff member already owns this conversation")

    recent = session.scalar(
        select(AuditLog).where(
            AuditLog.conversation_id == conversation.id,
            AuditLog.action == "outreach_sent",
            AuditLog.created_at >= utcnow() - OUTREACH_COOLDOWN,
        )
    )
    if recent:
        raise HTTPException(status_code=409, detail="This patient was already texted in the last 24 hours")

    sid = sms.send(patient.phone_number, replies.OUTREACH)  # raises before anything is recorded
    message = add_message(session, conversation, "outbound", replies.OUTREACH)
    message.twilio_sid, message.delivery_status = sid, "queued"
    log_audit(
        session, conversation.id, f"staff:{body.staff_name}", "outreach_sent", {"message_id": str(message.id)}
    )
    session.commit()
    return {"conversation_id": str(conversation.id), "message_id": str(message.id)}
