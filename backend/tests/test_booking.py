"""Patient booking link: tokens, the public page API, outreach guards, and the campaign workflow."""

import asyncio
import inspect
import re
from datetime import date, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from temporalio import workflow as tw

from app import models
from app.booking import api as booking_api
from app.booking import outreach, ratelimit, service
from app.booking import workflows as booking_workflows
from app.booking.activities import BookingActivities
from app.booking.tokens import hash_token
from app.channels.sms import MockSmsSender, get_sms_sender
from app.db import get_session
from app.main import app
from app.services import optout
from app.services.conversations import escalate, get_or_create_conversation
from tests.conftest import NOW
from tests.test_workflows import Factory, _name

DOB = date(1988, 4, 12)  # Taylor
WRONG = date(1990, 1, 1)
PUBLIC = "/public/booking"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    monkeypatch.setattr(booking_api, "utcnow", lambda: NOW)
    ratelimit.reset()


@pytest.fixture()
def sms():
    return MockSmsSender()


@pytest.fixture()
def client(session, sms):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_sms_sender] = lambda: sms
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def link(session, world):
    row, token = service.create_link(session, world.taylor, "annual_wellness_visit", NOW)
    row.token = token  # test convenience only; the column does not exist
    return row


def body(slot_id=None, dob=DOB):
    out = {"date_of_birth": dob.isoformat()}
    if slot_id:
        out["slot_id"] = str(slot_id)
    return out


# ------------------------------------------------------------ tokens


def test_only_the_hash_of_the_token_is_stored(session, link):
    stored = session.scalar(select(models.BookingLink.token_hash))
    assert stored == hash_token(link.token) and link.token not in stored
    assert len(link.token) >= 40  # 256 bits


def test_a_new_link_revokes_the_old_one(session, world, link):
    newer, _ = service.create_link(session, world.taylor, "annual_wellness_visit", NOW)
    assert link.revoked_at is not None and newer.revoked_at is None


# ------------------------------------------------------------ opening the link


def test_opening_a_link_shows_only_a_first_name_and_changes_nothing(client, session, link):
    data = client.get(f"{PUBLIC}/{link.token}").json()
    assert data["status"] == "ok" and data["first_name"] == "Taylor" and data["clinic"] == "Test Clinic"
    assert "Morgan" not in str(data) and "1988" not in str(data)
    session.refresh(link)
    assert link.used_at is None and link.failed_attempts == 0


@pytest.mark.parametrize("token", ["nope", "a" * 300])
def test_unknown_tokens_are_invalid(client, token):
    assert client.get(f"{PUBLIC}/{token}").json()["status"] == "invalid"


def test_expired_link(client, session, link):
    link.expires_at = NOW - timedelta(seconds=1)
    assert client.get(f"{PUBLIC}/{link.token}").json()["status"] == "expired"
    assert client.post(f"{PUBLIC}/{link.token}/slots", json=body()).status_code == 410


def test_the_page_never_reveals_a_name_to_a_stranger(client, link):
    r = client.post(f"{PUBLIC}/{link.token}/slots", json=body(dob=WRONG))
    assert r.status_code == 403
    assert "Taylor" not in r.text and r.json()["detail"]["attempts_remaining"] == 1


# ------------------------------------------------------------ identity


def test_two_wrong_birthdates_lock_the_link_and_ask_for_a_human(client, session, world, link):
    client.post(f"{PUBLIC}/{link.token}/slots", json=body(dob=WRONG))
    r = client.post(f"{PUBLIC}/{link.token}/slots", json=body(dob=WRONG))
    assert r.status_code == 423 and r.json()["detail"]["code"] == "locked"
    # Even the right date of birth no longer works: the link is revoked.
    assert client.post(f"{PUBLIC}/{link.token}/slots", json=body()).status_code == 410
    convo = get_or_create_conversation(session, world.clinic.id, world.taylor.phone_number)
    assert convo.state == "escalated"
    reason = session.scalar(select(models.Escalation.reason))
    assert reason == "booking_link_verification_failed"


