import json

import httpx
import pytest

from app.agent.llm import JSON_SYSTEM_PROMPT, LLMError
from app.agent.ollama import OllamaLLM
from app.agent.providers import make_llm, model_label
from app.agent.schemas import Understanding

VALID = {
    "intent": "book",
    "confidence": 0.9,
    "asks_clinical_question": False,
    "wants_human": False,
    "frustrated": False,
    "appointment_type": "therapy_session",
}


def chat_response(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


def llm_with(*responses):
    """An OllamaLLM whose HTTP calls return queued responses (or raise queued exceptions)."""
    queue = list(responses)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaLLM(model="test-model", base_url="http://ollama.test:11434/", client=client), requests


def test_valid_json_is_parsed():
    llm, requests = llm_with(chat_response(VALID))
    result = llm.understand("I need therapy", {"phase": "idle"})
    assert isinstance(result, Understanding) and result.intent == "book"
    assert len(requests) == 1


def test_request_shape():
    llm, requests = llm_with(chat_response(VALID))
    llm.understand("I need therapy", {"phase": "idle"})
    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == "http://ollama.test:11434/api/chat"  # trailing slash handled
    assert body["model"] == "test-model"
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0}
    assert body["format"] == Understanding.model_json_schema()
    system, user = body["messages"]
    assert system == {"role": "system", "content": JSON_SYSTEM_PROMPT}
    assert "I need therapy" in user["content"] and '"phase": "idle"' in user["content"]


def test_invalid_json_is_retried_once():
    llm, requests = llm_with(chat_response("not json at all"), chat_response(VALID))
    assert llm.understand("x", {}).intent == "book"
    assert len(requests) == 2


def test_schema_violation_is_retried_then_raises():
    bad = {**VALID, "intent": "teleport"}
    llm, requests = llm_with(chat_response(bad), chat_response(bad))
    with pytest.raises(LLMError):
        llm.understand("x", {})
    assert len(requests) == 2


def test_server_errors_are_retried():
    llm, requests = llm_with(httpx.Response(500), chat_response(VALID))
    assert llm.understand("x", {}).intent == "book"
    assert len(requests) == 2


def test_timeouts_are_retried_then_raise():
    llm, requests = llm_with(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    with pytest.raises(LLMError):
        llm.understand("x", {})
    assert len(requests) == 2


def test_malformed_response_body_is_an_error():
    llm, _ = llm_with(httpx.Response(200, json={"unexpected": True}), httpx.Response(200, json={}))
    with pytest.raises(LLMError):
        llm.understand("x", {})


def test_unreachable_server_fails_fast_with_a_helpful_message():
    llm, requests = llm_with(httpx.ConnectError("refused"))
    with pytest.raises(LLMError, match="ollama serve"):
        llm.understand("x", {})
    assert len(requests) == 1  # no pointless retry


def test_missing_model_fails_fast_with_the_pull_command():
    llm, requests = llm_with(httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(LLMError, match="ollama pull test-model"):
        llm.understand("x", {})
    assert len(requests) == 1


def test_provider_factory():
    assert type(make_llm("ollama")).__name__ == "OllamaLLM"
    assert type(make_llm("heuristic")).__name__ == "HeuristicLLM"
    with pytest.raises(ValueError, match="unknown LLM provider"):
        make_llm("gpt")


def test_model_labels():
    assert model_label("ollama", "llama3.1:8b") == "ollama:llama3.1:8b"
    assert model_label("anthropic", "claude-sonnet-5-5") == "anthropic:claude-sonnet-5-5"
    assert "heuristic" in model_label("heuristic")


def test_default_provider_is_ollama():
    from app.config import Settings

    assert Settings(_env_file=None).llm_provider == "ollama"
