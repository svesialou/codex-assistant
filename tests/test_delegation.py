from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_telegram_bot.config import Config, normalize_orchestrator_mode
from codex_telegram_bot.project_index import ProjectInfo
from codex_telegram_bot.services.delegation import (
    DelegationService,
    PlanError,
    ProcessResult,
    ProcessSpec,
    SubprocessRunner,
    parse_plan,
    parse_subtasks,
)
from codex_telegram_bot.task_store import TaskStore


def config_for(tmp: str, **overrides) -> Config:
    return Config(
        bot_token="token",
        allowed_chat_ids={10},
        allowed_user_ids=set(),
        project_roots=[],
        index_dir=Path(tmp) / "index",
        state_dir=Path(tmp) / "state",
        codex_bin="codex",
        model=None,
        poll_timeout_seconds=30,
        prompt_debounce_seconds=60,
        plan_timeout_seconds=60,
        run_timeout_seconds=None,
        transcribe_command=None,
        transcribe_timeout_seconds=300,
        env_file=Path(tmp) / "telegram.env",
        orchestrator_mode="delegate",
        **overrides,
    )


def project_for(path: str) -> ProjectInfo:
    return ProjectInfo(
        slug="demo",
        name="demo",
        path=path,
        base=path,
        is_git=False,
        branch=None,
        origin=None,
        languages=["Python"],
        markers=[],
        docs=[],
        test_hints=[],
    )


def sub(sid: str, role: str = "coder", deps: list[str] | None = None, files: list[str] | None = None):
    return {
        "id": sid,
        "role": role,
        "title": f"title {sid}",
        "brief": f"do {sid}",
        "depends_on": deps or [],
        "files": files if files is not None else [f"{sid}.py"],
        "acceptance": ["it works: `make test` passes"],
    }


def plan_json(*subtasks: dict) -> str:
    return json.dumps({"summary": "demo plan", "risk": "low", "subtasks": list(subtasks)})


def worker_done(summary: str = "ok") -> str:
    return json.dumps({"status": "done", "summary": summary})


def dispatch(*items: dict) -> str:
    return json.dumps({"status": "continue", "dispatch": list(items)})


def executor_done() -> str:
    return json.dumps({"status": "done", "final_report": "Changed: all"})


def critic(verdict: str, **extra) -> str:
    return json.dumps({"verdict": verdict, "summary": f"critic {verdict}", **extra})


class FakeProcesses:
    """Scripted replies per role; records every spawned process."""

    def __init__(self, **scripts: list[str]) -> None:
        self.scripts = {role: list(replies) for role, replies in scripts.items()}
        self.specs: list[ProcessSpec] = []
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.worker_delay = 0.0
        # Per-subtask worker outcomes, keyed by subtask id; take priority over `coder`/`researcher`.
        self.worker_results: dict[str, list[ProcessResult]] = {}

    def __call__(self, spec: ProcessSpec) -> ProcessResult:
        with self.lock:
            self.specs.append(spec)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if spec.role in {"coder", "researcher"} and self.worker_delay:
                time.sleep(self.worker_delay)
            with self.lock:
                for key, results in self.worker_results.items():
                    if spec.name.endswith(f"-{key}") and results:
                        return results.pop(0)
                replies = self.scripts.get(spec.role) or []
                if not replies:
                    raise AssertionError(f"no scripted reply left for {spec.role}")
                return ProcessResult(returncode=0, text=replies.pop(0))
        finally:
            with self.lock:
                self.running -= 1

    def roles(self) -> list[str]:
        return [spec.role for spec in self.specs]


class DelegationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.project_dir = Path(self.tmp) / "project"
        self.project_dir.mkdir()
        self.events: list[tuple[str, str]] = []
        self.notes: list[str] = []

    def run_pipeline(self, processes: FakeProcesses, **config_overrides):
        config = config_for(self.tmp, **config_overrides)
        store = TaskStore(config.state_dir)
        task = store.create_task(
            chat_id=10,
            user_id=None,
            project_slug="demo",
            project_name="demo",
            project_path=str(self.project_dir),
            prompt="Add feature X",
        )
        service = DelegationService(config, store, process_runner=processes)
        task = service.run(
            task,
            project_for(str(self.project_dir)),
            notify=self.notes.append,
            append_event=lambda task_id, kind, agent, message, data=None: self.events.append(
                (agent, message)
            ),
        )
        return store, task

    def state(self, store: TaskStore, task) -> dict:
        return json.loads((store.task_dir(task.id) / "delegation" / "state.json").read_text())


