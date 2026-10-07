from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ..codex_runner import CodexRunner, read_task_context
from ..config import Config
from ..project_index import ProjectInfo
from ..task_store import TaskRecord, TaskStore, now_iso
from .executors import executor_display_name
from .llm_provider import LlmProvider, LlmProviderUnavailable, LlmRequest
from .model_router import (
    ModelRouter,
    RoutingDecision,
    render_routing_decision,
    render_selected_flow,
)
from .project_memory_engine import ProjectMemoryEngine
from .redaction import RedactionService


@dataclass(frozen=True)
class ArchitectPlan:
    summary: str = ""
    risk_level: str = "medium"
    task_type: str = "feature"
    implementation_plan: list[str] = field(default_factory=list)
    files_to_inspect: list[str] = field(default_factory=list)
    files_likely_to_change: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    optional_questions: list[str] = field(default_factory=list)
    codex_prompt: str = ""

    @classmethod
    def from_text(cls, text: str, fallback_prompt: str) -> "ArchitectPlan":
        payload = parse_json_object(text)
        if not payload:
            return cls(summary=text[:500], codex_prompt=fallback_prompt)
        return cls(
            summary=str(payload.get("summary") or ""),
            risk_level=str(payload.get("risk_level") or "medium"),
            task_type=str(payload.get("task_type") or "feature"),
            implementation_plan=list_of_strings(payload.get("implementation_plan")),
            files_to_inspect=list_of_strings(payload.get("files_to_inspect")),
            files_likely_to_change=list_of_strings(payload.get("files_likely_to_change")),
            acceptance_criteria=list_of_strings(payload.get("acceptance_criteria")),
            test_plan=list_of_strings(payload.get("test_plan")),
            questions=list_of_strings(payload.get("questions")),
            optional_questions=list_of_strings(payload.get("optional_questions")),
            codex_prompt=str(payload.get("codex_prompt") or fallback_prompt),
        )


@dataclass(frozen=True)
class ReviewIssue:
    severity: str
    file: str
    comment: str
    suggested_fix: str = ""


@dataclass(frozen=True)
class ReviewResult:
    verdict: str = "approve"
    review_summary: str = ""
    issues: list[ReviewIssue] = field(default_factory=list)
    missing_tests: list[str] = field(default_factory=list)
    regression_risks: list[str] = field(default_factory=list)
    follow_up_prompt_for_codex: str = ""

    @classmethod
    def from_text(cls, text: str) -> "ReviewResult":
        payload = parse_json_object(text)
        if not payload:
            return cls(verdict="needs_human", review_summary=text[:800])
        issues: list[ReviewIssue] = []
        for item in payload.get("issues") or []:
            if isinstance(item, dict):
                issues.append(
                    ReviewIssue(
                        severity=str(item.get("severity") or "minor"),
                        file=str(item.get("file") or ""),
                        comment=str(item.get("comment") or ""),
                        suggested_fix=str(item.get("suggested_fix") or ""),
                    )
                )
        verdict = str(payload.get("verdict") or "needs_human").strip().lower()
        if verdict not in {"approve", "request_changes", "needs_human"}:
            verdict = "needs_human"
        return cls(
            verdict=verdict,
            review_summary=str(payload.get("review_summary") or ""),
            issues=issues,
            missing_tests=list_of_strings(payload.get("missing_tests")),
            regression_risks=list_of_strings(payload.get("regression_risks")),
            follow_up_prompt_for_codex=str(
                payload.get("follow_up_prompt_for_codex") or ""
            ),
        )