def test_a_conversation_a_human_owns_cannot_book_by_link(client, session, world, link):
    escalate(session, get_or_create_conversation(session, world.clinic.id, world.taylor.phone_number), "requested_human")
    r = client.post(f"{PUBLIC}/{link.token}/slots", json=body())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "needs_staff"


# ------------------------------------------------------------ slots and booking


def test_slots_are_the_open_slots_for_the_visit_type(client, world, link):
    data = client.post(f"{PUBLIC}/{link.token}/slots", json=body()).json()
    assert [s["slot_id"] for s in data["slots"]] == [str(world.pcp_slot.id)]  # primary care only
    assert data["timezone"] == "America/Los_Angeles"


def test_booking_creates_the_appointment_texts_a_confirmation_and_uses_the_link_up(client, session, world, link, sms):
    r = client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id))
    assert r.status_code == 200
    appt = session.get(models.Appointment, UUID(r.json()["appointment_id"]))
    assert appt.patient_id == world.taylor.id and appt.appointment_type == "annual_wellness_visit"
    assert world.pcp_slot.status == "booked"
    assert sms.sent and "You're booked" in sms.sent[0][1] and sms.sent[0][0] == world.taylor.phone_number
    session.refresh(link)
    assert link.used_at is not None and link.appointment_id == appt.id
    actions = [a.action for a in session.scalars(select(models.AuditLog))]
    assert "booked_via_link" in actions and "appointment_booked" in actions


def test_the_dashboard_can_find_the_conversation_for_a_link_booking(client, session, world, link):
    client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id))
    row = client.get("/api/appointments").json()[0]
    assert row["conversation_id"] == str(link.conversation_id)


def test_repeating_the_request_returns_the_same_booking_and_one_text(client, session, world, link, sms):
    first = client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id)).json()
    again = client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id)).json()
    assert first["appointment_id"] == again["appointment_id"] and len(sms.sent) == 1
    assert len(session.scalars(select(models.Appointment)).all()) == 1


def test_a_used_link_does_not_describe_the_booking_to_the_wrong_person(client, world, link):
    client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id))
    r = client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id, WRONG))
    assert r.status_code == 410 and "Test Clinic" not in r.text


def test_a_forwarded_link_cannot_book_without_the_birthdate(client, session, world, link):
    r = client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id, WRONG))
    assert r.status_code == 403
    assert world.pcp_slot.status == "open" and session.scalars(select(models.Appointment)).all() == []


def test_a_slot_someone_else_took_is_refused_with_a_clear_message(client, session, world, link):
    other, other_token = service.create_link(session, world.riley, "annual_wellness_visit", NOW)
    ok = client.post(f"{PUBLIC}/{other_token}/book", json=body(world.pcp_slot.id, date(1992, 9, 3)))
    assert ok.status_code == 200
    r = client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "slot_taken"
    session.refresh(link)
    assert link.used_at is None  # the patient can pick another time with the same link


def test_a_slot_of_the_wrong_kind_cannot_be_booked_by_id(client, world, link):
    r = client.post(f"{PUBLIC}/{link.token}/book", json=body(world.therapy_slots[0].id))
    assert r.status_code == 409  # a therapy slot is not a primary care slot


def test_booking_does_not_leave_the_texting_conversation_verified(client, session, world, link):
    convo = get_or_create_conversation(session, world.clinic.id, world.taylor.phone_number)
    assert convo.verified is False
    client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id))
    assert convo.verified is False


def test_no_confirmation_text_goes_to_a_number_that_opted_out(client, session, world, link, sms):
    optout.opt_out(session, world.taylor.phone_number)
    assert client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id)).status_code == 200
    assert sms.sent == []


def test_a_failed_confirmation_text_does_not_undo_the_booking(client, session, world, link):
    class Broken:
        def send(self, to, text):
            raise RuntimeError("twilio down")

    app.dependency_overrides[get_sms_sender] = lambda: Broken()
    assert client.post(f"{PUBLIC}/{link.token}/book", json=body(world.pcp_slot.id)).status_code == 200
    assert session.scalars(select(models.Appointment)).one().status == "booked"


