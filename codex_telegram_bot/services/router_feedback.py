"""Turn user and critic feedback on a finished run into router learning.

A run counted as a success that the user then has to correct was not a
success. Feedback is applied at most once per run: the first follow-up or
critic verdict wins and is stored on the run's task record.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..task_store import TaskRecord, TaskStore, now_iso
from .llm_provider import LlmProviderUnavailable
from .llm_router import FollowupClassifier
from .router_policy import RouterPolicy

LOG = logging.getLogger("codex_telegram_bot")
# Haiku may misread an extension as a correction; only confident ones count.
MIN_CORRECTION_CONFIDENCE = 0.7


@dataclass(frozen=True)
class FeedbackResult:
    label: str
    applied: bool
    reason: str = ""


class RouterFeedback:
    def __init__(
        self,
        store: TaskStore,
        policy: RouterPolicy,
        classifier: FollowupClassifier | None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.classifier = classifier

    def on_followup(self, followup: TaskRecord, parent: TaskRecord, parent_final: str) -> FeedbackResult | None:
        """Classify a continuation and charge a correction to the parent run.

        On a correction the follow-up gets the parent tier as its floor, so
        the router does not retry the fix on a cheaper model.
        """
        if self.classifier is None or not learnable(parent):
            return None
        try:
            label = self.classifier.classify(parent.prompt, parent_final, followup.prompt)
        except (LlmProviderUnavailable, ValueError) as exc:
            LOG.warning("follow-up classification failed for task %s: %s", followup.id, exc)
            return None
        correction = label.label == "correction" and label.confidence >= MIN_CORRECTION_CONFIDENCE
        result = self._apply(
            parent,
            label=label.label,
            correction=correction,
            source="followup",
            source_task_id=followup.id,
            reason=label.reason,
            confidence=label.confidence,
        )
        if result is not None and result.applied:
            followup.model_routing = {
                **followup.model_routing,
                "floor_tier": parent.model_routing.get("selected_tier"),
            }
            self.store.save_task(followup)
        return result

    def on_critic(
        self,
        task: TaskRecord,
        verdict: str,
        effort_fit: str,
        reason: str,
    ) -> FeedbackResult | None:
        """Charge a manual critic verdict to the task's own run."""
        if not learnable(task):
            return None
        return self._apply(
            task,
            label=f"critic:{verdict}",
            correction=verdict == "request_changes",
            source="critic",
            source_task_id=task.id,
            reason=reason,
            overkill=effort_fit == "over",
        )

    def _apply(
        self,
        task: TaskRecord,
        label: str,
        correction: bool,
        source: str,
        source_task_id: str,
        reason: str,
        confidence: float | None = None,
        overkill: bool = False,
    ) -> FeedbackResult | None:
        # Reload: the record on disk may be newer than the caller's copy.
        current = self.store.load_task(task.id) or task
        if (current.model_routing.get("feedback") or {}).get("label"):
            return None
        routing = current.model_routing
        if correction or overkill:
            self.policy.record_feedback(
                str(routing.get("complexity") or ""),
                current.project_slug,
                str(routing.get("selected_tier") or ""),
                counted_success=routing.get("outcome") == "success",
                correction=correction,
                overkill=overkill,
            )
        feedback = {
            "label": label,
            "applied": correction,
            "source": source,
            "task_id": source_task_id,
            "reason": reason[:200],
            "at": now_iso(),
        }
        if confidence is not None:
            feedback["confidence"] = confidence
        current.model_routing = {**routing, "feedback": feedback}
        self.store.save_task(current)
        task.model_routing = current.model_routing
        return FeedbackResult(label=label, applied=correction, reason=reason)


def learnable(task: TaskRecord) -> bool:
    """Only runs the router chose and learned from, without feedback yet."""
    routing = task.model_routing or {}
    return (
        bool(routing.get("selected_tier"))
        and routing.get("manual_tier", "auto") == "auto"
        and routing.get("outcome") in {"success", "failure"}
        and not (routing.get("feedback") or {}).get("label")
    )