class OrchestratorService:
    def __init__(
        self,
        config: Config,
        store: TaskStore,
        runner: CodexRunner,
        model_router: ModelRouter,
        claude_provider: LlmProvider,
        memory_engine: ProjectMemoryEngine,
        redactor: RedactionService | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.runner = runner
        self.model_router = model_router
        self.claude_provider = claude_provider
        self.memory_engine = memory_engine
        self.redactor = redactor or RedactionService()

    def execute(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        orchestrator_enabled: bool,
        recover: bool = False,
        executor_provider: str = "codex",
        notify: Callable[[str], None] | None = None,
        append_event: Callable[..., None] | None = None,
    ) -> TaskRecord:
        manual_tier = str(task.model_routing.get("manual_tier") or "auto")
        decision = self.model_router.route(
            task,
            manual_tier=manual_tier,
            executor_provider=executor_provider,
        )
        self._record_routing(task, decision, orchestrator_enabled)
        self._append_event(
            append_event,
            task.id,
            "ModelRouter",
            f"{decision.classification.complexity}: {decision.selected_flow}",
            decision.to_dict(),
        )
        if notify is not None:
            notify(render_routing_decision(decision))

        if not orchestrator_enabled:
            return self._run_executor(task, project, decision, recover=recover)

        if not self._executor_available(decision):
            selected_name = executor_display_name(decision.executor_provider)
            reason = self.runner.provider_unavailable_reason(decision.executor_provider)
            fallback_possible = (
                decision.executor_provider != "codex"
                and not self.runner.provider_unavailable_reason("codex")
            )
            if (
                self.config.orchestrator_strict_mode
                or not self.config.orchestrator_fallback_to_codex
                or not fallback_possible
            ):
                task.phase = "failed"
                task.error = f"{selected_name} executor is unavailable: {reason}"
                self.store.save_task(task)
                self._save_trace(task, decision, final_status="failed")
                return task
            self._append_event(
                append_event,
                task.id,
                "Orchestrator",
                f"{selected_name} executor unavailable; falling back to Codex executor.",
                {"fallback_to_codex": True, "reason": reason},
            )
            if notify is not None:
                notify(
                    f"{selected_name} executor недоступен ({reason}). "
                    "Перехожу на Codex по fallback-настройке."
                )
            decision = self._fallback_to_codex_decision(task, decision)

        if not decision.classification.requires_architect and not decision.classification.requires_reviewer:
            executor_name = executor_display_name(decision.executor_provider)
            self._append_event(
                append_event,
                task.id,
                "Orchestrator",
                f"Cheap path selected: {executor_name}-only flow.",
                None,
            )
            return self._run_executor(task, project, decision, recover=recover)

        if not self._claude_orchestrator_available(decision):
            if self.config.orchestrator_strict_mode or not self.config.orchestrator_fallback_to_codex:
                task.phase = "failed"
                task.error = "Claude orchestrator is unavailable and fallback is disabled."
                self.store.save_task(task)
                self._save_trace(task, decision, final_status="failed")
                return task
            self._append_event(
                append_event,
                task.id,
                "Orchestrator",
                "Claude architect/reviewer unavailable; running executor-only flow.",
                {"executor_provider": decision.executor_provider},
            )
            if notify is not None:
                executor_name = executor_display_name(decision.executor_provider)
                notify(f"Claude architect/reviewer недоступен. Запускаю {executor_name}-only flow.")
            decision = self._without_claude_roles_decision(task, decision)
            task = self._run_executor(task, project, decision, recover=recover)
            self._save_trace(
                task,
                decision,
                codex_summary=read_final(task),
                final_status="done" if task.phase == "completed" else "failed",
            )
            return task

        architect_plan: ArchitectPlan | None = None
        if decision.classification.requires_architect:
            if notify is not None:
                notify("Claude architect: составляю план.")
            try:
                architect_plan = self._run_architect(task, project, decision)
            except LlmProviderUnavailable as exc:
                return self._handle_provider_failure(task, project, decision, exc, recover)
            self._append_event(
                append_event,
                task.id,
                "ClaudeArchitect",
                "Architect plan prepared.",
                asdict(architect_plan),
            )
            if architect_plan.questions:
                task.phase = "planned"
                task.plan_text = render_architect_plan(architect_plan)
                task.error = "Claude architect has blocking questions."
                self.store.save_task(task)
                self._save_trace(
                    task,
                    decision,
                    architect_plan=architect_plan,
                    final_status="needs_human",
                )
                if notify is not None:
                    notify(
                        "Claude architect: есть уточняющие вопросы.\n"
                        + "\n".join(f"- {item}" for item in architect_plan.questions[:3])
                    )
                return task

            task.plan_text = render_architect_plan(architect_plan)
            task.prepared_codex_prompt = architect_plan.codex_prompt
            self.store.save_task(task)
            if notify is not None:
                notify("Claude architect: составил план.")

        task = self._run_executor(task, project, decision, recover=recover)
        if notify is not None:
            executor_name = executor_display_name(decision.executor_provider)
            notify(f"{executor_name}: завершил реализацию." if task.phase == "completed" else f"{executor_name}: выполнение завершилось ошибкой.")
        if task.phase != "completed" or not decision.classification.requires_reviewer:
            self._save_trace(
                task,
                decision,
                architect_plan=architect_plan,
                codex_summary=read_final(task),
                final_status="done" if task.phase == "completed" else "failed",
            )
            return task

        review_round = 0
        review: ReviewResult | None = None
        while review_round <= decision.classification.max_review_rounds:
            if notify is not None:
                notify("Claude reviewer: проверяю diff.")
            try:
                review = self._run_reviewer(task, project, decision, architect_plan)
            except LlmProviderUnavailable as exc:
                return self._handle_provider_failure(task, project, decision, exc, recover)
            task.orchestrator_review_rounds = review_round
            self.store.save_task(task)
            self._append_event(
                append_event,
                task.id,
                "ClaudeReviewer",
                f"Reviewer verdict: {review.verdict}",
                asdict(review),
            )

            if review.verdict == "approve":
                if notify is not None:
                    notify("Claude reviewer: approved.")
                self._save_trace(
                    task,
                    decision,
                    architect_plan=architect_plan,
                    codex_summary=read_final(task),
                    review=review,
                    final_status="done",
                )
                return task

            if review.verdict == "needs_human":
                task.phase = "needs_human"
                task.error = review.review_summary or "Claude reviewer requested human review."
                self.store.save_task(task)
                self._save_trace(
                    task,
                    decision,
                    architect_plan=architect_plan,
                    codex_summary=read_final(task),
                    review=review,
                    final_status="needs_human",
                )
                return task

            if review_round >= decision.classification.max_review_rounds:
                task.phase = "needs_human"
                task.error = (
                    "Claude reviewer still sees issues after "
                    f"{decision.classification.max_review_rounds} review rounds."
                )
                self.store.save_task(task)
                self._save_trace(
                    task,
                    decision,
                    architect_plan=architect_plan,
                    codex_summary=read_final(task),
                    review=review,
                    final_status="needs_human",
                )
                return task

            if not review.follow_up_prompt_for_codex.strip():
                task.phase = "needs_human"
                task.error = "Claude requested changes without a follow-up Codex prompt."
                self.store.save_task(task)
                return task

            review_round += 1
            if notify is not None:
                notify("Claude reviewer: requested changes. Agent исправляет замечания.")
            task.prepared_codex_prompt = review.follow_up_prompt_for_codex.strip()
            self.store.save_task(task)
            task = self.runner.run_revision_execution(
                task,
                project,
                review_round,
                model=decision.selected_models.get("executor"),
                provider=decision.executor_provider,
            )

        return task

    def _record_routing(
        self,
        task: TaskRecord,
        decision: RoutingDecision,
        orchestrator_enabled: bool,
    ) -> None:
        task.orchestrator_enabled = orchestrator_enabled
        task.model_routing = decision.to_dict()
        task.trace_id = task.trace_id or f"trace-{task.id}"
        self.store.save_task(task)

    def _run_executor(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        decision: RoutingDecision,
        recover: bool = False,
    ) -> TaskRecord:
        model = decision.selected_models.get("executor")
        if recover:
            return self.runner.run_recovery_execution(
                task,
                project,
                model=model,
                provider=decision.executor_provider,
            )
        return self.runner.run_execution(
            task,
            project,
            model=model,
            provider=decision.executor_provider,
        )

    def _executor_available(self, decision: RoutingDecision) -> bool:
        return not self.runner.provider_unavailable_reason(decision.executor_provider)

    def _claude_orchestrator_available(self, decision: RoutingDecision) -> bool:
        if not (
            decision.classification.requires_architect
            or decision.classification.requires_reviewer
        ):
            return True
        if not self.config.claude_enabled:
            return False
        return bool(self.config.claude_command)

    def _fallback_to_codex_decision(
        self,
        task: TaskRecord,
        decision: RoutingDecision,
    ) -> RoutingDecision:
        fallback = self.model_router.route(
            task,
            manual_tier=decision.manual_tier,
            executor_provider="codex",
        )
        self._record_routing(task, fallback, task.orchestrator_enabled)
        return fallback

    def _without_claude_roles_decision(
        self,
        task: TaskRecord,
        decision: RoutingDecision,
    ) -> RoutingDecision:
        classification = replace(
            decision.classification,
            requires_architect=False,
            requires_reviewer=False,
            requires_debate=False,
            recommended_claude_architect_tier="none",
            recommended_claude_reviewer_tier="none",
            max_review_rounds=0,
        )
        selected_models = dict(decision.selected_models)
        selected_models["architect"] = None
        selected_models["reviewer"] = None
        routed = replace(
            decision,
            classification=classification,
            selected_flow=render_selected_flow(classification, decision.executor_provider),
            selected_models=selected_models,
            warnings=[
                *decision.warnings,
                "Claude architect/reviewer unavailable; executor-only flow selected.",
            ],
        )
        self._record_routing(task, routed, task.orchestrator_enabled)
        return routed

    def _run_architect(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        decision: RoutingDecision,
    ) -> ArchitectPlan:
        context = read_task_context(self.config.index_dir, task, project)
        context_pack = self.memory_engine.build_context_pack(task).render()
        prompt = architect_prompt(task, context, context_pack)
        redacted = self.redactor.redact_text(prompt, max_chars=50000)
        response = self.claude_provider.complete(
            LlmRequest(
                role="architect",
                prompt=redacted.text,
                model=decision.selected_models.get("architect"),
                max_tokens=self.config.claude_max_tokens,
            )
        )
        return ArchitectPlan.from_text(response.text, fallback_prompt=task.prompt)

    def _run_reviewer(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        decision: RoutingDecision,
        architect_plan: ArchitectPlan | None,
    ) -> ReviewResult:
        diff = read_git_diff(project.path)
        context_pack = self.memory_engine.build_context_pack(task).render()
        prompt = reviewer_prompt(
            task,
            architect_plan,
            read_final(task),
            diff,
            context_pack,
        )
        redacted = self.redactor.redact_text(prompt, max_chars=60000)
        response = self.claude_provider.complete(
            LlmRequest(
                role="reviewer",
                prompt=redacted.text,
                model=decision.selected_models.get("reviewer"),
                max_tokens=self.config.claude_max_tokens,
            )
        )
        return ReviewResult.from_text(response.text)

    def _handle_provider_failure(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        decision: RoutingDecision,
        exc: Exception,
        recover: bool,
    ) -> TaskRecord:
        if self.config.orchestrator_strict_mode or not self.config.orchestrator_fallback_to_codex:
            task.phase = "failed"
            task.error = f"Claude orchestrator failed: {exc}"
            self.store.save_task(task)
            self._save_trace(task, decision, final_status="failed")
            return task
        fallback = self._fallback_to_codex_decision(task, decision)
        task = self._run_executor(task, project, fallback, recover=recover)
        self._save_trace(
            task,
            fallback,
            codex_summary=read_final(task),
            final_status="done" if task.phase == "completed" else "failed",
        )
        return task

    def _save_trace(
        self,
        task: TaskRecord,
        decision: RoutingDecision,
        architect_plan: ArchitectPlan | None = None,
        codex_summary: str = "",
        review: ReviewResult | None = None,
        final_status: str = "done",
    ) -> None:
        payload = {
            "task_id": task.id,
            "created_at": now_iso(),
            "orchestrator_enabled": task.orchestrator_enabled,
            "complexity": decision.classification.complexity,
            "selected_flow": decision.selected_flow,
            "selected_models": decision.selected_models,
            "routing_reason": decision.routing_reason,
            "estimated_tokens": decision.estimated_tokens,
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "estimated_cost": None,
            "actual_cost": None,
            "escalations": decision.escalations,
            "downgrades": decision.downgrades,
            "architect_model": decision.selected_models.get("architect"),
            "reviewer_model": decision.selected_models.get("reviewer"),
            "codex_model": decision.selected_models.get("executor"),
            "executor_provider": decision.executor_provider,
            "architect_plan": asdict(architect_plan) if architect_plan else None,
            "codex_summary": codex_summary[:4000],
            "review_verdict": review.verdict if review else None,
            "review": asdict(review) if review else None,
            "review_rounds": task.orchestrator_review_rounds,
            "final_status": final_status,
        }
        path = self.store.task_dir(task.id) / "trace.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _append_event(
        self,
        append_event: Callable[..., None] | None,
        task_id: str,
        agent: str,
        message: str,
        data: dict[str, Any] | None,
    ) -> None:
        if append_event is not None:
            append_event(task_id, "agent_message", agent, message, data)


