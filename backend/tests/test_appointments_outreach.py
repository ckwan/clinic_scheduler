from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.agent import replies
from app.channels.sms import MockSmsSender, get_sms_sender
from app.db import get_session
from app.main import app
from app.services import optout, scheduling
from app.services.conversations import escalate, get_or_create_conversation
from tests.conftest import NOW


@pytest.fixture()
def sms():
    return MockSmsSender()


@pytest.fixture()
def client(session, sms):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_sms_sender] = lambda: sms
    yield TestClient(app)
    app.dependency_overrides.clear()


def book(session, world, conversation, slot_index=0, key="appt-key-0001"):
    conversation.verified = True
    appt = scheduling.book_appointment(
        session, conversation, world.therapy_slots[slot_index].id, "therapy_session", key, now=NOW
    )
    conversation.verified = False
    return appt


# ------------------------------------------------------------ appointments


def test_appointments_show_patient_provider_and_source_conversation(client, session, world, conversation):
    appt = book(session, world, conversation)
    rows = client.get("/api/appointments").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == str(appt.id) and row["patient"] == "Taylor Morgan" and row["provider"] == "Dr. Chen"
    assert row["conversation_id"] == str(conversation.id)
    assert row["phone"] == "***-0111" and row["confirmed"] is False


def test_appointment_without_a_conversation_has_no_link(client, session, world):
    slot = world.therapy_slots[0]
    session.add(models.Appointment(
        patient_id=world.taylor.id, slot_id=slot.id, appointment_type="checkup", idempotency_key="seed-1"
    ))
    session.flush()
    assert client.get("/api/appointments").json()[0]["conversation_id"] is None


def test_confirmed_column_reflects_reminder_replies(client, session, world, conversation):
    appt = book(session, world, conversation)
    scheduling.confirm_attendance(session, conversation, appt.id, now=NOW)
    row = client.get("/api/appointments").json()[0]
    assert row["confirmed"] is True and row["confirmed_at"]


def test_status_filters(client, session, world, conversation):
    confirmed = book(session, world, conversation, 0, "k1")
    unconfirmed = book(session, world, conversation, 1, "k2")
    cancelled = book(session, world, conversation, 2, "k3")
    scheduling.confirm_attendance(session, conversation, confirmed.id, now=NOW)
    conversation.verified = True
    scheduling.cancel_appointment(session, conversation, cancelled.id)

    def ids(status):
        return {r["id"] for r in client.get(f"/api/appointments?status={status}").json()}

    assert ids("all") == {str(confirmed.id), str(unconfirmed.id), str(cancelled.id)}
    assert ids("booked") == {str(confirmed.id), str(unconfirmed.id)}
    assert ids("confirmed") == {str(confirmed.id)}
    assert ids("unconfirmed") == {str(unconfirmed.id)}
    assert ids("cancelled") == {str(cancelled.id)}
    assert client.get("/api/appointments?status=bogus").status_code == 422


def test_day_filter_uses_the_clinic_timezone(client, session, world, conversation):
    # NOW is Wed 2026-10-21 12:00 UTC (5 AM in Los Angeles). Slots are +24h, +25h and +48h,
    # so two fall on Thursday Oct 22 in Los Angeles and one on Friday Oct 23.
    for i, key in enumerate(["d1", "d2", "d3"]):
        book(session, world, conversation, i, key)
    thursday = client.get("/api/appointments?day=2026-10-22").json()
    friday = client.get("/api/appointments?day=2026-10-23").json()
    assert len(thursday) == 2 and len(friday) == 1
    assert client.get("/api/appointments?day=not-a-date").status_code == 422


def test_appointments_are_sorted_by_time(client, session, world, conversation):
    book(session, world, conversation, 2, "s1")
    book(session, world, conversation, 0, "s2")
    times = [r["starts_at"] for r in client.get("/api/appointments").json()]
    assert times == sorted(times)


# ------------------------------------------------------------ patients and outreach