def test_public_endpoints_are_rate_limited(client, link):
    for _ in range(ratelimit.MAX_REQUESTS):
        client.get(f"{PUBLIC}/{link.token}")
    assert client.get(f"{PUBLIC}/{link.token}").status_code == 429


def test_public_endpoints_do_not_need_the_dashboard_token(client, link, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "dashboard_token", "secret")
    assert client.get(f"{PUBLIC}/{link.token}").status_code == 200
    assert client.post("/api/patients/00000000-0000-0000-0000-000000000000/booking-link", json={}).status_code == 401


# ------------------------------------------------------------ outreach


def test_the_text_has_the_link_and_the_opt_out_but_the_transcript_does_not_keep_the_secret(session, world, sms):
    row = outreach.send_booking_link(session, sms, world.taylor, now=NOW)
    to, text = sms.sent[0]
    token = re.search(r"/book/([\w-]+)", text).group(1)
    assert to == world.taylor.phone_number and "STOP" in text and "yearly visit" in text
    assert "wellness" not in text and "Test Clinic" in text
    assert hash_token(token) == row.token_hash
    stored = session.scalars(select(models.Message)).one().body
    assert token not in stored and "/book/…" in stored


def test_outreach_guards(session, world, sms):
    optout.opt_out(session, world.riley.phone_number)
    with pytest.raises(outreach.NotSent) as e:
        outreach.send_booking_link(session, sms, world.riley, now=NOW)
    assert e.value.reason == "opted_out"

    outreach.send_booking_link(session, sms, world.taylor, now=NOW)
    with pytest.raises(outreach.NotSent) as e:
        outreach.send_booking_link(session, sms, world.taylor, now=NOW + timedelta(hours=1))
    assert e.value.reason == "cooldown"
    outreach.send_booking_link(session, sms, world.taylor, kind="nudge", now=NOW + timedelta(days=3))
    assert len(sms.sent) == 2
    assert session.scalars(select(models.BookingLink).where(models.BookingLink.revoked_at.is_(None))).all().__len__() == 1

    escalate(session, get_or_create_conversation(session, world.clinic.id, world.taylor.phone_number), "frustrated")
    with pytest.raises(outreach.NotSent) as e:
        outreach.send_booking_link(session, sms, world.taylor, now=NOW + timedelta(days=9))
    assert e.value.reason == "escalated"


def test_a_failed_send_creates_no_message_and_leaves_no_booking_trail(session, world):
    class Broken:
        def send(self, to, text):
            raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        outreach.send_booking_link(session, Broken(), world.taylor, now=NOW)
    assert session.scalars(select(models.Message)).all() == []
    assert session.scalars(select(models.AuditLog).where(models.AuditLog.action == "outreach_sent")).all() == []


def test_staff_button_endpoint(client, session, world, sms):
    r = client.post(f"/api/patients/{world.taylor.id}/booking-link", json={"staff_name": "Pat"})
    assert r.status_code == 200 and sms.sent
    again = client.post(f"/api/patients/{world.taylor.id}/booking-link", json={"staff_name": "Pat"})
    assert again.status_code == 409 and "24 hours" in again.json()["detail"]
    assert client.post(f"/api/patients/{world.riley.id}/booking-link", json={}).status_code == 200
    rows = {p["name"]: p for p in client.get("/api/patients").json()}
    assert rows["Taylor Morgan"]["last_outreach_at"] and rows["Taylor Morgan"]["due_for_wellness"] is True


def test_who_is_due(session, world):
    world.taylor.last_wellness_visit_on = NOW.date() - timedelta(days=400)
    world.riley.last_wellness_visit_on = NOW.date() - timedelta(days=100)
    assert [p.first_name for p in outreach.find_due_patients(session, NOW)] == ["Taylor"]
    world.riley.last_wellness_visit_on = None  # never seen: due
    assert {p.first_name for p in outreach.find_due_patients(session, NOW)} == {"Taylor", "Riley"}

    optout.opt_out(session, world.riley.phone_number)
    assert [p.first_name for p in outreach.find_due_patients(session, NOW)] == ["Taylor"]
    link, _ = service.create_link(session, world.taylor, "annual_wellness_visit", NOW)
    assert outreach.find_due_patients(session, NOW) == []  # a live link already out
    link.used_at = NOW
    session.add(models.Appointment(patient_id=world.taylor.id, slot_id=world.pcp_slot.id,
                                   appointment_type="annual_wellness_visit", idempotency_key="k1"))
    session.flush()
    assert outreach.find_due_patients(session, NOW) == []  # already has a visit booked


