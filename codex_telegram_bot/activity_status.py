from __future__ import annotations

import re
from pathlib import Path

from .services.executors import executor_display_name
from .stream_events import (
    SUBAGENT_COMPLETED,
    SUBAGENT_FAILED,
    SUBAGENT_RUNNING,
    SUBAGENT_UNFINISHED,
    SubagentState,
)
from .task_store import TaskRecord


LOG_TAIL_BYTES = 64 * 1024
MAX_ACTIVITY_LINES = 6
MAX_ACTIVITY_CHARS = 900
MAX_STATUS_CHARS = 1800
MAX_SUBAGENT_LINES = 10
MAX_SUBAGENT_LINE_CHARS = 140

SUBAGENT_ICONS = {
    SUBAGENT_RUNNING: "⏳",
    SUBAGENT_COMPLETED: "✅",
    SUBAGENT_FAILED: "❌",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASS|API_KEY|CHAT_ID|KEY)[A-Z0-9_]*)"
    r"\s*=\s*([^\s,;]+)"
)

PHASE_LABELS = {
    "created": "created",
    "planning": "planning",
    "planned": "planned",
    "running": "running",
    "completed": "completed",
    "agent_running": "agent running",
    "agent_completed": "agent completed",
    "failed": "failed",
    "canceled": "canceled",
}

DEFAULT_ACTIVITY = {
    "planning": "Планирует задачу в read-only режиме.",
    "running": "Выполняет подтвержденную задачу.",
    "agent_running": "Готовит ответ в read-only agent mode.",
}

FINAL_ACTIVITY = {
    "planned": "План готов. Следующим сообщением отправляю результат.",
    "completed": "Выполнение завершено. Следующим сообщением отправляю финальный ответ.",
    "agent_completed": "Ответ агента готов. Следующим сообщением отправляю результат.",
    "failed": "Запуск завершился ошибкой. Детали будут в следующем сообщении.",
    "canceled": "Задача отменена.",
}

SKIP_LINE_PREFIXES = (
    "openai codex ",
    "--------",
    "workdir:",
    "model:",
    "provider:",
    "approval:",
    "sandbox:",
    "reasoning effort:",
    "reasoning summaries:",
    "session id:",
    "user",
    "hook:",
    "tokens used",
    "warning: codex's linux sandbox",
)

CODE_OR_DIFF_PREFIXES = (
    "```",
    "diff --git ",
    "index ",
    "@@",
    "+++ ",
    "--- ",
    "*** begin patch",
    "*** end patch",
)


def extract_codex_activity(log_path: Path) -> str | None:
    text = read_log_tail(log_path)
    if not text:
        return None

    blocks = codex_message_blocks(text.splitlines())
    if blocks:
        return blocks[-1]

    return infer_activity_from_tail(text)


def render_codex_status(
    task: TaskRecord,
    phase: str,
    activity: str | None = None,
    subagents: list[SubagentState] | None = None,
) -> str:
    activity_text = activity or FINAL_ACTIVITY.get(phase) or DEFAULT_ACTIVITY.get(
        phase,
        "Agent работает над задачей.",
    )
    activity_text = truncate_activity(activity_text)
    phase_label = PHASE_LABELS.get(phase, phase)
    title = "Статус Agent"
    if task.executor_provider:
        title += f" · {executor_display_name(task.executor_provider)}"
    lines = [
        title,
        f"Задача: {task.id}",
        f"Проект: {task.project_slug}",
        f"Этап: {phase_label}",
        "",
        "Сейчас:",
        activity_text,
        "",
        *render_subagent_lines(subagents or []),
        "Сообщение обновляется во время работы.",
    ]
    text = "\n".join(lines)
    if len(text) <= MAX_STATUS_CHARS:
        return text

    over = len(text) - MAX_STATUS_CHARS
    shortened = truncate_text(activity_text, max(120, len(activity_text) - over - 3))
    lines[6] = shortened
    return "\n".join(lines)


def render_subagent_lines(subagents: list[SubagentState]) -> list[str]:
    if not subagents:
        return []
    done = sum(
        1 for agent in subagents if agent.status in (SUBAGENT_COMPLETED, SUBAGENT_FAILED)
    )
    lines = [f"Агенты ({done}/{len(subagents)}):"]
    for agent in subagents[-MAX_SUBAGENT_LINES:]:
        icon = SUBAGENT_ICONS.get(agent.status, "⚠️")
        name = agent.agent_type or "agent"
        line = f"{icon} {name}"
        if agent.description:
            line += f" — {agent.description}"
        if agent.status == SUBAGENT_RUNNING and agent.activity:
            line += f" · {agent.activity}"
        elif agent.status == SUBAGENT_UNFINISHED:
            line += " · не завершён"
        lines.append(truncate_text(sanitize_activity_line(line) or line, MAX_SUBAGENT_LINE_CHARS))
    hidden = len(subagents) - MAX_SUBAGENT_LINES
    if hidden > 0:
        lines.insert(1, f"… и ещё {hidden}")
    lines.append("")
    return lines


def read_log_tail(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def codex_message_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    capturing = False
    block: list[str] = []

    def flush() -> None:
        nonlocal block
        text = "\n".join(block).strip()
        block = []
        if text:
            blocks.append(truncate_activity(text))

    for raw_line in lines:
        if raw_line.strip() == "codex":
            if capturing:
                flush()
            capturing = True
            continue

        if not capturing:
            continue

        if raw_line.startswith("hook:") or raw_line.strip() == "tokens used":
            flush()
            capturing = False
            continue

        if len(block) >= MAX_ACTIVITY_LINES:
            continue

        line = sanitize_activity_line(raw_line)
        if line:
            block.append(line)

    if capturing:
        flush()

    return blocks


def sanitize_activity_line(line: str) -> str | None:
    clean = ANSI_RE.sub("", line).strip()
    if not clean:
        return None

    lower = clean.lower()
    if clean == "codex" or lower in {"user", "assistant"}:
        return None
    if any(lower.startswith(prefix) for prefix in SKIP_LINE_PREFIXES):
        return None
    if any(lower.startswith(prefix) for prefix in CODE_OR_DIFF_PREFIXES):
        return None

    return truncate_text(mask_secrets(clean), 320)


def mask_secrets(text: str) -> str:
    text = TELEGRAM_TOKEN_RE.sub("<hidden-token>", text)
    return SECRET_ASSIGNMENT_RE.sub(r"\1=<hidden>", text)


def infer_activity_from_tail(text: str) -> str | None:
    lower = text.lower()
    if "diff --git " in lower or "*** begin patch" in lower:
        return "Обновляет файлы и проверяет diff."
    if "traceback " in lower or "failed" in lower or "error" in lower:
        return "Анализирует ошибку или падение проверки."
    if "ok  " in lower or " pass" in lower or "tests" in lower:
        return "Запускает проверки и анализирует результат."
    return None


def truncate_activity(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    text = "\n".join(lines[:MAX_ACTIVITY_LINES])
    return truncate_text(text, MAX_ACTIVITY_CHARS)


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