class PlanValidationTest(unittest.TestCase):
    def test_valid_plan_parses(self) -> None:
        plan = parse_plan(plan_json(sub("a", "researcher"), sub("b", deps=["a"])), 5)
        self.assertEqual([s.id for s in plan.subtasks], ["a", "b"])
        self.assertEqual(plan.subtasks[1].depends_on, ["a"])

    def test_rejects_cycle(self) -> None:
        with self.assertRaisesRegex(PlanError, "cycle"):
            parse_plan(plan_json(sub("a", deps=["b"]), sub("b", deps=["a"])), 5)

    def test_rejects_unknown_role_dependency_and_path_like_id(self) -> None:
        with self.assertRaisesRegex(PlanError, "unknown role"):
            parse_plan(plan_json(sub("a", "wizard")), 5)
        with self.assertRaisesRegex(PlanError, "unknown id"):
            parse_plan(plan_json(sub("a", deps=["zzz"])), 5)
        with self.assertRaisesRegex(PlanError, "invalid subtask id"):
            parse_plan(plan_json(sub("../evil")), 5)

    def test_rejects_duplicates_empty_brief_limit_and_garbage(self) -> None:
        with self.assertRaisesRegex(PlanError, "duplicate"):
            parse_plan(plan_json(sub("a"), sub("a")), 5)
        with self.assertRaisesRegex(PlanError, "empty brief"):
            parse_plan(plan_json({**sub("a"), "brief": " "}), 5)
        with self.assertRaisesRegex(PlanError, "too many"):
            parse_plan(plan_json(sub("a"), sub("b")), 1)
        with self.assertRaisesRegex(PlanError, "not a JSON"):
            parse_plan("sorry, no plan", 5)

    def test_questions_skip_subtask_validation(self) -> None:
        plan = parse_plan(json.dumps({"questions": ["Which db?"], "subtasks": []}), 5)
        self.assertEqual(plan.questions, ["Which db?"])

    def test_dotfile_paths_keep_their_leading_dot(self) -> None:
        plan = parse_plan(plan_json(sub("a", files=[".github/workflows/ci.yml", "./x/../y.py"])), 5)
        self.assertEqual(plan.subtasks[0].files, [".github/workflows/ci.yml", "y.py"])

    def test_fix_subtasks_get_prefix_and_keep_known_deps(self) -> None:
        fixes = parse_subtasks(
            [sub("f1", deps=["s1"]), sub("f2", deps=["f1"])],
            5,
            known_ids={"s1"},
            id_prefix="r1-",
        )
        self.assertEqual([f.id for f in fixes], ["r1-f1", "r1-f2"])
        self.assertEqual(fixes[0].depends_on, ["s1"])
        self.assertEqual(fixes[1].depends_on, ["r1-f1"])


