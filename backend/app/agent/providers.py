"""Pick the model provider from settings (LLM_PROVIDER) or by name."""

from app.agent.llm import LLM, AnthropicLLM
from app.agent.ollama import OllamaLLM
from app.config import settings

PROVIDERS = ("ollama", "anthropic", "heuristic")


def make_llm(provider: str | None = None, model: str | None = None) -> LLM:
    provider = provider or settings.llm_provider
    if provider == "ollama":
        return OllamaLLM(model=model)
    if provider == "anthropic":
        return AnthropicLLM(model=model)
    if provider == "heuristic":
        from app.evals.heuristic import HeuristicLLM

        return HeuristicLLM()
    raise ValueError(f"unknown LLM provider '{provider}' (choose from {', '.join(PROVIDERS)})")


def model_label(provider: str | None = None, model: str | None = None) -> str:
    provider = provider or settings.llm_provider
    if provider == "ollama":
        return f"ollama:{model or settings.ollama_model}"
