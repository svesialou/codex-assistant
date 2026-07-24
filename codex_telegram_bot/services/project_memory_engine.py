from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..task_store import TaskRecord, TaskStore
from .git_safety import GitSnapshot, snapshot_summary
from .project_memory import ProjectMemoryStore
from .redaction import RedactionService


@dataclass(frozen=True)
class ContextPack:
    project_profile: str = ""
    relevant_project_memories: list[str] = field(default_factory=list)
    similar_past_tasks: list[str] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)
    active_task_context: str = ""
    recent_related_decisions: list[str] = field(default_factory=list)

    def render(self) -> str:
        sections = ["# Context Pack"]
        if self.project_profile:
            sections.extend(["", "## Project Profile", self.project_profile])
        if self.relevant_project_memories:
            sections.extend(["", "## Relevant Project Memories", *self.relevant_project_memories])
        if self.similar_past_tasks:
            sections.extend(["", "## Similar Past Tasks", *self.similar_past_tasks])
        if self.user_preferences:
            sections.extend(["", "## User Preferences", *self.user_preferences])
        if self.active_task_context:
            sections.extend(["", "## Active Task Context", self.active_task_context])
        if self.recent_related_decisions:
            sections.extend(["", "## Recent Decisions", *self.recent_related_decisions])
        return "\n".join(sections).strip()


class ProjectMemoryEngine:
    def __init__(
        self,
        config: Config,
        memory_store: ProjectMemoryStore,
        task_store: TaskStore,
        redactor: RedactionService | None = None,
    ) -> None:
        self.config = config
        self.memory_store = memory_store
        self.task_store = task_store
        self.redactor = redactor or RedactionService()

    def memory_enabled_for_task(self, task: TaskRecord) -> bool:
        return self.config.memory_enabled and task.project_slug != "root"

    def build_context_pack(self, task: TaskRecord) -> ContextPack:
        memories = self.search_project_memory(
            task.project_slug,
            task.prompt,
            limit=self.config.memory_max_items_per_project_context,
        )
        similar = [
            f"- {item.id}: {item.prompt[:180]} ({item.phase}, {item.updated_at})"
            for item in self.task_store.search_tasks(
                task.prompt,
                chat_id=task.chat_id,
                project_slug=task.project_slug,
                limit=self.config.memory_max_similar_tasks,
            )
            if item.id != task.id
        ]
        preferences = [
            "- prefers small safe diffs",
            "- prefers cost-optimized model routing",
            "- prefers short Telegram updates",
        ]
        return ContextPack(
            project_profile=f"{task.project_slug} at {task.project_path}",
            relevant_project_memories=memories,
            similar_past_tasks=similar,
            user_preferences=preferences,
            active_task_context=f"{task.id}: {task.prompt[:500]}",
        )

    def search_project_memory(
        self,
        project_slug: str,
        query: str,
        limit: int = 12,
    ) -> list[str]:
        text = self.memory_store.read_project_memory(project_slug, max_chars=12000)
        if not text:
            return []
        tokens = [token.lower() for token in query.split() if len(token) >= 3]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not tokens:
            return lines[-limit:]
        scored: list[tuple[int, str]] = []
        for line in lines:
            score = sum(1 for token in tokens if token in line.lower())
            if score:
                scored.append((score, line))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [line for _, line in scored[:limit]] or lines[-limit:]

    def remember_completed_task(
        self,
        task: TaskRecord,
        final_text: str,
        snapshot: GitSnapshot | None = None,
    ) -> bool:
        if not self.config.memory_auto_extract_after_task:
            return False
        if not self.memory_enabled_for_task(task):
            return False
        if task.phase != "completed":
            return False

        commands_run = extract_verified_line(final_text)
        parts = [
            f"Task {task.id}: {task.prompt[:300]}",
            f"Solution: {compact(final_text, 700)}",
        ]
        if snapshot is not None:
            parts.append(f"Diff: {snapshot_summary(snapshot)}")
        if commands_run:
            parts.append(f"Verified: {commands_run}")
        draft = "\n".join(parts)
        sanitized = self.redactor.redact_text(draft, max_chars=1600)
        if not sanitized.text:
            return False
        self.memory_store.append_project_memory(
            task.project_slug,
            sanitized.text,
            user_id=task.user_id,
        )
        return True

    def forget_project_memory(self, project_slug: str, item_id: str) -> bool:
        path = self.memory_store.project_memory_path(project_slug)
        if not path.exists():
            return False
        try:
            index = int(item_id.lstrip("#")) - 1
        except ValueError:
            return False
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        items = [line for line in lines if line.strip()]
        if index < 0 or index >= len(items):
            return False
        del items[index]
        path.write_text(("\n".join(items).rstrip() + "\n") if items else "", encoding="utf-8")
        return True


def compact(text: str, max_chars: int) -> str:
    value = " ".join(text.strip().split())
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."


def extract_verified_line(final_text: str) -> str:
    for line in final_text.splitlines():
        if line.strip().lower().startswith(("- verified:", "verified:", "- verified")):
            return line.strip()[:400]
    return ""
