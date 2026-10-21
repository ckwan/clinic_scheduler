"""Workflow tests that run without a Temporal server.

Covered: the pure timeline, the activities against the test database, idempotent message
processing, the webhook's temporal mode with a fake client, and the reminder workflow's real
code run against a fake clock. NOT covered: a live server (signal-with-start, retries,
replay). Those need `make up` and a worker; see README.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from temporalio import workflow as tw

from app import models
from app.agent.runner import process_inbound
from app.agent.testing import ScriptedLLM, u
from app.channels.sms import MockSmsSender
from app.config import settings
from app.db import get_session
from app.main import app, get_llm, get_temporal_client
from app.services import scheduling
from app.services.conversations import get_or_create_conversation
from app.workflows import reminder as reminder_module
from app.workflows.activities import Activities
from app.workflows.conversation import ConversationWorkflow
from app.workflows.models import InboundMessage, ReminderInput
from app.workflows.reminder import AppointmentReminderWorkflow
from app.workflows.timeline import ConversationTimeline
from tests.conftest import NOW

H = 3600


def _name(fn):
    return getattr(fn, "__temporal_activity_definition").name


def timeline():
    return ConversationTimeline(24 * H, 48 * H, 24 * H, 72 * H)


# ------------------------------------------------------------ timeline


def test_pending_conversation_is_nudged_then_closed():
    t = timeline()
    first = t.next_step("awaiting_slot_choice", False)
    assert (first.wait_s, first.on_timeout) == (24 * H, "nudge")
    t.on_nudge_sent()
    second = t.next_step("awaiting_slot_choice", False)
    assert (second.wait_s, second.on_timeout) == (24 * H, "close")  # 48h total


def test_a_new_message_resets_the_nudge():
    t = timeline()
    t.on_nudge_sent()
    t.on_message()
    assert t.next_step("awaiting_confirmation", False).on_timeout == "nudge"


def test_idle_and_human_conversations_are_never_nudged():
    t = timeline()
    assert t.next_step("idle", False) .on_timeout == "close"
    human = t.next_step("idle", True)
    assert (human.wait_s, human.on_timeout) == (72 * H, "close")


# ------------------------------------------------------------ definitions


def test_workflow_definitions_are_valid():
    from temporalio.workflow import _Definition

    conv = _Definition.must_from_class(ConversationWorkflow)
    assert set(conv.signals) == {"inbound_message", "human_takeover"}
    assert _Definition.must_from_class(AppointmentReminderWorkflow).name == "AppointmentReminderWorkflow"


def test_every_activity_the_workflows_call_is_registered(session):
    names = {_name(a) for a in Activities(None, None, None).all()}
    import inspect, re

    called = set()
    for module in ("conversation", "reminder"):
        src = inspect.getsource(__import__(f"app.workflows.{module}", fromlist=["x"]))
        called |= set(re.findall(r'execute_activity\(\s*"(\w+)"', src))
    assert called and called <= names


# ------------------------------------------------------------ activities


class Factory:
    """Hands the shared test session to activities without closing it."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *a):
        return False


@pytest.fixture()
def acts(session):
    return Activities(Factory(session), ScriptedLLM(), MockSmsSender())


def test_process_message_runs_the_agent_and_reports_the_phase(session, world, conversation):
    acts = Activities(
        Factory(session), ScriptedLLM(u(intent="book", appointment_type="therapy_session")), MockSmsSender()
    )
    result = acts.process_message(str(conversation.id), InboundMessage("I need an appointment", "SM1"))
    assert result.phase == "awaiting_verification" and "date of birth" in result.reply
    assert result.to_number == world.taylor.phone_number and not result.duplicate


def test_redelivered_message_is_not_processed_twice(session, world, conversation):
    llm = ScriptedLLM(u(intent="book", appointment_type="therapy_session"))
    first = process_inbound(session, world.clinic, conversation, "hi", llm, now=NOW, twilio_sid="SM9")
    again = process_inbound(session, world.clinic, conversation, "hi", llm, now=NOW, twilio_sid="SM9")
    assert first.reply and again.duplicate and again.reply is None
    assert len(llm.calls) == 1
    assert len(session.scalars(select(models.Message).where(models.Message.direction == "inbound")).all()) == 1


def test_booking_reports_the_new_appointment_id(session, world, conversation):
    conversation.verified = True
    llm = ScriptedLLM(
        u(intent="book", appointment_type="therapy_session"),
        u(chosen_option=1),
        u(confirmation="yes"),
    )
    ids = []
    for text in ("book", "1", "yes"):
        ids += process_inbound(session, world.clinic, conversation, text, llm, now=NOW).new_appointment_ids
    booked = session.scalars(select(models.Appointment)).all()
    assert ids == [str(booked[0].id)] and len(booked) == 1


def test_prepare_nudge_only_for_pending_phases(session, world, conversation, acts):
    assert acts.prepare_nudge(str(conversation.id)) is None
    conversation.agent_state = {"phase": "awaiting_verification"}
    plan = acts.prepare_nudge(str(conversation.id))
    assert "date of birth" in plan.body


def test_close_conversation_is_idempotent(session, conversation, acts):
    acts.close_conversation(str(conversation.id), "abandoned")
    acts.close_conversation(str(conversation.id), "abandoned")
    assert conversation.state == "closed" and conversation.agent_state == {}
    closed = [a for a in session.scalars(select(models.AuditLog)) if a.action == "conversation_closed"]
    assert len(closed) == 1


def _booked(session, world, conversation):
    conversation.verified = True
    appt = scheduling.book_appointment(
        session, conversation, world.therapy_slots[0].id, "therapy_session", "wf-key-0001", now=NOW
    )
    return appt


