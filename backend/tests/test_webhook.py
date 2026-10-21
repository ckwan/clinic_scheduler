import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.agent.testing import ScriptedLLM, u
from app.db import get_session
from app.main import app, get_llm
from app.services.conversations import escalate, get_or_create_conversation


@pytest.fixture()
def post(session):
    def _post(llm, **form):
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_llm] = lambda: llm
        try:
            return TestClient(app).post("/webhooks/twilio/sms", data=form)
        finally:
            app.dependency_overrides.clear()

    return _post


def test_inbound_text_gets_a_twiml_reply(post, world):
    llm = ScriptedLLM(u(intent="book", appointment_type="therapy_session"))
    response = post(
        llm, From=world.taylor.phone_number, To=world.clinic.phone_number, Body="I need an appointment"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "<Message>" in response.text and "date of birth" in response.text


def test_messages_are_committed(post, session, world):
    llm = ScriptedLLM(u(intent="book", appointment_type="therapy_session"))
    post(llm, From=world.taylor.phone_number, To=world.clinic.phone_number, Body="hi")
    assert len(session.scalars(select(models.Message)).all()) == 2


def test_unknown_clinic_number_gets_an_empty_response(post, world):
    llm = ScriptedLLM()
    response = post(llm, From="+15551112222", To="+15550000000", Body="hello")
    assert response.text == "<Response></Response>"
    assert llm.calls == []


def test_escalated_conversation_gets_no_automatic_reply(post, session, world):
    convo = get_or_create_conversation(session, world.clinic.id, world.taylor.phone_number)
    escalate(session, convo, "requested_human")
    llm = ScriptedLLM()
    response = post(llm, From=world.taylor.phone_number, To=world.clinic.phone_number, Body="hello?")
    assert response.text == "<Response></Response>"
    assert llm.calls == []


def test_reply_text_is_xml_escaped(post, world):
    llm = ScriptedLLM(u(intent="other"))
    response = post(llm, From=world.taylor.phone_number, To=world.clinic.phone_number, Body="<hi>")
    assert "<hi>" not in response.text
