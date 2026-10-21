from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import models
from app.services import scheduling
from app.services.conversations import get_or_create_conversation
from app.services.errors import (
    AppointmentNotFound,
    InvalidRequest,
    NotVerified,
    SlotUnavailable,
)
from tests.conftest import NOW

TAYLOR = ("Taylor", "Morgan", date(1988, 4, 12))


def book(session, convo, slot, key="key-0001-abcd", kind="therapy_session"):
    return scheduling.book_appointment(session, convo, slot.id, kind, key, now=NOW)


# ------------------------------------------------------------ conversations


def test_conversation_links_known_patient_by_phone(session, world, conversation):
    assert conversation.patient_id == world.taylor.id
    assert conversation.verified is False


def test_conversation_for_unknown_number_has_no_patient(session, world):
    convo = get_or_create_conversation(session, world.clinic.id, "+15559999999")
    assert convo.patient_id is None


def test_same_number_reuses_open_conversation(session, world, conversation):
    again = get_or_create_conversation(session, world.clinic.id, world.taylor.phone_number)
    assert again.id == conversation.id


# ------------------------------------------------------------ verification


def test_verify_success_is_case_insensitive(session, conversation):
    result = scheduling.verify_identity(session, conversation, " taylor ", "MORGAN", date(1988, 4, 12))
    assert result.verified and not result.escalated
    assert conversation.verified is True


def test_verify_wrong_dob_fails_and_counts_attempt(session, conversation):
    result = scheduling.verify_identity(session, conversation, "Taylor", "Morgan", date(1990, 1, 1))
    assert not result.verified
    assert result.attempts_remaining == 1
    assert conversation.verified is False


def test_two_failures_escalate(session, conversation):
    scheduling.verify_identity(session, conversation, "Taylor", "Morgan", date(1990, 1, 1))
    result = scheduling.verify_identity(session, conversation, "Taylor", "Wrong", TAYLOR[2])
    assert result.escalated and not result.verified
    assert conversation.state == "escalated"
    escalations = session.scalars(select(models.Escalation)).all()
    assert [e.reason for e in escalations] == ["verification_failed"]


def test_escalated_conversation_cannot_verify_later(session, conversation):
    scheduling.verify_identity(session, conversation, "x", "y", date(2000, 1, 1))
    scheduling.verify_identity(session, conversation, "x", "y", date(2000, 1, 1))
    result = scheduling.verify_identity(session, conversation, *TAYLOR)
    assert not result.verified and result.escalated


def test_correct_details_from_someone_elses_phone_do_not_verify(session, world):
    """Knowing Taylor's name and birthday is not enough from an unrecognized number."""
    stranger = get_or_create_conversation(session, world.clinic.id, "+15558888888")
    result = scheduling.verify_identity(session, stranger, *TAYLOR)
    assert not result.verified


# ------------------------------------------------------------ availability


def test_availability_matches_provider_type_and_orders(session, world, conversation):
    slots = scheduling.check_availability(session, conversation, "therapy_session", now=NOW)
    assert [s.slot_id for s in slots] == [s.id for s in world.therapy_slots]
    assert all(s.provider_name == "Dr. Chen" for s in slots)


def test_availability_for_wellness_visit_uses_primary_care(session, world, conversation):
    slots = scheduling.check_availability(session, conversation, "annual_wellness_visit", now=NOW)
    assert [s.slot_id for s in slots] == [world.pcp_slot.id]


def test_availability_excludes_booked_past_and_other_clinics(session, world, verified_conversation):
    book(session, verified_conversation, world.therapy_slots[0])
    ids = {s.slot_id for s in scheduling.check_availability(session, verified_conversation, "therapy_session", now=NOW)}
    assert world.therapy_slots[0].id not in ids
    assert world.past_slot.id not in ids
    assert world.other_clinic_slot.id not in ids


