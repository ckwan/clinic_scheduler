"""Check LangSmith tracing end to end: `make langsmith-check`.

Runs one real conversation turn with the offline rule based stand in (no model needed), sends
the trace, then asks LangSmith whether it arrived. Prints what is wrong when it did not.
"""

import os
import sys
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agent.runner import handle_inbound_message
from app.db import Base
from app.evals.heuristic import HeuristicLLM
from app.evals.world import NOW, build_world


def describe_settings() -> list[str]:
    key = os.environ.get("LANGSMITH_API_KEY", "")
    return [
        f"LANGSMITH_TRACING   = {os.environ.get('LANGSMITH_TRACING') or '(not set)'}",
        f"LANGSMITH_API_KEY   = {('set, ends in ' + key[-4:]) if key else '(not set)'}",
        f"LANGSMITH_PROJECT   = {os.environ.get('LANGSMITH_PROJECT')}",
        f"LANGSMITH_ENDPOINT  = {os.environ.get('LANGSMITH_ENDPOINT') or '(default: US, api.smith.langchain.com)'}",
        f"LANGSMITH_WORKSPACE_ID = {os.environ.get('LANGSMITH_WORKSPACE_ID') or '(not set)'}",
    ]


def problems() -> list[str]:
    found = []
    if os.environ.get("LANGSMITH_TRACING", "").lower() not in {"true", "1"}:
        found.append("LANGSMITH_TRACING is not true. Set LANGSMITH_TRACING=true in .env.")
    if not os.environ.get("LANGSMITH_API_KEY"):
        found.append("LANGSMITH_API_KEY is empty. Create a key at smith.langchain.com (Settings, API Keys).")
    return found


def run(client=None, wait_seconds: int = 15, sleep=time.sleep) -> int:
    print("\n".join(describe_settings()))
    found = problems()
    if found:
        print("\nTracing is off:\n  " + "\n  ".join(found))
        return 2

    from langsmith import Client, tracing_context

    client = client or Client()
    project = os.environ["LANGSMITH_PROJECT"]

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        world = build_world(session)
        with tracing_context(enabled=True, client=client, project_name=project):
            handle_inbound_message(
                session, world.clinic, world.phone_for("taylor"), "I need a therapy appointment",
                HeuristicLLM(), now=NOW,
            )
    try:
        client.flush()
        for _ in range(wait_seconds):
            runs = list(client.list_runs(project_name=project, is_root=True, limit=1))
            if runs:
                print(f"\nOK. A trace arrived in project '{project}' (run id {runs[0].id}).")
                print("Open smith.langchain.com, choose Tracing, then this project.")
                return 0
            sleep(1)
    except Exception as exc:
        name = type(exc).__name__
        print(f"\nCould not reach LangSmith: {name}: {str(exc)[:300]}")
        print("Usual causes: a wrong or revoked API key, an organization scoped key without")
        print("LANGSMITH_WORKSPACE_ID, an EU account without LANGSMITH_ENDPOINT, or no network.")
        return 1

    print(f"\nThe turn ran, but no trace showed up in project '{project}' after {wait_seconds}s.")
    print("Check the project name, the endpoint (EU accounts), and that the key belongs to the same workspace.")
    return 1


if __name__ == "__main__":
    sys.exit(run())
