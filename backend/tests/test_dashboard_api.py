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
