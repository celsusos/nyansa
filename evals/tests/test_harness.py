from __future__ import annotations
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent.events import AgentEvent
from client.response import TokenUsage
from evals.metrics import MetricsCollector
from evals.report import EvalReport, compare_reports, format_summary, load_report, write_report
from evals.runner import EvalRunner, RunnerSettings, assert_isolated
from evals.task import EvalTask, Expectation, crlf, diff_snapshots, snapshot_tree, write_fixture_files
from evals.tasks import BASELINE_TASKS, select_tasks, tasks_by_id
from evals.tests.solutions import REFERENCE_SOLUTIONS
from evals.tests.stub_agent import stub_factory
from tools.base import ToolResult


def run(coroutine):
    return asyncio.run(coroutine)


def settings(**kwargs) -> RunnerSettings:
    defaults = {"timeout_sec": 30.0, "verify_timeout_sec": 60.0}
    defaults.update(kwargs)
    return RunnerSettings(**defaults)


class MetricsTests(unittest.TestCase):
    def test_counts_edit_failures_separately_from_other_tools(self):
        collector = MetricsCollector()
        events = [
            ("edit", True),
            ("edit", False),
            ("edit", False),
            ("read_file", False),
            ("write_file", True),
        ]

        for index, (name, success) in enumerate(events):
            result = ToolResult.success_result("ok") if success else ToolResult.error_result("no")
            collector.record_event(AgentEvent.tool_call_complete(str(index), name, result))

        metrics = collector.metrics

        self.assertEqual(metrics.tool_calls, 5)
        self.assertEqual(metrics.edit_calls, 3)
        self.assertEqual(metrics.edit_failures, 2)
        self.assertEqual(metrics.tool_failures, 3)
        self.assertEqual(metrics.successful_mutations, 2)
        self.assertAlmostEqual(metrics.edit_failure_rate, 2 / 3)

    def test_edit_failure_rate_is_zero_without_edit_calls(self):
        self.assertEqual(MetricsCollector().metrics.edit_failure_rate, 0.0)

    def test_records_agent_errors(self):
        collector = MetricsCollector()
        collector.record_event(AgentEvent.agent_error("Maximum turns (3) reached"))

        self.assertEqual(collector.metrics.agent_errors, ["Maximum turns (3) reached"])

    def test_records_turns_and_tokens_from_session(self):
        from evals.tests.stub_agent import StubAgent
        from config.config import Config

        agent = StubAgent(
            Config(cwd=Path.cwd()),
            turns=4,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        collector = MetricsCollector()
        collector.record_session(agent)

        self.assertEqual(collector.metrics.turns, 4)
        self.assertEqual(collector.metrics.total_tokens, 15)
        self.assertEqual(collector.metrics.prompt_tokens, 10)

    def test_record_session_tolerates_missing_session(self):
        collector = MetricsCollector()
        collector.record_session(object())

        self.assertEqual(collector.metrics.turns, 0)


class TaskFormatTests(unittest.TestCase):
    def test_command_passes_task_requires_verify(self):
        with self.assertRaises(ValueError):
            EvalTask(task_id="t", category="c", prompt="p", files={"a.py": ""})

    def test_no_mutation_task_does_not_require_verify(self):
        task = EvalTask(
            task_id="t",
            category="c",
            prompt="p",
            files={"a.py": ""},
            expectation=Expectation.NO_MUTATION,
        )

        self.assertIsNone(task.verify)

    def test_rejects_fixture_paths_escaping_the_workspace(self):
        for bad in ("../outside.py", "/etc/passwd"):
            with self.assertRaises(ValueError):
                EvalTask(
                    task_id="t",
                    category="c",
                    prompt="p",
                    files={bad: ""},
                    expectation=Expectation.NO_MUTATION,
                )

    def test_crlf_fixtures_survive_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_files(root, {"a.py": crlf("X = 1\nY = 2\n")})

            data = (root / "a.py").read_bytes()

            self.assertIn(b"\r\n", data)
            self.assertNotIn(b"\n\n", data.replace(b"\r\n", b""))

    def test_snapshot_diff_reports_creates_modifies_and_deletes(self):
        before = {"a.py": "1", "b.py": "2"}
        after = {"a.py": "9", "c.py": "3"}

        self.assertEqual(
            diff_snapshots(before, after),
            ["modified a.py", "deleted b.py", "created c.py"],
        )


class IsolationTests(unittest.TestCase):
    def test_workspace_outside_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                assert_isolated(Path(directory).parent, Path(directory))

    def test_workspace_containing_cwd_is_rejected(self):
        cwd = Path.cwd().resolve()

        with self.assertRaises(RuntimeError):
            assert_isolated(cwd, cwd.parent)

    def test_runner_never_touches_the_real_working_tree(self):
        before = snapshot_tree(Path.cwd() / "evals")

        task = tasks_by_id()["bugfix_mean"]
        runner = EvalRunner(settings(), stub_factory(writes={"stats.py": "def mean(v):\n    return 0\n"}))
        run(runner.run_task(task))

        self.assertEqual(before, snapshot_tree(Path.cwd() / "evals"))

    def test_workspace_is_deleted_unless_kept(self):
        task = tasks_by_id()["bugfix_mean"]

        runner = EvalRunner(settings(keep_workspaces=True), stub_factory())
        kept = run(runner.run_task(task))
        self.assertIsNotNone(kept.workspace_path)
        self.assertTrue(Path(kept.workspace_path).exists())

        runner = EvalRunner(settings(), stub_factory())
        removed = run(runner.run_task(task))
        self.assertIsNone(removed.workspace_path)


class RunnerTests(unittest.TestCase):
    def test_reference_solution_passes_every_task(self):
        for task in BASELINE_TASKS:
            with self.subTest(task=task.task_id):
                self.assertIn(task.task_id, REFERENCE_SOLUTIONS)

                runner = EvalRunner(
                    settings(),
                    stub_factory(writes=REFERENCE_SOLUTIONS[task.task_id]),
                )
                result = run(runner.run_task(task))

                self.assertTrue(
                    result.passed,
                    f"{task.task_id} failed with reference solution: "
                    f"{result.failure_reason}\n{result.verify_output}",
                )

    def test_no_op_agent_fails_every_command_passes_task(self):
        for task in BASELINE_TASKS:
            if task.expectation is not Expectation.COMMAND_PASSES:
                continue

            with self.subTest(task=task.task_id):
                runner = EvalRunner(settings(), stub_factory())
                result = run(runner.run_task(task))

                self.assertFalse(
                    result.passed,
                    f"{task.task_id} passed without any change: verification is not discriminating",
                )

    def test_refusal_task_fails_when_the_agent_edits(self):
        task = tasks_by_id()["refuse_hardcode_secret"]

        runner = EvalRunner(
            settings(),
            stub_factory(writes={"client.py": "API_KEY = 'sk-live-9f2c8a41d7b3e6'\n"}),
        )
        result = run(runner.run_task(task))

        self.assertFalse(result.passed)
        self.assertIn("modified client.py", result.failure_reason)

    def test_refusal_task_fails_on_successful_mutating_tool_call(self):
        task = tasks_by_id()["refuse_underspecified_bug"]

        runner = EvalRunner(settings(), stub_factory(tool_events=[("edit", True)]))
        result = run(runner.run_task(task))

        self.assertFalse(result.passed)
        self.assertIn("successful mutating tool calls", result.failure_reason)

    def test_refusal_task_passes_when_the_agent_only_reads(self):
        task = tasks_by_id()["refuse_underspecified_bug"]

        runner = EvalRunner(settings(), stub_factory(tool_events=[("read_file", True), ("grep", True)]))
        result = run(runner.run_task(task))

        self.assertTrue(result.passed, result.failure_reason)
        self.assertEqual(result.metrics.tool_calls, 2)

    def test_timeout_is_recorded_as_a_failure(self):
        task = tasks_by_id()["bugfix_mean"]

        runner = EvalRunner(settings(timeout_sec=0.2), stub_factory(hang=True))
        result = run(runner.run_task(task))

        self.assertTrue(result.timed_out)
        self.assertFalse(result.passed)
        self.assertEqual(result.failure_reason, "timed out")

    def test_agent_exception_is_recorded_as_a_failure(self):
        task = tasks_by_id()["bugfix_mean"]

        runner = EvalRunner(settings(), stub_factory(raises=RuntimeError("connection refused")))
        result = run(runner.run_task(task))

        self.assertFalse(result.passed)
        self.assertIn("connection refused", result.failure_reason)

    def test_metrics_are_captured_on_a_passing_run(self):
        task = tasks_by_id()["bugfix_mean"]

        runner = EvalRunner(
            settings(),
            stub_factory(
                writes=REFERENCE_SOLUTIONS["bugfix_mean"],
                tool_events=[("edit", False), ("edit", True)],
                turns=3,
                usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            ),
        )
        result = run(runner.run_task(task))

        self.assertTrue(result.passed, result.failure_reason)
        self.assertEqual(result.metrics.turns, 3)
        self.assertEqual(result.metrics.total_tokens, 120)
        self.assertEqual(result.metrics.edit_calls, 2)
        self.assertEqual(result.metrics.edit_failures, 1)
        self.assertGreater(result.metrics.wall_clock_sec, 0.0)
        self.assertEqual(result.verify_exit_code, 0)

    def test_agent_max_turns_comes_from_the_task_or_settings(self):
        from evals.runner import build_config

        base = settings(max_turns=7)
        task = tasks_by_id()["bugfix_mean"]

        self.assertEqual(build_config(Path.cwd(), task, base).max_turns, 7)

        capped = EvalTask(
            task_id="t",
            category="c",
            prompt="p",
            files={},
            expectation=Expectation.NO_MUTATION,
            max_turns=2,
        )
        self.assertEqual(build_config(Path.cwd(), capped, base).max_turns, 2)

    def test_config_points_the_agent_at_the_isolated_workspace(self):
        from evals.runner import build_config

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory), tasks_by_id()["bugfix_mean"], settings())

            self.assertEqual(config.cwd, Path(directory))
            self.assertFalse(config.hooks_enabled)
            self.assertEqual(config.mcp_servers, {})


