from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Config
from ..task_store import TaskRecord


COMPLEXITIES = ("trivial", "small", "medium", "large", "critical")
TIERS = ("cheap", "standard", "strong", "max")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}

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
    escalations: list[str] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["complexity"] = self.classification.complexity
        payload["selected_tier"] = self.classification.recommended_codex_tier
        return payload


class ModelRouter:
    def __init__(self, config: Config) -> None:
        self.config = config

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
                recommended_claude_architect_tier="strong",
                recommended_claude_reviewer_tier="strong",
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
            recommended_claude_architect_tier="strong",
            recommended_claude_reviewer_tier="strong",
            max_review_rounds=min(2, self.config.orchestrator_max_review_rounds),
            estimated_risk="high",
        )

    def route(self, task: TaskRecord, manual_tier: str | None = None) -> RoutingDecision:
        classification = self.classify(task)
        tier = normalize_manual_tier(manual_tier)
        if tier is None:
            tier = (
                "auto"
                if self.config.orchestrator_model_routing == "auto"
                else self.config.orchestrator_default_tier
            )
        warnings: list[str] = []
        escalations: list[str] = []
        downgrades: list[str] = []

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

        if routed.recommended_codex_tier == "max":
            if not self.config.orchestrator_budget.allow_max_tier:
                routed = replace_codex_tier(routed, "strong")
                downgrades.append("max tier disabled by budget config")
            elif self.config.orchestrator_require_confirm_for_max:
                warnings.append("max tier requires user confirmation")

        selected_models = {
            "classifier": self.config.models.claude.for_tier("cheap"),
            "architect": self.config.models.claude.for_tier(
                routed.recommended_claude_architect_tier
            )
            if routed.recommended_claude_architect_tier != "none"
            else None,
            "executor": self.config.models.codex.for_tier(routed.recommended_codex_tier),
            "reviewer": self.config.models.claude.for_tier(
                routed.recommended_claude_reviewer_tier
            )
            if routed.recommended_claude_reviewer_tier != "none"
            else None,
        }
        flow = render_selected_flow(routed)
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
            escalations=escalations,
            downgrades=downgrades,
            warnings=warnings,
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


def render_selected_flow(classification: TaskClassification) -> str:
    if not classification.requires_architect and not classification.requires_reviewer:
        return f"Codex {classification.recommended_codex_tier} only"
    parts: list[str] = []
    if classification.requires_architect:
        parts.append(f"Claude Architect {classification.recommended_claude_architect_tier}")
    parts.append(f"Codex {classification.recommended_codex_tier}")
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
        f"- reason: {decision.routing_reason}",
    ]
    if decision.manual_tier != "auto":
        lines.append(f"- manual tier: {decision.manual_tier}")
    for warning in decision.warnings:
        lines.append(f"- warning: {warning}")
    return "\n".join(lines)
