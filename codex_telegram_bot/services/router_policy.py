"""Self-learning executor routing: run the cheapest tier that keeps succeeding.

Every executor run is recorded per (complexity, project) bucket and in a
complexity-wide fallback bucket. The regex classification from ModelRouter
stays the starting rule; stats only move the choice once they are proven:

- a tier is proven good/bad only when a Wilson confidence bound clears the
  target success rate, so one failure in a handful of runs proves nothing;
- among proven cheaper tiers the one with the best `quality - cost` utility
  replaces the rule tier (the LLMRouter quality/cost trade-off);
- a rule tier that keeps failing is skipped in favour of the next one;
- occasionally a one-step cheaper tier is explored to keep learning, more
  often when the critic keeps calling the chosen tier overkill;
- a user correction after a "successful" run turns that run into a failure.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

LADDER = ("cheap", "standard", "strong", "max")
LADDER_RANK = {tier: index for index, tier in enumerate(LADDER)}
TIER_EFFORT = {"cheap": "low", "standard": "medium", "strong": "high", "max": "high"}
# Claude CLI aliases used when no explicit CLAUDE_*_MODEL is configured.
DEFAULT_CLAUDE_TIER_MODELS = {
    "cheap": "haiku",
    "standard": "sonnet",
    "strong": "opus",
    "max": "opus",
}
# Risky work never drops below this tier, whatever the stats say.
RISK_FLOOR = {"critical": "strong", "large": "standard"}
MIN_SAMPLES = 5
# Target success rate; Wilson bounds at Z decide "proven good/bad" against it.
SUCCESS_THRESHOLD = 0.75
CONFIDENCE_Z = 1.28
# Relative cost per tier (measured: small haiku $0.24, sonnet $0.45, opus $1.58)
# and how many success-rate points one strong-cost unit is worth.
TIER_COST = {"cheap": 0.15, "standard": 0.35, "strong": 1.0, "max": 1.3}
COST_WEIGHT = 0.25
EXPLORE_RATE = 0.1
# Exploration rate when the critic often finds the chosen tier overkill.
OVERKILL_EXPLORE_RATE = 0.3
OVERKILL_RATIO = 0.3
GLOBAL_PROJECT = "*"
# Pre-routing history only describes exit codes of the CLI default model;
# it is capped so live, reviewed runs can outweigh it.
LEGACY_CAP = 20
LEGACY_TIER = "strong"
STATS_VERSION = 2
# Last cumulative cost kept per Claude session, to price resumed runs.
MAX_TRACKED_SESSIONS = 500


@dataclass
class ArmStats:
    runs: int = 0
    successes: int = 0
    priced_runs: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    duration_seconds: float = 0.0
    # Runs the user had to correct afterwards (already counted as failures).
    corrections: int = 0
    # Runs the critic judged as more model than the task needed.
    overkill: int = 0

    def lower_bound(self) -> float:
        return wilson_bounds(self.successes, self.runs)[0]

    def upper_bound(self) -> float:
        return wilson_bounds(self.successes, self.runs)[1]

    def proven_good(self) -> bool:
        # 5/5 -> 0.75, 43/47 -> 0.85, 4/5 -> 0.51.
        return self.runs >= MIN_SAMPLES and self.lower_bound() >= SUCCESS_THRESHOLD

    def proven_bad(self) -> bool:
        # 6/7 -> 0.96 and 12/15 -> 0.90 are not bad; 3/6 -> 0.73 is.
        return self.runs >= MIN_SAMPLES and self.upper_bound() < SUCCESS_THRESHOLD

    def overkill_ratio(self) -> float:
        return self.overkill / self.runs if self.runs else 0.0

    def avg_cost(self) -> float | None:
        return self.cost_usd / self.priced_runs if self.priced_runs else None

    def avg_tokens(self) -> int:
        return self.tokens // self.runs if self.runs else 0


@dataclass(frozen=True)
class PolicyChoice:
    tier: str
    effort: str
    source: str  # rule | learned | explore
    reason: str


@dataclass(frozen=True)
class RunUsage:
    tokens: int = 0
    cost_usd: float | None = None
    # Claude reports `total_cost_usd` cumulatively for a resumed session:
    # (session_id, cumulative cost) per run, priced by RouterPolicy.price().
    session_costs: tuple[tuple[str, float], ...] = ()


@dataclass
class _Bucket:
    arms: dict[str, ArmStats] = field(default_factory=dict)

    def arm(self, tier: str) -> ArmStats:
        return self.arms.setdefault(tier, ArmStats())


class RouterPolicy:
    def __init__(
        self,
        path: Path,
        rng: random.Random | None = None,
        explore_rate: float = EXPLORE_RATE,
    ) -> None:
        self.path = path
        self.rng = rng or random.Random()
        self.explore_rate = explore_rate
        self._lock = threading.Lock()
        self._session_costs: dict[str, float] = {}
        self._buckets: dict[str, _Bucket] = self._load()

    def exists(self) -> bool:
        return self.path.exists()

    def choose(
        self,
        complexity: str,
        project_slug: str,
        rule_tier: str,
        max_tier: str,
    ) -> PolicyChoice:
        floor = RISK_FLOOR.get(complexity, "cheap")
        if LADDER_RANK[floor] > LADDER_RANK[max_tier]:
            floor = max_tier
        candidates = [
            tier
            for tier in LADDER
            if LADDER_RANK[floor] <= LADDER_RANK[tier] <= LADDER_RANK[max_tier]
        ]
        rule = min(max(rule_tier, floor, key=LADDER_RANK.get), max_tier, key=LADDER_RANK.get)
        with self._lock:
            stats = {tier: self._stats_for(complexity, project_slug, tier) for tier in candidates}

        chosen, source, reason = rule, "rule", "rule tier, not enough stats to change it"
        if stats[rule].proven_bad():
            stronger = [
                tier
                for tier in candidates
                if LADDER_RANK[tier] > LADDER_RANK[rule] and not stats[tier].proven_bad()
            ]
            chosen = stronger[0] if stronger else candidates[-1]
            source = "learned"
            reason = f"{rule} keeps failing ({format_rate(stats[rule])})"
        cheaper_proven = [
            tier
            for tier in candidates
            if LADDER_RANK[tier] < LADDER_RANK[chosen] and stats[tier].proven_good()
        ]
        if cheaper_proven:
            best = max([chosen, *cheaper_proven], key=lambda tier: utility(tier, stats[tier]))
            if best != chosen:
                chosen = best
                source = "learned"
                reason = f"{chosen} is proven ({format_rate(stats[chosen])})"

        index = candidates.index(chosen)
        explore_rate = self.explore_rate
        if (
            stats[chosen].runs >= MIN_SAMPLES
            and stats[chosen].overkill_ratio() >= OVERKILL_RATIO
        ):
            explore_rate = max(explore_rate, OVERKILL_EXPLORE_RATE)
        if index > 0 and self.rng.random() < explore_rate:
            cheaper = candidates[index - 1]
            if not stats[cheaper].proven_bad():
                chosen, source = cheaper, "explore"
                reason = f"exploring cheaper {cheaper} ({format_rate(stats[cheaper])})"
        return PolicyChoice(
            tier=chosen,
            effort=TIER_EFFORT[chosen],
            source=source,
            reason=reason,
        )

    def stats_summary(self, complexity: str, project_slug: str) -> str:
        with self._lock:
            return "; ".join(
                f"{tier} {format_rate(self._stats_for(complexity, project_slug, tier))}"
                for tier in LADDER
            )

    def record(
        self,
        complexity: str,
        project_slug: str,
        tier: str,
        success: bool,
        usage: RunUsage | None = None,
        duration_seconds: float = 0.0,
        save: bool = True,
    ) -> None:
        if tier not in LADDER_RANK:
            return
        usage = usage or RunUsage()
        with self._lock:
            for key in {bucket_key(complexity, project_slug), bucket_key(complexity, GLOBAL_PROJECT)}:
                arm = self._buckets.setdefault(key, _Bucket()).arm(tier)
                arm.runs += 1
                arm.successes += int(success)
                arm.tokens += usage.tokens
                arm.duration_seconds += max(0.0, duration_seconds)
                if usage.cost_usd is not None:
                    arm.priced_runs += 1
                    arm.cost_usd += usage.cost_usd
            if save:
                self._save_locked()

    def record_feedback(
        self,
        complexity: str,
        project_slug: str,
        tier: str,
        counted_success: bool,
        correction: bool = True,
        overkill: bool = False,
    ) -> None:
        """Adjust an already recorded run after user or critic feedback.

        A correction turns a run counted as a success into a failure; the
        caller makes sure feedback is applied once per run.
        """
        if tier not in LADDER_RANK:
            return
        with self._lock:
            for key in {bucket_key(complexity, project_slug), bucket_key(complexity, GLOBAL_PROJECT)}:
                arm = self._buckets.setdefault(key, _Bucket()).arm(tier)
                if correction:
                    arm.corrections += 1
                    if counted_success and arm.successes > 0:
                        arm.successes -= 1
                if overkill:
                    arm.overkill += 1
            self._save_locked()

    def cap_runs(self, tier: str, cap: int = LEGACY_CAP) -> None:
        """Scale one tier down to at most `cap` runs per bucket, keeping its rates."""
        with self._lock:
            for bucket in self._buckets.values():
                if tier in bucket.arms:
                    cap_arm(bucket.arms[tier], cap)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def price(self, usage: RunUsage, unknown_is_fresh: bool = True) -> RunUsage:
        """Turn cumulative per-session Claude costs into this run's cost.

        Live runs treat unknown sessions as fresh: the runner assigns a new
        session id per fresh run, and older sessions are learned by seeding.
        Seeding passes `unknown_is_fresh=False`, because history may start in
        the middle of a session; such a run only sets the baseline.
        """
        if not usage.session_costs:
            return usage
        cost = usage.cost_usd
        with self._lock:
            for session_id, cumulative in usage.session_costs:
                previous = self._session_costs.get(session_id)
                if previous is None and not unknown_is_fresh:
                    self._session_costs[session_id] = cumulative
                    continue
                previous = previous or 0.0
                if cumulative > previous:
                    cost = (cost or 0.0) + cumulative - previous
                    self._session_costs.pop(session_id, None)
                    self._session_costs[session_id] = cumulative
                elif cost is None:
                    cost = 0.0
            while len(self._session_costs) > MAX_TRACKED_SESSIONS:
                self._session_costs.pop(next(iter(self._session_costs)))
        return RunUsage(tokens=usage.tokens, cost_usd=cost)

    def global_stats(self) -> dict[str, dict[str, ArmStats]]:
        """Complexity -> tier -> stats copy, for rendering."""
        result: dict[str, dict[str, ArmStats]] = {}
        with self._lock:
            for key, bucket in self._buckets.items():
                complexity, _, project = key.partition("|")
                if project != GLOBAL_PROJECT:
                    continue
                result[complexity] = {
                    tier: ArmStats(**asdict(arm)) for tier, arm in bucket.arms.items()
                }
        return result

    def _stats_for(self, complexity: str, project_slug: str, tier: str) -> ArmStats:
        project = self._buckets.get(bucket_key(complexity, project_slug))
        if project is not None and project.arm(tier).runs >= MIN_SAMPLES:
            return project.arm(tier)
        fallback = self._buckets.get(bucket_key(complexity, GLOBAL_PROJECT))
        return fallback.arm(tier) if fallback is not None else ArmStats()

    def _load(self) -> dict[str, _Bucket]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") not in {1, STATS_VERSION}:
            return {}
        legacy = payload.get("version") == 1
        sessions = payload.get("sessions") or {}
        if isinstance(sessions, dict):
            self._session_costs = {
                str(key): float(value)
                for key, value in sessions.items()
                if isinstance(value, (int, float))
            }
        buckets: dict[str, _Bucket] = {}
        for key, arms in (payload.get("buckets") or {}).items():
            if not isinstance(arms, dict):
                continue
            bucket = _Bucket()
            for tier, raw in arms.items():
                if tier in LADDER_RANK and isinstance(raw, dict):
                    known = {name: raw[name] for name in ArmStats.__dataclass_fields__ if name in raw}
                    bucket.arms[tier] = ArmStats(**known)
                    if legacy and tier == LEGACY_TIER:
                        cap_arm(bucket.arms[tier], LEGACY_CAP)
            buckets[str(key)] = bucket
        if legacy:
            # v1 stats are dominated by seeded exit-code runs; keep a backup.
            backup = self.path.with_name(self.path.name + ".v1.bak")
            if not backup.exists():
                try:
                    backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
        return buckets

    def _save_locked(self) -> None:
        payload = {
            "version": STATS_VERSION,
            "buckets": {
                key: {tier: asdict(arm) for tier, arm in bucket.arms.items()}
                for key, bucket in sorted(self._buckets.items())
            },
            "sessions": self._session_costs,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)


def bucket_key(complexity: str, project_slug: str) -> str:
    return f"{complexity}|{project_slug or GLOBAL_PROJECT}"


def format_rate(arm: ArmStats) -> str:
    return f"{arm.successes}/{arm.runs} ok"


def wilson_bounds(successes: int, runs: int, z: float = CONFIDENCE_Z) -> tuple[float, float]:
    if runs <= 0:
        return 0.0, 1.0
    rate = successes / runs
    denominator = 1 + z * z / runs
    center = rate + z * z / (2 * runs)
    margin = z * math.sqrt(rate * (1 - rate) / runs + z * z / (4 * runs * runs))
    return (
        max(0.0, (center - margin) / denominator),
        min(1.0, (center + margin) / denominator),
    )


def utility(tier: str, arm: ArmStats) -> float:
    return arm.lower_bound() - COST_WEIGHT * TIER_COST[tier]


def cap_arm(arm: ArmStats, cap: int) -> None:
    if arm.runs <= cap:
        return
    scale = cap / arm.runs
    arm.successes = round(arm.successes * scale)
    arm.corrections = round(arm.corrections * scale)
    arm.overkill = round(arm.overkill * scale)
    arm.cost_usd *= scale
    arm.priced_runs = round(arm.priced_runs * scale)
    arm.tokens = int(arm.tokens * scale)
    arm.duration_seconds *= scale
    arm.runs = cap


def next_tier(tier: str, max_tier: str) -> str | None:
    rank = LADDER_RANK.get(tier, 0) + 1
    if rank > LADDER_RANK[max_tier] or rank >= len(LADDER):
        return None
    return LADDER[rank]


def parse_run_usage(log_path: str | Path | None, offset: int = 0) -> RunUsage:
    """Sum executor usage written to a run log after `offset`.

    Claude stream-json emits one `result` event per run with `usage` and
    `total_cost_usd`; Codex prints `tokens used` followed by the count.
    """
    if not log_path:
        return RunUsage()
    try:
        with open(log_path, "rb") as handle:
            handle.seek(max(0, offset))
            data = handle.read()
    except OSError:
        return RunUsage()
    tokens = 0
    cost: float | None = None
    session_costs: list[tuple[str, float]] = []
    lines = data.decode("utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("{") and '"result"' in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            usage = event.get("usage") or {}
            tokens += sum(
                int(usage.get(name) or 0)
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            total = event.get("total_cost_usd")
            if isinstance(total, (int, float)):
                if event.get("session_id"):
                    session_costs.append((str(event["session_id"]), float(total)))
                else:
                    cost = (cost or 0.0) + float(total)
        elif line.strip() == "tokens used" and index + 1 < len(lines):
            digits = lines[index + 1].strip().replace(",", "")
            if digits.isdigit():
                tokens += int(digits)
    return RunUsage(tokens=tokens, cost_usd=cost, session_costs=tuple(session_costs))


def log_size(log_path: str | Path | None) -> int:
    if not log_path:
        return 0
    try:
        return Path(log_path).stat().st_size
    except OSError:
        return 0


SEED_KINDS = {"task", "direct_task", "followup_task", "agent_chat"}
SEED_OK_PHASES = {"completed", "agent_completed"}
# Kinds that always start a new executor session; follow-ups resume one.
FRESH_SESSION_KINDS = {"task", "direct_task", "agent_chat"}


def seed_from_history(
    policy: RouterPolicy,
    task_payloads: Iterable[dict[str, Any]],
    classify: Any,
    baseline_tier: str = "strong",
) -> int:
    """Seed the baseline tier from tasks that ran before routing existed.

    Those runs used the CLI default model at high effort, so they describe the
    `strong` rung only. Cheaper rungs still have to be earned by live runs.
    """
    seeded = 0
    # Chronological order so cumulative session costs are priced as deltas.
    for payload in sorted(task_payloads, key=lambda item: str(item.get("created_at") or "")):
        if payload.get("kind") not in SEED_KINDS:
            continue
        phase = payload.get("phase")
        if phase not in SEED_OK_PHASES and phase != "failed":
            continue
        if (payload.get("model_routing") or {}).get("selected_tier"):
            continue
        try:
            complexity = classify(payload)
        except Exception:  # noqa: BLE001 - one broken record must not stop seeding
            continue
        policy.record(
            complexity,
            str(payload.get("project_slug") or ""),
            baseline_tier,
            success=phase in SEED_OK_PHASES,
            usage=policy.price(
                parse_run_usage(payload.get("run_log_path")),
                unknown_is_fresh=payload.get("kind") in FRESH_SESSION_KINDS,
            ),
            save=False,
        )
        seeded += 1
    policy.cap_runs(baseline_tier, LEGACY_CAP)
    policy.save()
    return seeded


def render_router_stats(policy: RouterPolicy, baseline_tier: str = "strong") -> str:
    stats = policy.global_stats()
    if not stats:
        return "Router: статистики пока нет."
    lines = ["Router stats (tier: runs, success, avg cost, avg tokens, user fixes):"]
    savings = 0.0
    for complexity in ("trivial", "small", "medium", "large", "critical"):
        arms = stats.get(complexity)
        if not arms:
            continue
        lines.append(f"\n{complexity}:")
        baseline = arms.get(baseline_tier)
        baseline_cost = baseline.avg_cost() if baseline else None
        for tier in LADDER:
            arm = arms.get(tier)
            if arm is None or not arm.runs:
                continue
            avg_cost = arm.avg_cost()
            cost_text = f"${avg_cost:.2f}" if avg_cost is not None else "-"
            mark = " ✓" if arm.proven_good() else (" ✗" if arm.proven_bad() else "")
            lines.append(
                f"- {tier} ({TIER_EFFORT[tier]}): {arm.runs}, "
                f"{arm.successes * 100 // arm.runs}%, {cost_text}, {arm.avg_tokens()}, "
                f"fix {arm.corrections}{mark}"
            )
            if tier != baseline_tier and baseline_cost is not None and avg_cost is not None:
                savings += arm.priced_runs * (baseline_cost - avg_cost)
    lines.append(f"\nЭкономия vs {baseline_tier}: ~${savings:.2f}")
    lines.append(
        f"✓ proven ≥{int(SUCCESS_THRESHOLD * 100)}%, ✗ proven below it "
        f"(≥{MIN_SAMPLES} runs, Wilson z={CONFIDENCE_Z}). History before routing is capped "
        f"at {LEGACY_CAP} runs per arm."
    )
    return "\n".join(lines)


def render_recent_routing(task_payloads: Iterable[dict[str, Any]], days: int = 30) -> str:
    """Where routed runs actually went lately, and how often the user fixed them."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    tiers: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    corrected: Counter[str] = Counter()
    for payload in task_payloads:
        routing = payload.get("model_routing") or {}
        tier = routing.get("selected_tier")
        if not tier or str(payload.get("created_at") or "") < since:
            continue
        tiers[tier] += 1
        sources[str(routing.get("policy_source") or "-")] += 1
        if (routing.get("feedback") or {}).get("applied"):
            corrected[tier] += 1
    total = sum(tiers.values())
    if not total:
        return f"Последние {days} дней: маршрутизированных задач нет."
    lines = [f"Последние {days} дней ({total} задач):"]
    for tier in LADDER:
        if tiers[tier]:
            lines.append(
                f"- {tier}: {tiers[tier]} ({tiers[tier] * 100 // total}%), "
                f"исправлений {corrected[tier]}"
            )
    lines.append("Источник: " + ", ".join(f"{name} {count}" for name, count in sources.most_common()))
    return "\n".join(lines)