def test_patients_list_shows_next_appointment_and_opt_out(client, session, world, conversation, monkeypatch):
    monkeypatch.setattr("app.api.dashboard.utcnow", lambda: NOW)  # slots are relative to NOW
    book(session, world, conversation)
    optout.opt_out(session, world.riley.phone_number)
    rows = {r["name"]: r for r in client.get("/api/patients").json()}
    assert rows["Taylor Morgan"]["next_appointment"] is not None  # booked for tomorrow
    assert rows["Riley Brooks"]["next_appointment"] is None
    assert rows["Riley Brooks"]["opted_out"] is True and rows["Taylor Morgan"]["opted_out"] is False
    assert "***" in rows["Riley Brooks"]["phone"]


def test_text_patient_sends_the_first_message(client, session, world, sms):
    r = client.post(f"/api/patients/{world.riley.id}/outreach", json={"staff_name": "Dana"})
    assert r.status_code == 200
    assert sms.sent == [(world.riley.phone_number, replies.OUTREACH)]
    assert "STOP" in replies.OUTREACH

    convo = session.scalars(select(models.Conversation)).one()
    assert r.json()["conversation_id"] == str(convo.id)
    assert convo.phone_number == world.riley.phone_number and convo.patient_id == world.riley.id
    message = session.scalars(select(models.Message)).one()
    assert message.direction == "outbound" and message.delivery_status == "queued" and message.twilio_sid
    audit = [a for a in session.scalars(select(models.AuditLog)) if a.action == "outreach_sent"]
    assert audit[0].actor == "staff:Dana"


def test_opted_out_patients_are_not_texted(client, session, world, sms):
    optout.opt_out(session, world.riley.phone_number)
    r = client.post(f"/api/patients/{world.riley.id}/outreach", json={})
    assert r.status_code == 409 and "opted out" in r.json()["detail"]
    assert sms.sent == [] and session.scalars(select(models.Message)).all() == []


def test_a_double_click_texts_only_once(client, world, sms):
    assert client.post(f"/api/patients/{world.riley.id}/outreach", json={}).status_code == 200
    again = client.post(f"/api/patients/{world.riley.id}/outreach", json={})
    assert again.status_code == 409 and "24 hours" in again.json()["detail"]
    assert len(sms.sent) == 1


def test_no_outreach_over_a_conversation_staff_own(client, session, world, sms):
    convo = get_or_create_conversation(session, world.clinic.id, world.riley.phone_number)
    escalate(session, convo, "requested_human")
    r = client.post(f"/api/patients/{world.riley.id}/outreach", json={})
    assert r.status_code == 409 and sms.sent == []


def test_failed_send_is_not_recorded(client, session, world):
    class Down:
        def send(self, to, body):
            raise RuntimeError("twilio down")

    app.dependency_overrides[get_sms_sender] = lambda: Down()
    with pytest.raises(RuntimeError):
        client.post(f"/api/patients/{world.riley.id}/outreach", json={})
    assert session.scalars(select(models.Message)).all() == []
    assert [a for a in session.scalars(select(models.AuditLog)) if a.action == "outreach_sent"] == []


def test_unknown_patient_is_404(client):
    assert client.post("/api/patients/00000000-0000-0000-0000-000000000000/outreach", json={}).status_code == 404


def test_mock_sender_ids_are_unique_across_instances():
    assert MockSmsSender().send("+1", "a") != MockSmsSender().send("+1", "a")


def test_reply_after_outreach_continues_in_the_same_conversation(client, session, world):
    from app.agent.runner import handle_inbound_message
    from app.agent.testing import ScriptedLLM, u

    client.post(f"/api/patients/{world.riley.id}/outreach", json={})
    reply = handle_inbound_message(
        session, world.clinic, world.riley.phone_number, "I'd like a checkup",
        ScriptedLLM(u(intent="book", appointment_type="checkup")), now=NOW,
    )
    assert "date of birth" in reply
    assert len(session.scalars(select(models.Conversation)).all()) == 1
