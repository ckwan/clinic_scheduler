from types import SimpleNamespace

import pytest

from app.agent.llm import TOOL_NAME, AnthropicLLM, LLMError
from app.agent.schemas import Understanding

VALID = {
    "intent": "book",
    "confidence": 0.9,
    "asks_clinical_question": False,
    "wants_human": False,
    "frustrated": False,
    "appointment_type": "therapy_session",
}
INVALID = {**VALID, "intent": "teleport"}


def tool_response(payload):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=payload)])


class FakeClient:
    """Mimics client.messages.create, returning or raising queued items."""

    def __init__(self, *queue):
        self.queue = list(queue)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def llm_with(*queue):
    client = FakeClient(*queue)
    return AnthropicLLM(model="test-model", client=client), client


def test_valid_tool_call_is_parsed():
    llm, client = llm_with(tool_response(VALID))
    result = llm.understand("I need therapy", {"phase": "idle"})
    assert isinstance(result, Understanding) and result.intent == "book"
    assert len(client.requests) == 1


def test_request_forces_the_tool_and_sends_context_and_message():
    llm, client = llm_with(tool_response(VALID))
    llm.understand("I need therapy", {"phase": "idle"})
    request = client.requests[0]
    assert request["model"] == "test-model"
    assert request["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert request["tools"][0]["name"] == TOOL_NAME
    content = request["messages"][0]["content"]
    assert "I need therapy" in content and '"phase": "idle"' in content


def test_schema_marks_safety_fields_required():
    required = set(Understanding.model_json_schema()["required"])
    assert {"intent", "confidence", "asks_clinical_question", "wants_human", "frustrated"} <= required


def test_invalid_output_is_retried_once():
    llm, client = llm_with(tool_response(INVALID), tool_response(VALID))
    assert llm.understand("x", {}).intent == "book"
    assert len(client.requests) == 2


def test_two_invalid_outputs_raise():
    llm, client = llm_with(tool_response(INVALID), tool_response(INVALID))
    with pytest.raises(LLMError):
        llm.understand("x", {})
    assert len(client.requests) == 2


def test_missing_tool_call_raises():
    no_tool = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")])
    llm, _ = llm_with(no_tool, no_tool)
    with pytest.raises(LLMError):
        llm.understand("x", {})


def test_api_errors_are_retried_then_raised():
    llm, client = llm_with(RuntimeError("rate limited"), RuntimeError("rate limited"))
    with pytest.raises(LLMError):
        llm.understand("x", {})
    assert len(client.requests) == 2


def test_api_error_then_success():
    llm, _ = llm_with(RuntimeError("blip"), tool_response(VALID))
    assert llm.understand("x", {}).intent == "book"