def architect_prompt(task: TaskRecord, context: str, context_pack: str) -> str:
    return f"""You are Claude Architect for a Telegram-controlled Codex task.

Return only a JSON object with this shape:
{{
  "summary": "short task understanding",
  "risk_level": "low | medium | high",
  "task_type": "feature | bugfix | refactor | investigation | review",
  "implementation_plan": ["step"],
  "files_to_inspect": [],
  "files_likely_to_change": [],
  "acceptance_criteria": [],
  "test_plan": [],
  "questions": [],
  "optional_questions": [],
  "codex_prompt": "final prompt for Codex"
}}

Use questions only for blocking uncertainty. Put non-blocking questions into optional_questions.
Do not include secrets. Do not request broad rewrites unless required.

Project:
- name: {task.project_name}
- path: {task.project_path}
- slug: {task.project_slug}

Context pack:
{context_pack}

Project agent context:
{context}

Original task:
{task.prompt}
"""


def reviewer_prompt(
    task: TaskRecord,
    architect_plan: ArchitectPlan | None,
    codex_summary: str,
    diff: str,
    context_pack: str,
) -> str:
    plan = render_architect_plan(architect_plan) if architect_plan else "-"
    return f"""You are Claude Reviewer for a Telegram-controlled Codex task.

Return only JSON:
{{
  "verdict": "approve | request_changes | needs_human",
  "review_summary": "short review summary",
  "issues": [
    {{
      "severity": "critical | major | minor | nit",
      "file": "path/to/file",
      "comment": "what is wrong",
      "suggested_fix": "how to fix"
    }}
  ],
  "missing_tests": [],
  "regression_risks": [],
  "follow_up_prompt_for_codex": "prompt for fixes if needed"
}}

Be strict about correctness, security, tests, and backward compatibility.

Original task:
{task.prompt}

Architect plan:
{plan}

Context pack:
{context_pack}

Codex final summary:
{codex_summary}

Diff:
{diff}
"""


