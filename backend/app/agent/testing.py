"""Test doubles for the LLM. Used by unit tests and by evals that exercise the graph itself."""

from typing import Any

from app.agent.llm import LLMError
from app.agent.schemas import Understanding


def u(**fields: Any) -> Understanding:
    """Build an Understanding with sensible defaults, so scripts only state what matters."""
    defaults: dict[str, Any] = dict(
        intent="other",
        confidence=0.9,
        asks_clinical_question=False,
        wants_human=False,
        frustrated=False,
    )
    defaults.update(fields)
    return Understanding(**defaults)


class ScriptedLLM:
    """Returns queued Understandings in order. An Exception in the queue is raised instead."""

    def __init__(self, *script: Understanding | Exception) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def understand(self, message: str, context: dict[str, Any]) -> Understanding:
        self.calls.append((message, context))
        if not self._script:
            raise AssertionError("ScriptedLLM ran out of responses")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def failing_llm() -> ScriptedLLM:
    return ScriptedLLM(LLMError("boom"))
