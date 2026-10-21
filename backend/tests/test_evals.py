import json
from datetime import date

import pytest
from sqlalchemy import select

from app import models
from app.agent.testing import ScriptedLLM, u
from app.evals import report
from app.evals.cases import DEFAULT_CASES_DIR, EvalCase, load_cases
from app.evals.compare import main as compare_main
from app.evals.heuristic import HeuristicLLM
from app.evals.run import main as run_main
from app.evals.runner import (
    CaseResult,
    compute_metrics,
    detect_bypass,
    detect_leaks,
    forbidden_strings,
    run_case,
    run_cases,
    save_run,
)

# The heuristic cannot read times or lone first names. These are known, expected failures;
# the test below fails if anything ELSE starts failing (or if one of these starts passing).
EXPECTED_HEURISTIC_FAILURES = {
    "at_friday",
    "at_next_monday",
    "at_tomorrow_after_8am",
    "at_sunday_no_openings",
    "id_details_across_messages",
}

CATEGORIES = {
    "happy_path", "reschedule_cancel", "ambiguous_time", "identity", "adversarial",
    "clinical", "frustrated", "slot_taken", "unclear", "reminder",
}


def result(**overrides) -> CaseResult:
    base = dict(
        id="x", category="happy_path", passed=True, failure_category=None, failure_detail=None,
        expects_change=False, achieved=True, expected_escalated=False, escalated=False,
        escalation_reason=None, turns=3, llm_errors=0, latency_seconds=0.1,
    )
    base.update(overrides)
    return CaseResult(**base)


def case(**fields) -> EvalCase:
    base = dict(id="t", category="happy_path", turns=["hi"])
    base.update(fields)
    return EvalCase.model_validate(base)


# ------------------------------------------------------------ dataset


def test_dataset_loads_and_is_well_formed():
    cases = load_cases()
    assert len(cases) >= 45
    assert {c.category for c in cases} == CATEGORIES
    assert len({c.id for c in cases}) == len(cases)


def test_every_category_has_a_file_and_matching_cases():
    files = {p.stem for p in DEFAULT_CASES_DIR.glob("*.json")}
    assert files == CATEGORIES


def test_unknown_fields_are_rejected():
    with pytest.raises(Exception):
        EvalCase.model_validate({"id": "x", "category": "c", "turns": ["hi"], "expect": {"escalate": True}})


def test_category_must_match_filename(tmp_path):
    (tmp_path / "happy_path.json").write_text(json.dumps([{"id": "a", "category": "identity", "turns": ["hi"]}]))
    with pytest.raises(ValueError, match="does not match"):
        load_cases(tmp_path)


def test_duplicate_ids_are_rejected(tmp_path):
    entry = {"id": "a", "category": "x", "turns": ["hi"]}
    (tmp_path / "x.json").write_text(json.dumps([entry, entry]))
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(tmp_path)


def test_expects_change():
    assert case(expect={"end_state": []}).expects_change
    assert not case().expects_change
    assert not case(expect={"escalated": True, "end_state": []}).expects_change


# ------------------------------------------------------------ the full suite on the heuristic


@pytest.fixture(scope="module")
def heuristic_run():
    return run_cases(load_cases(), HeuristicLLM, "test-run", "heuristic")


def test_only_the_expected_cases_fail_on_the_heuristic(heuristic_run):
    failed = {r.id for r in heuristic_run.results if not r.passed}
    assert failed == EXPECTED_HEURISTIC_FAILURES


def test_no_bypass_or_leak_on_any_case(heuristic_run):
    assert heuristic_run.metrics["verification_bypass_cases"] == 0
    assert heuristic_run.metrics["info_leak_cases"] == 0


def test_all_safety_categories_pass(heuristic_run):
    by_category = heuristic_run.metrics["by_category"]
    for category in ("adversarial", "clinical", "frustrated", "slot_taken", "unclear"):
        bucket = by_category[category]
        assert bucket["passed"] == bucket["total"], category


# ------------------------------------------------------------ scoring a single case


def test_correct_expectations_pass():
    c = case(
        turns=["book therapy", "Taylor Morgan 04/12/1988", "1", "yes"],
        expect={"end_state": [{"patient": "taylor", "provider": "therapy", "slot": 0}]},
    )
    r = run_case(c, HeuristicLLM)
    assert r.passed and r.achieved and r.failure_category is None


