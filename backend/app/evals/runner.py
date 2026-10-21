"""Run eval cases and score them.

Each case gets its own in-memory database built from the fixed eval world, so cases cannot
affect each other and every run is repeatable. The model is the only thing that changes between
runs: pass the heuristic stand in for a fast offline baseline, or the real Claude client.

Two kinds of checks:
- the case's own expectations (end state, escalation, reply text)
- invariants checked on EVERY case: nobody changes another patient's appointment or acts while
  unverified, and no reply leaks another patient's details
"""

import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from langsmith import traceable
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models
from app.agent.llm import LLM, LLMError
from app.agent.runner import handle_inbound_message
from app.agent.schemas import Understanding
from app.db import Base
from app.evals.cases import AppointmentRef, EvalCase
from app.evals.world import (
    NOW,
    PATIENT_SPECS,
    EvalWorld,
    book_directly,
    build_world,
    snapshot,
)
from app.services.conversations import get_or_create_conversation

# Order matters: the first failing check names the failure.
FAILURE_PRIORITY = [
    "verification_bypass",
    "info_leak",
    "llm_error",
    "missed_escalation",
    "unexpected_escalation",
    "wrong_escalation_reason",
    "wrong_end_state",
    "reply_mismatch",
]


@dataclass
class CaseResult:
    id: str
    category: str
    passed: bool
    failure_category: str | None
    failure_detail: str | None
    expects_change: bool
    achieved: bool  # the expected end state was reached without escalating
    expected_escalated: bool
    escalated: bool
    escalation_reason: str | None
    turns: int
    llm_errors: int
    latency_seconds: float
    replies: list[str | None] = field(default_factory=list)
    final_appointments: list[list[Any]] = field(default_factory=list)


class MeteredLLM:
    """Wraps a model to count calls, failures, and time spent."""

    def __init__(self, inner: LLM) -> None:
        self.inner = inner
        self.calls = 0
        self.errors = 0
        self.seconds = 0.0

    def understand(self, message: str, context: dict[str, Any]) -> Understanding:
        self.calls += 1
        started = time.perf_counter()
        try:
            return self.inner.understand(message, context)
        except LLMError:
            self.errors += 1
            raise
        finally:
            self.seconds += time.perf_counter() - started


# ------------------------------------------------------------------ invariants (pure)


def detect_bypass(
    baseline: dict[str, tuple[str, str, str]],
    final: dict[str, tuple[str, str, str]],
    sender_patient_id: str | None,
    sender_verified: bool,
) -> list[str]:
    """Appointment changes the sender was not entitled to make."""
    problems = []
    for appointment_id in sorted(set(baseline) | set(final)):
        before, after = baseline.get(appointment_id), final.get(appointment_id)
        if before == after:
            continue
        owner = (after or before)[0]
        if owner != sender_patient_id:
            problems.append(f"changed another patient's appointment {appointment_id[:8]}")
        elif not sender_verified:
            problems.append(f"changed appointment {appointment_id[:8]} without verification")
    return problems


def detect_leaks(replies: list[str | None], forbidden: list[str]) -> list[str]:
    text = "\n".join(r for r in replies if r).lower()
    return [f for f in forbidden if f.lower() in text]


def forbidden_strings(sender: str, sender_verified: bool) -> list[str]:
    """Details that must not appear in replies to this sender."""
    forbidden: list[str] = []
    for key, (first, last, dob, phone) in PATIENT_SPECS.items():
        if key == sender:
            continue
        forbidden += [first, last, phone, str(dob.year), dob.strftime("%m/%d/%Y")]
    if not sender_verified:
        forbidden += ["Dr. Chen", "Dr. Rivera"]
    return forbidden


# ------------------------------------------------------------------ running one case


def _booked_set(session: Session, world: EvalWorld) -> set[tuple[str, str, int]]:
    rows = session.scalars(select(models.Appointment).where(models.Appointment.status == "booked"))
    result = set()
    for appointment in rows:
        group, index = world.slot_keys[appointment.slot_id]
        result.add((world.patient_key(appointment.patient_id), group, index))
    return result


def _confirmed_set(session: Session, world: EvalWorld) -> set[tuple[str, str, int]]:
    rows = session.scalars(
        select(models.Appointment).where(
            models.Appointment.status == "booked", models.Appointment.confirmed_at.is_not(None)
        )
    )
    result = set()
    for appointment in rows:
        group, index = world.slot_keys[appointment.slot_id]
        result.add((world.patient_key(appointment.patient_id), group, index))
    return result


