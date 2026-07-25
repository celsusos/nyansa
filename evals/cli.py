from __future__ import annotations
import asyncio
import sys
from pathlib import Path

import click

from config.config import ApprovalPolicy
from evals.report import EvalReport, compare_reports, format_summary, load_report, write_report
from evals.runner import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SEC,
    EvalRunner,
    RunnerSettings,
    endpoint_reachable,
)
from evals.tasks import BASELINE_TASKS, select_tasks

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_SKIPPED = 0


@click.command()
@click.option("--task", "task_ids", multiple=True, help="Run only these task ids.")
@click.option("--category", default=None, help="Run only tasks in this category.")
@click.option("--list", "list_only", is_flag=True, help="List the task set and exit.")
@click.option("--base-url", default="http://localhost:8000/v1", show_default=True)
@click.option("--model", "model_name", default="local-model", show_default=True)
@click.option("--max-turns", default=DEFAULT_MAX_TURNS, show_default=True)
@click.option("--timeout", "timeout_sec", default=DEFAULT_TIMEOUT_SEC, show_default=True)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=Path("evals/results/latest.json"),
    show_default=True,
)
@click.option(
    "--baseline",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Compare against a previous results file and report regressions.",
)
@click.option("--keep-workspaces", is_flag=True, help="Do not delete task workspaces.")
@click.option(
    "--skip-endpoint-check",
    is_flag=True,
    help="Run even if the model endpoint does not answer a preflight request.",
)
def main(
    task_ids: tuple[str, ...],
    category: str | None,
    list_only: bool,
    base_url: str,
    model_name: str,
    max_turns: int,
    timeout_sec: float,
    output: Path,
    baseline: Path | None,
    keep_workspaces: bool,
    skip_endpoint_check: bool,
) -> None:
    try:
        tasks = select_tasks(list(task_ids))
    except KeyError as error:
        raise click.ClickException(str(error)) from error

    if category:
        tasks = [task for task in tasks if task.category == category]
        if not tasks:
            raise click.ClickException(f"No tasks in category {category!r}")

    if list_only:
        for task in tasks:
            click.echo(f"{task.task_id}  [{task.category}]  {task.expectation.value}")
        click.echo(f"\n{len(tasks)} tasks ({len(BASELINE_TASKS)} in the baseline set)")
        sys.exit(EXIT_OK)

    if not skip_endpoint_check and not asyncio.run(endpoint_reachable(base_url)):
        click.echo(
            f"Skipping evals: no OpenAI-compatible model endpoint reachable at {base_url}.\n"
            "Start a server, pass --base-url, or use --skip-endpoint-check to run anyway.",
            err=True,
        )
        sys.exit(EXIT_SKIPPED)

    settings = RunnerSettings(
        base_url=base_url,
        model_name=model_name,
        max_turns=max_turns,
        timeout_sec=timeout_sec,
        approval=ApprovalPolicy.AUTO,
        keep_workspaces=keep_workspaces,
    )

    runner = EvalRunner(settings)
    click.echo(f"Running {len(tasks)} tasks against {model_name} at {base_url}\n")

    results = asyncio.run(_run(runner, tasks))
    report = EvalReport(results=results, model_name=model_name, base_url=base_url)

    write_report(output, report)
    click.echo("\n" + format_summary(report))
    click.echo(f"\nresults written to {output}")

    if baseline:
        click.echo("\ncompared to baseline:")
        click.echo(compare_reports(load_report(baseline), report))

    sys.exit(EXIT_OK if report.passed == report.total else EXIT_FAILURES)


async def _run(runner: EvalRunner, tasks: list) -> list:
    results = []
    for index, task in enumerate(tasks, start=1):
        click.echo(f"[{index}/{len(tasks)}] {task.task_id} ... ", nl=False)
        result = await runner.run_task(task)
        status = "pass" if result.passed else f"fail ({result.failure_reason})"
        click.echo(status)
        results.append(result)
    return results


if __name__ == "__main__":
    main()