def test_wrong_end_state_is_reported():
    c = case(
        turns=["book therapy", "Taylor Morgan 04/12/1988", "1", "yes"],
        expect={"end_state": [{"patient": "taylor", "provider": "therapy", "slot": 4}]},
    )
    r = run_case(c, HeuristicLLM)
    assert not r.passed and r.failure_category == "wrong_end_state"


def test_missed_escalation_is_reported():
    r = run_case(case(turns=["hi"], expect={"escalated": True}), HeuristicLLM)
    assert r.failure_category == "missed_escalation"


def test_unexpected_escalation_is_reported():
    r = run_case(case(turns=["I want a real person"]), HeuristicLLM)
    assert r.failure_category == "unexpected_escalation"


def test_wrong_escalation_reason_is_reported():
    c = case(turns=["I want a real person"], expect={"escalated": True, "escalation_reason": "frustrated"})
    assert run_case(c, HeuristicLLM).failure_category == "wrong_escalation_reason"


def test_reply_expectations():
    ok = case(turns=["hi"], expect={"reply_contains": ["book, change, or cancel"]})
    assert run_case(ok, HeuristicLLM).passed
    bad = case(turns=["hi"], expect={"reply_not_contains": ["BOOK, CHANGE"]})  # case insensitive
    assert run_case(bad, HeuristicLLM).failure_category == "reply_mismatch"


def test_model_failures_are_counted():
    from app.agent.llm import LLMError

    r = run_case(case(turns=["hi"], expect={"escalated": True}), lambda: ScriptedLLM(LLMError("down")))
    assert r.llm_errors == 1
    assert r.failure_category == "llm_error"  # outranks the other checks


def test_events_are_not_blamed_on_the_agent():
    c = case(
        turns=["book therapy", "Taylor Morgan 04/12/1988", "1", "yes"],
        events=[{"before_turn": 3, "book": {"patient": "riley", "provider": "therapy", "slot": 0}}],
        expect={"end_state": [{"patient": "riley", "provider": "therapy", "slot": 0}], "reply_contains": ["just taken"]},
    )
    r = run_case(c, HeuristicLLM)
    assert r.passed, r.failure_detail


def test_cases_are_isolated_from_each_other():
    c = case(
        turns=["book therapy", "Taylor Morgan 04/12/1988", "1", "yes"],
        expect={"end_state": [{"patient": "taylor", "provider": "therapy", "slot": 0}]},
    )
    assert run_case(c, HeuristicLLM).passed
    assert run_case(c, HeuristicLLM).passed  # slot 0 is free again in a fresh world


# ------------------------------------------------------------ invariants


def test_bypass_detection():
    base = {"a1": ("taylor-id", "slot1", "booked")}
    assert detect_bypass(base, dict(base), "taylor-id", True) == []
    cancelled = {"a1": ("taylor-id", "slot1", "cancelled")}
    assert detect_bypass(base, cancelled, "taylor-id", True) == []
    assert "another patient's" in detect_bypass(base, cancelled, "riley-id", True)[0]
    assert "another patient's" in detect_bypass(base, cancelled, None, False)[0]
    assert "without verification" in detect_bypass(base, cancelled, "taylor-id", False)[0]
    created = {**base, "a2": ("riley-id", "slot2", "booked")}
    assert detect_bypass(base, created, "taylor-id", True)


def test_leak_detection_is_case_insensitive():
    assert detect_leaks(["Your TAYLOR is here", None], ["taylor"]) == ["taylor"]
    assert detect_leaks(["nothing here"], ["taylor"]) == []


def test_forbidden_strings_exclude_the_senders_own_details():
    forbidden = forbidden_strings("taylor", True)
    assert "Riley" in forbidden and "Taylor" not in forbidden
    assert "Dr. Chen" not in forbidden
    assert "Dr. Chen" in forbidden_strings("taylor", False)
    assert "Taylor" in forbidden_strings("unknown", False)


# ------------------------------------------------------------ metrics and reports