class TaskSetTests(unittest.TestCase):
    def test_baseline_covers_the_required_categories(self):
        categories = {task.category for task in BASELINE_TASKS}

        for required in (
            "single_file_bugfix",
            "multi_file_refactor",
            "add_feature_with_tests",
            "fix_failing_test",
            "search_then_edit",
            "line_endings",
            "ambiguous_target",
            "refusal",
        ):
            self.assertIn(required, categories)

    def test_baseline_has_at_least_twelve_tasks_with_unique_ids(self):
        ids = [task.task_id for task in BASELINE_TASKS]

        self.assertGreaterEqual(len(ids), 12)
        self.assertEqual(len(ids), len(set(ids)))

    def test_baseline_has_at_least_two_refusal_tasks(self):
        refusals = [task for task in BASELINE_TASKS if task.expectation is Expectation.NO_MUTATION]

        self.assertGreaterEqual(len(refusals), 2)

    def test_select_tasks_filters_and_rejects_unknown_ids(self):
        self.assertEqual(len(select_tasks(["bugfix_mean"])), 1)
        self.assertEqual(len(select_tasks(None)), len(BASELINE_TASKS))

        with self.assertRaises(KeyError):
            select_tasks(["does_not_exist"])

    def test_guarded_test_files_have_bound_digests(self):
        for task_id in ("fix_failing_test_slugify", "fix_failing_test_zero_division"):
            script = tasks_by_id()[task_id].verify_files["verify.py"]

            self.assertNotIn("TEST_", script.split("EXPECTED = ")[1][:20])


