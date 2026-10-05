from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.config import Config
from codex_telegram_bot.services.model_router import ModelRouter
from codex_telegram_bot.task_store import TaskRecord


def config_for(tmp: str, task_provider: str = "codex") -> Config:
    return Config(
        bot_token="token",
        allowed_chat_ids={1},
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
        task_provider=task_provider,
    )


def task(prompt: str) -> TaskRecord:
    return TaskRecord(
        id="task-1",
        chat_id=10,
        user_id=20,
        project_slug="demo",
        project_name="demo",
        project_path="/tmp/demo",
        prompt=prompt,
    )


class ModelRouterTest(unittest.TestCase):
    def test_trivial_task_uses_cheap_codex_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp))
            decision = router.route(task("Исправь typo в README"))

        self.assertEqual(decision.classification.complexity, "trivial")
        self.assertFalse(decision.classification.requires_architect)
        self.assertFalse(decision.classification.requires_reviewer)
        self.assertEqual(decision.classification.recommended_codex_tier, "cheap")
        self.assertIn("Codex cheap only", decision.selected_flow)

    def test_executor_provider_defaults_to_configured_task_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp, task_provider="claude"))
            decision = router.route(task("Исправь typo в README"))

        self.assertEqual(decision.executor_provider, "claude")
        self.assertIn("Claude cheap only", decision.selected_flow)

    def test_large_task_uses_architect_reviewer_and_strong_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp))
            decision = router.route(task("Добавь orchestrator service и model router"))

        self.assertEqual(decision.classification.complexity, "large")
        self.assertTrue(decision.classification.requires_architect)
        self.assertTrue(decision.classification.requires_reviewer)
        self.assertEqual(decision.classification.recommended_codex_tier, "strong")

    def test_critical_task_caps_auto_tier_at_strong(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp))
            decision = router.route(task("Почини auth security bug в production"))

        self.assertEqual(decision.classification.complexity, "critical")
        self.assertEqual(decision.classification.recommended_codex_tier, "strong")
        self.assertIn("auto tier capped at strong", decision.downgrades)

    def test_manual_cheap_warns_when_task_is_risky(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp))
            decision = router.route(
                task("Почини auth security bug"),
                manual_tier="cheap",
            )

        self.assertEqual(decision.classification.recommended_codex_tier, "cheap")
        self.assertTrue(decision.warnings)


if __name__ == "__main__":
    unittest.main()
