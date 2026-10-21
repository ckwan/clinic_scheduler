"""Signature checks, STOP/START/HELP, delivery callbacks, and the smoke test script."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app import models
from app.agent import replies
from app.agent.testing import ScriptedLLM, u
from app.channels import smoke_test
from app.channels.sms import MockSmsSender
from app.config import settings
from app.db import get_session
from app.main import app, get_llm
from app.services import optout
from app.services.delivery import record_delivery_status
from app.workflows.activities import Activities
from tests.test_workflows import Factory

TOKEN = "test-auth-token"
BASE = "https://clinic.example.test"


@pytest.fixture()
def client(session):
    llm = ScriptedLLM(u(intent="book", appointment_type="therapy_session"))
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_llm] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def signed(monkeypatch):
    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    monkeypatch.setattr(settings, "twilio_auth_token", TOKEN)
    monkeypatch.setattr(settings, "public_base_url", BASE)


def form(world, body="hi", **extra):
    return {"From": world.taylor.phone_number, "To": world.clinic.phone_number, "Body": body, **extra}


def sign(path, data, token=TOKEN):
    return {"X-Twilio-Signature": RequestValidator(token).compute_signature(BASE + path, data)}


# ------------------------------------------------------------ signatures


def test_valid_signature_is_accepted(client, world, signed):
    data = form(world)
    r = client.post("/webhooks/twilio/sms", data=data, headers=sign("/webhooks/twilio/sms", data))
    assert r.status_code == 200 and "date of birth" in r.text


@pytest.mark.parametrize("headers", [{}, {"X-Twilio-Signature": "bogus"}])
def test_missing_or_bad_signature_is_rejected(client, world, signed, session, headers):
    r = client.post("/webhooks/twilio/sms", data=form(world), headers=headers)
    assert r.status_code == 403
    assert session.scalars(select(models.Message)).all() == []  # nothing ran


def test_signature_made_with_another_token_is_rejected(client, world, signed):
    data = form(world)
    r = client.post("/webhooks/twilio/sms", data=data, headers=sign("/webhooks/twilio/sms", data, "other"))
    assert r.status_code == 403


def test_tampered_body_is_rejected(client, world, signed):
    data = form(world, "book me")
    headers = sign("/webhooks/twilio/sms", data)
    r = client.post("/webhooks/twilio/sms", data={**data, "Body": "cancel everything"}, headers=headers)
    assert r.status_code == 403


def test_validation_fails_closed_when_not_configured(client, world, monkeypatch):
    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    monkeypatch.setattr(settings, "public_base_url", "")
    assert client.post("/webhooks/twilio/sms", data=form(world)).status_code == 503


def test_status_callback_also_requires_a_signature(client, signed):
    assert client.post("/webhooks/twilio/status", data={"MessageSid": "SM1", "MessageStatus": "sent"}).status_code == 403


# ------------------------------------------------------------ opt-out


@pytest.mark.parametrize("text", ["STOP", "stop", " Stop. ", "STOPALL", "Unsubscribe", "QUIT", "end"])
def test_stop_keywords(text):
    assert optout.keyword(text) == "stop"


@pytest.mark.parametrize("text", ["please stop texting me about parking", "cancel", "I want to cancel", "stop by at 3", "yes"])
def test_ordinary_messages_are_not_keywords(text):
    assert optout.keyword(text) is None


def test_stop_opts_out_and_agent_stays_silent(client, world, session):
    r = client.post("/webhooks/twilio/sms", data=form(world, "STOP"))
    assert r.text == "<Response></Response>"
    assert optout.is_opted_out(session, world.taylor.phone_number)
    assert session.scalars(select(models.Message)).all() == []  # the agent never saw it


def test_opted_out_numbers_are_ignored_until_start(client, world, session):
    client.post("/webhooks/twilio/sms", data=form(world, "STOP"))
    assert client.post("/webhooks/twilio/sms", data=form(world, "I need an appointment")).text == "<Response></Response>"
    assert session.scalars(select(models.Message)).all() == []

    started = client.post("/webhooks/twilio/sms", data=form(world, "START"))
    assert "subscribed again" in started.text and not optout.is_opted_out(session, world.taylor.phone_number)
    assert "date of birth" in client.post("/webhooks/twilio/sms", data=form(world, "I need an appointment")).text


def test_help_replies_without_the_agent(client, world, session):
    assert "Reply STOP" in client.post("/webhooks/twilio/sms", data=form(world, "HELP")).text
    assert session.scalars(select(models.Message)).all() == []


def test_a_stranger_can_opt_out_too(client, world, session):
    client.post("/webhooks/twilio/sms", data={"From": "+15557770000", "To": world.clinic.phone_number, "Body": "STOP"})
    assert optout.is_opted_out(session, "+15557770000")


def test_reminders_never_tell_patients_to_reply_cancel():
    # Twilio treats a bare CANCEL as STOP, so the reminder would unsubscribe the patient.
    assert "CANCEL" not in replies.reminder("therapy session") and "CANCEL" not in replies.REMINDER_DECLINED


# ------------------------------------------------------------ sending respects opt-out


def acts_for(session, sms=None):
    return Activities(Factory(session), ScriptedLLM(), sms or MockSmsSender())


def test_send_sms_skips_opted_out_numbers(session, world):
    sms = MockSmsSender()
    optout.opt_out(session, world.taylor.phone_number)
    assert acts_for(session, sms).send_sms(world.taylor.phone_number, "hello") == "skipped:opted_out"
    assert sms.sent == []


def test_send_sms_stores_the_provider_id_for_delivery_callbacks(session, world, conversation):
    from app.services.conversations import add_message

    message = add_message(session, conversation, "outbound", "hello")
    sid = acts_for(session).send_sms(world.taylor.phone_number, "hello", str(message.id))
    assert message.twilio_sid == sid and message.delivery_status == "queued"


def test_a_bookkeeping_failure_does_not_trigger_a_resend(session, world):
    sms = MockSmsSender()
    # An id that is not a UUID makes the bookkeeping fail after the text was sent.
    sid = acts_for(session, sms).send_sms(world.taylor.phone_number, "hello", "not-a-uuid")
    assert sid and len(sms.sent) == 1


def test_nudges_and_reminders_skip_opted_out_numbers(session, world, conversation):
    conversation.agent_state = {"phase": "awaiting_verification"}
    optout.opt_out(session, world.taylor.phone_number)
    assert acts_for(session).prepare_nudge(str(conversation.id)) is None


# ------------------------------------------------------------ delivery status


@pytest.fixture()
def sent_message(session, conversation):
    from app.services.conversations import add_message

    return add_message(session, conversation, "outbound", "hello", twilio_sid="SMout1")


def test_status_moves_forward(session, sent_message):
    record_delivery_status(session, "SMout1", "sent")
    record_delivery_status(session, "SMout1", "delivered")
    assert sent_message.delivery_status == "delivered"


def test_late_callbacks_do_not_move_status_backward(session, sent_message):
    record_delivery_status(session, "SMout1", "delivered")
    record_delivery_status(session, "SMout1", "sent")
    assert sent_message.delivery_status == "delivered"


def test_failure_is_recorded_in_the_audit_log(session, sent_message):
    record_delivery_status(session, "SMout1", "undelivered", "30003")
    assert sent_message.delivery_error_code == "30003"
    entry = [a for a in session.scalars(select(models.AuditLog)) if a.action == "sms_delivery_failed"]
    assert len(entry) == 1 and entry[0].detail["error_code"] == "30003"


def test_error_21610_opts_the_recipient_out(session, sent_message):
    record_delivery_status(session, "SMout1", "failed", "21610", to_number="+15555550111")
    assert optout.is_opted_out(session, "+15555550111")


def test_unknown_ids_are_ignored(session):
    assert record_delivery_status(session, "SMnope", "delivered") is False


def test_status_webhook_updates_the_message(client, session, sent_message):
    r = client.post("/webhooks/twilio/status", data={"MessageSid": "SMout1", "MessageStatus": "delivered"})
    assert r.status_code == 204 and sent_message.delivery_status == "delivered"


def test_dashboard_shows_delivery_failures(client, session, conversation, sent_message):
    record_delivery_status(session, "SMout1", "failed", "30007")
    detail = client.get(f"/api/conversations/{conversation.id}").json()
    assert detail["messages"][0]["delivery"] == "failed" and detail["messages"][0]["delivery_error"] == "30007"


# ------------------------------------------------------------ smoke test script


class FakeTwilio:
    """Mimics client.messages.create(...) and client.messages(sid).fetch()."""

    def __init__(self, final="delivered", fail=False):
        self.created = []
        outer = self

        class Messages:
            def create(self, **kw):
                if fail:
                    raise RuntimeError("Unverified number")
                outer.created.append(kw)
                return SimpleNamespace(sid="SMfake", status="queued", error_code=None)

            def __call__(self, sid):
                code = None if final == "delivered" else 30003
                return SimpleNamespace(fetch=lambda: SimpleNamespace(status=final, error_code=code))

        self.messages = Messages()


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setattr(settings, "sms_mock", False)
    for name, value in dict(twilio_account_sid="AC1", twilio_auth_token="t", twilio_from_number="+15550001111").items():
        monkeypatch.setattr(settings, name, value)


def test_smoke_test_reports_missing_settings(monkeypatch, capsys):
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    assert smoke_test.run("+15551234567") == 2 and "TWILIO_ACCOUNT_SID" in capsys.readouterr().out


def test_smoke_test_refuses_to_run_in_mock_mode(live, monkeypatch, capsys):
    monkeypatch.setattr(settings, "sms_mock", True)
    assert smoke_test.run("+15551234567") == 2 and "SMS_MOCK" in capsys.readouterr().out


def test_smoke_test_success(live):
    fake = FakeTwilio()
    assert smoke_test.run("+15551234567", client=fake, sleep=lambda s: None) == 0
    assert fake.created[0]["to"] == "+15551234567"


def test_smoke_test_reports_send_failure(live, capsys):
    assert smoke_test.run("+15551234567", client=FakeTwilio(fail=True), sleep=lambda s: None) == 1
    assert "not verified" in capsys.readouterr().out


def test_smoke_test_reports_undelivered(live, capsys):
    assert smoke_test.run("+15551234567", client=FakeTwilio(final="undelivered"), sleep=lambda s: None) == 1
    assert "30003" in capsys.readouterr().out