def test_availability_respects_limit_and_window(session, world, conversation):
    one = scheduling.check_availability(session, conversation, "therapy_session", limit=1, now=NOW)
    assert len(one) == 1
    windowed = scheduling.check_availability(
        session, conversation, "therapy_session", latest=world.therapy_slots[1].starts_at, now=NOW
    )
    assert [s.slot_id for s in windowed] == [world.therapy_slots[0].id, world.therapy_slots[1].id]


def test_availability_does_not_need_verification(session, conversation):
    assert scheduling.check_availability(session, conversation, "therapy_session", now=NOW)


def test_availability_rejects_unknown_type(session, conversation):
    with pytest.raises(InvalidRequest):
        scheduling.check_availability(session, conversation, "dental_cleaning", now=NOW)


# ------------------------------------------------------------ booking


def test_booking_requires_verification(session, world, conversation):
    with pytest.raises(NotVerified):
        book(session, conversation, world.therapy_slots[0])


def test_booking_marks_slot_and_logs_audit(session, world, verified_conversation):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    assert appt.status == "booked" and appt.patient_id == world.taylor.id
    assert world.therapy_slots[0].status == "booked"
    actions = [a.action for a in session.scalars(select(models.AuditLog))]
    assert "appointment_booked" in actions
    assert verified_conversation.outcome == "booked"


def test_same_idempotency_key_returns_same_appointment(session, world, verified_conversation):
    first = book(session, verified_conversation, world.therapy_slots[0], key="retry-key-0001")
    second = book(session, verified_conversation, world.therapy_slots[0], key="retry-key-0001")
    assert first.id == second.id
    assert len(session.scalars(select(models.Appointment)).all()) == 1


def test_idempotency_key_cannot_be_reused_by_another_patient(
    session, world, verified_conversation, riley_conversation
):
    book(session, verified_conversation, world.therapy_slots[0], key="shared-key-001")
    with pytest.raises(InvalidRequest):
        book(session, riley_conversation, world.therapy_slots[1], key="shared-key-001")


def test_cannot_book_wrong_type_past_or_other_clinic_slot(session, world, verified_conversation):
    with pytest.raises(SlotUnavailable):
        book(session, verified_conversation, world.pcp_slot, key="k-wrong-type1")
    with pytest.raises(SlotUnavailable):
        book(session, verified_conversation, world.past_slot, key="k-past-slot01")
    with pytest.raises(SlotUnavailable):
        book(session, verified_conversation, world.other_clinic_slot, key="k-other-clin1")


def test_cannot_book_unknown_slot(session, verified_conversation):
    with pytest.raises(SlotUnavailable):
        scheduling.book_appointment(
            session, verified_conversation, uuid4(), "therapy_session", "k-unknown-001", now=NOW
        )


def test_second_patient_cannot_take_a_booked_slot(
    session, world, verified_conversation, riley_conversation
):
    book(session, verified_conversation, world.therapy_slots[0], key="taylor-key-001")
    with pytest.raises(SlotUnavailable):
        book(session, riley_conversation, world.therapy_slots[0], key="riley-key-0001")


