"""Local model through Ollama (https://ollama.com).

The model runs on your own machine, so patient messages never leave it. That makes it a good fit
for healthcare work, and it costs nothing per call.

Structured output uses Ollama's `format` field, which constrains decoding to the JSON schema of
`Understanding`. Small local models are less reliable than hosted ones at reading times and
names, which is exactly what the evals measure.
"""

from typing import Any

import httpx
from langsmith import traceable
from pydantic import ValidationError

from app.agent.llm import JSON_SYSTEM_PROMPT, LLMError, render_user_content
from app.agent.schemas import Understanding
from app.config import settings


class OllamaLLM:
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._model = model or settings.ollama_model
        self._base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def model(self) -> str:
        return self._model

    def _payload(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self._model,
            "stream": False,
            "format": Understanding.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": JSON_SYSTEM_PROMPT},
                {"role": "user", "content": render_user_content(message, context)},
            ],
        }

    @traceable(name="understand_message", run_type="llm")
    def understand(self, message: str, context: dict[str, Any]) -> Understanding:
        payload = self._payload(message, context)
        last_error: Exception | None = None

        for _ in range(2):
            try:
                response = self._client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
                content = response.json()["message"]["content"]
            except httpx.ConnectError as exc:
                # Retrying will not help: nothing is listening.
                raise LLMError(
                    f"could not reach Ollama at {self._base_url}. "
                    "Is it running? Start it with `ollama serve`."
                ) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise LLMError(
                        f"Ollama does not have the model '{self._model}'. "
                        f"Run `ollama pull {self._model}`."
                    ) from exc
                last_error = exc
                continue
            except (httpx.HTTPError, KeyError, ValueError) as exc:  # timeout, bad body
                last_error = exc
                continue

            try:
                return Understanding.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
        raise LLMError(f"could not get a valid understanding from {self._model}: {last_error}")
