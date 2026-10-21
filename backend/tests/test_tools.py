from app import models
from uuid import uuid4

from app.tools.scheduling import TOOLS, execute_tool, tool_schemas
from tests.conftest import NOW


def test_every_tool_has_a_schema():
    names = {s["name"] for s in tool_schemas()}
    assert names == set(TOOLS)


def test_unknown_tool(session, conversation):
    result = execute_tool(session, conversation, "drop_table", {})
    assert not result.ok and result.error_code == "unknown_tool"


def test_invalid_arguments_are_reported_not_raised(session, conversation):
    result = execute_tool(session, conversation, "verify_identity", {"first_name": "Taylor"})
    assert not result.ok and result.error_code == "invalid_arguments"
    assert "last_name" in result.message


def test_unknown_appointment_type_is_invalid_arguments(session, conversation):
    result = execute_tool(session, conversation, "check_availability", {"appointment_type": "botox"})
    assert result.error_code == "invalid_arguments"


def test_booking_before_verification_returns_not_verified(session, world, conversation):
    result = execute_tool(
        session,
        conversation,
        "book_appointment",
        {
            "slot_id": str(world.therapy_slots[0].id),
            "appointment_type": "therapy_session",
            "idempotency_key": "tool-key-0001",
        },
        now=NOW,
    )
    assert not result.ok and result.error_code == "not_verified"


def test_failed_verification_does_not_reveal_which_field_was_wrong(session, conversation):
    result = execute_tool(
        session,
        conversation,
        "verify_identity",
        {"first_name": "Taylor", "last_name": "Morgan", "date_of_birth": "1990-01-01"},
    )
    assert result.error_code == "verification_failed"
    assert result.message == "Those details did not match our records."
    assert result.data == {"verified": False, "attempts_remaining": 1, "escalated": False}


def test_full_happy_path_through_tools(session, world, conversation):
    verify = execute_tool(
        session,
        conversation,
        "verify_identity",
        {"first_name": "Taylor", "last_name": "Morgan", "date_of_birth": "1988-04-12"},
    )
    assert verify.ok

    availability = execute_tool(
        session, conversation, "check_availability", {"appointment_type": "therapy_session"}, now=NOW
    )
    assert availability.ok
    offered = availability.data["slots"]
    assert [s["slot_id"] for s in offered] == [str(s.id) for s in world.therapy_slots]

    book = execute_tool(
        session,
        conversation,
        "book_appointment",
        {
            "slot_id": offered[0]["slot_id"],
            "appointment_type": "therapy_session",
            "idempotency_key": "tool-key-0002",
        },
        now=NOW,
    )
    assert book.ok
    appointment_id = book.data["appointment_id"]

    listed = execute_tool(session, conversation, "list_appointments", {}, now=NOW)
    assert [a["appointment_id"] for a in listed.data["appointments"]] == [appointment_id]

    moved = execute_tool(
        session,
        conversation,
        "reschedule_appointment",
        {"appointment_id": appointment_id, "new_slot_id": offered[2]["slot_id"]},
        now=NOW,
    )
    assert moved.ok and moved.data["appointment_id"] == appointment_id

    cancelled = execute_tool(
        session, conversation, "cancel_appointment", {"appointment_id": appointment_id}, now=NOW
    )
    assert cancelled.ok and cancelled.data["status"] == "cancelled"


def test_cancel_unknown_appointment_through_tool(session, verified_conversation):
    result = execute_tool(
        session, verified_conversation, "cancel_appointment", {"appointment_id": str(uuid4())}
    )
    assert not result.ok and result.error_code == "appointment_not_found"


def _book(session, conversation, world, key):
    conversation.verified = True
    session.flush()
    slot = world.therapy_slots[0]
    result = execute_tool(
        session,
        conversation,
        "book_appointment",
        {"slot_id": str(slot.id), "appointment_type": "therapy_session", "idempotency_key": key},
        now=NOW,
    )
    assert result.ok
    conversation.verified = False
    session.flush()
    return result.data["appointment_id"]


def test_confirm_attendance_needs_ownership_not_verification(session, world, conversation):
    appointment_id = _book(session, conversation, world, "confirm-key-0001")
    result = execute_tool(session, conversation, "confirm_attendance", {"appointment_id": appointment_id}, now=NOW)
    assert result.ok
    appt = session.get(models.Appointment, appointment_id if not isinstance(appointment_id, str) else __import__("uuid").UUID(appointment_id))
    assert appt.confirmed_at is not None


def test_confirm_attendance_is_idempotent(session, world, conversation):
    appointment_id = _book(session, conversation, world, "confirm-key-0002")
    first = execute_tool(session, conversation, "confirm_attendance", {"appointment_id": appointment_id}, now=NOW)
    second = execute_tool(session, conversation, "confirm_attendance", {"appointment_id": appointment_id}, now=NOW)
    assert first.ok and second.ok


def test_confirm_attendance_on_someone_elses_appointment_looks_like_not_found(
    session, world, conversation, riley_conversation
):
    appointment_id = _book(session, conversation, world, "confirm-key-0003")
    result = execute_tool(
        session, riley_conversation, "confirm_attendance", {"appointment_id": appointment_id}, now=NOW
    )
    missing = execute_tool(
        session, riley_conversation, "confirm_attendance", {"appointment_id": str(uuid4())}, now=NOW
    )
    assert not result.ok and result.error_code == missing.error_code == "appointment_not_found"
    assert result.message == missing.message


def test_confirm_attendance_rejects_cancelled_appointment(session, world, conversation):
    appointment_id = _book(session, conversation, world, "confirm-key-0004")
    conversation.verified = True
    execute_tool(session, conversation, "cancel_appointment", {"appointment_id": appointment_id}, now=NOW)
    result = execute_tool(session, conversation, "confirm_attendance", {"appointment_id": appointment_id}, now=NOW)
    assert not result.ok