# ------------------------------------------------------------ campaign workflow


@pytest.fixture()
def acts(session, sms):
    return BookingActivities(Factory(session), sms)


class Fake:
    """Runs the real workflow code with a fake clock and activities called directly."""

    def __init__(self, monkeypatch, acts, start=NOW):
        self.now, self.acts, self.called, self.children = start, {_name(a): a for a in acts.all()}, [], []
        monkeypatch.setattr(tw, "now", lambda: self.now)
        monkeypatch.setattr(tw, "execute_activity", self.execute_activity)
        monkeypatch.setattr(tw, "start_child_workflow", self.start_child)
        monkeypatch.setattr(booking_workflows.asyncio, "sleep", self.sleep)

    async def execute_activity(self, name, arg=None, *, args=None, **kw):
        self.called.append(name)
        return self.acts[name](*(args if args is not None else [arg]))

    async def start_child(self, name, params, *, id, **kw):
        self.children.append((name, params, id))

    async def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def test_workflows_are_valid_and_every_activity_they_call_is_registered(acts):
    from temporalio.workflow import _Definition

    assert _Definition.must_from_class(booking_workflows.WellnessCampaignWorkflow).name == "WellnessCampaignWorkflow"
    names = {_name(a) for a in acts.all()}
    called = set(re.findall(r'execute_activity\(\s*"(\w+)"', inspect.getsource(booking_workflows)))
    assert called and called <= names


def test_campaign_starts_one_outreach_per_due_patient_once_a_year(session, world, acts, monkeypatch):
    fake = Fake(monkeypatch, acts)
    started = asyncio.run(booking_workflows.WellnessCampaignWorkflow().run(booking_workflows.CampaignInput()))
    assert started == 2
    assert {c[2] for c in fake.children} == {f"wellness-{world.taylor.id}-{NOW.year}", f"wellness-{world.riley.id}-{NOW.year}"}


def test_outreach_texts_then_nudges_a_patient_who_has_not_booked(session, world, acts, sms, monkeypatch):
    fake = Fake(monkeypatch, acts)
    monkeypatch.setattr(outreach, "utcnow", lambda: fake.now)
    run = lambda: asyncio.run(booking_workflows.WellnessOutreachWorkflow().run(
        booking_workflows.OutreachInput(str(world.taylor.id), nudge_after_s=3 * 86400)))
    assert run() == "nudged"
    assert len(sms.sent) == 2 and "Reminder from" in sms.sent[1][1]


def test_outreach_does_not_nudge_someone_who_booked(session, world, acts, sms, monkeypatch):
    fake = Fake(monkeypatch, acts)
    real_sleep = fake.sleep

    async def book_while_waiting(seconds):
        link = session.scalar(select(models.BookingLink))
        link.used_at = NOW
        await real_sleep(seconds)

    monkeypatch.setattr(booking_workflows.asyncio, "sleep", book_while_waiting)
    result = asyncio.run(booking_workflows.WellnessOutreachWorkflow().run(
        booking_workflows.OutreachInput(str(world.taylor.id))))
    assert result == "booked" and len(sms.sent) == 1


def test_outreach_stops_when_the_patient_opted_out(session, world, acts, sms, monkeypatch):
    optout.opt_out(session, world.taylor.phone_number)
    Fake(monkeypatch, acts)
    result = asyncio.run(booking_workflows.WellnessOutreachWorkflow().run(
        booking_workflows.OutreachInput(str(world.taylor.id))))
    assert result == "skipped:opted_out" and sms.sent == []