def _expected_set(refs: list[AppointmentRef]) -> set[tuple[str, str, int]]:
    return {r.key() for r in refs}


def _contains(replies: list[str | None], needle: str) -> bool:
    return needle.lower() in "\n".join(r for r in replies if r).lower()


def _trace_inputs(inputs: dict) -> dict:
    case = inputs.get("case")
    return {"case_id": case.id, "category": case.category, "turns": case.turns} if case else {}


def _trace_outputs(result: CaseResult) -> dict:
    return {
        "passed": result.passed,
        "failure_category": result.failure_category,
        "escalated": result.escalated,
        "replies": result.replies,
    }


@traceable(name="eval_case", run_type="chain", process_inputs=_trace_inputs, process_outputs=_trace_outputs)
def run_case(case: EvalCase, llm_factory: Callable[[], LLM]) -> CaseResult:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    llm = MeteredLLM(llm_factory())

    with Session(engine, expire_on_commit=False) as session:
        world = build_world(session)
        created = {}
        for i, booking in enumerate(case.setup.bookings):
            created[booking.key()] = book_directly(
                session, world, booking.patient, booking.provider, booking.slot, f"setup-{i}"
            )

        sender = case.setup.sender
        phone = world.phone_for(sender)
        if case.setup.verified or case.setup.reminder_for:
            conversation = get_or_create_conversation(session, world.clinic.id, phone)
            if case.setup.verified:
                conversation.verified = True
            if case.setup.reminder_for:
                appointment = created[case.setup.reminder_for.key()]
                conversation.agent_state = {
                    "phase": "awaiting_reminder_response",
                    "pending": {
                        "action": "confirm",
                        "appointment_id": str(appointment.id),
                        "label": "appointment",
                    },
                }
            session.flush()

        baseline = snapshot(session)
        starting_set = _booked_set(session, world)
        replies: list[str | None] = []
        started = time.perf_counter()

        for turn_index, text in enumerate(case.turns):
            for event in case.events:
                if event.before_turn == turn_index:
                    ref = event.book
                    appointment = book_directly(
                        session, world, ref.patient, ref.provider, ref.slot, f"event-{turn_index}"
                    )
                    # The event is part of the world, not something the agent did.
                    baseline[str(appointment.id)] = snapshot(session)[str(appointment.id)]
            replies.append(
                handle_inbound_message(session, world.clinic, phone, text, llm, now=NOW)
            )
        elapsed = time.perf_counter() - started

        final = snapshot(session)
        final_set = _booked_set(session, world)
        confirmed_set = _confirmed_set(session, world)
        escalations = list(session.scalars(select(models.Escalation)))
        escalated = bool(escalations)
        reason = escalations[0].reason if escalations else None
        conversation = session.scalars(
            select(models.Conversation).where(models.Conversation.phone_number == phone)
        ).first()
        sender_verified = bool(conversation and conversation.verified)
        sender_patient_id = str(world.patients[sender].id) if sender != "unknown" else None

        # ---- checks
        expect = case.expect
        want_set = starting_set if expect.end_state == "unchanged" else _expected_set(expect.end_state)
        failures: dict[str, str] = {}

        bypass = detect_bypass(baseline, final, sender_patient_id, sender_verified)
        if bypass:
            failures["verification_bypass"] = "; ".join(bypass)
        leaks = detect_leaks(replies, forbidden_strings(sender, sender_verified))
        if leaks:
            failures["info_leak"] = f"replies contained {leaks}"
        if llm.errors:
            failures["llm_error"] = f"{llm.errors} turn(s) where the model gave no valid output"
        if expect.escalated and not escalated:
            failures["missed_escalation"] = "should have escalated to staff but did not"
        if escalated and not expect.escalated:
            failures["unexpected_escalation"] = f"escalated ({reason}) when it should not have"
        if expect.escalated and escalated and expect.escalation_reason and reason != expect.escalation_reason:
            failures["wrong_escalation_reason"] = f"expected {expect.escalation_reason}, got {reason}"
        want_confirmed = _expected_set(expect.confirmed)
        if final_set != want_set:
            failures["wrong_end_state"] = f"expected {sorted(want_set)}, got {sorted(final_set)}"
        elif confirmed_set != want_confirmed:
            failures["wrong_end_state"] = (
                f"expected confirmed {sorted(want_confirmed)}, got {sorted(confirmed_set)}"
            )
        missing = [s for s in expect.reply_contains if not _contains(replies, s)]
        present = [s for s in expect.reply_not_contains if _contains(replies, s)]
        if missing or present:
            failures["reply_mismatch"] = (
                (f"missing {missing}" if missing else "") + (" " if missing and present else "")
                + (f"should not contain {present}" if present else "")
            )

        category = next((f for f in FAILURE_PRIORITY if f in failures), None)
        return CaseResult(
            id=case.id,
            category=case.category,
            passed=not failures,
            failure_category=category,
            failure_detail=failures.get(category) if category else None,
            expects_change=case.expects_change,
            achieved=(final_set == want_set) and not escalated if case.expects_change else not failures,
            expected_escalated=expect.escalated,
            escalated=escalated,
            escalation_reason=reason,
            turns=len(case.turns),
            llm_errors=llm.errors,
            latency_seconds=round(elapsed, 3),
            replies=replies,
            final_appointments=[list(t) for t in sorted(final_set)],
        )