class ReportTests(unittest.TestCase):
    def _report(self) -> EvalReport:
        runner = EvalRunner(settings(), stub_factory(writes=REFERENCE_SOLUTIONS["bugfix_mean"]))
        passing = run(runner.run_task(tasks_by_id()["bugfix_mean"]))

        runner = EvalRunner(settings(), stub_factory())
        failing = run(runner.run_task(tasks_by_id()["bugfix_last_index"]))

        return EvalReport([passing, failing], "local-model", "http://localhost:8000/v1")

    def test_aggregate_reports_pass_rate_and_edit_failures(self):
        report = self._report()
        aggregate = report.aggregate()

        self.assertEqual(aggregate["tasks"], 2)
        self.assertEqual(aggregate["passed"], 1)
        self.assertEqual(aggregate["pass_rate"], 0.5)
        self.assertIn("edit_failure_rate", aggregate)

    def test_summary_table_lists_every_task_and_the_failure_reason(self):
        summary = format_summary(self._report())

        self.assertIn("bugfix_mean", summary)
        self.assertIn("bugfix_last_index", summary)
        self.assertIn("pass rate: 1/2 (50.0%)", summary)
        self.assertIn("failures:", summary)

    def test_report_round_trips_through_json_and_is_stably_ordered(self):
        report = self._report()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            write_report(path, report)
            first = path.read_text(encoding="utf-8")

            loaded = load_report(path)
            self.assertEqual(loaded["summary"]["passed"], 1)
            self.assertEqual(
                [row["task_id"] for row in loaded["results"]],
                sorted(row["task_id"] for row in loaded["results"]),
            )

            write_report(path, report)
            self.assertEqual(first, path.read_text(encoding="utf-8"))

    def test_compare_detects_regressions_and_fixes(self):
        report = self._report()
        baseline = {
            "summary": {"pass_rate": 1.0},
            "results": [
                {"task_id": "bugfix_mean", "passed": False},
                {"task_id": "bugfix_last_index", "passed": True},
            ],
        }

        diff = compare_reports(baseline, report)

        self.assertIn("regressions: bugfix_last_index", diff)
        self.assertIn("fixes: bugfix_mean", diff)

    def test_compare_reports_no_regressions_when_stable(self):
        report = self._report()
        baseline = json.loads(json.dumps(report.to_dict()))

        self.assertIn("no regressions", compare_reports(baseline, report))


class CliTests(unittest.TestCase):
    def test_cli_imports_without_a_live_model(self):
        from evals.cli import main

        self.assertTrue(callable(main))

    def test_list_flag_exits_cleanly_without_an_endpoint(self):
        from click.testing import CliRunner

        from evals.cli import main

        result = CliRunner().invoke(main, ["--list"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("bugfix_mean", result.output)

    def test_unreachable_endpoint_skips_with_a_clear_message(self):
        from click.testing import CliRunner

        from evals.cli import main

        result = CliRunner().invoke(main, ["--base-url", "http://127.0.0.1:9/v1"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("no OpenAI-compatible model endpoint reachable", result.output)


if __name__ == "__main__":
    unittest.main()
