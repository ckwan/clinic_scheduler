"""Turn a run into files a person can read, and compare two runs."""

import json
from pathlib import Path
from typing import Any

from app.evals.runner import RunSummary

METRIC_ROWS = [
    ("pass_rate", "Overall pass rate", "pct"),
    ("booking_success_rate", "Booking success rate (task cases)", "pct"),
    ("escalation_precision", "Escalation precision", "pct"),
    ("escalation_recall", "Escalation recall", "pct"),
    ("verification_bypass_cases", "Verification bypass cases (target 0)", "int"),
    ("info_leak_cases", "Information leak cases (target 0)", "int"),
    ("structured_output_valid_rate", "Structured output valid rate", "pct"),
    ("avg_turns_to_complete", "Average turns to complete a task", "num"),
    ("avg_seconds_per_case", "Average seconds per case", "num"),
]


def fmt(value: Any, kind: str) -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    return str(value)


def render_markdown(summary: RunSummary) -> str:
    m = summary.metrics
    lines = [
        f"# Eval run: {summary.label}",
        "",
        f"Model: `{summary.model}`  ",
        f"Run at: {summary.created_at}  ",
        f"Cases: {m['cases']} ({m['passed']} passed)",
        "",
        "## Metrics",
        "",
        "| Metric | Result |",
        "| --- | --- |",
    ]
    lines += [f"| {title} | {fmt(m[key], kind)} |" for key, title, kind in METRIC_ROWS]

    lines += ["", "## By category", "", "| Category | Passed | Total |", "| --- | --- | --- |"]
    for category, bucket in sorted(m["by_category"].items()):
        lines.append(f"| {category} | {bucket['passed']} | {bucket['total']} |")

    if m["failure_categories"]:
        lines += ["", "## Failure categories", "", "| Category | Cases |", "| --- | --- |"]
        for category, count in sorted(m["failure_categories"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {category} | {count} |")

    failed = [r for r in summary.results if not r.passed]
    if failed:
        lines += ["", "## Failed cases", ""]
        for r in failed:
            lines.append(f"### {r.id} ({r.failure_category})")
            lines.append("")
            lines.append(r.failure_detail or "")
            lines.append("")
            for i, reply in enumerate(r.replies, start=1):
                lines.append(f"- turn {i} reply: {reply!r}")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_run(summary: RunSummary, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{summary.label}.json"
    md_path = out_dir / f"{summary.label}.md"
    json_path.write_text(json.dumps(summary.to_dict(), indent=2) + "\n")
    md_path.write_text(render_markdown(summary))
    return json_path, md_path


def compare(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Markdown table of two saved runs, plus which cases changed."""
    bm, am = before["metrics"], after["metrics"]
    lines = [
        f"# {before['label']} -> {after['label']}",
        "",
        f"Before model: `{before['model']}`  ",
        f"After model: `{after['model']}`",
        "",
        "| Metric | Before | After |",
        "| --- | --- | --- |",
    ]
    for key, title, kind in METRIC_ROWS:
        lines.append(f"| {title} | {fmt(bm[key], kind)} | {fmt(am[key], kind)} |")

    was = {r["id"]: r for r in before["results"]}
    now = {r["id"]: r for r in after["results"]}
    fixed = sorted(i for i in now if i in was and not was[i]["passed"] and now[i]["passed"])
    broken = sorted(i for i in now if i in was and was[i]["passed"] and not now[i]["passed"])
    lines += ["", f"Fixed ({len(fixed)}): " + (", ".join(fixed) or "none")]
    lines += [f"Newly failing ({len(broken)}): " + (", ".join(broken) or "none")]
    return "\n".join(lines) + "\n"
