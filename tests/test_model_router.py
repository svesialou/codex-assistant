from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.config import Config
from codex_telegram_bot.services.llm_provider import (
    LlmProviderUnavailable,
    LlmRequest,
    LlmResponse,
    MockProvider,
)
from codex_telegram_bot.services.llm_router import LlmTierAdvisor
from codex_telegram_bot.services.model_router import ModelRouter
from codex_telegram_bot.services.router_policy import RouterPolicy
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


class FailingProvider:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise LlmProviderUnavailable("timeout")


def advisor_replying(text: str) -> LlmTierAdvisor:
    return LlmTierAdvisor(MockProvider([LlmResponse(text=text)]), model="haiku", timeout_seconds=45)


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

    def test_llm_explicit_model_request_overrides_short_prompt_rule(self) -> None:
        advice = '{"tier": "strong", "explicit_user_request": true, "reason": "user asked for opus"}'
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp), advisor=advisor_replying(advice))
            decision = router.route(task("Поправь текст кнопки, используй opus"))

        self.assertEqual(decision.classification.recommended_codex_tier, "strong")
        self.assertEqual(decision.policy_source, "llm-explicit")

    def test_llm_failure_falls_back_to_rule_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(
                config_for(tmp),
                advisor=LlmTierAdvisor(FailingProvider(), model="haiku", timeout_seconds=45),
            )
            decision = router.route(task("Исправь typo в README"))

        self.assertEqual(decision.classification.recommended_codex_tier, "cheap")
        self.assertEqual(decision.policy_source, "rule")

    def test_explicit_request_is_not_moved_by_learned_stats(self) -> None:
        advice = '{"tier": "strong", "explicit_user_request": true, "reason": "user asked for opus"}'
        with tempfile.TemporaryDirectory() as tmp:
            policy = RouterPolicy(Path(tmp) / "stats.json")
            for _ in range(6):
                policy.record("small", "demo", "cheap", True, save=False)
            router = ModelRouter(
                config_for(tmp),
                policy=policy,
                advisor=advisor_replying(advice),
            )
            decision = router.route(task("Поправь баг, используй opus"))

        self.assertEqual(decision.classification.recommended_codex_tier, "strong")
        self.assertEqual(decision.policy_source, "llm-explicit")

    def test_llm_tier_is_floor_for_medium_tasks_against_learned_stats(self) -> None:
        advice = '{"tier": "strong", "explicit_user_request": false, "reason": "needs care"}'
        long_feature = "Добавь feature для фильтра списка. " + "Детали задачи. " * 30
        with tempfile.TemporaryDirectory() as tmp:
            policy = RouterPolicy(Path(tmp) / "stats.json")
            for _ in range(6):
                policy.record("medium", "demo", "cheap", True, save=False)
            router = ModelRouter(
                config_for(tmp),
                policy=policy,
                advisor=advisor_replying(advice),
            )
            decision = router.route(task(long_feature))

        self.assertEqual(decision.classification.complexity, "medium")
        self.assertEqual(decision.classification.recommended_codex_tier, "strong")
        self.assertEqual(decision.policy_source, "llm")


if __name__ == "__main__":
    unittest.main()