def render_architect_plan(plan: ArchitectPlan | None) -> str:
    if plan is None:
        return ""
    lines = [
        f"Summary: {plan.summary or '-'}",
        f"Risk: {plan.risk_level}",
        f"Type: {plan.task_type}",
        "",
        "Implementation plan:",
    ]
    lines.extend(f"- {item}" for item in plan.implementation_plan or ["-"])
    lines.append("")
    lines.append("Acceptance criteria:")
    lines.extend(f"- {item}" for item in plan.acceptance_criteria or ["-"])
    lines.append("")
    lines.append("Test plan:")
    lines.extend(f"- {item}" for item in plan.test_plan or ["-"])
    if plan.optional_questions:
        lines.append("")
        lines.append("Optional questions:")
        lines.extend(f"- {item}" for item in plan.optional_questions)
    return "\n".join(lines)


def read_final(task: TaskRecord) -> str:
    if not task.final_path:
        return ""
    path = Path(task.final_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def read_git_diff(project_path: str, max_chars: int = 50000) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", project_path, "diff", "--stat"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        stat = completed.stdout.strip()
        completed = subprocess.run(
            ["git", "-C", project_path, "diff", "--", "."],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        diff = completed.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = f"Diff stat:\n{stat or '-'}\n\nDiff:\n{diff or '-'}"
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "\n..."


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
