"""Quick check that the configured model works:  python -m app.agent.check

Sends a few sample messages and prints what the model understood, so you can tell right away
whether Ollama is running, the model is pulled, and it can produce valid structured output.
"""

import sys
import time

from app.agent.llm import LLMError
from app.agent.providers import make_llm, model_label
from app.config import settings

SAMPLES = [
    ("Hi, I'd like to book a therapy session", {"phase": "idle", "verified": False}),
    ("Taylor Morgan 04/12/1988", {"phase": "awaiting_verification", "verified": False}),
    ("tomorrow morning works", {"phase": "idle", "verified": True, "today": "Wednesday 2026-10-21",
                                "timezone": "America/Los_Angeles"}),
    ("Can I take ibuprofen with my other medication?", {"phase": "idle", "verified": False}),
]


def main() -> int:
    print(f"Provider: {model_label()}")
    llm = make_llm()
    failures = 0
    for message, context in SAMPLES:
        started = time.perf_counter()
        try:
            understanding = llm.understand(message, context)
        except LLMError as exc:
            failures += 1
            print(f"\nFAIL  {message!r}\n      {exc}")
            if "could not reach" in str(exc) or "does not have the model" in str(exc):
                break
            continue
        seconds = time.perf_counter() - started
        shown = understanding.model_dump(exclude_none=True, mode="json")
        print(f"\nOK    {message!r}  ({seconds:.1f}s)\n      {shown}")
    print(f"\n{len(SAMPLES) - failures} of {len(SAMPLES)} samples understood.")
    if settings.llm_provider == "ollama" and failures:
        print("Tip: small models struggle with structured output. Try a larger one, e.g. "
              "OLLAMA_MODEL=llama3.1:8b or qwen2.5:7b.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
