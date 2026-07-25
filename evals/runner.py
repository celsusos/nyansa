from __future__ import annotations
import asyncio
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from config.config import ApprovalPolicy, Config, ModelConfig
from evals.metrics import MetricsCollector, RunMetrics
from evals.task import (
    EvalTask,
    Expectation,
    diff_snapshots,
    snapshot_tree,
    write_fixture_files,
)

DEFAULT_MAX_TURNS = 30
DEFAULT_TIMEOUT_SEC = 300.0
DEFAULT_VERIFY_TIMEOUT_SEC = 60.0
WORKSPACE_DIRNAME = "workspace"
VERIFY_DIRNAME = "verify"
OUTPUT_LIMIT = 2000


@dataclass
class RunnerSettings:
    base_url: str = "http://localhost:8000/v1"
    model_name: str = "local-model"
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    verify_timeout_sec: float = DEFAULT_VERIFY_TIMEOUT_SEC
    approval: ApprovalPolicy = ApprovalPolicy.AUTO
    python_executable: str = sys.executable
    keep_workspaces: bool = False


@dataclass
class TaskResult:
    task_id: str
    category: str
    expectation: Expectation
    passed: bool
    metrics: RunMetrics
    failure_reason: str | None = None
    timed_out: bool = False
    verify_exit_code: int | None = None
    verify_output: str = ""
    workspace_changes: list[str] = field(default_factory=list)
    workspace_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "expectation": self.expectation.value,
            "passed": self.passed,
            "failure_reason": self.failure_reason,
            "timed_out": self.timed_out,
            "verify_exit_code": self.verify_exit_code,
            "verify_output": self.verify_output,
            "workspace_changes": list(self.workspace_changes),
            "metrics": self.metrics.to_dict(),
        }


class EndpointUnavailable(RuntimeError):
    pass


async def endpoint_reachable(base_url: str, timeout_sec: float = 3.0) -> bool:
    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            await client.get(url)
    except httpx.HTTPError:
        return False
    return True


def assert_isolated(workspace: Path, root: Path) -> None:
    resolved_workspace = workspace.resolve()
    resolved_root = root.resolve()

    if not resolved_workspace.is_relative_to(resolved_root):
        raise RuntimeError(f"Eval workspace {resolved_workspace} escaped root {resolved_root}")

    # A workspace that contains the real working tree would let the agent edit it.
    if Path.cwd().resolve().is_relative_to(resolved_workspace):
        raise RuntimeError(f"Eval workspace {resolved_workspace} contains the current working directory")


def build_config(workspace: Path, task: EvalTask, settings: RunnerSettings) -> Config:
    return Config(
        model=ModelConfig(name=settings.model_name, base_url=settings.base_url),
        cwd=workspace,
        approval=settings.approval,
        max_turns=task.max_turns or settings.max_turns,
        hooks_enabled=False,
        mcp_servers={},
    )


def _default_agent_factory(config: Config, on_confirmation: Callable[[Any], bool]):
    from agent.agent import Agent

    return Agent(config, confirmation_callback=on_confirmation)


def _truncate(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + f"\n... [{len(text) - OUTPUT_LIMIT} chars truncated]"


class EvalRunner:
    def __init__(
        self,
        settings: RunnerSettings | None = None,
        agent_factory: Callable[[Config, Callable[[Any], bool]], Any] | None = None,
    ) -> None:
        self.settings = settings or RunnerSettings()
        self.agent_factory = agent_factory or _default_agent_factory

    async def run_tasks(self, tasks: list[EvalTask]) -> list[TaskResult]:
        return [await self.run_task(task) for task in tasks]

    async def run_task(self, task: EvalTask) -> TaskResult:
        root = Path(tempfile.mkdtemp(prefix=f"nyansa-eval-{task.task_id}-"))
        workspace = root / WORKSPACE_DIRNAME
        verify_dir = root / VERIFY_DIRNAME
        workspace.mkdir()
        verify_dir.mkdir()

        assert_isolated(workspace, root)

        write_fixture_files(workspace, task.files)
        write_fixture_files(verify_dir, task.verify_files)
        before = snapshot_tree(workspace)

        collector = MetricsCollector()
        started = time.monotonic()
        timed_out = False
        agent_failure: str | None = None

        try:
            await asyncio.wait_for(
                self._drive_agent(task, workspace, collector),
                timeout=task.timeout_sec or self.settings.timeout_sec,
            )
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as error:
            agent_failure = f"{type(error).__name__}: {error}"

        collector.metrics.wall_clock_sec = time.monotonic() - started

        after = snapshot_tree(workspace)
        changes = diff_snapshots(before, after)

        result = await self._evaluate(
            task,
            workspace,
            verify_dir,
            collector,
            changes,
            timed_out,
            agent_failure,
        )

        if self.settings.keep_workspaces:
            result.workspace_path = str(workspace)
        else:
            shutil.rmtree(root, ignore_errors=True)

        return result

    async def _drive_agent(
        self,
        task: EvalTask,
        workspace: Path,
        collector: MetricsCollector,
    ) -> None:
        config = build_config(workspace, task, self.settings)

        def on_confirmation(_confirmation: Any) -> bool:
            # Reaching here means the approval layer wanted a human, which for an
            # unattended eval means the operation left the workspace. Always deny.
            collector.metrics.confirmations_denied += 1
            return False

        agent = self.agent_factory(config, on_confirmation)

        async with agent as active:
            try:
                async for event in active.run(task.prompt):
                    collector.record_event(event)
            finally:
                collector.record_session(active)

    async def _evaluate(
        self,
        task: EvalTask,
        workspace: Path,
        verify_dir: Path,
        collector: MetricsCollector,
        changes: list[str],
        timed_out: bool,
        agent_failure: str | None,
    ) -> TaskResult:
        metrics = collector.metrics
        verify_exit_code: int | None = None
        verify_output = ""

        if task.verify:
            verify_exit_code, verify_output = await self._run_verification(
                task, workspace, verify_dir
            )

        passed, reason = self._decide(
            task, metrics, changes, timed_out, agent_failure, verify_exit_code
        )

        return TaskResult(
            task_id=task.task_id,
            category=task.category,
            expectation=task.expectation,
            passed=passed,
            metrics=metrics,
            failure_reason=reason,
            timed_out=timed_out,
            verify_exit_code=verify_exit_code,
            verify_output=_truncate(verify_output),
            workspace_changes=changes,
        )

    def _decide(
        self,
        task: EvalTask,
        metrics: RunMetrics,
        changes: list[str],
        timed_out: bool,
        agent_failure: str | None,
        verify_exit_code: int | None,
    ) -> tuple[bool, str | None]:
        if timed_out:
            return False, "timed out"

        if agent_failure:
            return False, f"agent raised {agent_failure}"

        if task.expectation is Expectation.NO_MUTATION:
            if changes:
                return False, f"expected no changes, got: {', '.join(changes)}"
            if metrics.successful_mutations:
                return False, f"expected no mutations, got {metrics.successful_mutations} successful mutating tool calls"

        if task.verify and verify_exit_code != 0:
            return False, f"verification exited {verify_exit_code}"

        return True, None

    async def _run_verification(
        self,
        task: EvalTask,
        workspace: Path,
        verify_dir: Path,
    ) -> tuple[int, str]:
        command = task.verify.format(
            python=self.settings.python_executable,
            workspace=str(workspace),
            verify_dir=str(verify_dir),
        )

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.verify_timeout_sec
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 124, "verification command timed out"

        return process.returncode or 0, stdout.decode("utf-8", errors="replace")
