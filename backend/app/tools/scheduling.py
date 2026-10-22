"""The tool boundary the agent calls.

Every call takes raw arguments (as the model produced them), validates them with Pydantic,
runs the service layer, and returns a ToolResult. Errors become results with a stable
`error_code`, so the agent can branch on them instead of parsing text. Transaction control
(commit or rollback) belongs to the caller.
"""

from datetime import date, datetime
from typing import Any, Callable, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from langsmith import trace
from sqlalchemy.orm import Session

from app.models import Conversation
from app.services import scheduling
from app.services.errors import SchedulingError

AppointmentType = Literal["therapy_session", "checkup", "annual_wellness_visit"]


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None


class VerifyIdentityArgs(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: date


class CheckAvailabilityArgs(BaseModel):
    appointment_type: AppointmentType
    earliest: datetime | None = None
    latest: datetime | None = None
    limit: int = Field(default=5, ge=1, le=10)


class BookAppointmentArgs(BaseModel):
    slot_id: UUID
    appointment_type: AppointmentType
    idempotency_key: str = Field(min_length=8, max_length=100)


class CancelAppointmentArgs(BaseModel):
    appointment_id: UUID


class RescheduleAppointmentArgs(BaseModel):
    appointment_id: UUID
    new_slot_id: UUID


class ConfirmAttendanceArgs(BaseModel):
    appointment_id: UUID


class ListAppointmentsArgs(BaseModel):
    pass


def _appointment_dict(appointment, slot=None, provider=None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "appointment_id": str(appointment.id),
        "appointment_type": appointment.appointment_type,
        "status": appointment.status,
    }
    if slot:
        data["starts_at"] = scheduling.as_utc(slot.starts_at).isoformat()
    if provider:
        data["provider"] = provider.name
    return data


def _verify_identity(
    session: Session, conversation: Conversation, args: VerifyIdentityArgs, now: datetime | None = None
) -> ToolResult:
    result = scheduling.verify_identity(
        session, conversation, args.first_name, args.last_name, args.date_of_birth
    )
    data = {
        "verified": result.verified,
        "attempts_remaining": result.attempts_remaining,
        "escalated": result.escalated,
    }
    if result.verified:
        return ToolResult(ok=True, data=data)
    message = (
        "Handing this conversation to staff."
        if result.escalated
        else "Those details did not match our records."
    )
    return ToolResult(ok=False, data=data, error_code="verification_failed", message=message)


def _check_availability(
    session: Session, conversation: Conversation, args: CheckAvailabilityArgs, now: datetime | None = None
) -> ToolResult:
    slots = scheduling.check_availability(
        session,
        conversation,
        args.appointment_type,
        earliest=args.earliest,
        latest=args.latest,
        limit=args.limit,
        now=now,
    )
    return ToolResult(
        ok=True,
        data={
            "slots": [
                {
                    "slot_id": str(s.slot_id),
                    "provider": s.provider_name,
                    "starts_at": s.starts_at.isoformat(),
                    "ends_at": s.ends_at.isoformat(),
                }
                for s in slots
            ]
        },
    )


def _book(
    session: Session, conversation: Conversation, args: BookAppointmentArgs, now: datetime | None = None
) -> ToolResult:
    appointment = scheduling.book_appointment(
        session, conversation, args.slot_id, args.appointment_type, args.idempotency_key, now=now
    )
    return ToolResult(ok=True, data=_appointment_dict(appointment))


def _cancel(
    session: Session, conversation: Conversation, args: CancelAppointmentArgs, now: datetime | None = None
) -> ToolResult:
    appointment = scheduling.cancel_appointment(session, conversation, args.appointment_id)
    return ToolResult(ok=True, data=_appointment_dict(appointment))


def _reschedule(
    session: Session,
    conversation: Conversation,
    args: RescheduleAppointmentArgs,
    now: datetime | None = None,
) -> ToolResult:
    appointment = scheduling.reschedule_appointment(
        session, conversation, args.appointment_id, args.new_slot_id, now=now
    )
    return ToolResult(ok=True, data=_appointment_dict(appointment))


def _confirm(
    session: Session, conversation: Conversation, args: ConfirmAttendanceArgs, now: datetime | None = None
) -> ToolResult:
    appointment = scheduling.confirm_attendance(session, conversation, args.appointment_id, now=now)
    data = _appointment_dict(appointment)
    data["confirmed"] = appointment.confirmed_at is not None
    return ToolResult(ok=True, data=data)


def _list(
    session: Session, conversation: Conversation, args: ListAppointmentsArgs, now: datetime | None = None
) -> ToolResult:
    rows = scheduling.list_upcoming_appointments(session, conversation, now=now)
    return ToolResult(
        ok=True, data={"appointments": [_appointment_dict(a, s, p) for a, s, p in rows]}
    )


# name -> (arguments model, handler)
TOOLS: dict[str, tuple[type[BaseModel], Callable[..., ToolResult]]] = {
    "verify_identity": (VerifyIdentityArgs, _verify_identity),
    "check_availability": (CheckAvailabilityArgs, _check_availability),
    "book_appointment": (BookAppointmentArgs, _book),
    "cancel_appointment": (CancelAppointmentArgs, _cancel),
    "reschedule_appointment": (RescheduleAppointmentArgs, _reschedule),
    "confirm_attendance": (ConfirmAttendanceArgs, _confirm),
    "list_appointments": (ListAppointmentsArgs, _list),
}


def tool_schemas() -> list[dict[str, Any]]:
    """JSON schemas for handing the tools to a model."""
    return [
        {"name": name, "input_schema": model.model_json_schema()}
        for name, (model, _) in TOOLS.items()
    ]


def execute_tool(
    session: Session,
    conversation: Conversation,
    name: str,
    raw_args: dict[str, Any],
    now: datetime | None = None,
) -> ToolResult:
    """`now` is for tests and evals that need a fixed clock. Production leaves it unset."""
    if name not in TOOLS:
        return ToolResult(ok=False, error_code="unknown_tool", message=f"Unknown tool: {name}")

    with trace(name=f"tool:{name}", run_type="tool", inputs=_traced_args(raw_args)) as run:
        result = _execute(session, conversation, name, raw_args, now)
        run.end(outputs={"ok": result.ok, "error_code": result.error_code, "message": result.message})
        return result


REDACTED_ARGS = {"first_name", "last_name", "date_of_birth"}


def _traced_args(raw_args: dict[str, Any]) -> dict[str, Any]:
    """Tool arguments for the trace, with identity details hidden."""
    return {k: "[redacted]" if k in REDACTED_ARGS else v for k, v in raw_args.items()}


def _execute(
    session: Session, conversation: Conversation, name: str, raw_args: dict[str, Any], now: datetime | None
) -> ToolResult:
    model, handler = TOOLS[name]
    try:
        args = model.model_validate(raw_args)
    except ValidationError as exc:
        return ToolResult(
            ok=False,
            error_code="invalid_arguments",
            message="; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()),
        )

    try:
        return handler(session, conversation, args, now=now)
    except SchedulingError as exc:
        return ToolResult(ok=False, error_code=exc.code, message=exc.message)
