"""LangSmith tracing: the .env bridge, what gets traced, what is kept out, and the check command."""

import json
import os
from unittest.mock import MagicMock

import pytest
from langsmith import tracing_context

from app import config
from app.agent import trace_check
from app.agent.runner import handle_inbound_message
from app.agent.testing import ScriptedLLM, u
from app.channels.sms import _send_inputs
from tests.conftest import NOW
from tests.test_ollama import VALID, chat_response, llm_with

NAMES = ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID")


@pytest.fixture()
def clean_env(monkeypatch):
    from langsmith import utils

    for name in NAMES:
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    utils.get_env_var.cache_clear()  # the SDK caches environment lookups
    yield
    utils.get_env_var.cache_clear()


# ------------------------------------------------------------ the .env bridge (the bug)


def test_values_in_dotenv_reach_the_sdk(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_text("LANGSMITH_TRACING=true\nLANGSMITH_API_KEY=lsv2_abc\nLANGSMITH_PROJECT=mine\n")
    config.export_langsmith_env((str(env),))
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_abc" and os.environ["LANGSMITH_PROJECT"] == "mine"

    from langsmith.utils import tracing_is_enabled

    assert tracing_is_enabled() is True


def test_the_shell_wins_over_dotenv(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_PROJECT", "from-shell")
    env = tmp_path / ".env"
    env.write_text("LANGSMITH_PROJECT=from-file\n")
    config.export_langsmith_env((str(env),))
    assert os.environ["LANGSMITH_PROJECT"] == "from-shell"


def test_empty_values_are_ignored_and_the_project_has_a_default(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_text("LANGSMITH_API_KEY=\nLANGSMITH_TRACING=false\n")
    config.export_langsmith_env((str(env),))
    assert "LANGSMITH_API_KEY" not in os.environ
    assert os.environ["LANGSMITH_PROJECT"] == "clinic-scheduler"


def test_missing_dotenv_is_fine(clean_env, tmp_path):
    config.export_langsmith_env((str(tmp_path / "nope.env"),))
    assert os.environ["LANGSMITH_PROJECT"] == "clinic-scheduler"


# ------------------------------------------------------------ what is traced


def traced_runs(client):
    return [call.kwargs for call in client.create_run.call_args_list]


def run_booking_turns(session, world, llm):
    client = MagicMock()
    with tracing_context(enabled=True, client=client):
        handle_inbound_message(session, world.clinic, world.taylor.phone_number, "I need a therapy appointment", llm, now=NOW)
        handle_inbound_message(session, world.clinic, world.taylor.phone_number, "Taylor Morgan 04/12/1988", llm, now=NOW)
    return client


def test_turn_llm_and_tool_calls_are_traced(session, world):
    llm = ScriptedLLM(
        u(intent="book", appointment_type="therapy_session"),
        u(first_name="Taylor", last_name="Morgan", date_of_birth="1988-04-12"),
    )
    runs = traced_runs(run_booking_turns(session, world, llm))
    by_name = {r["name"]: r for r in runs}
    assert "clinic_scheduler_turn" in by_name
    assert "tool:verify_identity" in by_name and "tool:check_availability" in by_name
    assert by_name["tool:verify_identity"]["run_type"] == "tool"


def test_the_model_call_is_traced_as_an_llm_run(session, world):
    llm, _ = llm_with(chat_response(VALID))
    client = MagicMock()
    with tracing_context(enabled=True, client=client):
        handle_inbound_message(session, world.clinic, world.taylor.phone_number, "book therapy", llm, now=NOW)
    llm_runs = [r for r in traced_runs(client) if r["name"] == "understand_message"]
    assert llm_runs and llm_runs[0]["run_type"] == "llm"


def test_identity_details_are_kept_out_of_tool_traces(session, world):
    llm = ScriptedLLM(
        u(intent="book", appointment_type="therapy_session"),
        u(first_name="Taylor", last_name="Morgan", date_of_birth="1988-04-12"),
    )
    tool_runs = [r for r in traced_runs(run_booking_turns(session, world, llm)) if r["name"].startswith("tool:")]
    text = json.dumps([r["inputs"] for r in tool_runs], default=str)
    assert "1988" not in text and "Morgan" not in text
    verify = next(r for r in tool_runs if r["name"] == "tool:verify_identity")
    assert verify["inputs"]["date_of_birth"] == "[redacted]"


def test_nothing_is_sent_when_tracing_is_off(session, world):
    client = MagicMock()
    with tracing_context(enabled=False, client=client):
        handle_inbound_message(
            session, world.clinic, world.taylor.phone_number, "hi",
            ScriptedLLM(u(intent="book", appointment_type="therapy_session")), now=NOW,
        )
    client.create_run.assert_not_called()


def test_sms_traces_hide_the_number_and_the_text():
    traced = _send_inputs({"self": object(), "to": "+15555550111", "body": "Reminder: your visit is Friday"})
    assert traced == {"to": "***0111", "characters": 30}


# ------------------------------------------------------------ the check command


def test_check_explains_what_is_missing(clean_env, capsys):
    assert trace_check.run(client=MagicMock()) == 2
    out = capsys.readouterr().out
    assert "LANGSMITH_TRACING is not true" in out and "LANGSMITH_API_KEY is empty" in out


@pytest.fixture()
def tracing_on(clean_env, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_secretkey1234")
    monkeypatch.setenv("LANGSMITH_PROJECT", "clinic-scheduler")


def test_check_succeeds_when_the_trace_arrives(tracing_on, capsys):
    client = MagicMock()
    client.list_runs.return_value = [MagicMock(id="run-1")]
    assert trace_check.run(client=client, sleep=lambda s: None) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "1234" in out and "lsv2_secretkey1234" not in out  # key is never printed whole
    assert client.create_run.called  # the turn really was traced


def test_check_reports_a_rejected_key(tracing_on, capsys):
    client = MagicMock()
    client.list_runs.side_effect = RuntimeError("403 Forbidden")
    assert trace_check.run(client=client, sleep=lambda s: None) == 1
    assert "403" in capsys.readouterr().out


def test_check_reports_a_missing_trace(tracing_on, capsys):
    client = MagicMock()
    client.list_runs.return_value = []
    assert trace_check.run(client=client, wait_seconds=2, sleep=lambda s: None) == 1
    assert "no trace showed up" in capsys.readouterr().out


def test_a_repo_root_dotenv_is_found_when_started_from_backend(clean_env, tmp_path):
    root, backend = tmp_path / ".env", tmp_path / "backend.env"
    root.write_text("LANGSMITH_API_KEY=from-root\nLANGSMITH_PROJECT=root-project\n")
    backend.write_text("LANGSMITH_PROJECT=backend-project\n")
    config.export_langsmith_env((str(root), str(backend)))
    assert os.environ["LANGSMITH_API_KEY"] == "from-root"
    assert os.environ["LANGSMITH_PROJECT"] == "backend-project"  # later file wins


def test_settings_look_in_the_repo_root_first():
    from pathlib import Path

    root_env = Path(config.ENV_FILES[0])
    assert root_env.name == ".env" and (root_env.parent / "backend").is_dir()
    assert config.ENV_FILES[1] == ".env"