class DelegationFlowTest(DelegationTestCase):
    def test_happy_path_routes_models_and_parallelizes_independent_workers(self) -> None:
        processes = FakeProcesses(
            planner=[
                plan_json(
                    sub("r1", "researcher", files=[]),
                    sub("c1", deps=["r1"], files=["a.py"]),
                    sub("c2", deps=["r1"], files=["b.py"]),
                )
            ],
            executor=[
                dispatch({"id": "r1", "brief": "find the pattern"}),
                dispatch({"id": "c1"}, {"id": "c2"}),
                executor_done(),
            ],
            researcher=[worker_done("pattern found")],
            coder=[worker_done("c1 done"), worker_done("c2 done")],
            critic=[critic("approve")],
        )
        processes.worker_delay = 0.2

        store, task = self.run_pipeline(processes)

        self.assertEqual(task.phase, "completed", task.error)
        self.assertEqual(
            processes.roles(),
            ["planner", "executor", "researcher", "executor", "coder", "coder", "executor", "critic"],
        )
        by_role = {spec.role: spec for spec in processes.specs}
        for role in ("planner", "executor", "critic"):
            self.assertEqual(by_role[role].model, "opus")
            self.assertEqual(by_role[role].effort, "medium")
        self.assertEqual(by_role["coder"].model, "sonnet")
        self.assertEqual(by_role["researcher"].model, "haiku")
        self.assertEqual(processes.max_running, 2)
        # The worker brief carries what the executor wrote and the dependency result.
        coder_prompt = next(s.prompt for s in processes.specs if s.role == "coder")
        self.assertIn("pattern found", coder_prompt)
        self.assertIn("Changed: all", Path(task.final_path).read_text(encoding="utf-8"))
        self.assertEqual({s["status"] for s in self.state(store, task)["subtasks"]}, {"done"})

    def test_lead_roles_cannot_implement(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"), sub("r1", "researcher", files=[]))],
            executor=[dispatch({"id": "c1"}, {"id": "r1"}), executor_done()],
            coder=[worker_done()],
            researcher=[worker_done()],
            critic=[critic("approve")],
        )
        self.run_pipeline(processes)
        by_role = {spec.role: spec for spec in processes.specs}
        for tool in ("Edit", "Write", "NotebookEdit", "Bash", "Agent", "Workflow"):
            self.assertIn(tool, by_role["executor"].disallowed_tools)
        for role in ("planner", "critic", "researcher"):
            for tool in ("Edit", "Write", "NotebookEdit"):
                self.assertIn(tool, by_role[role].disallowed_tools, role)
        self.assertEqual(by_role["coder"].disallowed_tools, ())

    def test_coders_with_overlapping_files_are_not_run_in_parallel(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1", files=["pkg/a.py"]), sub("c2", files=["pkg"]))],
            executor=[
                dispatch({"id": "c1"}, {"id": "c2"}),
                dispatch({"id": "c2"}),
                executor_done(),
            ],
            coder=[worker_done(), worker_done()],
            critic=[critic("approve")],
        )
        processes.worker_delay = 0.1
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "completed", task.error)
        self.assertEqual(processes.max_running, 1)

    def test_parallelism_is_capped_by_config(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"), sub("c2"), sub("c3"))],
            executor=[
                dispatch({"id": "c1"}, {"id": "c2"}, {"id": "c3"}),
                dispatch({"id": "c2"}, {"id": "c3"}),
                executor_done(),
            ],
            coder=[worker_done()] * 3,
            critic=[critic("approve")],
        )
        processes.worker_delay = 0.1
        _, task = self.run_pipeline(processes, delegate_max_parallel=2)
        self.assertEqual(task.phase, "completed", task.error)
        self.assertEqual(processes.max_running, 2)

    def test_dependency_order_is_enforced_even_if_executor_dispatches_early(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("a"), sub("b", deps=["a"], files=["b.py"]))],
            executor=[
                dispatch({"id": "b"}, {"id": "a"}),
                dispatch({"id": "b"}),
                executor_done(),
            ],
            coder=[worker_done("a"), worker_done("b")],
            critic=[critic("approve")],
        )
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "completed", task.error)
        workers = [s.name for s in processes.specs if s.role == "coder"]
        self.assertTrue(workers[0].endswith("-a") and workers[1].endswith("-b"))

    def test_worker_attempt_limit_stops_a_run_that_cannot_progress(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"))],
            executor=[dispatch({"id": "c1"})] * 4,
        )
        processes.worker_results["c1"] = [
            ProcessResult(returncode=1, text="boom"),
            ProcessResult(returncode=0, text="no json here"),
        ]
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "failed")
        self.assertIn("no progress", task.error)
        self.assertEqual(processes.roles().count("coder"), 2)
        self.assertNotIn("critic", processes.roles())

    def test_critic_request_changes_runs_fix_round_then_approves(self) -> None:
        fix = {**sub("f1", files=["a.py"]), "brief": "fix the null check in a.py"}
        processes = FakeProcesses(
            planner=[plan_json(sub("c1", files=["a.py"]))],
            executor=[
                dispatch({"id": "c1"}),
                executor_done(),
                dispatch({"id": "r1-f1"}),
                executor_done(),
            ],
            coder=[worker_done("impl"), worker_done("fixed")],
            critic=[
                critic("request_changes", fix_subtasks=[fix]),
                critic("approve"),
            ],
        )
        store, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "completed", task.error)
        self.assertEqual(processes.roles().count("critic"), 2)
        self.assertIn("r1-f1", [s["id"] for s in self.state(store, task)["subtasks"]])
        third_executor_prompt = [s.prompt for s in processes.specs if s.role == "executor"][2]
        self.assertIn("r1-f1", third_executor_prompt)

    def test_critic_still_unhappy_after_max_rounds_needs_human(self) -> None:
        fix = sub("f1")
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"))],
            executor=[
                dispatch({"id": "c1"}),
                executor_done(),
                dispatch({"id": "r1-f1"}),
                executor_done(),
            ],
            coder=[worker_done(), worker_done()],
            critic=[
                critic("request_changes", fix_subtasks=[fix]),
                critic("request_changes", fix_subtasks=[fix]),
            ],
        )
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "needs_human")
        self.assertIn("2 rounds", task.error)

    def test_critic_needs_human_stops_immediately(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"))],
            executor=[dispatch({"id": "c1"}), executor_done()],
            coder=[worker_done()],
            critic=[critic("needs_human")],
        )
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "needs_human")
        self.assertEqual(processes.roles().count("critic"), 1)

    def test_invalid_critic_verdict_never_counts_as_approval(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"))],
            executor=[dispatch({"id": "c1"}), executor_done()],
            coder=[worker_done()],
            critic=["looks good to me", "{}"],
        )
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "needs_human")
        self.assertIn("invalid verdict", task.error)

    def test_invalid_plan_twice_fails_without_running_anything_else(self) -> None:
        processes = FakeProcesses(planner=["not json", plan_json(sub("a", deps=["a"]))])
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "failed")
        self.assertIn("invalid plan", task.error)
        self.assertEqual(processes.roles(), ["planner", "planner"])
        self.assertIn("rejected", processes.specs[1].prompt)

    def test_planner_questions_stop_for_human(self) -> None:
        processes = FakeProcesses(
            planner=[json.dumps({"questions": ["Which database?"], "subtasks": []})]
        )
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "needs_human")
        self.assertEqual(processes.roles(), ["planner"])

    def test_executor_done_with_open_subtasks_is_rejected(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"))],
            executor=[executor_done(), executor_done()],
        )
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "failed")
        self.assertNotIn("critic", processes.roles())

    def test_executor_blocked_needs_human(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"))],
            executor=[json.dumps({"status": "blocked", "final_report": "missing credentials"})],
        )
        _, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "needs_human")
        self.assertIn("missing credentials", task.error)

    def test_executor_can_add_fix_subtask_for_failed_worker(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1", files=["a.py"]))],
            executor=[
                dispatch({"id": "c1"}),
                dispatch(
                    {
                        "id": "c1-fix1",
                        "role": "coder",
                        "brief": "repair c1: tests failed",
                        "files": ["a.py"],
                    }
                ),
                dispatch({"id": "c1"}),
                executor_done(),
            ],
            critic=[critic("approve")],
        )
        processes.worker_results["c1"] = [
            ProcessResult(returncode=1, text="boom"),
            ProcessResult(returncode=0, text=worker_done("c1 now ok")),
        ]
        processes.worker_results["c1-fix1"] = [ProcessResult(returncode=0, text=worker_done("fix"))]
        store, task = self.run_pipeline(processes)
        self.assertEqual(task.phase, "completed", task.error)
        statuses = {s["id"]: s["status"] for s in self.state(store, task)["subtasks"]}
        self.assertEqual(statuses, {"c1": "done", "c1-fix1": "done"})

    def test_cancel_stops_pipeline_without_touching_phase(self) -> None:
        def canceled(spec: ProcessSpec) -> ProcessResult:
            return ProcessResult(returncode=-1, canceled=True)

        config = config_for(self.tmp)
        store = TaskStore(config.state_dir)
        task = store.create_task(10, None, "demo", "demo", str(self.project_dir), "x")
        task = DelegationService(config, store, process_runner=canceled).run(
            task, project_for(str(self.project_dir))
        )
        self.assertEqual(task.phase, "running")

    def test_events_and_notifications_report_progress(self) -> None:
        processes = FakeProcesses(
            planner=[plan_json(sub("c1"))],
            executor=[dispatch({"id": "c1"}), executor_done()],
            coder=[worker_done()],
            critic=[critic("approve")],
        )
        self.run_pipeline(processes)
        agents = {agent for agent, _ in self.events}
        self.assertTrue(
            {"DelegatePlanner", "Worker:c1", "DelegateExecutor", "DelegateCritic"} <= agents
        )
        self.assertTrue(any(note.startswith("Critic: approve") for note in self.notes))


