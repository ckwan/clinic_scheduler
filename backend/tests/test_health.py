from fastapi.testclient import TestClient

from app.channels.sms import MockSmsSender
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mock_sender_records_messages():
    sender = MockSmsSender()
    sid = sender.send("+15555550111", "hello")
    assert sid.startswith("mock-")
    assert sender.sent == [("+15555550111", "hello")]
