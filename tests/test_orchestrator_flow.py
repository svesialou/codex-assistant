from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.bot import CodexTelegramBot
from codex_telegram_bot.config import Config
from codex_telegram_bot.project_index import ProjectInfo
from codex_telegram_bot.services.llm_provider import LlmRequest, LlmResponse, MockProvider
from codex_telegram_bot.services.llm_router import FollowupClassifier
from codex_telegram_bot.task_store import TaskRecord, TaskStore


def config_for(
    tmp: str,
    orchestrator: bool = True,
    task_provider: str = "codex",
    router_learning: bool = True,
) -> Config:
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
        orchestrator_default_mode=orchestrator,
        task_provider=task_provider,
        router_learning=router_learning,
    )


def demo_project() -> ProjectInfo:
    return ProjectInfo(
        slug="demo",
        name="demo",
        path="/tmp/demo",
        base="/tmp",
        is_git=False,
        branch=None,
        origin=None,
        languages=["Python"],
        markers=[],
        docs=[],
        test_hints=[],
    )


class FakeRunner:
    def __init__(
        self,
        store: TaskStore,
        unavailable: dict[str, str] | None = None,
        returncodes: list[int] | None = None,
    ) -> None:
        self.store = store
        self.calls: list[tuple[str, str | None, str]] = []
        self.efforts: list[str | None] = []
        self.runtime_id = "fake"
        self.unavailable = unavailable or {}
        # Return code per call; missing entries succeed.
        self.returncodes = list(returncodes or [])

    def provider_unavailable_reason(self, provider: str) -> str:
        return self.unavailable.get(provider, "")

    def _run(
        self,
        name: str,
        task: TaskRecord,
        model: str | None,
        provider: str,
        effort: str | None,
        success_phase: str = "completed",
    ) -> TaskRecord:
        self.calls.append((name, model, provider))
        self.efforts.append(effort)
        returncode = self.returncodes.pop(0) if self.returncodes else 0
        task.returncode = returncode
        task.phase = success_phase if returncode == 0 else "failed"
        task.final_path = str(self.store.task_dir(task.id) / "final.md")
        task.run_log_path = str(self.store.task_dir(task.id) / "run.log")
        Path(task.final_path).write_text(
            "- Changed:\n- done\n- Verified:\n- fake\n" if returncode == 0 else "",
            encoding="utf-8",
        )
        with open(task.run_log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps({"type": "result", "total_cost_usd": 0.5,
                                  "usage": {"input_tokens": 100, "output_tokens": 50}}) + "\n")
        self.store.save_task(task)
        return task

    def run_execution(self, task, project, model=None, provider="codex", effort=None):
        return self._run("run_execution", task, model, provider, effort)

    def run_recovery_execution(self, task, project, model=None, provider="codex", effort=None):
        return self._run("run_recovery_execution", task, model, provider, effort)

    def run_revision_execution(
        self, task, project, round_number, model=None, provider="codex", effort=None
    ):
        return self._run("run_revision_execution", task, model, provider, effort)

    def run_agent_chat(self, task, project, model=None, provider="codex", effort=None):
        return self._run("run_agent_chat", task, model, provider, effort, "agent_completed")

    def run_followup_execution(self, task, project, model=None, provider="codex", effort=None):
        return self._run("run_followup_execution", task, model, provider, effort)


class FakeReviewer:
    def __init__(self, verdict: str, effort_fit: str = "ok") -> None:
        self.verdict = verdict
        self.effort_fit = effort_fit
        self.models: list[str | None] = []
        self.roles: list[str] = []
        self.prompts: list[str] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.models.append(request.model)
        self.roles.append(request.role)
        self.prompts.append(request.prompt)
        return LlmResponse(
            text=json.dumps(
                {
                    "verdict": self.verdict,
                    "review_summary": "missing tests",
                    "effort_fit": self.effort_fit,
                    "follow_up_prompt_for_codex": "Add the missing tests.",
                }
            )
        )


def classifier_replying(label: str, confidence: float = 0.9) -> FollowupClassifier:
    reply = json.dumps({"label": label, "confidence": confidence, "reason": "user says it is broken"})
    return FollowupClassifier(MockProvider([LlmResponse(text=reply)]), model="haiku", timeout_seconds=5)


