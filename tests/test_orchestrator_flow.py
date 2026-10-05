from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.bot import CodexTelegramBot
from codex_telegram_bot.config import Config
from codex_telegram_bot.project_index import ProjectInfo
from codex_telegram_bot.task_store import TaskRecord, TaskStore


def config_for(
    tmp: str,
    orchestrator: bool = True,
    task_provider: str = "codex",
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
    def __init__(self, store: TaskStore, unavailable: dict[str, str] | None = None) -> None:
        self.store = store
        self.calls: list[tuple[str, str | None, str]] = []
        self.runtime_id = "fake"
        self.unavailable = unavailable or {}

    def provider_unavailable_reason(self, provider: str) -> str:
        return self.unavailable.get(provider, "")

    def run_execution(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        model: str | None = None,
        provider: str = "codex",
    ) -> TaskRecord:
        del project
        self.calls.append(("run_execution", model, provider))
        task.phase = "completed"
        task.final_path = str(self.store.task_dir(task.id) / "final.md")
        Path(task.final_path).write_text(
            "- Changed:\n- done\n- Verified:\n- fake\n",
            encoding="utf-8",
        )
        self.store.save_task(task)
        return task

    def run_recovery_execution(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        model: str | None = None,
        provider: str = "codex",
    ) -> TaskRecord:
        return self.run_execution(task, project, model=model, provider=provider)


class OrchestratorFlowTest(unittest.TestCase):
    def test_orchestrator_off_uses_old_codex_only_flow_without_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = CodexTelegramBot(config_for(tmp, orchestrator=False))
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


if __name__ == "__main__":
    unittest.main()
