from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.config import Config
from codex_telegram_bot.services.model_router import ModelRouter
from codex_telegram_bot.services.router_policy import (
    RouterPolicy,
    RunUsage,
    parse_run_usage,
    render_router_stats,
    seed_from_history,
)
from codex_telegram_bot.task_store import TaskRecord


class FixedRandom(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__()
        self.value = value

    def random(self) -> float:
        return self.value


def policy_at(tmp: str, explore: float = 1.0) -> RouterPolicy:
    # FixedRandom(explore): 0.0 always explores, 1.0 never does.
    return RouterPolicy(Path(tmp) / "router-stats.json", rng=FixedRandom(explore))


def record_many(policy: RouterPolicy, complexity: str, tier: str, ok: int, failed: int = 0) -> None:
    for index in range(ok + failed):
        policy.record(complexity, "demo", tier, success=index < ok)


class RouterPolicyTest(unittest.TestCase):
    def test_without_stats_keeps_rule_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            choice = policy_at(tmp).choose("medium", "demo", "standard", "strong")

        self.assertEqual((choice.tier, choice.effort, choice.source), ("standard", "medium", "rule"))

    def test_proven_cheaper_tier_replaces_rule_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            record_many(policy, "medium", "cheap", ok=5)
            choice = policy.choose("medium", "demo", "standard", "strong")

        self.assertEqual((choice.tier, choice.effort, choice.source), ("cheap", "low", "learned"))

    def test_mostly_failing_cheaper_tier_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp, explore=0.0)
            record_many(policy, "medium", "cheap", ok=3, failed=3)
            choice = policy.choose("medium", "demo", "standard", "strong")

        # Not proven and proven bad: neither learned nor explored.
        self.assertEqual(choice.tier, "standard")

    def test_failing_rule_tier_moves_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            record_many(policy, "small", "cheap", ok=1, failed=5)
            choice = policy.choose("small", "demo", "cheap", "strong")

        self.assertEqual((choice.tier, choice.source), ("standard", "learned"))

    def test_risk_floor_blocks_cheap_tier_for_critical_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp, explore=0.0)
            record_many(policy, "critical", "cheap", ok=10)
            choice = policy.choose("critical", "demo", "strong", "strong")

        self.assertEqual(choice.tier, "strong")

    def test_exploration_tries_one_step_cheaper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            choice = policy_at(tmp, explore=0.0).choose("medium", "demo", "strong", "strong")

        self.assertEqual((choice.tier, choice.source), ("standard", "explore"))

    def test_project_stats_fall_back_to_complexity_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            record_many(policy, "medium", "cheap", ok=5)
            choice = policy.choose("medium", "other-project", "standard", "strong")

        self.assertEqual(choice.tier, "cheap")

    def test_stats_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            policy.record("small", "demo", "cheap", True, RunUsage(tokens=10, cost_usd=0.2))
            reloaded = policy_at(tmp).global_stats()

        arm = reloaded["small"]["cheap"]
        self.assertEqual((arm.runs, arm.successes, arm.tokens, arm.priced_runs), (1, 1, 10, 1))
        self.assertAlmostEqual(arm.cost_usd, 0.2)

    def test_parse_run_usage_reads_claude_and_codex_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text(
                "[claude executor started: mode=execute]\n"
                + json.dumps(
                    {
                        "type": "result",
                        "total_cost_usd": 0.25,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "cache_read_input_tokens": 30,
                            "cache_creation_input_tokens": 40,
                        },
                    }
                )
                + "\n{\"type\":\"assistant\",\"result\":\"not a result event\"}\n"
                + "tokens used\n1,234\n",
                encoding="utf-8",
            )
            usage = parse_run_usage(log)
            missing = parse_run_usage(Path(tmp) / "missing.log")

        self.assertEqual(usage.tokens, 100 + 1234)
        self.assertAlmostEqual(usage.cost_usd, 0.25)
        self.assertEqual(missing, RunUsage())

    def test_resumed_claude_session_is_priced_by_cost_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text(
                json.dumps({"type": "result", "session_id": "s1", "total_cost_usd": 1.0}) + "\n",
                encoding="utf-8",
            )
            policy = policy_at(tmp)
            first = policy.price(parse_run_usage(log))
            offset = log.stat().st_size
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "result", "session_id": "s1", "total_cost_usd": 1.5}) + "\n")
            second = policy.price(parse_run_usage(log, offset))
            # Re-pricing an already seen run must not count it again.
            repeat = policy.price(parse_run_usage(log))

        self.assertAlmostEqual(first.cost_usd, 1.0)
        self.assertAlmostEqual(second.cost_usd, 0.5)
        self.assertAlmostEqual(repeat.cost_usd, 0.0)

    def test_seed_records_history_as_baseline_tier_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            payloads = [
                {"kind": "task", "phase": "completed", "project_slug": "demo"},
                {"kind": "followup_task", "phase": "failed", "project_slug": "demo"},
                {"kind": "task", "phase": "canceled", "project_slug": "demo"},
                {"kind": "force_push_task", "phase": "completed", "project_slug": "demo"},
            ]
            seeded = seed_from_history(policy, payloads, lambda payload: "small")
            stats = policy.global_stats()
            choice = policy.choose("small", "demo", "cheap", "strong")
            text = render_router_stats(policy)

        self.assertEqual(seeded, 2)
        self.assertEqual(set(stats["small"]), {"strong"})
        self.assertEqual(stats["small"]["strong"].runs, 2)
        # A seeded strong baseline never overrides a cheaper rule tier.
        self.assertEqual(choice.tier, "cheap")
        self.assertIn("small:", text)


def config_for(tmp: str, **overrides) -> Config:
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
        task_provider="claude",
        **overrides,
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


class RouterEscalationTest(unittest.TestCase):
    def test_escalation_stops_at_auto_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp), policy_at(tmp))
            decision = router.route(task("Исправь typo в README"))
            first = router.escalate(decision)
            second = router.escalate(first)
            third = router.escalate(second)

        self.assertEqual(decision.selected_models["executor"], "haiku")
        self.assertEqual(first.selected_models["executor"], "sonnet")
        self.assertEqual((second.classification.recommended_codex_tier, second.effort), ("strong", "high"))
        self.assertIsNone(third)

    def test_manual_tier_is_never_escalated_or_relearned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            record_many(policy, "trivial", "standard", ok=0, failed=6)
            router = ModelRouter(config_for(tmp), policy)
            decision = router.route(task("Исправь typo в README"), manual_tier="standard")

        self.assertEqual(decision.classification.recommended_codex_tier, "standard")
        self.assertEqual(decision.policy_source, "manual")
        self.assertIsNone(router.escalate(decision))

    def test_learning_off_keeps_configured_claude_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(config_for(tmp, router_learning=False), policy_at(tmp))
            decision = router.route(task("Исправь typo в README"))

        self.assertIsNone(decision.selected_models["executor"])
        self.assertEqual((decision.policy_source, decision.effort), ("rule", ""))
        self.assertIsNone(router.escalate(decision))


if __name__ == "__main__":
    unittest.main()