def routed_bot(tmp: str, fake_returncodes: list[int] | None = None):
    bot = CodexTelegramBot(
        config_for(tmp, orchestrator=False, task_provider="claude", router_learning=True)
    )
    bot.projects = [demo_project()]
    bot.router_policy.explore_rate = 0.0
    fake = FakeRunner(bot.store, returncodes=fake_returncodes)
    bot.runner = fake
    bot.orchestrator.runner = fake
    sent: list[str] = []
    bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or 1
    bot.edit_message = lambda chat_id, message_id, text, reply_markup=None: True
    return bot, fake, sent


def router_stats(bot: CodexTelegramBot) -> dict:
    path = bot.config.state_dir / "router-stats.json"
    return json.loads(path.read_text(encoding="utf-8"))["buckets"] if path.exists() else {}


class OrchestratorFlowTest(unittest.TestCase):
    def test_orchestrator_off_uses_old_codex_only_flow_without_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = CodexTelegramBot(config_for(tmp, orchestrator=False, router_learning=False))
            bot.projects = [demo_project()]
            fake = FakeRunner(bot.store)
            bot.runner = fake
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or 1
            bot.edit_message = lambda chat_id, message_id, text, reply_markup=None: True
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "do work")

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)

        self.assertEqual(fake.calls, [("run_execution", None, "codex")])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.phase, "completed")
        self.assertEqual(loaded.model_routing, {})
        self.assertFalse(any("ModelRouter:" in item for item in sent))

    def test_orchestrator_on_uses_executor_only_when_claude_roles_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = CodexTelegramBot(config_for(tmp, orchestrator=True))
            bot.projects = [demo_project()]
            fake = FakeRunner(bot.store)
            bot.runner = fake
            bot.orchestrator.runner = fake
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or 1
            bot.edit_message = lambda chat_id, message_id, text, reply_markup=None: True
            task = bot.store.create_task(
                10,
                20,
                "demo",
                "demo",
                "/tmp/demo",
                "Добавь orchestrator service и model router",
            )

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)
            trace_exists = (bot.store.task_dir(task.id) / "trace.json").exists()

        self.assertEqual(fake.calls[0][0], "run_execution")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.phase, "completed")
        self.assertEqual(loaded.model_routing["complexity"], "large")
        self.assertTrue(any("Codex-only" in item for item in sent))
        self.assertTrue(trace_exists)

    def test_orchestrator_preserves_claude_executor_when_roles_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp, orchestrator=True)
            config = Config(
                **{
                    **config.__dict__,
                    "task_provider": "claude",
                    "claude_executor_command": "claude -p",
                }
            )
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            fake = FakeRunner(bot.store)
            bot.runner = fake
            bot.orchestrator.runner = fake
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or 1
            bot.edit_message = lambda chat_id, message_id, text, reply_markup=None: True
            task = bot.store.create_task(
                10,
                20,
                "demo",
                "demo",
                "/tmp/demo",
                "Добавь orchestrator service и model router",
            )

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)

        self.assertEqual(fake.calls[0][2], "claude")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.model_routing["executor_provider"], "claude")
        self.assertTrue(any("Claude-only" in item for item in sent))

    def test_direct_run_routes_cheap_tier_with_effort_and_learns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, sent = routed_bot(tmp)
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "Исправь typo в README")

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)
            trace = json.loads((bot.store.task_dir(task.id) / "trace.json").read_text())
            stats = router_stats(bot)

        self.assertEqual(fake.calls, [("run_execution", "haiku", "claude")])
        self.assertEqual(fake.efforts, ["low"])
        self.assertEqual(loaded.phase, "completed")
        self.assertEqual(loaded.model_routing["verify"], "light")
        self.assertTrue(any(item.startswith("router: haiku (cheap)") for item in sent))
        self.assertEqual(trace["actual_cost"], 0.5)
        self.assertEqual(trace["actual_tokens"], 150)
        self.assertEqual(stats["trivial|demo"]["cheap"]["successes"], 1)
        self.assertEqual(stats["trivial|*"]["cheap"]["priced_runs"], 1)

    def test_failed_cheap_run_escalates_once_and_records_both_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, sent = routed_bot(tmp, fake_returncodes=[1, 0])
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "Исправь typo в README")

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)
            stats = router_stats(bot)["trivial|demo"]

        self.assertEqual(
            fake.calls,
            [
                ("run_execution", "haiku", "claude"),
                ("run_recovery_execution", "sonnet", "claude"),
            ],
        )
        self.assertEqual(fake.efforts, ["low", "medium"])
        self.assertEqual(loaded.phase, "completed")
        self.assertEqual(loaded.model_routing["policy_source"], "escalate")
        self.assertTrue(any(item.startswith("Escalate: router: sonnet") for item in sent))
        self.assertEqual((stats["cheap"]["runs"], stats["cheap"]["successes"]), (1, 0))
        self.assertEqual((stats["standard"]["runs"], stats["standard"]["successes"]), (1, 1))

    def test_cancelled_run_neither_escalates_nor_learns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, _ = routed_bot(tmp, fake_returncodes=[-15])
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "Исправь typo в README")

            bot.execute_task(task.id)
            stats = router_stats(bot)

        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(stats, {})

    def test_high_risk_run_gets_full_verify_and_fixes_review_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, sent = routed_bot(tmp)
            reviewer = FakeReviewer("request_changes")
            bot.orchestrator.verify_provider = reviewer
            task = bot.store.create_task(
                10, 20, "demo", "demo", "/tmp/demo", "Добавь orchestrator service и model router"
            )

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)
            stats = router_stats(bot)["large|demo"]

        # large is capped at strong, so the retry stays on strong with reviewer feedback.
        self.assertEqual(
            fake.calls,
            [
                ("run_execution", "opus", "claude"),
                ("run_revision_execution", "opus", "claude"),
            ],
        )
        self.assertEqual(reviewer.models, ["sonnet"])
        self.assertEqual(loaded.prepared_codex_prompt, "Add the missing tests.")
        self.assertTrue(any(item.startswith("Verify full: request_changes") for item in sent))
        self.assertEqual((stats["strong"]["runs"], stats["strong"]["successes"]), (2, 1))

    def test_explored_cheaper_tier_is_reviewed_before_it_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, _ = routed_bot(tmp)
            bot.router_policy.explore_rate = 1.0
            reviewer = FakeReviewer("approve")
            bot.orchestrator.verify_provider = reviewer
            prompt = "Добавь feature для экспорта отчётов. " + "Подробности задачи. " * 15
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", prompt)

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)
            stats = router_stats(bot)["medium|demo"]

        self.assertEqual(fake.calls, [("run_execution", "haiku", "claude")])
        self.assertEqual(loaded.model_routing["policy_source"], "explore")
        self.assertEqual(loaded.model_routing["verify"], "full")
        self.assertEqual(reviewer.models, ["sonnet"])
        self.assertEqual(stats["cheap"]["successes"], 1)

    def test_agent_chat_is_routed_with_light_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, sent = routed_bot(tmp)
            reviewer = FakeReviewer("request_changes")
            bot.orchestrator.verify_provider = reviewer
            task = bot.store.create_task(
                10, 20, "demo", "demo", "/tmp/demo", "Как устроен orchestrator?", kind="agent_chat"
            )

            bot.run_agent_chat(task.id)
            loaded = bot.store.load_task(task.id)

        self.assertEqual(fake.calls[0][0], "run_agent_chat")
        self.assertEqual(loaded.phase, "agent_completed")
        self.assertEqual(loaded.model_routing["verify"], "light")
        self.assertEqual(reviewer.models, [])
        self.assertFalse(any(item.startswith("router:") for item in sent))


    def test_correction_followup_turns_parent_run_into_failure_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, _ = routed_bot(tmp)
            bot.spawn = lambda target, *args: target(*args)
            parent = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "Исправь typo в README")
            bot.execute_task(parent.id)
            bot.router_feedback.classifier = classifier_replying("correction")

            bot.create_followup_task_for_parent(
                10, 20, parent.id, "Опечатка осталась, исправь нормально", "telegram", ""
            )
            followup = next(item for item in bot.store.tasks() if item.parent_task_id == parent.id)
            again = bot.router_feedback.on_followup(followup, bot.store.load_task(parent.id), "")
            stats = router_stats(bot)["trivial|demo"]["cheap"]
            loaded_parent = bot.store.load_task(parent.id)
            loaded_followup = bot.store.load_task(followup.id)

        self.assertEqual(fake.calls[1][0], "run_followup_execution")
        self.assertEqual(loaded_parent.model_routing["feedback"]["label"], "correction")
        self.assertTrue(loaded_parent.model_routing["feedback"]["applied"])
        self.assertEqual(loaded_followup.model_routing["floor_tier"], "cheap")
        # Parent run 1/1 became 0/1; the follow-up is a small task in its own bucket.
        self.assertEqual((stats["runs"], stats["successes"], stats["corrections"]), (1, 0, 1))
        self.assertIsNone(again)

    def test_extension_followup_does_not_count_against_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, _, _ = routed_bot(tmp)
            bot.spawn = lambda target, *args: target(*args)
            parent = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "Исправь typo в README")
            bot.execute_task(parent.id)
            bot.router_feedback.classifier = classifier_replying("extension")

            bot.create_followup_task_for_parent(10, 20, parent.id, "Теперь закоммить", "telegram", "")
            stats = router_stats(bot)["trivial|demo"]["cheap"]
            loaded_parent = bot.store.load_task(parent.id)

        self.assertEqual(loaded_parent.model_routing["feedback"]["label"], "extension")
        self.assertFalse(loaded_parent.model_routing["feedback"]["applied"])
        self.assertEqual((stats["runs"], stats["successes"], stats["corrections"]), (1, 1, 0))

    def test_medium_run_gets_critic_review_on_any_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, sent = routed_bot(tmp)
            bot.config = Config(**{**bot.config.__dict__, "router_critic_min_complexity": "medium"})
            bot.model_router.config = bot.config
            reviewer = FakeReviewer("approve", effort_fit="over")
            bot.orchestrator.verify_provider = reviewer
            prompt = "Добавь feature для экспорта отчётов. " + "Подробности задачи. " * 15
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", prompt)

            bot.execute_task(task.id)
            loaded = bot.store.load_task(task.id)
            stats = router_stats(bot)["medium|demo"]

        self.assertEqual(fake.calls, [("run_execution", "sonnet", "claude")])
        self.assertEqual(loaded.model_routing["verify"], "full")
        self.assertEqual(loaded.model_routing["outcome"], "success")
        self.assertEqual(reviewer.models, ["sonnet"])
        self.assertIn("You are the Critic", reviewer.prompts[0])
        self.assertEqual(stats["standard"]["overkill"], 1)

    def test_critic_button_reviews_task_learns_and_offers_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot, fake, sent = routed_bot(tmp)
            bot.spawn = lambda target, *args: target(*args)
            parent = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "Исправь typo в README")
            bot.execute_task(parent.id)
            reviewer = FakeReviewer("request_changes")
            bot.orchestrator.verify_provider = reviewer

            bot.handle_task_callback(10, 20, "review", parent.id)
            critic_exists = (bot.store.task_dir(parent.id) / "critic.json").exists()
            bot.handle_task_callback(10, 20, "critic_fix", parent.id)
            followup = next(item for item in bot.store.tasks() if item.parent_task_id == parent.id)
            stats = router_stats(bot)["trivial|demo"]["cheap"]

        self.assertEqual(reviewer.roles, ["critic"])
        self.assertTrue(critic_exists)
        self.assertTrue(any(item.startswith(f"🧐 Critic for {parent.id}") for item in sent))
        self.assertTrue(any("Router: учёл как исправление для cheap" in item for item in sent))
        self.assertEqual(followup.source, "critic")
        self.assertIn("Add the missing tests.", followup.prompt)
        self.assertEqual(fake.calls[-1][0], "run_followup_execution")
        self.assertEqual((stats["runs"], stats["successes"], stats["corrections"]), (1, 0, 1))


if __name__ == "__main__":
    unittest.main()
