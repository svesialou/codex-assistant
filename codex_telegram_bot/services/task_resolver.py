from __future__ import annotations

import re
from dataclasses import dataclass

from ..task_store import TaskRecord, TaskStore


CONTINUE_QUERY_RE = re.compile(
    r"^\s*(?:продолжи|continue|возобнови|доделай)\s+(?:задач[уы]\s+)?(?:про\s+)?(?P<query>.+)$",
    re.IGNORECASE,
)
SEARCH_QUERY_RE = re.compile(
    r"^\s*(?:найди|покажи|что\s+было\s+с|где\s+мы\s+делали)\s+(?P<query>.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskResolveResult:
    action: str
    task: TaskRecord | None
    matches: list[TaskRecord]
    confidence: float
    query: str


class TaskResolver:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def resolve_message(
        self,
        chat_id: int,
        text: str,
        project_slug: str | None = None,
    ) -> TaskResolveResult:
        continue_match = CONTINUE_QUERY_RE.match(text)
        search_match = SEARCH_QUERY_RE.match(text)
        if not continue_match and not search_match:
            return TaskResolveResult("none", None, [], 0.0, "")

        query = (
            continue_match.group("query")
            if continue_match is not None
            else search_match.group("query")
        ).strip()
        matches = self.store.search_tasks(
            query,
            chat_id=chat_id,
            project_slug=project_slug,
            limit=5,
        )
        action = "resume_existing" if continue_match is not None else "show_matches"
        if len(matches) == 1:
            return TaskResolveResult(action, matches[0], matches, 0.82, query)
        if matches:
            return TaskResolveResult("ask_clarification", None, matches, 0.62, query)
        return TaskResolveResult("create_new", None, [], 0.25, query)
