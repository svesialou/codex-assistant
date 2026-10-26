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

    def test_one_failure_in_few_runs_does_not_flip_to_stronger_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            record_many(policy, "small", "standard", ok=6, failed=1)
            record_many(policy, "small", "cheap", ok=12, failed=3)
            standard = policy.choose("small", "demo", "standard", "strong")
            cheap = policy.choose("small", "demo", "cheap", "strong")

        self.assertEqual((standard.tier, standard.source), ("standard", "rule"))
        self.assertEqual((cheap.tier, cheap.source), ("cheap", "rule"))

    def test_proven_cheap_tier_beats_reliable_strong_on_utility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            record_many(policy, "small", "strong", ok=20)
            record_many(policy, "small", "cheap", ok=43, failed=4)
            choice = policy.choose("small", "demo", "strong", "strong")

        self.assertEqual((choice.tier, choice.source), ("cheap", "learned"))

    def test_user_correction_turns_counted_success_into_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp)
            record_many(policy, "small", "cheap", ok=5)
            policy.record_feedback("small", "demo", "cheap", counted_success=True)
            reloaded = RouterPolicy(Path(tmp) / "router-stats.json", explore_rate=0.0)
            arm = reloaded.global_stats()["small"]["cheap"]
            choice = reloaded.choose("small", "demo", "standard", "strong")

        self.assertEqual((arm.runs, arm.successes, arm.corrections), (5, 4, 1))
        # 4/5 is no longer proven, so the cheaper tier is not picked.
        self.assertEqual(choice.tier, "standard")

    def test_v1_stats_cap_seeded_strong_runs_and_keep_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router-stats.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "buckets": {
                            "small|*": {
                                "strong": {"runs": 1453, "successes": 1443, "priced_runs": 1000,
                                           "cost_usd": 1580.0},
                                "cheap": {"runs": 47, "successes": 43},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            policy = RouterPolicy(path)
            arms = policy.global_stats()["small"]
            backup_exists = path.with_name("router-stats.json.v1.bak").exists()
            policy.save()
            version = json.loads(path.read_text(encoding="utf-8"))["version"]

        self.assertEqual((arms["strong"].runs, arms["strong"].successes), (20, 20))
        self.assertAlmostEqual(arms["strong"].avg_cost(), 1.58, places=1)
        self.assertEqual((arms["cheap"].runs, arms["cheap"].successes), (47, 43))
        self.assertTrue(backup_exists)
        self.assertEqual(version, 2)

    def test_critic_overkill_verdicts_raise_exploration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_at(tmp, explore=0.2)
            record_many(policy, "medium", "strong", ok=6)
            before = policy.choose("medium", "demo", "strong", "strong")
            for _ in range(2):
                policy.record_feedback(
                    "medium", "demo", "strong", counted_success=True, correction=False, overkill=True
                )
            after = policy.choose("medium", "demo", "strong", "strong")

        self.assertEqual(before.tier, "strong")
        self.assertEqual((after.tier, after.source), ("standard", "explore"))

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