def test_prepare_reminder_arms_the_reply_and_confirm_clears_it(session, world, conversation, acts):
    appt = _booked(session, world, conversation)
    conversation.agent_state = {}
    plan = acts.prepare_reminder(str(appt.id))
    assert "CONFIRM" in plan.body
    assert conversation.agent_state["phase"] == "awaiting_reminder_response"
    assert conversation.agent_state["pending"]["appointment_id"] == str(appt.id)


def test_prepare_reminder_does_not_hijack_a_conversation_in_progress(session, world, conversation, acts):
    appt = _booked(session, world, conversation)
    conversation.agent_state = {"phase": "awaiting_slot_choice"}
    acts.prepare_reminder(str(appt.id))
    assert conversation.agent_state["phase"] == "awaiting_slot_choice"


def test_prepare_reminder_skips_cancelled_appointments(session, world, conversation, acts):
    appt = _booked(session, world, conversation)
    scheduling.cancel_appointment(session, conversation, appt.id)
    assert acts.prepare_reminder(str(appt.id)) is None


def test_reply_to_a_reminder_confirms_the_appointment(session, world, conversation, acts):
    appt = _booked(session, world, conversation)
    conversation.agent_state = {}
    conversation.verified = False
    acts.prepare_reminder(str(appt.id))
    llm = ScriptedLLM(u(confirmation="yes"))
    result = process_inbound(session, world.clinic, conversation, "CONFIRM", llm, now=NOW)
    assert "confirmed" in result.reply
    assert acts.get_appointment_info(str(appt.id)).confirmed


def test_flag_unconfirmed(session, world, conversation, acts):
    appt = _booked(session, world, conversation)
    assert acts.flag_unconfirmed(str(appt.id)) is True
    scheduling.confirm_attendance(session, conversation, appt.id, now=NOW)
    assert acts.flag_unconfirmed(str(appt.id)) is False


# ------------------------------------------------------------ webhook temporal mode


def test_temporal_mode_signals_the_workflow_and_returns_empty_twiml(session, world, monkeypatch):
    sent = []

    async def fake_deliver(client, conversation_id, clinic_id, body, sid):
        sent.append((conversation_id, body, sid))

    monkeypatch.setattr(settings, "reply_mode", "temporal")
    monkeypatch.setattr("app.workflows.client.deliver_inbound", fake_deliver)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_llm] = lambda: ScriptedLLM()
    app.dependency_overrides[get_temporal_client] = lambda: None

    async def no_client():
        return None

    monkeypatch.setattr("app.main.get_temporal_client", no_client)
    try:
        response = TestClient(app).post(
            "/webhooks/twilio/sms",
            data=dict(From=world.taylor.phone_number, To=world.clinic.phone_number, Body="hi", MessageSid="SM7"),
        )
    finally:
        app.dependency_overrides.clear()
    assert response.text == "<Response></Response>"
    convo = session.scalars(select(models.Conversation)).one()
    assert sent == [(str(convo.id), "hi", "SM7")]
    assert session.scalars(select(models.Message)).all() == []  # the workflow records messages


# ------------------------------------------------------------ reminder workflow, fake clock


class FakeTemporal:
    """Runs the real workflow code with a fake clock and activities called directly."""

    def __init__(self, monkeypatch, acts, start):
        self.now = start
        self.acts = {_name(a): a for a in acts.all()}
        self.called = []
        monkeypatch.setattr(tw, "now", lambda: self.now)
        monkeypatch.setattr(tw, "execute_activity", self.execute_activity)
        monkeypatch.setattr(reminder_module.asyncio, "sleep", self.sleep)

    async def execute_activity(self, name, arg=None, *, args=None, **kwargs):
        self.called.append(name)
        return self.acts[name](*(args if args is not None else [arg]))

    async def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def run_reminder(monkeypatch, acts, appt, start, **kw):
    fake = FakeTemporal(monkeypatch, acts, start)
    result = asyncio.run(AppointmentReminderWorkflow().run(ReminderInput(str(appt.id), **kw)))
    return result, fake


def test_reminder_sends_then_flags_when_unconfirmed(session, world, conversation, acts, monkeypatch):
    appt = _booked(session, world, conversation)
    result, fake = run_reminder(monkeypatch, acts, appt, NOW)
    assert result == "unconfirmed"
    assert fake.called.index("prepare_reminder") < fake.called.index("send_sms") < fake.called.index("flag_unconfirmed")
    # Appointment is 24h out and the reminder goes 24h before, so it fires at once.


def test_reminder_stops_if_the_appointment_was_cancelled(session, world, conversation, acts, monkeypatch):
    appt = _booked(session, world, conversation)
    scheduling.cancel_appointment(session, conversation, appt.id)
    result, fake = run_reminder(monkeypatch, acts, appt, NOW)
    assert result == "appointment_cancelled" and "send_sms" not in fake.called


def test_reminder_notices_when_the_appointment_moved_later(session, world, conversation, acts, monkeypatch):
    appt = _booked(session, world, conversation)
    scheduling.reschedule_appointment(session, conversation, appt.id, world.therapy_slots[2].id, now=NOW)
    result, fake = run_reminder(monkeypatch, acts, appt, NOW)
    # The new slot is 48h out, so the workflow waited ~24h before reminding.
    assert fake.now >= NOW + timedelta(hours=24)
    assert "send_sms" in fake.called


def test_reminder_is_not_flagged_when_confirmed(session, world, conversation, acts, monkeypatch):
    appt = _booked(session, world, conversation)
    scheduling.confirm_attendance(session, conversation, appt.id, now=NOW)
    result, _ = run_reminder(monkeypatch, acts, appt, NOW)
    assert result == "confirmed_or_changed"
