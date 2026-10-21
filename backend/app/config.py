from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clinic_scheduler:clinic_scheduler@localhost:5432/clinic_scheduler"
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
    anthropic_model: str = "claude-sonnet-5-5"

    # How replies reach the patient: "twiml" answers inside the webhook response;
    # "temporal" hands the message to a durable workflow that sends the reply with retries.
    reply_mode: str = "twiml"
    temporal_task_queue: str = "clinic_scheduler"

    # Dashboard. If dashboard_token is set, /api requires "Authorization: Bearer <token>".
    dashboard_token: str = ""
    eval_results_dir: str = ""  # defaults to <repo>/evals/results


settings = Settings()
