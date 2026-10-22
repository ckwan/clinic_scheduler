import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.channels.sms import MockSmsSender, get_sms_sender
from app.config import settings
from app.db import get_session
from app.main import app
from app.services.conversations import add_message, escalate


@pytest.fixture()
def sms():
    return MockSmsSender()


@pytest.fixture()
def client(session, sms, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "eval_results_dir", str(tmp_path))
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_sms_sender] = lambda: sms
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def escalated(session, conversation):
    add_message(session, conversation, "inbound", "my chest hurts")
    add_message(session, conversation, "outbound", "Please call 911 if this is an emergency.")
    return conversation, escalate(session, conversation, "clinical_question")


def test_phone_numbers_are_masked(client, world, escalated):
    rows = client.get("/api/conversations").json()
    assert rows[0]["phone"] == "***-0111" and world.taylor.phone_number not in json.dumps(rows)
    assert rows[0]["patient"] == "Taylor Morgan"


def test_summary_counts(client, escalated):
    s = client.get("/api/summary").json()
    assert s["conversations"] == 1 and s["open_escalations"] == 1 and s["escalation_rate"] == 1.0


def test_transcript_has_senders_and_audit(client, escalated):
    convo, _ = escalated
    d = client.get(f"/api/conversations/{convo.id}").json()
    assert [m["sender"] for m in d["messages"]] == ["patient", "agent"]
    assert any(a["action"] == "escalated" for a in d["audit"])
    assert d["escalations"][0]["reason"] == "clinical_question"


def test_unknown_conversation_is_404(client):
    assert client.get("/api/conversations/00000000-0000-0000-0000-000000000000").status_code == 404


def test_staff_cannot_reply_unless_escalated(client, conversation):
    r = client.post(f"/api/conversations/{conversation.id}/reply", json={"body": "hi"})
    assert r.status_code == 409


def test_take_reply_resolve_flow(client, session, sms, escalated):
    convo, esc = escalated
    assert client.post(f"/api/escalations/{esc.id}/take", json={"staff_name": "Dana"}).json()["status"] == "taken"
    # a second person cannot grab it
    assert client.post(f"/api/escalations/{esc.id}/take", json={"staff_name": "Sam"}).status_code == 409

    sent = client.post(f"/api/conversations/{convo.id}/reply", json={"body": "Dana here, calling you now."})
    assert sent.status_code == 200 and sms.sent == [(convo.phone_number, "Dana here, calling you now.")]
    detail = client.get(f"/api/conversations/{convo.id}").json()
    assert detail["messages"][-1]["sender"] == "staff"

    done = client.post(f"/api/escalations/{esc.id}/resolve").json()
    assert done["status"] == "resolved"
    session.refresh(convo)
    assert convo.state == "closed"
    assert client.get("/api/escalations?status=active").json() == []


def test_failed_sms_is_not_recorded_as_sent(client, session, escalated, monkeypatch):
    convo, _ = escalated

    class Down:
        def send(self, to, body):
            raise RuntimeError("twilio down")

    app.dependency_overrides[get_sms_sender] = lambda: Down()
    with pytest.raises(RuntimeError):
        client.post(f"/api/conversations/{convo.id}/reply", json={"body": "hello"})
    assert len(session.scalars(select(models.Message)).all()) == 2


def test_token_is_required_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", "s3cret")
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/summary", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_eval_runs_are_listed_and_fetched(client, tmp_path):
    run = {"label": "x", "model": "m", "created_at": "2026-10-21T00:00:00+00:00",
           "metrics": {"pass_rate": 1.0}, "results": []}
    (tmp_path / "x.json").write_text(json.dumps(run))
    (tmp_path / "junk.json").write_text("{}")
    assert [r["label"] for r in client.get("/api/evals").json()] == ["x"]
    assert client.get("/api/evals/x").json()["model"] == "m"
    assert client.get("/api/evals/nope").status_code == 404
    assert client.get("/api/evals/..%2Fsecrets").status_code == 404


# ------------------------------------------------------------ staff escalation


def test_staff_can_escalate_a_live_conversation(client, session, conversation):
    add_message(session, conversation, "inbound", "hello")
    r = client.post(
        f"/api/conversations/{conversation.id}/escalate",
        json={"reason": "staff_agent_confused", "staff_name": "Dana"},
    )
    assert r.status_code == 200 and r.json()["status"] == "open" and r.json()["reason"] == "staff_agent_confused"
    session.refresh(conversation)
    assert conversation.state == "escalated"

    audit = [a for a in session.scalars(select(models.AuditLog)) if a.action == "escalated"]
    assert [a.actor for a in audit] == ["staff:Dana"]  # recorded as staff, not as the agent
    assert [e["conversation_id"] for e in client.get("/api/escalations?status=active").json()] == [str(conversation.id)]


def test_the_agent_stays_silent_after_a_staff_escalation(client, session, world, conversation):
    from app.agent.runner import handle_inbound_message
    from app.agent.testing import ScriptedLLM
    from tests.conftest import NOW

    client.post(f"/api/conversations/{conversation.id}/escalate", json={"reason": "staff_other"})
    llm = ScriptedLLM()  # would raise if the agent were asked anything
    assert handle_inbound_message(session, world.clinic, conversation.phone_number, "hello?", llm, now=NOW) is None


def test_staff_can_reply_after_escalating(client, session, sms, conversation):
    client.post(f"/api/conversations/{conversation.id}/escalate", json={"reason": "staff_patient_needs_help"})
    sent = client.post(f"/api/conversations/{conversation.id}/reply", json={"body": "Calling you now."})
    assert sent.status_code == 200 and sms.sent[0][1] == "Calling you now."


def test_cannot_escalate_twice_or_a_closed_conversation(client, session, conversation):
    url = f"/api/conversations/{conversation.id}/escalate"
    assert client.post(url, json={"reason": "staff_other"}).status_code == 200
    assert client.post(url, json={"reason": "staff_other"}).status_code == 409

    conversation.state = "closed"
    session.flush()
    assert client.post(url, json={"reason": "staff_other"}).status_code == 409


def test_escalation_reason_must_be_one_of_the_known_ones(client, conversation):
    r = client.post(f"/api/conversations/{conversation.id}/escalate", json={"reason": "because"})
    assert r.status_code == 422


def test_escalating_an_unknown_conversation_is_404(client):
    r = client.post("/api/conversations/00000000-0000-0000-0000-000000000000/escalate", json={"reason": "staff_other"})
    assert r.status_code == 404