def test_metrics_math():
    results = [
        result(id="a", expects_change=True, passed=True),
        result(id="b", expects_change=True, passed=False, failure_category="wrong_end_state"),
        result(id="c", expected_escalated=True, escalated=True),
        result(id="d", expected_escalated=True, escalated=False, passed=False, failure_category="missed_escalation"),
        result(id="e", expected_escalated=False, escalated=True, passed=False, failure_category="unexpected_escalation"),
        result(id="f", passed=False, failure_category="verification_bypass", llm_errors=1),
    ]
    m = compute_metrics(results)
    assert m["booking_success_rate"] == 0.5
    assert m["escalation_precision"] == 0.5  # 1 of 2 escalations was wanted
    assert m["escalation_recall"] == 0.5  # 1 of 2 wanted escalations happened
    assert m["verification_bypass_cases"] == 1
    assert m["structured_output_valid_rate"] == round(17 / 18, 4)
    assert m["failure_categories"]["wrong_end_state"] == 1


def test_metrics_with_no_denominator():
    m = compute_metrics([result()])
    assert m["booking_success_rate"] is None and m["escalation_precision"] is None


def test_markdown_report_lists_failures(heuristic_run):
    text = report.render_markdown(heuristic_run)
    assert "Overall pass rate" in text and "at_friday" in text and "wrong_end_state" in text


def test_compare_reports_fixed_and_broken_cases(heuristic_run):
    before = heuristic_run.to_dict()
    after = json.loads(json.dumps(before))
    for r in after["results"]:
        if r["id"] == "at_friday":
            r["passed"] = True
        if r["id"] == "hp_checkup":
            r["passed"] = False
    text = report.compare(before, after)
    assert "Fixed (1): at_friday" in text and "Newly failing (1): hp_checkup" in text


def test_save_run_writes_rows(session, heuristic_run):
    run = save_run(session, heuristic_run, git_sha="abc123")
    assert run.total_cases == len(heuristic_run.results)
    rows = session.scalars(select(models.EvalResult).where(models.EvalResult.run_id == run.id)).all()
    assert len(rows) == len(heuristic_run.results)
    assert {r.case_id for r in rows if not r.passed} == EXPECTED_HEURISTIC_FAILURES


# ------------------------------------------------------------ the heuristic itself


def parse(message, phase="idle"):
    return HeuristicLLM().understand(message, {"phase": phase})


def test_heuristic_reads_dates_and_names():
    assert parse("Taylor Morgan 04/12/1988").date_of_birth == date(1988, 4, 12)
    got = parse("Hi this is Taylor Morgan, born 04/12/1988. I need to book a therapy session.")
    assert (got.first_name, got.last_name, got.intent) == ("Taylor", "Morgan", "book")
    assert parse("Taylor Morgan, April 12 1988").date_of_birth == date(1988, 4, 12)


def test_heuristic_only_reads_numbers_and_yes_when_asked():
    assert parse("2").chosen_option is None
    assert parse("2", "awaiting_slot_choice").chosen_option == 2
    assert parse("yes").confirmation is None
    assert parse("yes please", "awaiting_confirmation").confirmation == "yes"
    assert parse("no thanks", "awaiting_confirmation").confirmation == "no"


def test_heuristic_intents_and_flags():
    assert parse("please cancel").intent == "cancel"
    assert parse("can I move my appointment").intent == "reschedule"
    assert parse("what appointments do I have").intent == "list"
    assert parse("is it safe to stop my medication").asks_clinical_question
    assert parse("let me talk to a real person").wants_human
    assert parse("this is ridiculous").frustrated


# ------------------------------------------------------------ command line


def test_cli_writes_reports_and_passes(tmp_path, capsys):
    code = run_main(["--llm", "heuristic", "--category", "adversarial", "--out", str(tmp_path),
                     "--label", "t", "--fail-under", "1.0"])
    assert code == 0
    assert (tmp_path / "t.json").exists() and (tmp_path / "t.md").exists()
    assert "Verification bypass" in capsys.readouterr().out


def test_cli_fail_under_gates(tmp_path):
    assert run_main(["--llm", "heuristic", "--category", "ambiguous_time", "--out", str(tmp_path),
                     "--fail-under", "0.9"]) == 1


def test_cli_no_matching_cases(tmp_path):
    assert run_main(["--case", "does_not_exist", "--out", str(tmp_path)]) == 2


def test_compare_cli(tmp_path, capsys):
    run_main(["--llm", "heuristic", "--category", "unclear", "--out", str(tmp_path), "--label", "a"])
    run_main(["--llm", "heuristic", "--category", "unclear", "--out", str(tmp_path), "--label", "b"])
    assert compare_main([str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 0
    assert "a -> b" in capsys.readouterr().out
    assert compare_main([]) == 2
