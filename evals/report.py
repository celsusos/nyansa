from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.runner import TaskResult

SCHEMA_VERSION = 1


@dataclass
class EvalReport:
    results: list[TaskResult]
    model_name: str
    base_url: str

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def pass_rate(self) -> float:
        if not self.total:
            return 0.0
        return self.passed / self.total

    def aggregate(self) -> dict[str, Any]:
        edit_calls = sum(result.metrics.edit_calls for result in self.results)
        edit_failures = sum(result.metrics.edit_failures for result in self.results)

        return {
            "tasks": self.total,
            "passed": self.passed,
            "failed": self.total - self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "timeouts": sum(1 for result in self.results if result.timed_out),
            "total_turns": sum(result.metrics.turns for result in self.results),
            "total_tokens": sum(result.metrics.total_tokens for result in self.results),
            "total_tool_calls": sum(result.metrics.tool_calls for result in self.results),
            "total_wall_clock_sec": round(
                sum(result.metrics.wall_clock_sec for result in self.results), 2
            ),
            "edit_calls": edit_calls,
            "edit_failures": edit_failures,
            "edit_failure_rate": round(edit_failures / edit_calls, 4) if edit_calls else 0.0,
            "confirmations_denied": sum(
                result.metrics.confirmations_denied for result in self.results
            ),
        }

    def by_category(self) -> dict[str, dict[str, int]]:
        buckets: dict[str, dict[str, int]] = {}
        for result in self.results:
            bucket = buckets.setdefault(result.category, {"passed": 0, "total": 0})
            bucket["total"] += 1
            if result.passed:
                bucket["passed"] += 1
        return dict(sorted(buckets.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "model": self.model_name,
            "base_url": self.base_url,
            "summary": self.aggregate(),
            "by_category": self.by_category(),
            "results": [
                result.to_dict()
                for result in sorted(self.results, key=lambda item: item.task_id)
            ],
        }


def write_report(path: Path, report: EvalReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=False)
    path.write_text(payload + "\n", encoding="utf-8")


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _status(result: TaskResult) -> str:
    if result.passed:
        return "PASS"
    if result.timed_out:
        return "TIMEOUT"
    return "FAIL"


def format_summary(report: EvalReport) -> str:
    rows = [
        (
            result.task_id,
            _status(result),
            str(result.metrics.turns),
            f"{result.metrics.wall_clock_sec:.1f}s",
            str(result.metrics.total_tokens),
            str(result.metrics.tool_calls),
            f"{result.metrics.edit_failures}/{result.metrics.edit_calls}",
        )
        for result in sorted(report.results, key=lambda item: item.task_id)
    ]

    headers = ("task", "status", "turns", "wall", "tokens", "tools", "edit fail")
    widths = [
        max(len(headers[column]), *(len(row[column]) for row in rows)) if rows else len(headers[column])
        for column in range(len(headers))
    ]

    def render(values: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[column]) for column, value in enumerate(values)).rstrip()

    lines = [render(headers), render(tuple("-" * width for width in widths))]
    lines.extend(render(row) for row in rows)

    aggregate = report.aggregate()
    lines.append("")
    lines.append(
        f"pass rate: {aggregate['passed']}/{aggregate['tasks']} "
        f"({aggregate['pass_rate'] * 100:.1f}%)"
    )
    lines.append(
        f"edit failures: {aggregate['edit_failures']}/{aggregate['edit_calls']} calls "
        f"({aggregate['edit_failure_rate'] * 100:.1f}%)"
    )
    lines.append(
        f"totals: {aggregate['total_turns']} turns, "
        f"{aggregate['total_tokens']} tokens, "
        f"{aggregate['total_tool_calls']} tool calls, "
        f"{aggregate['total_wall_clock_sec']}s"
    )

    lines.append("")
    lines.append("by category:")
    for category, bucket in report.by_category().items():
        lines.append(f"  {category}: {bucket['passed']}/{bucket['total']}")

    failures = [result for result in report.results if not result.passed]
    if failures:
        lines.append("")
        lines.append("failures:")
        for result in sorted(failures, key=lambda item: item.task_id):
            lines.append(f"  {result.task_id}: {result.failure_reason or 'unknown'}")

    return "\n".join(lines)


def compare_reports(baseline: dict[str, Any], current: EvalReport) -> str:
    old = {row["task_id"]: bool(row["passed"]) for row in baseline.get("results", [])}
    new = {result.task_id: result.passed for result in current.results}

    regressions = sorted(task for task, passed in new.items() if old.get(task) and not passed)
    fixes = sorted(task for task, passed in new.items() if passed and old.get(task) is False)
    added = sorted(task for task in new if task not in old)
    removed = sorted(task for task in old if task not in new)

    old_rate = baseline.get("summary", {}).get("pass_rate", 0.0)
    lines = [
        f"pass rate: {old_rate * 100:.1f}% -> {current.pass_rate * 100:.1f}%",
    ]

    for label, entries in (
        ("regressions", regressions),
        ("fixes", fixes),
        ("new tasks", added),
        ("removed tasks", removed),
    ):
        if entries:
            lines.append(f"{label}: {', '.join(entries)}")

    if not regressions:
        lines.append("no regressions")

    return "\n".join(lines)