def test_database_blocks_two_active_appointments_on_one_slot(session, world, verified_conversation):
    """Backstop for races: even if the status check is bypassed, the unique index holds."""
    slot = world.therapy_slots[0]
    book(session, verified_conversation, slot)
    session.add(
        models.Appointment(
            patient_id=world.riley.id,
            slot_id=slot.id,
            appointment_type="therapy_session",
            idempotency_key="sneaky-key-0001",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


# ------------------------------------------------------------ cancel


def test_cancel_frees_slot_and_is_idempotent(session, world, verified_conversation):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    scheduling.cancel_appointment(session, verified_conversation, appt.id)
    scheduling.cancel_appointment(session, verified_conversation, appt.id)
    assert appt.status == "cancelled"
    assert world.therapy_slots[0].status == "open"
    # and the slot can be booked again
    again = book(session, verified_conversation, world.therapy_slots[0], key="rebook-key-001")
    assert again.status == "booked"


def test_cancel_requires_verification(session, world, verified_conversation, conversation):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    verified_conversation.verified = False
    with pytest.raises(NotVerified):
        scheduling.cancel_appointment(session, verified_conversation, appt.id)


def test_cannot_cancel_another_patients_appointment(
    session, world, verified_conversation, riley_conversation
):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    with pytest.raises(AppointmentNotFound):
        scheduling.cancel_appointment(session, riley_conversation, appt.id)
    assert appt.status == "booked"


def test_missing_and_foreign_appointments_look_identical(
    session, world, verified_conversation, riley_conversation
):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    with pytest.raises(AppointmentNotFound) as foreign:
        scheduling.cancel_appointment(session, riley_conversation, appt.id)
    with pytest.raises(AppointmentNotFound) as missing:
        scheduling.cancel_appointment(session, riley_conversation, uuid4())
    assert foreign.value.message == missing.value.message


# ------------------------------------------------------------ reschedule


def test_reschedule_keeps_appointment_id_and_swaps_slots(session, world, verified_conversation):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    original_id = appt.id
    moved = scheduling.reschedule_appointment(
        session, verified_conversation, appt.id, world.therapy_slots[2].id, now=NOW
    )
    assert moved.id == original_id
    assert moved.slot_id == world.therapy_slots[2].id
    assert world.therapy_slots[0].status == "open"
    assert world.therapy_slots[2].status == "booked"
    assert verified_conversation.outcome == "rescheduled"


def test_reschedule_to_same_slot_is_a_noop(session, world, verified_conversation):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    same = scheduling.reschedule_appointment(
        session, verified_conversation, appt.id, world.therapy_slots[0].id, now=NOW
    )
    assert same.slot_id == world.therapy_slots[0].id


def test_reschedule_to_taken_slot_fails_and_changes_nothing(
    session, world, verified_conversation, riley_conversation
):
    mine = book(session, verified_conversation, world.therapy_slots[0], key="taylor-key-001")
    book(session, riley_conversation, world.therapy_slots[1], key="riley-key-0001")
    with pytest.raises(SlotUnavailable):
        scheduling.reschedule_appointment(
            session, verified_conversation, mine.id, world.therapy_slots[1].id, now=NOW
        )
    assert mine.slot_id == world.therapy_slots[0].id
    assert world.therapy_slots[0].status == "booked"


def test_cannot_reschedule_another_patients_appointment(
    session, world, verified_conversation, riley_conversation
):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    with pytest.raises(AppointmentNotFound):
        scheduling.reschedule_appointment(
            session, riley_conversation, appt.id, world.therapy_slots[1].id, now=NOW
        )


def test_cannot_reschedule_cancelled_appointment(session, world, verified_conversation):
    appt = book(session, verified_conversation, world.therapy_slots[0])
    scheduling.cancel_appointment(session, verified_conversation, appt.id)
    with pytest.raises(InvalidRequest):
        scheduling.reschedule_appointment(
            session, verified_conversation, appt.id, world.therapy_slots[1].id, now=NOW
        )


# ------------------------------------------------------------ listing


def test_list_shows_only_own_upcoming_booked_appointments(
    session, world, verified_conversation, riley_conversation
):
    mine = book(session, verified_conversation, world.therapy_slots[0], key="taylor-key-001")
    gone = book(session, verified_conversation, world.therapy_slots[1], key="taylor-key-002")
    scheduling.cancel_appointment(session, verified_conversation, gone.id)
    book(session, riley_conversation, world.therapy_slots[2], key="riley-key-0001")

    rows = scheduling.list_upcoming_appointments(session, verified_conversation, now=NOW)
    assert [a.id for a, _, _ in rows] == [mine.id]


def test_list_requires_verification(session, conversation):
    with pytest.raises(NotVerified):
        scheduling.list_upcoming_appointments(session, conversation, now=NOW)
