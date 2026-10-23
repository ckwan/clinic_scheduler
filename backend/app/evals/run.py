"""Command line entry point.

    python -m app.evals.run --llm heuristic --label heuristic-baseline
    python -m app.evals.run --llm ollama --label llama32-baseline
    python -m app.evals.run --llm ollama --model llama3.1:8b --label llama31-8b
    python -m app.evals.run --llm heuristic --category adversarial --fail-under 1.0
"""

import argparse
import subprocess
import sys
from pathlib import Path

from app.agent.providers import PROVIDERS, make_llm, model_label
from app.evals.cases import DEFAULT_CASES_DIR, load_cases
from app.evals.heuristic import HeuristicLLM
from app.evals.report import fmt, METRIC_ROWS, write_run
from app.evals.runner import CaseResult, run_cases, save_run

DEFAULT_OUT_DIR = DEFAULT_CASES_DIR.parent / "results"


def _git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Clinic Scheduler evals")
    parser.add_argument("--llm", choices=list(PROVIDERS), default="heuristic")
    parser.add_argument("--model", help="model name for ollama or anthropic (default from settings)")
    parser.add_argument("--label", help="name for this run (default: <llm>-run)")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--category", help="only run this category")
    parser.add_argument("--case", help="only run this case id")
    parser.add_argument("--save-db", action="store_true", help="also store the run in Postgres")
    parser.add_argument(
        "--fail-under",
        type=float,
        help="exit 1 if the overall pass rate is below this (0 to 1), or any bypass or leak occurs",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.case:
        cases = [c for c in cases if c.id == args.case]
    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 2

    if args.llm == "heuristic":
        factory = HeuristicLLM
    else:
        shared = make_llm(args.llm, args.model)  # one client for the whole run
        factory = lambda: shared  # noqa: E731
    model = model_label(args.llm, args.model)
    label = args.label or f"{args.llm}-run"

    def show(result: CaseResult) -> None:
        mark = "PASS" if result.passed else f"FAIL [{result.failure_category}]"
        print(f"{mark:<34} {result.id}")

    summary = run_cases(cases, factory, label, model, on_result=show)
    json_path, md_path = write_run(summary, args.out)

    print()
    for key, title, kind in METRIC_ROWS:
        print(f"{title:<42} {fmt(summary.metrics[key], kind)}")
    print(f"\nWrote {json_path} and {md_path}")

    if args.save_db:
        from app.db import SessionLocal

        with SessionLocal() as session:
            save_run(session, summary, git_sha=_git_sha())
            session.commit()
        print("Saved run to the database")

    if args.fail_under is not None:
        m = summary.metrics
        if m["verification_bypass_cases"] or m["info_leak_cases"] or (m["pass_rate"] or 0) < args.fail_under:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
