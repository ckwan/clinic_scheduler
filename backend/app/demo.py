"""Offline demo: `make demo`. Runs four short conversations through the real agent, tools and
database (in memory), using the rule based stand in for the model, so it needs no Docker,
model, or API key. Because the stand in is not a real model, the demo shows the guardrails and
flows, not language understanding."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models
from app.agent.runner import handle_inbound_message
from app.db import Base
from app.evals.heuristic import HeuristicLLM
from app.evals.world import NOW, build_world

SCENARIOS = [
    (
        "1. Happy path: verify, pick a time, confirm",
        "taylor",
        ["I need a therapy appointment", "Taylor Morgan 04/12/1988", "1", "yes"],
    ),
    (
        "2. Guardrail: a stranger tries to cancel someone else's appointment",
        "unknown",
        ["cancel my appointment", "Taylor Morgan 04/12/1988"],
    ),
    (
        "3. Safety: a clinical question goes to a human, never answered",
        "riley",
        ["I have a rash, which medication should I take?"],
    ),
    (
        "4. Frustrated patient asks for a person",
        "casey",
        ["this is ridiculous, I want a real person"],
    ),
]


def main() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    llm = HeuristicLLM()
    with Session(engine, expire_on_commit=False) as session:
        world = build_world(session)
        for title, who, messages in SCENARIOS:
            print(f"\n=== {title}")
            phone = world.phone_for(who)
            for text in messages:
                reply = handle_inbound_message(session, world.clinic, phone, text, llm, now=NOW)
                print(f"  patient: {text}")
                print(f"  agent:   {reply or '(no reply, a human owns this conversation)'}")
        session.commit()

        booked = session.query(models.Appointment).filter_by(status="booked").count()
        escalated = [e.reason for e in session.query(models.Escalation)]
        print(f"\nResult: {booked} appointment booked, escalations: {escalated}")
        print("The stranger's cancel attempt changed nothing; the clinical question was not answered.")


if __name__ == "__main__":
    main()
