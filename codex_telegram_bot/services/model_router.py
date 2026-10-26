from __future__ import annotations

import logging
import re
import zlib
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..config import Config, normalize_task_provider
from ..task_store import TaskRecord
from .llm_provider import LlmProviderUnavailable
from .llm_router import LlmTierAdvisor, TierAdvice
from .router_policy import (
    DEFAULT_CLAUDE_TIER_MODELS,
    RISK_FLOOR,
    TIER_EFFORT,
    PolicyChoice,
    RouterPolicy,
    next_tier,
)
from .task_similarity import SimilarRuns, TaskSimilarity


LOG = logging.getLogger("codex_telegram_bot")
COMPLEXITIES = ("trivial", "small", "medium", "large", "critical")
COMPLEXITY_RANK = {complexity: index for index, complexity in enumerate(COMPLEXITIES)}
TIERS = ("cheap", "standard", "strong", "max")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
# Similar past tasks may pick a cheaper tier when it succeeded on all of
# at least this many neighbours.
SIMILAR_MIN_RUNS = 3

TRIVIAL_RE = re.compile(
    r"\b(typo|опечат|copy|copywriting|readme|css|текст|текста|"
    r"переимен|лог|config|конфиг|dependency version|version bump)\b",
    re.IGNORECASE,
)
SMALL_RE = re.compile(r"\b(баг|bug|fix|почин|валидац|one file|одном файле)\b", re.IGNORECASE)
MEDIUM_RE = re.compile(
    r"\b(feature|фич|интеграц|business|логик|нескольк|tests?|тест)\b",
    re.IGNORECASE,
)
LARGE_RE = re.compile(
    r"\b(refactor|рефактор|архитект|migration|миграц|schema|api|grpc|"
    r"database|db|оркестратор|orchestrator|memory engine|modelrouter)\b",
    re.IGNORECASE,
)
CRITICAL_RE = re.compile(
    r"\b(security|auth|authorization|аутентификац|авторизац|billing|payment|"
    r"permissions?|prod|production|data loss|удален|секрет|credential)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskClassification:
    complexity: str
    confidence: float
    reasoning_summary: str
    requires_architect: bool
    requires_reviewer: bool
    requires_debate: bool
    recommended_codex_tier: str
    recommended_claude_architect_tier: str
    recommended_claude_reviewer_tier: str
    max_review_rounds: int
    estimated_risk: str
    requires_human_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoutingDecision:
    classification: TaskClassification
    selected_flow: str
    selected_models: dict[str, str | None]
    routing_reason: str
    estimated_tokens: int
    manual_tier: str = "auto"
    executor_provider: str = "codex"
    escalations: list[str] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    effort: str = ""
    policy_source: str = "rule"
    policy_reason: str = ""
    # Tier the regex rule recommended before the learned policy moved it.
    rule_tier: str = ""
    # Critic review for complex work, sampled review for the rest: every tier
    # gets the same reviewed success signal, not only the cheap ones.
    critic_review: bool = False
    verify_sampled: bool = False
    # "light" for read-only runs that have no diff to review.
    verify_override: str = ""
    # Lowest tier allowed after a user correction of the parent run.
    floor_tier: str = ""

    @property
    def verify_mode(self) -> str:
        if self.verify_override:
            return self.verify_override
        if (
            self.classification.estimated_risk == "high"
            or self.critic_review
            or self.verify_sampled
        ):
            return "full"
        tier = self.classification.recommended_codex_tier
        if self.rule_tier and TIER_RANK[tier] < TIER_RANK[self.rule_tier]:
            # A cheaper tier than the rule must prove itself on reviewed runs,
            # otherwise exit-code-only successes would teach blind downgrades.
            return "full"
        return "light"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["complexity"] = self.classification.complexity
        payload["selected_tier"] = self.classification.recommended_codex_tier
        payload["executor_provider"] = self.executor_provider
        payload["verify"] = self.verify_mode
        return payload


class ModelRouter:
    def __init__(
        self,
        config: Config,
        policy: RouterPolicy | None = None,
        advisor: LlmTierAdvisor | None = None,
        similarity: TaskSimilarity | None = None,
    ) -> None:
        self.config = config
        self.policy = policy
        self.advisor = advisor
        self.similarity = similarity
        # route() runs several times per task (orchestrator, fallback, /routing);
        # the advisor and the kNN index are asked once per task id.
        self._advice_cache: dict[str, TierAdvice | None] = {}
        self._similar_cache: dict[str, SimilarRuns] = {}

    def classify(self, task: TaskRecord) -> TaskClassification:
        text = " ".join(
            [
                task.prompt,
                task.project_slug,
                task.project_name,
                " ".join(item.get("file_name", "") for item in task.attachments),
            ]
        )
        normalized_len = len(task.prompt.strip())

        if CRITICAL_RE.search(text):
            return TaskClassification(
                complexity="critical",
                confidence=0.78,
                reasoning_summary="Задача выглядит чувствительной: auth/security/billing/production область.",
                requires_architect=True,
                requires_reviewer=True,
                requires_debate=self.config.orchestrator_debate,
                recommended_codex_tier="max",
                recommended_claude_architect_tier="strong",
                recommended_claude_reviewer_tier="strong",
                max_review_rounds=min(2, self.config.orchestrator_max_review_rounds),
                estimated_risk="high",
                requires_human_approval=True,
            )
        if LARGE_RE.search(text):
            return TaskClassification(
                complexity="large",
                confidence=0.74,
                reasoning_summary="Задача похожа на архитектурную, orchestration/refactor/API/DB работу.",
                requires_architect=True,
                requires_reviewer=True,
                requires_debate=self.config.orchestrator_debate,
                recommended_codex_tier="strong",
                recommended_claude_architect_tier="standard",
                recommended_claude_reviewer_tier="standard",
                max_review_rounds=min(2, self.config.orchestrator_max_review_rounds),
                estimated_risk="high",
            )
        if TRIVIAL_RE.search(text) and normalized_len < 220:
            return TaskClassification(
                complexity="trivial",
                confidence=0.82,
                reasoning_summary="Похоже на простой copy/config/docs/CSS/typo fix.",
                requires_architect=False,
                requires_reviewer=False,
                requires_debate=False,
                recommended_codex_tier="cheap",
                recommended_claude_architect_tier="none",
                recommended_claude_reviewer_tier="none",
                max_review_rounds=0,
                estimated_risk="low",
            )
        if SMALL_RE.search(text) or normalized_len < 260:
            return TaskClassification(
                complexity="small",
                confidence=0.68,
                reasoning_summary="Похоже на локальный багфикс или небольшое изменение.",
                requires_architect=False,
                requires_reviewer=True,
                requires_debate=False,
                recommended_codex_tier="standard" if "auth" in text.lower() else "cheap",
                recommended_claude_architect_tier="none",
                recommended_claude_reviewer_tier="cheap",
                max_review_rounds=min(1, self.config.orchestrator_max_review_rounds),
                estimated_risk="medium" if "auth" in text.lower() else "low",
            )
        if MEDIUM_RE.search(text) or normalized_len < 1200:
            return TaskClassification(
                complexity="medium",
                confidence=0.66,
                reasoning_summary="Обычная feature/bugfix задача с возможной бизнес-логикой.",
                requires_architect=True,
                requires_reviewer=True,
                requires_debate=False,
                recommended_codex_tier="standard",
                recommended_claude_architect_tier="standard",
                recommended_claude_reviewer_tier="standard",
                max_review_rounds=min(1, self.config.orchestrator_max_review_rounds),
                estimated_risk="medium",
            )
        return TaskClassification(
            complexity="large",
            confidence=0.61,
            reasoning_summary="Запрос длинный или размытый; безопаснее считать его крупной задачей.",
            requires_architect=True,
            requires_reviewer=True,
            requires_debate=self.config.orchestrator_debate,
            recommended_codex_tier="strong",
            recommended_claude_architect_tier="standard",
            recommended_claude_reviewer_tier="standard",
            max_review_rounds=min(2, self.config.orchestrator_max_review_rounds),
            estimated_risk="high",
        )

    def route(
        self,
        task: TaskRecord,
        manual_tier: str | None = None,
        executor_provider: str | None = None,
    ) -> RoutingDecision:
        classification = self.classify(task)
        tier = normalize_manual_tier(manual_tier)
        provider = normalize_task_provider(executor_provider or self.config.task_provider)
        if tier is None:
            tier = (
                "auto"
                if self.config.orchestrator_model_routing == "auto"
                else self.config.orchestrator_default_tier
            )
        warnings: list[str] = []
        escalations: list[str] = []
        downgrades: list[str] = []
        policy_source = "manual" if tier != "auto" else "rule"
        policy_reason = ""
        rule_tier = ""
        advice = self.advice_for(task, classification) if tier == "auto" else None
        if advice is not None:
            classification = apply_advice(classification, advice)

        selected_codex_tier = classification.recommended_codex_tier
        if tier != "auto":
            selected_codex_tier = tier
            if (
                TIER_RANK[tier] < TIER_RANK[classification.recommended_codex_tier]
                and classification.estimated_risk != "low"
            ):
                warnings.append("Manual tier ниже рекомендованного для риска задачи.")
        else:
            max_auto = self.config.orchestrator_max_auto_tier
            if TIER_RANK[selected_codex_tier] > TIER_RANK[max_auto]:
                selected_codex_tier = max_auto
                downgrades.append(f"auto tier capped at {max_auto}")

        routed = replace_codex_tier(classification, selected_codex_tier)
        if task.attachments and routed.complexity == "trivial":
            routed = replace_complexity(routed, "small", "standard")
            escalations.append("attachments make trivial task small")

        floor_tier = str(task.model_routing.get("floor_tier") or "")
        if floor_tier not in TIER_RANK:
            floor_tier = ""
        if tier == "auto" and advice is not None and advice.explicit_user_request:
            # The user named the model: learned/explore must not move it.
            policy_source = "llm-explicit"
            policy_reason = advice.reason
        elif tier == "auto" and self.policy is not None and self.config.router_learning:
            rule_tier = routed.recommended_codex_tier
            choice = self.policy.choose(
                routed.complexity,
                task.project_slug,
                routed.recommended_codex_tier,
                self.auto_tier_ceiling(),
            )
            if choice.tier == rule_tier:
                choice = self.similar_choice(task, routed.complexity, choice)
            llm_floor = self.llm_floor(routed.complexity, rule_tier) if advice is not None else None
            if llm_floor is not None and TIER_RANK[choice.tier] < TIER_RANK[llm_floor]:
                choice = PolicyChoice(
                    tier=llm_floor,
                    effort=TIER_EFFORT[llm_floor],
                    source="rule" if llm_floor == rule_tier else "learned",
                    reason=f"kept near LLM tier {rule_tier}",
                )
            if floor_tier and TIER_RANK[choice.tier] < TIER_RANK[floor_tier]:
                target = min(floor_tier, self.auto_tier_ceiling(), key=TIER_RANK.get)
                choice = PolicyChoice(
                    tier=target,
                    effort=TIER_EFFORT[target],
                    source="feedback",
                    reason=f"user corrected a {floor_tier} run",
                )
            routed = replace_codex_tier(routed, choice.tier)
            policy_source = choice.source
            policy_reason = choice.reason

        if advice is not None and policy_source == "rule":
            policy_source = "llm"
            policy_reason = advice.reason

        if routed.recommended_codex_tier == "max":
            if not self.config.orchestrator_budget.allow_max_tier:
                routed = replace_codex_tier(routed, "strong")
                downgrades.append("max tier disabled by budget config")
            elif self.config.orchestrator_require_confirm_for_max:
                warnings.append("max tier requires user confirmation")

        executor_model = self.executor_model(provider, routed.recommended_codex_tier)
        selected_models = {
            "classifier": self.config.models.claude.for_tier("cheap"),
            "architect": self.config.models.claude.for_tier(
                routed.recommended_claude_architect_tier
            )
            if routed.recommended_claude_architect_tier != "none"
            else None,
            "executor": executor_model,
            "reviewer": self.config.models.claude.for_tier(
                routed.recommended_claude_reviewer_tier
            )
            if routed.recommended_claude_reviewer_tier != "none"
            else None,
        }
        flow = render_selected_flow(routed, provider)
        estimated_tokens = self.config.orchestrator_budget.per_task_token_budget.get(
            routed.complexity,
            0,
        )
        return RoutingDecision(
            classification=routed,
            selected_flow=flow,
            selected_models=selected_models,
            routing_reason=routed.reasoning_summary,
            estimated_tokens=estimated_tokens,
            manual_tier=tier,
            executor_provider=provider,
            escalations=escalations,
            downgrades=downgrades,
            warnings=warnings,
            effort=(
                TIER_EFFORT.get(routed.recommended_codex_tier, "")
                if self.config.router_learning
                else ""
            ),
            policy_source=policy_source,
            policy_reason=policy_reason,
            rule_tier=rule_tier,
            critic_review=self.critic_review_for(routed.complexity),
            verify_sampled=(
                tier == "auto"
                and stable_fraction(task.id) < self.config.router_verify_sample_rate
            ),
            floor_tier=floor_tier,
        )

    def critic_review_for(self, complexity: str) -> bool:
        minimum = self.config.router_critic_min_complexity
        if minimum not in COMPLEXITY_RANK:
            return False
        return COMPLEXITY_RANK[complexity] >= COMPLEXITY_RANK[minimum]

    @staticmethod
    def llm_floor(complexity: str, llm_tier: str) -> str | None:
        """How far stats may move below the LLM tier.

        Large+ work keeps the LLM tier; medium work may go one step cheaper
        on proven stats; small work follows the stats.
        """
        if COMPLEXITY_RANK[complexity] >= COMPLEXITY_RANK["large"]:
            return llm_tier
        if complexity == "medium":
            return TIERS[max(0, TIER_RANK[llm_tier] - 1)]
        return None

    def similar_runs(self, task: TaskRecord) -> SimilarRuns:
        if self.similarity is None:
            return SimilarRuns()
        if task.id not in self._similar_cache:
            try:
                self._similar_cache[task.id] = self.similarity.similar(task.prompt, task.id)
            except Exception:  # noqa: BLE001 - a broken history record must not block routing
                LOG.exception("similar task lookup failed for task %s", task.id)
                self._similar_cache[task.id] = SimilarRuns()
        return self._similar_cache[task.id]

    def similar_choice(
        self,
        task: TaskRecord,
        complexity: str,
        choice: PolicyChoice,
    ) -> PolicyChoice:
        """A cheaper tier that solved every similar past task it was tried on."""
        floor = RISK_FLOOR.get(complexity, "cheap")
        per_tier = self.similar_runs(task).per_tier()
        for tier in TIERS:
            if TIER_RANK[tier] >= TIER_RANK[choice.tier] or TIER_RANK[tier] < TIER_RANK[floor]:
                continue
            ok, total = per_tier.get(tier, (0, 0))
            if total >= SIMILAR_MIN_RUNS and ok == total:
                return PolicyChoice(
                    tier=tier,
                    effort=TIER_EFFORT[tier],
                    source="similar",
                    reason=f"{tier} solved {ok}/{total} similar tasks",
                )
        return choice

    def planning_model(self, task: TaskRecord, provider: str) -> str | None:
        """Read-only planning: sonnet unless the work is large or critical."""
        if self.config.orchestrator_model_routing != "auto" or not self.config.router_learning:
            return None
        classification = self.classify(task)
        tier = (
            "strong"
            if COMPLEXITY_RANK[classification.complexity] >= COMPLEXITY_RANK["large"]
            else "standard"
        )
        advice = self.advice_for(task, classification)
        if advice is not None and advice.explicit_user_request:
            tier = advice.tier
        return self.executor_model(normalize_task_provider(provider), tier)

    def advice_for(self, task: TaskRecord, classification: TaskClassification) -> TierAdvice | None:
        if self.advisor is None:
            return None
        if task.id not in self._advice_cache:
            self._advice_cache[task.id] = self._request_advice(task, classification)
        return self._advice_cache[task.id]

    def _request_advice(
        self,
        task: TaskRecord,
        classification: TaskClassification,
    ) -> TierAdvice | None:
        stats = (
            self.policy.stats_summary(classification.complexity, task.project_slug)
            if self.policy is not None
            else "no stats"
        )
        try:
            advice = self.advisor.advise(
                task,
                classification.complexity,
                classification.recommended_codex_tier,
                stats,
                similar=self.similar_runs(task).render(),
            )
        except (LlmProviderUnavailable, ValueError) as exc:
            LOG.warning("LLM tier advice failed for task %s, using rules: %s", task.id, exc)
            return None
        if advice.tier not in TIERS:
            LOG.warning("LLM tier advice has invalid tier for task %s: %s", task.id, advice.tier)
            return None
        return advice

    def executor_model(self, provider: str, tier: str) -> str | None:
        if provider == "claude":
            configured = self.config.models.claude.for_tier(tier)
            if configured or not self.config.router_learning:
                return configured
            return DEFAULT_CLAUDE_TIER_MODELS.get(tier)
        return self.config.models.codex.for_tier(tier)

    def auto_tier_ceiling(self) -> str:
        ceiling = self.config.orchestrator_max_auto_tier
        if ceiling == "max" and (
            self.config.orchestrator_require_confirm_for_max
            or not self.config.orchestrator_budget.allow_max_tier
        ):
            return "strong"
        return ceiling

    def escalate(self, decision: RoutingDecision) -> RoutingDecision | None:
        """Next executor tier after a failed verify, or None at the ceiling."""
        current = decision.classification.recommended_codex_tier
        if (
            decision.manual_tier != "auto"
            or not self.config.router_learning
            or not self.config.router_auto_escalate
        ):
            return None
        target = next_tier(current, self.auto_tier_ceiling())
        if target is None:
            return None
        classification = replace_codex_tier(decision.classification, target)
        selected_models = dict(decision.selected_models)
        selected_models["executor"] = self.executor_model(decision.executor_provider, target)
        return replace(
            decision,
            classification=classification,
            selected_flow=render_selected_flow(classification, decision.executor_provider),
            selected_models=selected_models,
            effort=TIER_EFFORT[target],
            escalations=[*decision.escalations, f"verify failed: {current} -> {target}"],
            policy_source="escalate",
            policy_reason=f"{current} failed verify",
        )

    def evaluate_post_run(
        self,
        decision: RoutingDecision,
        touched_files: int,
        failed: bool,
        major_review_issues: bool = False,
    ) -> list[str]:
        triggers: list[str] = []
        if failed:
            triggers.append("codex_failed")
        if touched_files > 5 and decision.classification.complexity in {"trivial", "small"}:
            triggers.append("diff_touched_more_files_than_expected")
        if major_review_issues:
            triggers.append("reviewer_found_major_or_critical_issues")
        return triggers


def stable_fraction(value: str) -> float:
    """Deterministic [0, 1) per task id, so re-routing keeps the same sample."""
    return (zlib.crc32(value.encode("utf-8")) % 10000) / 10000


def normalize_manual_tier(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    tier = value.strip().lower()
    if tier not in {"auto", *TIERS}:
        raise ValueError(f"Invalid tier: {value}")
    return tier


def replace_codex_tier(
    classification: TaskClassification,
    tier: str,
) -> TaskClassification:
    data = classification.to_dict()
    data["recommended_codex_tier"] = tier
    return TaskClassification(**data)


def apply_advice(
    classification: TaskClassification,
    advice: TierAdvice,
) -> TaskClassification:
    tier = advice.tier
    floor = RISK_FLOOR.get(classification.complexity)
    # Only an explicit user request may go below the risk floor.
    if not advice.explicit_user_request and floor and TIER_RANK[floor] > TIER_RANK[tier]:
        tier = floor
    return replace_codex_tier(classification, tier)


def replace_complexity(
    classification: TaskClassification,
    complexity: str,
    tier: str,
) -> TaskClassification:
    data = classification.to_dict()
    data["complexity"] = complexity
    data["recommended_codex_tier"] = tier
    data["requires_reviewer"] = True
    data["recommended_claude_reviewer_tier"] = "cheap"
    data["max_review_rounds"] = max(1, data["max_review_rounds"])
    return TaskClassification(**data)


def render_selected_flow(
    classification: TaskClassification,
    executor_provider: str = "codex",
) -> str:
    executor_name = "Claude" if executor_provider == "claude" else "Codex"
    if not classification.requires_architect and not classification.requires_reviewer:
        return f"{executor_name} {classification.recommended_codex_tier} only"
    parts: list[str] = []
    if classification.requires_architect:
        parts.append(f"Claude Architect {classification.recommended_claude_architect_tier}")
    parts.append(f"{executor_name} {classification.recommended_codex_tier}")
    if classification.requires_reviewer:
        parts.append(f"Claude Reviewer {classification.recommended_claude_reviewer_tier}")
    if classification.requires_debate:
        parts.append("optional debate")
    return " -> ".join(parts)


def render_routing_decision(decision: RoutingDecision) -> str:
    lines = [
        "ModelRouter:",
        f"- complexity: {decision.classification.complexity}",
        f"- selected flow: {decision.selected_flow}",
        f"- executor provider: {decision.executor_provider}",
        f"- reason: {decision.routing_reason}",
    ]
    lines.append(f"- {render_executor_choice(decision)}")
    if decision.manual_tier != "auto":
        lines.append(f"- manual tier: {decision.manual_tier}")
    for warning in decision.warnings:
        lines.append(f"- warning: {warning}")
    return "\n".join(lines)


def render_executor_choice(decision: RoutingDecision) -> str:
    model = decision.selected_models.get("executor") or "default"
    tier = decision.classification.recommended_codex_tier
    source = decision.policy_source
    if decision.policy_reason:
        source = f"{source}: {decision.policy_reason}"
    return (
        f"router: {model} ({tier}) · effort {decision.effort or '-'} · "
        f"verify {decision.verify_mode} · {source}"
    )