class ConfigAndCommandTest(unittest.TestCase):
    def test_mode_defaults_to_classic_and_rejects_unknown(self) -> None:
        self.assertEqual(normalize_orchestrator_mode(None), "classic")
        self.assertEqual(normalize_orchestrator_mode(" Delegate "), "delegate")
        with self.assertRaises(ValueError):
            normalize_orchestrator_mode("chaos")

    def test_subprocess_argv_carries_model_effort_and_tool_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = SubprocessRunner(config_for(tmp), lambda: False)
            argv = runner.argv(
                ProcessSpec(
                    name="01-executor",
                    role="executor",
                    model="opus",
                    effort="medium",
                    system_prompt="SYS",
                    prompt="p",
                    cwd=tmp,
                    permission_mode="bypassPermissions",
                    disallowed_tools=("Edit", "Write", "Bash"),
                    timeout_seconds=5,
                    log_dir=Path(tmp),
                )
            )
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Edit,Write,Bash")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "SYS")

    def test_subprocess_runner_times_out_and_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_claude.sh"
            script.write_text("#!/bin/sh\ncat >/dev/null\nsleep 30\n", encoding="utf-8")
            script.chmod(0o755)
            config = config_for(tmp, claude_executor_command=str(script))
            spec = ProcessSpec(
                name="t",
                role="coder",
                model="sonnet",
                effort=None,
                system_prompt="s",
                prompt="p",
                cwd=tmp,
                permission_mode="bypassPermissions",
                disallowed_tools=(),
                timeout_seconds=1,
                log_dir=Path(tmp) / "logs",
            )
            started = time.monotonic()
            result = SubprocessRunner(config, lambda: False)(spec)
            self.assertTrue(result.timed_out)
            self.assertLess(time.monotonic() - started, 15)
            result = SubprocessRunner(config, lambda: True)(spec)
            self.assertTrue(result.canceled)


if __name__ == "__main__":
    unittest.main()
