from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
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
from codex_telegram_bot.services.task_similarity import TaskSimilarity
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


def routed_payload(task_id: str, prompt: str, tier: str, outcome: str) -> dict:
    return {
        "id": task_id,
        "prompt": prompt,
        "phase": "completed",
        "model_routing": {"selected_tier": tier, "manual_tier": "auto", "outcome": outcome},
    }


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

    def test_proven_stats_move_medium_task_one_step_below_llm_tier(self) -> None:
        advice = '{"tier": "strong", "explicit_user_request": false, "reason": "needs care"}'
        long_feature = "Добавь feature для фильтра списка. " + "Детали задачи. " * 30
        with tempfile.TemporaryDirectory() as tmp:
            policy = RouterPolicy(Path(tmp) / "stats.json", explore_rate=0.0)
            for _ in range(6):
                policy.record("medium", "demo", "cheap", True, save=False)
            router = ModelRouter(
                config_for(tmp),
                policy=policy,
                advisor=advisor_replying(advice),
            )
            decision = router.route(task(long_feature))

        self.assertEqual(decision.classification.complexity, "medium")
        # Proven cheap is not trusted two steps below the LLM tier.
        self.assertEqual(decision.classification.recommended_codex_tier, "standard")
        self.assertEqual(decision.policy_source, "learned")
        self.assertEqual(decision.verify_mode, "full")

    def test_llm_tier_is_floor_for_large_tasks_against_learned_stats(self) -> None:
        advice = '{"tier": "strong", "explicit_user_request": false, "reason": "cross-service"}'
        with tempfile.TemporaryDirectory() as tmp:
            policy = RouterPolicy(Path(tmp) / "stats.json", explore_rate=0.0)
            for _ in range(10):
                policy.record("large", "demo", "standard", True, save=False)
            router = ModelRouter(
                config_for(tmp),
                policy=policy,
                advisor=advisor_replying(advice),
            )
            decision = router.route(task("Сделай миграцию схемы и grpc api"))

        self.assertEqual(decision.classification.complexity, "large")
        self.assertEqual(decision.classification.recommended_codex_tier, "strong")

    def test_similar_past_successes_pick_cheaper_tier(self) -> None:
        details = " Детали задачи и пожелания по интерфейсу." * 8
        past = [
            routed_payload(f"past-{index}", "Добавь feature: фильтр списка задач." + details, "cheap", "success")
            for index in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            policy = RouterPolicy(Path(tmp) / "stats.json", explore_rate=0.0)
            router = ModelRouter(
                config_for(tmp),
                policy=policy,
                similarity=TaskSimilarity(lambda: past),
            )
            decision = router.route(task("Добавь feature: фильтры списка задач." + details))
            unrelated = ModelRouter(
                config_for(tmp),
                policy=policy,
                similarity=TaskSimilarity(lambda: past),
            ).route(task("Сделай выгрузку отчёта о платежах в CSV. " + "Колонки и формат. " * 20))

        self.assertEqual(decision.classification.complexity, "medium")
        self.assertEqual(decision.classification.recommended_codex_tier, "cheap")
        self.assertEqual(decision.policy_source, "similar")
        # Cheaper than the rule tier, so the run must be reviewed before it counts.
        self.assertEqual(decision.verify_mode, "full")
        self.assertNotEqual(unrelated.policy_source, "similar")

    def test_similar_task_corrected_by_user_counts_as_failure(self) -> None:
        corrected = routed_payload("past-1", "Поправь фильтр списка задач", "cheap", "success")
        corrected["model_routing"]["feedback"] = {"label": "correction", "applied": True}
        similar = TaskSimilarity(
            lambda: [
                corrected,
                routed_payload("past-2", "Поправь фильтр списка задач", "cheap", "success"),
            ]
        ).similar("Поправь фильтры списка задач")

        self.assertEqual(similar.per_tier(), {"cheap": (1, 2)})

    def test_corrected_parent_tier_is_floor_for_the_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = RouterPolicy(Path(tmp) / "stats.json", explore_rate=0.0)
            for _ in range(10):
                policy.record("small", "demo", "cheap", True, save=False)
            router = ModelRouter(config_for(tmp), policy=policy)
            fix = task("Поправь баг, кнопка всё ещё не работает")
            fix.model_routing = {"floor_tier": "standard"}
            decision = router.route(fix)

        self.assertEqual(decision.classification.recommended_codex_tier, "standard")
        self.assertEqual(decision.policy_source, "feedback")

    def test_planning_uses_sonnet_unless_work_is_large(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp, task_provider="claude"), policy=None)
            small = router.planning_model(task("Почини баг в парсере"), "claude")
            large = router.planning_model(task("Поменяй grpc api оркестратора"), "claude")

        self.assertEqual((small, large), ("sonnet", "opus"))

    def test_critic_and_sampled_review_apply_to_strong_runs_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                config_for(tmp),
                router_critic_min_complexity="medium",
                router_verify_sample_rate=1.0,
            )
            router = ModelRouter(config, policy=None)
            medium = router.route(task("Добавь feature для фильтра. " + "Детали. " * 40))
            small = router.route(task("Почини баг в парсере"))
            manual = router.route(task("Почини баг в парсере"), manual_tier="strong")

        self.assertTrue(medium.critic_review)
        self.assertEqual(medium.verify_mode, "full")
        self.assertFalse(small.critic_review)
        self.assertEqual(small.verify_mode, "full")
        # Sampling only measures auto-routed tiers.
        self.assertFalse(manual.verify_sampled)


if __name__ == "__main__":
    unittest.main()
