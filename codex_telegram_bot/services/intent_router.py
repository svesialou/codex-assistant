from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentContext:
    last_task_id: str = ""
    last_planned_task_id: str = ""
    last_completed_task_id: str = ""
    last_task_with_session_id: str = ""


@dataclass(frozen=True)
class IntentResult:
    intent: str
    confidence: float
    project_query: str | None = None
    task_text: str | None = None
    task_id: str | None = None
    memory_text: str | None = None
    alias: str | None = None
    requires_confirmation: bool = False


SELECT_PROJECT_PATTERNS = [
    re.compile(
        r"^\s*(?:давай(?:те)?\s+)?работа(?:ем|ть)\s+(?:с|над|в|по)\s+(?:проектом\s+|проект\s+)?(?P<project>.+?)\s*[.!?]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:переключись|выбери|открой)\s+(?:на\s+)?(?:проект\s+)?(?P<project>.+?)\s*[.!?]?\s*$",
        re.IGNORECASE,
    ),
]
ALIAS_RE = re.compile(
    r"^\s*(?:называй|зови)\s+(?P<project>.+?)\s+(?:просто\s+|как\s+)?(?P<alias>[A-Za-zА-Яа-я0-9._-]+)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
REMEMBER_PATTERNS = [
    re.compile(r"^\s*запомни\s*:?\s*(?P<text>.+)$", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"^\s*сохрани\s+(?:это\s+)?в\s+память\s+проекта\s*:?\s*(?P<text>.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
]
QUESTION_RE = re.compile(r"^\s*(что|как|почему|зачем|где|когда|можешь|можно|сколько)\b", re.IGNORECASE)
CONTINUE_SEARCH_RE = re.compile(
    r"^\s*(?P<verb>продолжи|continue|возобнови|доделай|найди|покажи)\s+"
    r"(?P<query>.+)$",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def clean_project_query(value: str) -> str:
    query = value.strip(" `\"'.!?")
    if re.fullmatch(r"[А-Яа-яёЁ]{4,}", query) and query.lower().endswith("ом"):
        return query[:-2]
    return query


class IntentRouter:
    def route(self, text: str, context: IntentContext | None = None) -> IntentResult:
        context = context or IntentContext()
        stripped = text.strip()
        normalized = normalize_text(stripped).lower()
        if not normalized:
            return IntentResult("unknown", 0.0)

        alias_match = ALIAS_RE.match(stripped)
        if alias_match:
            return IntentResult(
                "add_alias",
                0.94,
                project_query=alias_match.group("project").strip(),
                alias=alias_match.group("alias").strip(),
                requires_confirmation=True,
            )

        for pattern in REMEMBER_PATTERNS:
            match = pattern.match(stripped)
            if match:
                memory_text = match.group("text").strip()
                return IntentResult(
                    "remember",
                    0.96,
                    memory_text=memory_text,
                    requires_confirmation=True,
                )

        for pattern in SELECT_PROJECT_PATTERNS:
            match = pattern.match(stripped)
            if match:
                project_query = clean_project_query(match.group("project"))
                return IntentResult("select_project", 0.92, project_query=project_query)

        if any(phrase in normalized for phrase in ["покажи мозги", "мозги команды", "думает команда", "обсуждение команды"]):
            return IntentResult("show_brain", 0.95, task_id=context.last_task_id or None)
        if normalized in {"мозги", "brain"}:
            return IntentResult("show_brain", 0.9, task_id=context.last_task_id or None)

        if "покажи память" in normalized or "память проекта" in normalized or "что ты знаешь про этот проект" in normalized:
            return IntentResult("show_memory", 0.92)
        if normalized in {"память", "memory"}:
            return IntentResult("show_memory", 0.9)

        if "покажи логи" in normalized or normalized in {"логи", "logs", "лог"}:
            return IntentResult("show_logs", 0.92, task_id=context.last_task_id or None)
        if normalized in {"статус", "status", "что там", "как дела"} or "что сейчас выполняется" in normalized:
            return IntentResult("show_status", 0.9, task_id=context.last_task_id or None)
        if normalized in {"план", "покажи план"} or normalized.startswith("покажи план"):
            return IntentResult("show_plan", 0.9, task_id=context.last_planned_task_id or context.last_task_id or None)
        if normalized in {"safety", "безопасность", "покажи изменения", "изменения", "diff", "diff stat"}:
            return IntentResult("show_safety", 0.88, task_id=context.last_task_id or None)
        if normalized in {"review", "ревью", "проверь результат", "проверь"}:
            return IntentResult("show_review", 0.88, task_id=context.last_completed_task_id or context.last_task_id or None)

        if normalized in {"продолжи", "продолжить", "continue", "давай дальше"}:
            return IntentResult(
                "continue_task",
                0.92,
                task_id=context.last_task_with_session_id or context.last_completed_task_id or None,
            )
        continue_match = CONTINUE_SEARCH_RE.match(stripped)
        if continue_match:
            verb = continue_match.group("verb").lower()
            query = continue_match.group("query").strip()
            if verb in {"найди", "покажи"}:
                return IntentResult("search_tasks", 0.78, task_text=query)
            return IntentResult("continue_task", 0.78, task_text=query)
        if normalized in {"запускай", "делай", "выполняй", "выполнить", "запусти", "go"}:
            return IntentResult(
                "confirm_execution",
                0.92,
                task_id=context.last_planned_task_id or context.last_task_id or None,
            )
        if normalized in {"отмени", "отмена", "cancel", "останови", "стоп"}:
            return IntentResult("cancel_task", 0.92, task_id=context.last_task_id or None)

        if normalized in {"проекты", "список проектов", "list projects"}:
            return IntentResult("list_projects", 0.9)
        if normalized in {"задачи", "список задач", "tasks"}:
            return IntentResult("list_tasks", 0.9)

        if stripped.endswith("?") or QUESTION_RE.match(stripped):
            return IntentResult("agent_chat", 0.72, task_text=stripped)

        if len(stripped) >= 8:
            return IntentResult("create_task", 0.68, task_text=stripped)

        return IntentResult("unknown", 0.2)
