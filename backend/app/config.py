import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict


# The README puts .env in the repository root, but the API, worker and tests are started from
# backend/. Look in both places (backend/.env wins if both exist). Variables already set in the
# shell win over either file. In Docker neither file exists and Compose sets the environment.
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = (str(REPO_ROOT / ".env"), ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, extra="ignore")

    database_url: str = "postgresql+psycopg://clinicline:clinicline@localhost:5432/clinicline"
    temporal_address: str = "localhost:7233"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    sms_mock: bool = True
    # Twilio signs every webhook request. Keep this on for anything reachable from the internet.
    twilio_validate_signature: bool = True
    # The public https URL Twilio calls (your ngrok or production URL, no trailing slash). The
    # signature covers the exact URL, so behind a tunnel or proxy it must be set explicitly.
    public_base_url: str = ""

    # Which model reads patient messages: "ollama" (local, default) or "anthropic".
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    anthropic_api_key: str = ""

    # How replies reach the patient: "twiml" answers inside the webhook response;
    # "temporal" hands the message to a durable workflow that sends the reply with retries.
    reply_mode: str = "twiml"
    temporal_task_queue: str = "clinicline"

    # Patient booking links (app/booking). BOOKING_BASE_URL is where the patient page is served:
    # the dashboard host (http://localhost:5173 in development, your public URL in production).
    booking_base_url: str = "http://localhost:5173"
    booking_link_ttl_days: int = 7

    # Dashboard. If dashboard_token is set, /api requires "Authorization: Bearer <token>".
    dashboard_token: str = ""
    eval_results_dir: str = ""  # defaults to <repo>/evals/results


settings = Settings()


# The LangSmith SDK reads real environment variables only. pydantic-settings reads .env into the
# Settings object but does not export it, so LANGSMITH_TRACING=true in .env used to do nothing
# when running outside Docker. This copies the LangSmith variables across (without overriding
# anything already set in the shell) and picks a default project name.
LANGSMITH_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",  # EU accounts use https://eu.api.smith.langchain.com
    "LANGSMITH_WORKSPACE_ID",  # needed for organization scoped API keys
)
DEFAULT_LANGSMITH_PROJECT = "clinic-scheduler"


def export_langsmith_env(env_files: tuple[str, ...] = ENV_FILES) -> None:
    values: dict = {}
    for path in env_files:  # later files override earlier ones, matching Settings
        if os.path.exists(path):
            values.update({k: v for k, v in dotenv_values(path).items() if v})
    for name in LANGSMITH_VARS:
        if not os.environ.get(name) and values.get(name):
            os.environ[name] = values[name]
    if not os.environ.get("LANGSMITH_PROJECT"):
        os.environ["LANGSMITH_PROJECT"] = DEFAULT_LANGSMITH_PROJECT


export_langsmith_env()