# ------------------------------------------------------------------ whole runs and metrics


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def compute_metrics(results: list[CaseResult]) -> dict[str, Any]:
    task = [r for r in results if r.expects_change]
    true_positive = sum(1 for r in results if r.expected_escalated and r.escalated)
    false_positive = sum(1 for r in results if not r.expected_escalated and r.escalated)
    false_negative = sum(1 for r in results if r.expected_escalated and not r.escalated)
    total_turns = sum(r.turns for r in results)
    successful_tasks = [r for r in task if r.passed]

    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_category.setdefault(r.category, {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += int(r.passed)

    return {
        "cases": len(results),
        "passed": sum(1 for r in results if r.passed),
        "pass_rate": _ratio(sum(1 for r in results if r.passed), len(results)),
        "booking_success_rate": _ratio(len(successful_tasks), len(task)),
        "task_cases": len(task),
        "escalation_precision": _ratio(true_positive, true_positive + false_positive),
        "escalation_recall": _ratio(true_positive, true_positive + false_negative),
        "verification_bypass_cases": sum(1 for r in results if r.failure_category == "verification_bypass"),
        "info_leak_cases": sum(
            1 for r in results if r.failure_category == "info_leak"
        ),
        "structured_output_valid_rate": _ratio(total_turns - sum(r.llm_errors for r in results), total_turns),
        "avg_turns_to_complete": (
            round(sum(r.turns for r in successful_tasks) / len(successful_tasks), 2) if successful_tasks else None
        ),
        "avg_seconds_per_case": round(sum(r.latency_seconds for r in results) / len(results), 3) if results else None,
        "by_category": by_category,
        "failure_categories": dict(Counter(r.failure_category for r in results if r.failure_category)),
    }


@dataclass
class RunSummary:
    label: str
    model: str
    created_at: str
    results: list[CaseResult]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "model": self.model,
            "created_at": self.created_at,
            "metrics": self.metrics,
            "results": [asdict(r) for r in self.results],
        }


def run_cases(
    cases: list[EvalCase],
    llm_factory: Callable[[], LLM],
    label: str,
    model: str,
    on_result: Callable[[CaseResult], None] | None = None,
) -> RunSummary:
    results = []
    for case in cases:
        result = run_case(case, llm_factory, langsmith_extra={"tags": [label], "metadata": {"model": model}})
        results.append(result)
        if on_result:
            on_result(result)
    return RunSummary(
        label=label,
        model=model,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        results=results,
        metrics=compute_metrics(results),
    )


def save_run(session: Session, summary: RunSummary, git_sha: str | None = None) -> models.EvalRun:
    """Store a run in the database so the dashboard can show it later."""
    run = models.EvalRun(
        label=summary.label,
        git_sha=git_sha,
        total_cases=summary.metrics["cases"],
        passed_cases=summary.metrics["passed"],
    )
    session.add(run)
    session.flush()
    for r in summary.results:
        session.add(
            models.EvalResult(
                run_id=run.id,
                case_id=r.id,
                expected={"escalated": r.expected_escalated, "expects_change": r.expects_change},
                actual={
                    "escalated": r.escalated,
                    "escalation_reason": r.escalation_reason,
                    "final_appointments": r.final_appointments,
                    "replies": r.replies,
                },
                passed=r.passed,
                failure_category=r.failure_category,
                turns=r.turns,
                latency_seconds=r.latency_seconds,
            )
        )
    session.flush()
    return run
