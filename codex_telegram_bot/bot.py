from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from .activity_status import extract_codex_activity, render_codex_status
from .agent_roles import AGENT_ROLES
from .config import Config, normalize_task_provider
from .codex_runner import CodexRunner, extract_session_id
from .project_index import (
    ProjectInfo,
    ROOT_PROJECT_SLUG,
    build_index,
    load_index,
    resolve_project,
    search_projects,
)
from .slack import (
    SlackAPI,
    SlackDesktopNotificationStore,
    SlackDesktopNotificationWatcher,
    SlackNotification,
    SlackStateStore,
    SlackWatcher,
    render_slack_notification,
)
from .services.git_safety import (
    GitSnapshot,
    capture_git_snapshot,
    is_git_repo,
    read_git_snapshot,
    snapshot_summary,
    write_git_snapshot,
)
from .services.executors import executor_agent_name, executor_display_name
from .services.intent_router import IntentContext, IntentResult, IntentRouter
from .services.llm_provider import ClaudeProvider
from .services.model_router import ModelRouter, normalize_manual_tier, render_routing_decision
from .services.orchestrator import OrchestratorService
from .services.project_aliases import (
    ProjectAliasStore,
    ProjectResolution,
    render_aliases,
    render_resolution_error,
)
from .services.project_memory_engine import ProjectMemoryEngine
from .services.project_memory import ProjectMemoryStore
from .services.project_resolver import ProjectResolver
from .services.redaction import RedactionService
from .services.runtime_identity import render_runtime_identity, runtime_identity
from .services.runtime_sync import compare_runtime_files, render_runtime_drift
from .services.task_resolver import TaskResolver
from .services.task_events import TaskEventLog, render_task_events
from .task_store import (
    ChatState,
    TaskRecord,
    TaskStore,
    is_recorded_process_alive,
    is_process_alive,
    terminate_process_group,
)
from .telegram_api import TelegramAPI
from .voice import TranscriptionError, transcribe_audio


LOG = logging.getLogger("codex_telegram_bot")


HELP_TEXT = """Codex Telegram bot

Commands:
/menu - show button menu
/projects [query] - list indexed projects
/refresh - rebuild project index
/project <query> - set default project for this chat
/context <project> - show project agent context
/new - start a new task in the selected context
/agent on|off - toggle read-only AI agent chat mode
/orchestrator_on - enable Claude/Codex orchestration for this chat
/orchestrator_off - disable orchestration and use Codex-only flow
/orchestrator_status - show orchestration status
/settings orchestrator on|off|status - manage orchestration
/settings provider codex|claude|status - choose main task executor
/debug on|off - toggle debug trace details
/ask <text> - ask Codex in read-only agent mode
/task <project> <text> - create a Codex task in a project
/task <text> - create a task in the selected default project
/run [tier=auto|cheap|standard|strong|max] <project> <text> - execute directly
/run <text> - execute directly in the selected default project
/answer <task_id> <text> - add clarification and rerun planning
/confirm <task_id> - execute a planned task
/continue <task_id> <text> - continue a completed task in its executor session
/cancel <task_id> - cancel a task or stop a running Codex process
/status [task_id] - show global active processes or one task
/processes - show active Codex processes across projects
/logs <task_id> - show recent execution log lines
/brain <task_id> - show agent event log for a task
/events <task_id> - same as /brain
/alias list|add|remove - manage project aliases
/aliases - list project aliases
/remember <text> - propose a project memory note
/memory [status|search|forget|summarize|export] - manage project memory
/runtime - show source/runtime identity

Flow:
1. Send a task as plain text, or use /run for custom direct execution.
2. The bot resolves project context, creates a task, and picks a model route.
3. If orchestration is enabled, Claude may plan/review and Codex implements.
4. Logs, diff, trace, memory, and continuation actions are available from the task card.
"""

BOT_COMMANDS = [
    {"command": "menu", "description": "Show main menu"},
    {"command": "new", "description": "Start a planned Codex task"},
    {"command": "run", "description": "Run a task directly"},
    {"command": "task", "description": "Create a planned Codex task"},
    {"command": "settings", "description": "Open settings"},
    {"command": "orchestrator_on", "description": "Enable orchestration"},
    {"command": "orchestrator_off", "description": "Disable orchestration"},
    {"command": "orchestrator_status", "description": "Show orchestrator status"},
    {"command": "status", "description": "Show processes or task status"},
    {"command": "processes", "description": "Show active Codex processes"},
    {"command": "projects", "description": "List indexed projects"},
    {"command": "project", "description": "Select default project"},
    {"command": "alias", "description": "Manage project aliases"},
    {"command": "memory", "description": "Show project memory"},
    {"command": "remember", "description": "Save project memory note"},
    {"command": "debug", "description": "Toggle debug details"},
    {"command": "help", "description": "Show help"},
]

PROJECT_PAGE_SIZE = 8
PROJECT_CANDIDATE_MIN_CONFIDENCE = 0.3
PROJECT_CHOICE_TTL_SECONDS = 900.0
TASK_FILTERS: dict[str, set[str] | None] = {
    "active": {"created", "planning", "planned", "running", "agent_running", "needs_human"},
    "planned": {"planned"},
    "running": {"planning", "running", "agent_running"},
    "done": {"completed", "agent_completed"},
    "failed": {"failed"},
    "all": None,
}
PROCESS_PHASES = {"planning", "running", "agent_running"}
BLOCKING_FOLLOWUP_PHASES = {"created", "planning", "planned", "running", "agent_running"}
ATTACHABLE_PHASES = {"created", "planned", "failed"}
ANSWERABLE_PHASES = ATTACHABLE_PHASES | PROCESS_PHASES
MAX_ATTACHMENT_SIZE_BYTES = 20 * 1024 * 1024
PROMPT_DRAFT_SEPARATOR = "\n\n"
DEFAULT_CONTINUATION_PROMPT = "Continue development using the attached context."
STATUS_POLL_SECONDS = 1.0
STATUS_EDIT_MIN_SECONDS = 4.0


def configure_logging(state_dir: Path) -> None:
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
        ],
    )


def inline_task_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "▶️ Выполнить", "callback_data": f"task:execute:{task_id}"},
                {"text": "✏️ Уточнить", "callback_data": f"task:answer:{task_id}"},
            ],
            [
                {"text": "🧠 Мозги", "callback_data": f"task:brain:{task_id}"},
                {"text": "🛡 Safety", "callback_data": f"task:safety:{task_id}"},
            ],
            [
                {"text": "📎 Файлы", "callback_data": f"task:files:{task_id}"},
                {"text": "📜 Логи", "callback_data": f"task:logs:{task_id}"},
            ],
            [
                {"text": "🧾 Trace", "callback_data": f"task:trace:{task_id}"},
                {"text": "🧠 Почему режим?", "callback_data": f"task:routing:{task_id}"},
            ],
            [
                {"text": "❌ Отменить", "callback_data": f"task:cancel:{task_id}"},
                {"text": "📊 Статус", "callback_data": f"task:status:{task_id}"},
            ],
        ]
    }


def completed_task_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 Review", "callback_data": f"task:review:{task_id}"},
                {"text": "📊 Изменения", "callback_data": f"task:safety:{task_id}"},
            ],
            [
                {"text": "📜 Логи", "callback_data": f"task:logs:{task_id}"},
                {"text": "🔁 Продолжить", "callback_data": f"task:continue:{task_id}"},
            ],
            [
                {"text": "🧾 Trace", "callback_data": f"task:trace:{task_id}"},
                {"text": "🧠 Routing", "callback_data": f"task:routing:{task_id}"},
            ],
            [
                {"text": "➕ Контекст", "callback_data": f"contctx:{task_id}"},
                {"text": "📎 Файлы", "callback_data": f"contattach:{task_id}"},
            ],
            [
                {"text": "💾 В память", "callback_data": f"task:remember:{task_id}"},
                {"text": "🧠 Мозги", "callback_data": f"task:brain:{task_id}"},
            ],
            [
                {"text": "🚀 Force push", "callback_data": f"task:force_push:{task_id}"},
            ],
            [{"text": "📊 Статус", "callback_data": f"task:status:{task_id}"}, {"text": "Menu", "callback_data": "menu"}],
        ]
    }


def running_task_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✏️ Уточнить", "callback_data": f"task:answer:{task_id}"},
                {"text": "📊 Статус", "callback_data": f"task:status:{task_id}"},
            ],
            [
                {"text": "🧠 Мозги", "callback_data": f"task:brain:{task_id}"},
                {"text": "📜 Логи", "callback_data": f"task:logs:{task_id}"},
            ],
            [
                {"text": "🧾 Trace", "callback_data": f"task:trace:{task_id}"},
                {"text": "🧠 Routing", "callback_data": f"task:routing:{task_id}"},
            ],
            [
                {"text": "❌ Остановить", "callback_data": f"task:cancel:{task_id}"},
            ],
        ]
    }


def created_task_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📋 План", "callback_data": f"task:plan:{task_id}"},
                {"text": "🧠 Мозги", "callback_data": f"task:brain:{task_id}"},
            ],
            [
                {"text": "🧠 Routing", "callback_data": f"task:routing:{task_id}"},
                {"text": "🧾 Trace", "callback_data": f"task:trace:{task_id}"},
            ],
            [
                {"text": "📎 Файлы", "callback_data": f"task:files:{task_id}"},
                {"text": "❌ Отменить", "callback_data": f"task:cancel:{task_id}"},
            ],
        ]
    }


def failed_task_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📜 Логи", "callback_data": f"task:logs:{task_id}"},
                {"text": "🧠 Мозги", "callback_data": f"task:brain:{task_id}"},
            ],
            [
                {"text": "🧾 Trace", "callback_data": f"task:trace:{task_id}"},
                {"text": "🧠 Routing", "callback_data": f"task:routing:{task_id}"},
            ],
            [
                {"text": "✏️ Уточнить", "callback_data": f"task:answer:{task_id}"},
                {"text": "❌ Закрыть", "callback_data": f"task:cancel:{task_id}"},
            ],
        ]
    }


def memory_confirm_keyboard(project_slug: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Сохранить", "callback_data": f"memory:save_pending:{project_slug}"},
                {"text": "✏️ Изменить", "callback_data": f"memory:edit_pending:{project_slug}"},
            ],
            [{"text": "❌ Не сохранять", "callback_data": f"memory:skip_pending:{project_slug}"}],
        ]
    }


def alias_confirm_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Добавить", "callback_data": "alias:save_pending"},
                {"text": "❌ Отмена", "callback_data": "alias:cancel_pending"},
            ]
        ]
    }


def followup_draft_keyboard(parent_task_id: str, task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Add context", "callback_data": f"contctx:{parent_task_id}"},
                {"text": "Add files", "callback_data": f"contattach:{parent_task_id}"},
            ],
            [
                {"text": "Run continuation", "callback_data": f"contrun:{parent_task_id}"},
                {"text": "Cancel draft", "callback_data": f"cancel:{task_id}"},
            ],
            [
                {"text": "Draft task", "callback_data": f"task:{task_id}"},
                {"text": "Parent task", "callback_data": f"task:{parent_task_id}"},
            ],
            [{"text": "Menu", "callback_data": "menu"}],
        ]
    }


def main_menu_keyboard(agent_mode: bool | None = None) -> dict[str, Any]:
    del agent_mode
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Задачи", "callback_data": "tasks:active"},
                {"text": "▶️ Run custom", "callback_data": "runnew"},
            ],
            [
                {"text": "⚙️ Настройки", "callback_data": "settings:open"},
                {"text": "❓ Помощь", "callback_data": "help"},
            ],
        ]
    }


def settings_keyboard(
    orchestrator_enabled: bool,
    debug_enabled: bool,
    memory_enabled: bool,
    task_provider: str,
) -> dict[str, Any]:
    orchestrator_label = "Orchestrator: ON" if orchestrator_enabled else "Orchestrator: OFF"
    provider_label = f"Executor: {task_provider.upper()}"
    debug_label = "Debug: ON" if debug_enabled else "Debug: OFF"
    memory_label = "Memory: ON" if memory_enabled else "Memory: OFF"
    return {
        "inline_keyboard": [
            [{"text": orchestrator_label, "callback_data": "settings:orchestrator_toggle"}],
            [{"text": provider_label, "callback_data": "settings:task_provider_toggle"}],
            [{"text": memory_label, "callback_data": "settings:memory_toggle"}],
            [{"text": debug_label, "callback_data": "settings:debug_toggle"}],
            [
                {"text": "📁 Projects", "callback_data": "projects:0"},
                {"text": "🔄 Refresh", "callback_data": "refresh"},
            ],
            [
                {"text": "Aliases", "callback_data": "alias:list"},
                {"text": "Root", "callback_data": "root"},
            ],
            [{"text": "Menu", "callback_data": "menu"}],
        ]
    }


def project_keyboard(projects: list[ProjectInfo], page: int) -> dict[str, Any]:
    start = page * PROJECT_PAGE_SIZE
    page_projects = projects[start : start + PROJECT_PAGE_SIZE]
    rows: list[list[dict[str, str]]] = []
    for offset, project in enumerate(page_projects):
        index = start + offset
        label = project.slug[:32]
        rows.append([{"text": label, "callback_data": f"proj:{index}"}])

    nav: list[dict[str, str]] = []
    if page > 0:
        nav.append({"text": "Prev", "callback_data": f"projects:{page - 1}"})
    if start + PROJECT_PAGE_SIZE < len(projects):
        nav.append({"text": "Next", "callback_data": f"projects:{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "Root", "callback_data": "root"}, {"text": "Menu", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def tasks_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Active", "callback_data": "tasks:active"},
                {"text": "Planned", "callback_data": "tasks:planned"},
                {"text": "Running", "callback_data": "tasks:running"},
            ],
            [
                {"text": "Done", "callback_data": "tasks:done"},
                {"text": "Failed", "callback_data": "tasks:failed"},
                {"text": "All", "callback_data": "tasks:all"},
            ],
            [{"text": "▶️ Run custom", "callback_data": "runnew"}, {"text": "Menu", "callback_data": "menu"}],
        ]
    }


def agent_response_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Create task from this", "callback_data": f"taskfrom:{task_id}"}],
            [{"text": "New task", "callback_data": "new"}, {"text": "Menu", "callback_data": "menu"}],
        ]
    }


def task_progress_keyboard(task_id: str, phase: str) -> dict[str, Any]:
    if phase == "planned":
        return inline_task_keyboard(task_id)
    if phase in {"planning", "running", "agent_running"}:
        return running_task_keyboard(task_id)
    if phase == "failed":
        return failed_task_keyboard(task_id)
    if phase == "completed":
        return completed_task_keyboard(task_id)
    return created_task_keyboard(task_id)


def command_parts(text: str) -> tuple[str, str]:
    head, _, tail = text.partition(" ")
    command = head.split("@", 1)[0].lower()
    return command, tail.strip()


def extract_tier_override(args: str) -> tuple[str, str]:
    parts = args.strip().split()
    if not parts:
        return "auto", ""
    first = parts[0]
    if first.startswith("tier="):
        tier = normalize_manual_tier(first.split("=", 1)[1]) or "auto"
        return tier, " ".join(parts[1:]).strip()
    return "auto", args.strip()


def normalize_context_project_slugs(
    primary_slug: str,
    slugs: list[str] | tuple[str, ...],
) -> list[str]:
    result: list[str] = []
    for slug in [primary_slug, *slugs]:
        if slug and slug not in result:
            result.append(slug)
    return result or [primary_slug]


def split_project_queries(query: str) -> list[str]:
    value = re.sub(r"\s+(?:и|and)\s+", ",", query, flags=re.IGNORECASE)
    return [
        item.strip(" \t\r\n`'\".?!")
        for item in re.split(r"[,;]+", value)
        if item.strip(" \t\r\n`'\".?!")
    ]


def pending_task_id(pending_action: str | None, action: str) -> str | None:
    if not pending_action:
        return None
    pending, _, task_id = pending_action.partition(":")
    if pending != action or not task_id:
        return None
    return task_id


def continue_task_action(task_id: str) -> str:
    return f"continue_task:{task_id}"


def followup_context_action(task_id: str) -> str:
    return f"followup_context:{task_id}"


def safe_attachment_name(name: str) -> str:
    clean = Path(name).name.strip() or "attachment"
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", clean).strip(" .")
    return clean[:120] or "attachment"


def tail_file(path: str, max_lines: int = 60) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return "Log file does not exist yet."
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]) or "Log is empty."


def read_task_final(task: TaskRecord) -> str:
    if not task.final_path:
        return ""
    path = Path(task.final_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def short_task(task: TaskRecord) -> str:
    suffix = f", rc={task.returncode}" if task.returncode is not None else ""
    return (
        f"{task.id}: {task.phase}{suffix}\n"
        f"Project: {task.project_slug}\n"
        f"Task: {task.prompt[:220]}"
    )


def render_task_routing(routing: dict[str, Any]) -> str:
    classification = routing.get("classification") or {}
    selected_models = routing.get("selected_models") or {}
    lines = [
        "ModelRouter:",
        f"- complexity: {routing.get('complexity') or classification.get('complexity') or '-'}",
        f"- selected flow: {routing.get('selected_flow') or '-'}",
        f"- reason: {routing.get('routing_reason') or '-'}",
        f"- executor tier: {routing.get('selected_tier') or classification.get('recommended_codex_tier') or '-'}",
    ]
    if selected_models:
        lines.append("- selected models:")
        for key in ["classifier", "architect", "executor", "reviewer"]:
            lines.append(f"  {key}: {selected_models.get(key) or 'none'}")
    lines.append(f"- executor provider: {routing.get('executor_provider') or '-'}")
    warnings = routing.get("warnings") or []
    for warning in warnings:
        lines.append(f"- warning: {warning}")
    return "\n".join(lines)


@dataclass(frozen=True)
class PromptDraft:
    chat_id: int
    user_id: int | None
    action: str
    project_slug: str
    context_project_slugs: tuple[str, ...]
    parts: tuple[str, ...]
    source: str
    source_path: str
    manual_tier: str = "auto"


@dataclass(frozen=True)
class LiveStatusHandle:
    chat_id: int
    message_id: int | None
    stop_event: threading.Event | None = None
    thread: threading.Thread | None = None


def process_summary(
    tasks: list[TaskRecord],
    pid_alive: Callable[[int | None], bool] = is_process_alive,
) -> str:
    active: list[TaskRecord] = []
    stale_count = 0
    for task in tasks:
        if is_recorded_process_alive(task, pid_alive):
            active.append(task)
        else:
            stale_count += 1

    if not active:
        if stale_count:
            return (
                "No active Codex processes across projects.\n"
                f"Stale active task records: {stale_count}."
            )
        return "No active Codex processes across projects."

    lines = [f"Active Codex processes across projects: {len(active)}"]
    for task in active:
        lines.extend(
            [
                "",
                f"- {task.id}: {task.phase}, pid={task.pid}",
                f"  Project: {task.project_slug}",
                f"  Updated: {task.updated_at}",
                f"  Task: {task.prompt[:180]}",
            ]
        )
    if stale_count:
        lines.extend(["", f"Stale active task records: {stale_count}."])
    return "\n".join(lines)


class CodexTelegramBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = TelegramAPI(config.bot_token)
        self.store = TaskStore(config.state_dir)
        self.runner = CodexRunner(config, self.store)
        self.aliases = ProjectAliasStore(config.index_dir)
        self.events = TaskEventLog(self.store)
        self.memory = ProjectMemoryStore(config.index_dir, config.state_dir)
        self.redactor = RedactionService()
        self.memory_engine = ProjectMemoryEngine(
            config,
            self.memory,
            self.store,
            redactor=self.redactor,
        )
        self.intent_router = IntentRouter()
        self.project_resolver = ProjectResolver(self.aliases)
        self.task_resolver = TaskResolver(self.store)
        self.model_router = ModelRouter(config)
        self.orchestrator = OrchestratorService(
            config=config,
            store=self.store,
            runner=self.runner,
            model_router=self.model_router,
            claude_provider=ClaudeProvider(config),
            memory_engine=self.memory_engine,
            redactor=self.redactor,
        )
        self.projects: list[ProjectInfo] = []
        self.offset: int | None = None
        self.stop_event = threading.Event()
        self.worker_threads: set[threading.Thread] = set()
        self._projects_lock = threading.RLock()
        self._draft_lock = threading.RLock()
        self._draft_timers: dict[int, threading.Timer] = {}
        self.slack_watcher: SlackWatcher | None = None
        self.slack_desktop_watcher: SlackDesktopNotificationWatcher | None = None
        if config.slack_enabled() and config.slack_token is not None:
            self.slack_watcher = SlackWatcher(
                api=SlackAPI(config.slack_token),
                state_store=SlackStateStore(config.state_dir),
                watch_dms=config.slack_watch_dms,
                channel_ids=config.slack_channel_ids,
                history_limit=config.slack_history_limit,
                poll_interval_seconds=config.slack_poll_interval_seconds,
            )
        if config.slack_desktop_enabled():
            self.slack_desktop_watcher = SlackDesktopNotificationWatcher(
                state_store=SlackDesktopNotificationStore(config.state_dir),
            )

    def load_or_build_index(self) -> None:
        with self._projects_lock:
            projects = load_index(self.config.index_dir)
            if not projects:
                projects = build_index(
                    self.config.project_roots,
                    self.config.index_dir,
                    self.config.workspace_root,
                )
            self.projects = projects
            LOG.info("loaded %d projects", len(projects))

    def refresh_index(self) -> int:
        with self._projects_lock:
            self.projects = build_index(
                self.config.project_roots,
                self.config.index_dir,
                self.config.workspace_root,
            )
            return len(self.projects)

    def configure_telegram_menu(self) -> None:
        try:
            self.api.set_my_commands(BOT_COMMANDS)
            self.api.set_chat_menu_button({"type": "commands"})
        except Exception:
            LOG.exception("failed to configure telegram command menu")

    def find_project(self, query: str) -> ProjectInfo | None:
        with self._projects_lock:
            result = self.aliases.resolve(self.projects, query)
            return result.project

    def resolve_project_query(self, query: str) -> ProjectResolution:
        with self._projects_lock:
            return self.aliases.resolve(self.projects, query)

    def project_matches(self, query: str, limit: int = 6) -> list[ProjectInfo]:
        normalized = query.strip().lower()
        if not normalized:
            return []
        with self._projects_lock:
            matches = [
                project
                for project in self.projects
                if normalized in project.slug.lower()
                or normalized in project.name.lower()
                or normalized in project.path.lower()
            ]
        return matches[:limit]

    def project_by_index(self, index: int) -> ProjectInfo | None:
        with self._projects_lock:
            if 0 <= index < len(self.projects):
                return self.projects[index]
        return None

    def current_projects(self, chat_id: int) -> list[ProjectInfo]:
        state = self.store.load_chat_state(chat_id)
        slugs = list(state.active_project_slugs)
        if not slugs and state.selected_project_slug:
            slugs = [state.selected_project_slug]
        if not slugs:
            slugs = [ROOT_PROJECT_SLUG]

        projects: list[ProjectInfo] = []
        for slug in slugs:
            project = self.find_project(slug)
            if project is not None and project.slug not in {item.slug for item in projects}:
                projects.append(project)

        if projects:
            return projects

        root = self.find_project(ROOT_PROJECT_SLUG)
        return [root] if root is not None else []

    def current_project(self, chat_id: int) -> ProjectInfo | None:
        projects = self.current_projects(chat_id)
        return projects[0] if projects else None

    def resolve_task_project(self, chat_id: int, text: str):
        with self._projects_lock:
            projects = list(self.projects)
        return self.project_resolver.resolve(
            text,
            projects,
            default_project=self.current_project(chat_id),
        )

    def send_project_resolution_prompt(self, chat_id: int, result) -> None:
        # Candidates are only meaningful when the resolver matched real signal in the
        # text. The low-confidence branches return an arbitrary slice of the index,
        # so fall back to the paginated picker there.
        if result.candidates and result.confidence >= PROJECT_CANDIDATE_MIN_CONFIDENCE:
            rows = [
                [{"text": item.slug[:32], "callback_data": f"project:select:{item.slug}"}]
                for item in result.candidates[:6]
            ]
            self.send(
                chat_id,
                "Нужно уточнить проект для задачи. Выбери репозиторий.",
                reply_markup={"inline_keyboard": rows},
            )
            return
        self.send(
            chat_id,
            "Не понял, в каком проекте будут изменения. Выберите проект.",
            reply_markup=project_keyboard(list(self.projects), 0),
        )

    def remember_pending_project_choice(
        self,
        chat_id: int,
        user_id: int | None,
        text: str,
        action: str,
        source: str,
        source_path: str,
        manual_tier: str,
    ) -> None:
        state = self.store.load_chat_state(chat_id)
        state.pending_project_choice_action = action
        state.pending_project_choice_user_id = user_id
        state.pending_project_choice_text = text
        state.pending_project_choice_source = source
        state.pending_project_choice_source_path = source_path
        state.pending_project_choice_manual_tier = manual_tier
        state.pending_project_choice_at = time.time()
        self.store.save_chat_state(state)

    def take_pending_project_choice(self, chat_id: int) -> ChatState | None:
        state = self.store.load_chat_state(chat_id)
        pending = replace(state)
        had_choice = bool(state.pending_project_choice_action and state.pending_project_choice_text)
        if had_choice:
            state.pending_project_choice_action = ""
            state.pending_project_choice_user_id = None
            state.pending_project_choice_text = ""
            state.pending_project_choice_source = "text"
            state.pending_project_choice_source_path = ""
            state.pending_project_choice_manual_tier = "auto"
            state.pending_project_choice_at = 0.0
            self.store.save_chat_state(state)
        if not had_choice:
            return None
        # A stale answer should not silently start a task the user asked for hours ago.
        age = time.time() - pending.pending_project_choice_at
        if pending.pending_project_choice_at <= 0 or age > PROJECT_CHOICE_TTL_SECONDS:
            return None
        return pending

    def resume_pending_project_choice(self, chat_id: int, project: ProjectInfo) -> bool:
        pending = self.take_pending_project_choice(chat_id)
        if pending is None:
            return False
        self.send(chat_id, f"Ок, проект для задачи: {project.slug}.")
        self.queue_prompt_draft(
            chat_id,
            pending.pending_project_choice_user_id,
            project,
            pending.pending_project_choice_text,
            action=pending.pending_project_choice_action,
            source=pending.pending_project_choice_source,
            source_path=pending.pending_project_choice_source_path,
            manual_tier=pending.pending_project_choice_manual_tier,
        )
        return True

    def queue_task_prompt_resolving_project(
        self,
        chat_id: int,
        user_id: int | None,
        text: str,
        action: str,
        source: str,
        source_path: str,
        manual_tier: str = "auto",
    ) -> bool:
        result = self.resolve_task_project(chat_id, text)
        if result.project is None:
            self.remember_pending_project_choice(
                chat_id,
                user_id,
                text,
                action=action,
                source=source,
                source_path=source_path,
                manual_tier=manual_tier,
            )
            self.send_project_resolution_prompt(chat_id, result)
            return False
        self.take_pending_project_choice(chat_id)
        self.queue_prompt_draft(
            chat_id,
            user_id,
            result.project,
            text,
            action=action,
            source=source,
            source_path=source_path,
            manual_tier=manual_tier,
        )
        return True

    def effective_orchestrator_mode(self, chat_id: int) -> bool:
        state = self.store.load_chat_state(chat_id)
        if state.orchestrator_mode is None:
            return self.config.orchestrator_default_mode
        return state.orchestrator_mode

    def effective_memory_mode(self, chat_id: int) -> bool:
        state = self.store.load_chat_state(chat_id)
        if state.memory_enabled is None:
            return self.config.memory_enabled
        return state.memory_enabled

    def effective_task_provider(self, chat_id: int) -> str:
        state = self.store.load_chat_state(chat_id)
        return normalize_task_provider(state.task_provider or self.config.task_provider)

    def provider_status_text(self, provider: str) -> str:
        reason = self.runner.provider_unavailable_reason(provider)
        return reason or "ready"

    def task_context_project_slugs(
        self,
        chat_id: int,
        project: ProjectInfo,
    ) -> list[str]:
        current_slugs = [item.slug for item in self.current_projects(chat_id)]
        if project.slug in current_slugs:
            return normalize_context_project_slugs(project.slug, current_slugs)
        return [project.slug]

    def current_context_label(self, chat_id: int) -> str:
        projects = self.current_projects(chat_id)
        if not projects:
            return "not selected"
        if len(projects) == 1:
            project = projects[0]
            return f"{project.slug} ({project.path})"
        return (
            f"{projects[0].slug} ({projects[0].path})\n"
            f"Active projects: {', '.join(project.slug for project in projects)}"
        )

    def authorize(self, chat_id: int, user_id: int | None) -> bool:
        if chat_id not in self.config.allowed_chat_ids:
            return False
        if self.config.allowed_user_ids and user_id not in self.config.allowed_user_ids:
            return False
        return True

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        try:
            return self.api.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception:
            LOG.exception("failed to send telegram message")
            return None

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        try:
            self.api.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup=reply_markup,
            )
            return True
        except RuntimeError as exc:
            if "message is not modified" in str(exc).lower():
                LOG.debug(
                    "telegram message is not modified chat=%s message=%s",
                    chat_id,
                    message_id,
                )
                return True
            LOG.exception("failed to edit telegram message")
            return False
        except Exception:
            LOG.exception("failed to edit telegram message")
            return False

    def spawn(self, target: Any, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.worker_threads.add(thread)
        thread.start()

    def cleanup_threads(self) -> None:
        self.worker_threads = {thread for thread in self.worker_threads if thread.is_alive()}

    def append_event(
        self,
        task_id: str,
        event_type: str,
        agent: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.events.append_event(task_id, event_type, agent, message, data)
        except Exception:
            LOG.exception("failed to append task event")

    def remember_task_context(self, task: TaskRecord, view: str = "") -> None:
        state = self.store.load_chat_state(task.chat_id)
        state.last_task_id = task.id
        if task.phase == "planned":
            state.last_planned_task_id = task.id
        if task.phase in {"completed", "agent_completed"}:
            state.last_completed_task_id = task.id
        if task.codex_session_id or task.claude_session_id:
            state.last_task_with_session_id = task.id
        if view:
            state.last_shown_view = view
        self.store.save_chat_state(state)

    def intent_context(self, chat_id: int) -> IntentContext:
        state = self.store.load_chat_state(chat_id)
        return IntentContext(
            last_task_id=state.last_task_id,
            last_planned_task_id=state.last_planned_task_id,
            last_completed_task_id=state.last_completed_task_id,
            last_task_with_session_id=state.last_task_with_session_id,
        )

    def recent_context_task(
        self,
        chat_id: int,
        task_id: str | None = None,
        phases: set[str] | None = None,
    ) -> TaskRecord | None:
        if task_id and task_id != "last":
            task = self.store.load_task(task_id)
            if (
                task is not None
                and task.chat_id == chat_id
                and (phases is None or task.phase in phases)
            ):
                return task
            return None

        state = self.store.load_chat_state(chat_id)
        candidate_ids = [
            state.last_task_id,
            state.last_planned_task_id,
            state.last_completed_task_id,
            state.last_task_with_session_id,
        ]
        for candidate_id in candidate_ids:
            if not candidate_id:
                continue
            task = self.store.load_task(candidate_id)
            if task is None or task.chat_id != chat_id:
                continue
            if phases is None or task.phase in phases:
                return task

        tasks = self.store.recent_tasks(chat_id, limit=1, phases=phases)
        return tasks[0] if tasks else None

    def set_last_view(self, chat_id: int, view: str) -> None:
        state = self.store.load_chat_state(chat_id)
        state.last_shown_view = view
        self.store.save_chat_state(state)

    def send_codex_status(
        self,
        task: TaskRecord,
        phase: str,
        activity: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        if reply_markup is None:
            reply_markup = task_progress_keyboard(task.id, phase)
        return self.send(
            task.chat_id,
            render_codex_status(task, phase, activity),
            reply_markup=reply_markup,
        )

    def start_codex_status(
        self,
        task: TaskRecord,
        phase: str,
        log_path: Path,
        message_id: int | None = None,
        activity: str | None = None,
    ) -> LiveStatusHandle:
        if message_id is None:
            message_id = self.send_codex_status(task, phase, activity)
        if message_id is None:
            return LiveStatusHandle(chat_id=task.chat_id, message_id=None)

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.watch_codex_status,
            args=(task, phase, log_path, message_id, stop_event),
            daemon=True,
        )
        self.worker_threads.add(thread)
        thread.start()
        return LiveStatusHandle(
            chat_id=task.chat_id,
            message_id=message_id,
            stop_event=stop_event,
            thread=thread,
        )

    def watch_codex_status(
        self,
        task: TaskRecord,
        phase: str,
        log_path: Path,
        message_id: int,
        stop_event: threading.Event,
    ) -> None:
        last_text = render_codex_status(task, phase)
        last_edit = time.monotonic()
        while not stop_event.wait(STATUS_POLL_SECONDS):
            activity = extract_codex_activity(log_path)
            if not activity:
                continue

            text = render_codex_status(task, phase, activity)
            if text == last_text:
                continue
            if time.monotonic() - last_edit < STATUS_EDIT_MIN_SECONDS:
                continue

            self.edit_message(
                task.chat_id,
                message_id,
                text,
                reply_markup=task_progress_keyboard(task.id, phase),
            )
            last_text = text
            last_edit = time.monotonic()

    def finish_codex_status(
        self,
        handle: LiveStatusHandle,
        task: TaskRecord,
        activity: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if handle.stop_event is not None:
            handle.stop_event.set()
        if handle.thread is not None:
            handle.thread.join(timeout=1)
        if handle.message_id is None:
            return
        self.edit_message(
            handle.chat_id,
            handle.message_id,
            render_codex_status(task, task.phase, activity),
            reply_markup=reply_markup,
        )

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, message: dict[str, Any]) -> None:
        text = message.get("text") or ""
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = int(chat.get("id"))
        user_id = int(user["id"]) if user.get("id") is not None else None

        if not self.authorize(chat_id, user_id):
            LOG.warning("unauthorized telegram access chat=%s user=%s", chat_id, user_id)
            return

        if "voice" in message:
            self.spawn(self.handle_voice_message, chat_id, user_id, message)
            return

        if "document" in message or "photo" in message:
            self.spawn(self.handle_file_message, chat_id, user_id, message)
            return

        if not text.startswith("/"):
            self.handle_plain_text(chat_id, user_id, text)
            return

        command, args = command_parts(text)
        try:
            self.dispatch_command(chat_id, user_id, command, args)
        except Exception as exc:
            LOG.exception("command failed: %s", command)
            self.send(chat_id, f"Command failed: {exc}")

    def handle_callback(self, callback: dict[str, Any]) -> None:
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        message_id = message.get("message_id")
        user = callback.get("from") or {}
        callback_id = callback.get("id") or ""
        chat_id = int(chat.get("id"))
        user_id = int(user["id"]) if user.get("id") is not None else None

        if not self.authorize(chat_id, user_id):
            LOG.warning("unauthorized callback chat=%s user=%s", chat_id, user_id)
            return

        try:
            self.api.answer_callback_query(callback_id, "Received")
        except Exception:
            LOG.exception("failed to answer callback")

        action, _, value = data.partition(":")
        if data == "menu":
            self.show_menu(chat_id)
        elif data == "settings:open":
            self.show_settings(chat_id)
        elif data == "help":
            self.send(chat_id, HELP_TEXT, reply_markup=main_menu_keyboard())
        elif data == "root":
            self.select_root(chat_id)
        elif data == "new":
            self.start_new_task_input(chat_id)
        elif data == "runnew":
            self.start_direct_task_input(chat_id)
        elif data == "refresh":
            self.spawn(self.refresh_index_command, chat_id)
        elif data == "processes":
            self.show_processes(chat_id)
        elif action == "projects":
            self.show_projects_page(
                chat_id,
                int(value or "0"),
                message_id if isinstance(message_id, int) else None,
            )
        elif action == "proj":
            project = self.project_by_index(int(value))
            if project is None:
                self.send(chat_id, "Project button is stale. Press Projects again.")
            else:
                self.select_project_by_record(chat_id, project)
        elif action == "project":
            subaction, _, slug = value.partition(":")
            if subaction == "select" and slug:
                self.select_project(chat_id, slug)
        elif action == "tasks":
            self.show_tasks(chat_id, value or "active")
        elif action == "task":
            subaction, _, task_id = value.partition(":")
            if subaction in {
                "brain",
                "plan",
                "status",
                "logs",
                "safety",
                "review",
                "trace",
                "routing",
                "execute",
                "continue",
                "cancel",
                "files",
                "answer",
                "remember",
                "force_push",
            }:
                self.handle_task_callback(chat_id, user_id, subaction, task_id or "last")
            else:
                self.show_task(chat_id, value)
        elif action == "logs":
            self.logs(chat_id, value)
        elif action == "memory":
            subaction, _, target = value.partition(":")
            if subaction == "show":
                self.show_memory(chat_id, None if target == "current" else target)
            elif subaction == "save_pending":
                self.save_pending_memory(chat_id, user_id)
            elif subaction == "edit_pending":
                self.edit_pending_memory(chat_id, target)
            elif subaction == "skip_pending":
                self.skip_pending_memory(chat_id)
        elif action == "alias":
            if value == "list":
                self.show_aliases(chat_id)
            elif value == "save_pending":
                self.save_pending_alias(chat_id)
            elif value == "cancel_pending":
                self.cancel_pending_alias(chat_id)
        elif action == "taskfrom":
            self.create_task_from_agent(chat_id, value)
        elif action == "agent":
            self.toggle_agent_mode(chat_id)
        elif action == "settings":
            self.handle_settings_callback(chat_id, value)
        elif action == "attach":
            self.start_file_attachment_input(chat_id, value)
        elif action == "answer":
            self.start_answer_input(chat_id, value)
        elif action == "confirm":
            self.confirm_task(chat_id, value)
        elif action == "continue":
            self.start_continue_input(chat_id, value)
        elif action == "contctx":
            self.start_followup_context_input(chat_id, user_id, value)
        elif action == "contattach":
            self.start_followup_file_attachment_input(chat_id, user_id, value)
        elif action == "contrun":
            self.run_continuation_draft(chat_id, value)
        elif action == "cancel":
            self.cancel_task(chat_id, value)

    def dispatch_command(
        self,
        chat_id: int,
        user_id: int | None,
        command: str,
        args: str,
    ) -> None:
        if command in {"/start", "/help"}:
            self.send(chat_id, HELP_TEXT)
            self.show_menu(chat_id)
        elif command == "/menu":
            self.show_menu(chat_id)
        elif command == "/projects":
            self.list_projects(chat_id, args)
        elif command == "/refresh":
            self.spawn(self.refresh_index_command, chat_id)
        elif command == "/project":
            self.select_project(chat_id, args)
        elif command == "/context":
            self.show_context(chat_id, args)
        elif command in {"/alias", "/aliases"}:
            self.alias_command(chat_id, "list" if command == "/aliases" and not args else args)
        elif command == "/new":
            self.start_new_task_input(chat_id)
        elif command == "/agent":
            self.set_agent_mode(chat_id, args)
        elif command in {"/orchestrator_on", "/orchestrator_off", "/orchestrator_status"}:
            action = {
                "/orchestrator_on": "on",
                "/orchestrator_off": "off",
                "/orchestrator_status": "status",
            }[command]
            self.orchestrator_command(chat_id, action)
        elif command == "/settings":
            self.settings_command(chat_id, args)
        elif command == "/debug":
            self.debug_command(chat_id, args)
        elif command == "/ask":
            self.queue_agent_prompt(chat_id, user_id, args, source="text", source_path="")
        elif command == "/task":
            self.create_task(chat_id, user_id, args)
        elif command == "/run":
            self.create_direct_task(chat_id, user_id, args)
        elif command == "/answer":
            self.answer_task(chat_id, args)
        elif command == "/confirm":
            self.confirm_task(chat_id, args.strip())
        elif command == "/continue":
            self.continue_task(chat_id, user_id, args)
        elif command == "/cancel":
            self.cancel_task(chat_id, args.strip())
        elif command == "/status":
            self.status(chat_id, args.strip())
        elif command in {"/processes", "/running"}:
            self.show_processes(chat_id)
        elif command == "/logs":
            self.logs(chat_id, args.strip())
        elif command in {"/brain", "/events"}:
            self.show_task_brain(chat_id, args.strip() or "last")
        elif command == "/memory":
            self.memory_command(chat_id, args)
        elif command == "/remember":
            self.remember_command(chat_id, user_id, args)
        elif command == "/runtime":
            self.show_runtime_identity(chat_id)
        else:
            self.send(chat_id, "Unknown command. Use /help.")

    def show_menu(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        pending = f"\nPending input: {state.pending_action}" if state.pending_action else ""
        self.send(
            chat_id,
            f"Menu\nContext: {self.current_context_label(chat_id)}{pending}",
            reply_markup=main_menu_keyboard(),
        )

    def show_settings(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        orchestrator = self.effective_orchestrator_mode(chat_id)
        memory = self.effective_memory_mode(chat_id)
        task_provider = self.effective_task_provider(chat_id)
        self.send(
            chat_id,
            "\n".join(
                [
                    "Settings",
                    f"Orchestrator: {'ON' if orchestrator else 'OFF'}",
                    f"Model routing: {self.config.orchestrator_model_routing}",
                    f"Default tier: {state.orchestrator_default_tier or self.config.orchestrator_default_tier}",
                    f"Max auto tier: {self.config.orchestrator_max_auto_tier}",
                    f"Review rounds: {self.config.orchestrator_max_review_rounds}",
                    f"Debate: {'ON' if self.config.orchestrator_debate else 'OFF'}",
                    f"Fallback to Codex: {'ON' if self.config.orchestrator_fallback_to_codex else 'OFF'}",
                    f"Task provider: {task_provider}",
                    f"Codex executor: {self.provider_status_text('codex')}",
                    f"Claude executor: {self.provider_status_text('claude')}",
                    f"Memory: {'ON' if memory else 'OFF'}",
                    f"Debug: {'ON' if state.debug_mode else 'OFF'}",
                ]
            ),
            reply_markup=settings_keyboard(orchestrator, state.debug_mode, memory, task_provider),
        )

    def handle_settings_callback(self, chat_id: int, value: str) -> None:
        state = self.store.load_chat_state(chat_id)
        if value == "orchestrator_toggle":
            state.orchestrator_mode = not self.effective_orchestrator_mode(chat_id)
        elif value == "task_provider_toggle":
            state.task_provider = (
                "claude" if self.effective_task_provider(chat_id) == "codex" else "codex"
            )
        elif value == "memory_toggle":
            state.memory_enabled = not self.effective_memory_mode(chat_id)
        elif value == "debug_toggle":
            state.debug_mode = not state.debug_mode
        self.store.save_chat_state(state)
        self.show_settings(chat_id)

    def show_projects_page(
        self,
        chat_id: int,
        page: int,
        message_id: int | None = None,
    ) -> None:
        with self._projects_lock:
            projects = list(self.projects)
        if not projects:
            text = "Project index is empty. Press Refresh."
            if message_id is not None:
                self.edit_message(chat_id, message_id, text)
            else:
                self.send(chat_id, text)
            return
        page = max(0, min(page, max(0, (len(projects) - 1) // PROJECT_PAGE_SIZE)))
        start = page * PROJECT_PAGE_SIZE
        lines = [f"Projects page {page + 1}:"]
        for offset, project in enumerate(projects[start : start + PROJECT_PAGE_SIZE]):
            lines.append(f"{start + offset + 1}. {project.slug}")
        text = "\n".join(lines)
        reply_markup = project_keyboard(projects, page)
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, reply_markup=reply_markup)
        else:
            self.send(chat_id, text, reply_markup=reply_markup)

    def select_project_by_record(self, chat_id: int, project: ProjectInfo) -> None:
        self.select_projects_by_records(chat_id, [project])

    def select_projects_by_records(self, chat_id: int, projects: list[ProjectInfo]) -> None:
        unique_projects: list[ProjectInfo] = []
        for project in projects:
            if project.slug not in {item.slug for item in unique_projects}:
                unique_projects.append(project)

        if not unique_projects:
            self.send(chat_id, "Project not found or query is ambiguous. Use /projects <query>.")
            return

        state = self.store.load_chat_state(chat_id)
        state.selected_project_slug = unique_projects[0].slug
        state.active_project_slugs = [project.slug for project in unique_projects]
        state.pending_action = None
        self.store.save_chat_state(state)
        if len(unique_projects) == 1:
            # The chat was asked to clarify the target repo for a task whose text is
            # already stored; resume it instead of dropping the request.
            if self.resume_pending_project_choice(chat_id, unique_projects[0]):
                return
            text = (
                f"Ок, активный проект: {unique_projects[0].slug}.\n"
                f"{unique_projects[0].path}\n\n"
                "Что делаем?"
            )
        else:
            lines = [
                f"Selected active development context: {len(unique_projects)} projects",
                f"Primary: {unique_projects[0].slug}",
            ]
            lines.extend(f"- {project.slug} | {project.path}" for project in unique_projects)
            lines.append("")
            lines.append("Что делаем?")
            text = "\n".join(lines)
        self.send(chat_id, text, reply_markup=main_menu_keyboard(state.agent_mode))

    def select_root(self, chat_id: int) -> None:
        project = self.find_project(ROOT_PROJECT_SLUG)
        if project is None:
            self.send(chat_id, "Root context is missing. Run /refresh.")
            return
        self.select_project_by_record(chat_id, project)

    def start_new_task_input(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        state.pending_action = "new_task"
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            "Send task text or a voice message.\n"
            f"Context: {self.current_context_label(chat_id)}",
        )

    def start_direct_task_input(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        state.pending_action = "direct_task"
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            "Send task text or a voice message for direct execution without read-only planning.\n"
            f"Context: {self.current_context_label(chat_id)}",
        )

    def set_agent_mode(self, chat_id: int, args: str) -> None:
        value = args.strip().lower()
        if value not in {"on", "off", "toggle", ""}:
            self.send(chat_id, "Usage: /agent on|off")
            return

        state = self.store.load_chat_state(chat_id)
        if value == "on":
            state.agent_mode = True
        elif value == "off":
            state.agent_mode = False
        else:
            state.agent_mode = not state.agent_mode
        state.pending_action = None
        self.store.save_chat_state(state)
        self.show_menu(chat_id)

    def toggle_agent_mode(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        state.agent_mode = not state.agent_mode
        state.pending_action = None
        self.store.save_chat_state(state)
        self.show_menu(chat_id)

    def orchestrator_command(self, chat_id: int, action: str) -> None:
        state = self.store.load_chat_state(chat_id)
        if action == "on":
            state.orchestrator_mode = True
            self.store.save_chat_state(state)
        elif action == "off":
            state.orchestrator_mode = False
            self.store.save_chat_state(state)
        self.send(
            chat_id,
            self.render_orchestrator_status(chat_id),
            reply_markup=settings_keyboard(
                self.effective_orchestrator_mode(chat_id),
                self.store.load_chat_state(chat_id).debug_mode,
                self.effective_memory_mode(chat_id),
                self.effective_task_provider(chat_id),
            ),
        )

    def render_orchestrator_status(self, chat_id: int) -> str:
        enabled = self.effective_orchestrator_mode(chat_id)
        if enabled:
            return (
                "Orchestrator: ON\n"
                "Claude будет использоваться как архитектор и ревьюер, если ModelRouter решит, "
                "что это нужно. Основной executor берётся из настройки Task provider."
            )
        return (
            "Orchestrator: OFF\n"
            f"Задачи будут выполняться напрямую через {self.effective_task_provider(chat_id)}."
        )

    def settings_command(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if not parts:
            self.show_settings(chat_id)
            return
        if parts[0] == "orchestrator":
            action = parts[1] if len(parts) > 1 else "status"
            if action not in {"on", "off", "status"}:
                self.send(chat_id, "Usage: /settings orchestrator on|off|status")
                return
            self.orchestrator_command(chat_id, action)
            return
        if parts[0] == "provider":
            action = parts[1] if len(parts) > 1 else "status"
            if action not in {"codex", "claude", "status"}:
                self.send(chat_id, "Usage: /settings provider codex|claude|status")
                return
            self.task_provider_command(chat_id, action)
            return
        if parts[0] == "debug":
            self.debug_command(chat_id, parts[1] if len(parts) > 1 else "status")
            return
        self.send(chat_id, "Usage: /settings orchestrator on|off|status | provider codex|claude|status")

    def task_provider_command(self, chat_id: int, action: str) -> None:
        state = self.store.load_chat_state(chat_id)
        if action in {"codex", "claude"}:
            state.task_provider = action
            self.store.save_chat_state(state)
        self.send(
            chat_id,
            f"Task provider: {self.effective_task_provider(chat_id)}",
            reply_markup=settings_keyboard(
                self.effective_orchestrator_mode(chat_id),
                self.store.load_chat_state(chat_id).debug_mode,
                self.effective_memory_mode(chat_id),
                self.effective_task_provider(chat_id),
            ),
        )

    def debug_command(self, chat_id: int, args: str) -> None:
        value = args.strip().lower()
        state = self.store.load_chat_state(chat_id)
        if value == "on":
            state.debug_mode = True
        elif value == "off":
            state.debug_mode = False
        elif value in {"", "status"}:
            self.send(chat_id, f"Debug: {'ON' if state.debug_mode else 'OFF'}")
            return
        else:
            self.send(chat_id, "Usage: /debug on|off")
            return
        self.store.save_chat_state(state)
        self.send(chat_id, f"Debug: {'ON' if state.debug_mode else 'OFF'}")

    def list_projects(self, chat_id: int, query: str) -> None:
        with self._projects_lock:
            matches = search_projects(self.projects, query, limit=25)
            total = len(self.projects)
        if not matches:
            self.send(chat_id, "No matching projects. Try /refresh.")
            return

        lines = [f"Projects ({len(matches)} shown, {total} indexed):"]
        for project in matches:
            languages = ", ".join(project.languages) if project.languages else "unknown"
            lines.append(f"- {project.slug} | {languages} | {project.path}")
        if query.strip():
            self.send(chat_id, "\n".join(lines))
        else:
            with self._projects_lock:
                projects = list(self.projects)
            self.send(chat_id, "\n".join(lines), reply_markup=project_keyboard(projects, 0))

    def refresh_index_command(self, chat_id: int) -> None:
        self.send(chat_id, "Refreshing project index...")
        count = self.refresh_index()
        self.send(chat_id, f"Indexed {count} projects.\n{self.config.index_dir}/PROJECTS.md")

    def select_project(self, chat_id: int, query: str) -> None:
        result = self.resolve_project_query(query)
        if result.project is None:
            self.send(chat_id, render_resolution_error(result))
            return
        self.select_project_by_record(chat_id, result.project)

    def show_aliases(self, chat_id: int) -> None:
        self.send(
            chat_id,
            render_aliases(self.aliases.list_aliases()),
            reply_markup={"inline_keyboard": [[{"text": "Menu", "callback_data": "menu"}]]},
        )

    def alias_command(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if not parts or parts[0] == "list":
            self.show_aliases(chat_id)
            return
        if parts[0] == "remove" and len(parts) == 2:
            removed = self.aliases.remove_alias(parts[1])
            self.send(chat_id, f"Alias removed: {parts[1]}" if removed else f"Alias not found: {parts[1]}")
            return
        if parts[0] == "add" and len(parts) >= 3:
            alias = parts[1]
            project_query = " ".join(parts[2:])
            result = self.resolve_project_query(project_query)
            if result.project is None:
                self.send(chat_id, render_resolution_error(result))
                return
            try:
                self.aliases.add_alias(alias, result.project.slug)
            except ValueError as exc:
                self.send(chat_id, f"Alias not saved: {exc}")
                return
            self.send(chat_id, f"Alias saved: {alias} → {result.project.slug}")
            return
        self.send(chat_id, "Usage: /alias list | /alias add <alias> <project> | /alias remove <alias>")

    def show_memory(self, chat_id: int, project_slug: str | None = None) -> None:
        project = self.current_project(chat_id) if project_slug is None else self.find_project(project_slug)
        if project is None:
            self.send(chat_id, "Сначала выберите проект.", reply_markup=project_keyboard(list(self.projects), 0))
            return
        text = self.memory.read_project_memory(project.slug)
        self.set_last_view(chat_id, "memory")
        self.send(
            chat_id,
            f"Memory for {project.slug}:\n{text or '-'}",
            reply_markup=main_menu_keyboard(self.store.load_chat_state(chat_id).agent_mode),
        )

    def memory_command(self, chat_id: int, args: str = "") -> None:
        parts = args.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        project = self.current_project(chat_id)
        if project is None:
            self.send(chat_id, "Сначала выберите проект.", reply_markup=project_keyboard(list(self.projects), 0))
            return

        if action in {"", "show"}:
            self.show_memory(chat_id)
            return
        if action == "status":
            memory = self.memory.read_project_memory(project.slug, max_chars=12000)
            count = len([line for line in memory.splitlines() if line.strip()])
            self.send(
                chat_id,
                f"Memory: {'ON' if self.effective_memory_mode(chat_id) else 'OFF'}\n"
                f"Project: {project.slug}\nItems: {count}",
            )
            return
        if action == "search":
            if not rest.strip():
                self.send(chat_id, "Usage: /memory search <query>")
                return
            matches = self.memory_engine.search_project_memory(project.slug, rest, limit=8)
            self.send(chat_id, "Memory search:\n" + ("\n".join(matches) if matches else "-"))
            return
        if action == "forget":
            if not rest.strip():
                self.send(chat_id, "Usage: /memory forget <item_number>")
                return
            removed = self.memory_engine.forget_project_memory(project.slug, rest.strip())
            self.send(chat_id, "Memory item removed." if removed else "Memory item not found.")
            return
        if action in {"summarize", "export"}:
            target = self.find_project(rest.strip()) if rest.strip() else project
            if target is None:
                self.send(chat_id, "Project not found.")
                return
            self.show_memory(chat_id, target.slug)
            return
        self.send(chat_id, "Usage: /memory [status|search|forget|summarize|export]")

    def remember_command(self, chat_id: int, user_id: int | None, args: str) -> None:
        if not args.strip():
            self.send(chat_id, "Usage: /remember <text>")
            return
        self.propose_memory_note(chat_id, user_id, args)

    def propose_task_memory(self, chat_id: int, user_id: int | None, task_id: str) -> None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return
        final = ""
        if task.final_path and Path(task.final_path).exists():
            final = Path(task.final_path).read_text(encoding="utf-8", errors="replace").strip()
        draft_parts = [f"Task {task.id}: {task.prompt[:500]}"]
        if final:
            draft_parts.append(f"Result: {final[:1000]}")
        draft = "\n".join(draft_parts)
        self.append_event(task.id, "memory", "Scribe", "Prepared project memory draft.")
        self.propose_memory_note(chat_id, user_id, draft, project_slug=task.project_slug)

    def show_runtime_identity(self, chat_id: int) -> None:
        identity = runtime_identity(__file__)
        drifts = compare_runtime_files(
            Path(identity.source_path),
            Path(identity.installed_path),
        )
        roles = ", ".join(
            f"{role.emoji} {role.name}{' (write)' if role.write_access else ''}"
            for role in AGENT_ROLES.values()
        )
        self.send(
            chat_id,
            f"{render_runtime_identity(identity)}\n"
            f"{render_runtime_drift(drifts)}\n"
            f"Agent roles: {roles}",
        )

    def show_context(self, chat_id: int, query: str) -> None:
        project = self.find_project(query)
        if project is None:
            self.send(chat_id, "Project not found or query is ambiguous. Use /projects <query>.")
            return
        context_path = self.config.index_dir / "agents" / f"{project.slug}.md"
        if not context_path.exists():
            self.send(chat_id, "Context file is missing. Run /refresh.")
            return
        context = context_path.read_text(encoding="utf-8", errors="replace")
        self.send(chat_id, context[:3500])

    def parse_task_args(self, chat_id: int, args: str) -> tuple[ProjectInfo | None, str]:
        if not args:
            return None, ""

        first, _, rest = args.partition(" ")
        project = self.find_project(first)
        if project is not None and rest.strip():
            return project, rest.strip()

        state = self.store.load_chat_state(chat_id)
        if state.selected_project_slug:
            selected = self.find_project(state.selected_project_slug)
            if selected is not None:
                return selected, args.strip()

        return self.find_project(ROOT_PROJECT_SLUG), args.strip()

    def reset_prompt_draft(self, state: ChatState) -> None:
        state.prompt_draft_action = None
        state.prompt_draft_user_id = None
        state.prompt_draft_project_slug = None
        state.prompt_draft_context_project_slugs = []
        state.prompt_draft_parts = []
        state.prompt_draft_source = "text"
        state.prompt_draft_source_path = ""
        state.prompt_draft_manual_tier = "auto"
        state.prompt_draft_version += 1

    def prompt_draft_matches(
        self,
        state: ChatState,
        action: str,
        user_id: int | None,
        project: ProjectInfo,
    ) -> bool:
        return (
            state.prompt_draft_action == action
            and state.prompt_draft_user_id == user_id
            and state.prompt_draft_project_slug == project.slug
        )

    def queue_prompt_draft(
        self,
        chat_id: int,
        user_id: int | None,
        project: ProjectInfo,
        text: str,
        action: str,
        source: str,
        source_path: str,
        context_project_slugs: list[str] | None = None,
        manual_tier: str = "auto",
    ) -> None:
        part = text.strip()
        if not part:
            self.send(chat_id, "Prompt text is empty.")
            return
        draft_context_slugs = normalize_context_project_slugs(
            project.slug,
            context_project_slugs or self.task_context_project_slugs(chat_id, project),
        )

        while True:
            with self._draft_lock:
                state = self.store.load_chat_state(chat_id)
                if state.prompt_draft_parts and not self.prompt_draft_matches(
                    state,
                    action,
                    user_id,
                    project,
                ):
                    stale_version = state.prompt_draft_version
                else:
                    if not state.prompt_draft_parts:
                        state.prompt_draft_action = action
                        state.prompt_draft_user_id = user_id
                        state.prompt_draft_project_slug = project.slug
                        state.prompt_draft_context_project_slugs = draft_context_slugs
                        state.prompt_draft_source = source
                        state.prompt_draft_source_path = source_path
                        state.prompt_draft_manual_tier = manual_tier
                    state.prompt_draft_parts.append(part)
                    state.prompt_draft_version += 1
                    if action in {"new_task", "direct_task"}:
                        state.pending_action = action
                    self.store.save_chat_state(state)
                    part_count = len(state.prompt_draft_parts)
                    version = state.prompt_draft_version
                    break

            self.flush_prompt_draft(chat_id, expected_version=stale_version)

        self.schedule_prompt_draft_flush(chat_id, version)
        target = {
            "new_task": "task",
            "direct_task": "direct task",
            "agent_chat": "agent request",
        }.get(action, "continuation" if pending_task_id(action, "continue_task") else "prompt")
        self.send(
            chat_id,
            f"Prompt part {part_count} saved for {target}. "
            f"Starting after {self.config.prompt_debounce_seconds:g}s without new parts.",
        )

    def schedule_prompt_draft_flush(self, chat_id: int, expected_version: int) -> None:
        delay = max(0.0, self.config.prompt_debounce_seconds)
        with self._draft_lock:
            existing = self._draft_timers.pop(chat_id, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(
                delay,
                self.flush_prompt_draft,
                args=(chat_id, expected_version),
            )
            timer.daemon = True
            self._draft_timers[chat_id] = timer
            timer.start()

    def take_prompt_draft(
        self,
        chat_id: int,
        expected_version: int | None = None,
    ) -> PromptDraft | None:
        with self._draft_lock:
            state = self.store.load_chat_state(chat_id)
            if not state.prompt_draft_parts or not state.prompt_draft_action:
                return None
            if (
                expected_version is not None
                and state.prompt_draft_version != expected_version
            ):
                return None
            draft = PromptDraft(
                chat_id=chat_id,
                user_id=state.prompt_draft_user_id,
                action=state.prompt_draft_action,
                project_slug=state.prompt_draft_project_slug or ROOT_PROJECT_SLUG,
                context_project_slugs=tuple(
                    state.prompt_draft_context_project_slugs
                    or [state.prompt_draft_project_slug or ROOT_PROJECT_SLUG]
                ),
                parts=tuple(state.prompt_draft_parts),
                source=state.prompt_draft_source,
                source_path=state.prompt_draft_source_path,
                manual_tier=state.prompt_draft_manual_tier,
            )
            if (
                draft.action in {"new_task", "direct_task"}
                or pending_task_id(draft.action, "continue_task")
            ) and state.pending_action == draft.action:
                state.pending_action = None
            self.reset_prompt_draft(state)
            self.store.save_chat_state(state)
            timer = self._draft_timers.pop(chat_id, None)
            if timer is not None:
                timer.cancel()
            return draft

    def flush_prompt_draft(
        self,
        chat_id: int,
        expected_version: int | None = None,
    ) -> None:
        draft = self.take_prompt_draft(chat_id, expected_version=expected_version)
        if draft is None:
            return

        prompt = PROMPT_DRAFT_SEPARATOR.join(draft.parts).strip()
        if not prompt:
            self.send(chat_id, "Prompt draft is empty.")
            return

        project = self.find_project(draft.project_slug)
        if project is None:
            self.send(
                chat_id,
                "Draft context disappeared from index. Run /refresh and send the prompt again.",
            )
            return

        if draft.action == "new_task":
            self.create_task_for_project(
                draft.chat_id,
                draft.user_id,
                project,
                prompt,
                source=draft.source,
                source_path=draft.source_path,
                context_project_slugs=list(draft.context_project_slugs),
            )
        elif draft.action == "direct_task":
            self.create_direct_task_for_project(
                draft.chat_id,
                draft.user_id,
                project,
                prompt,
                source=draft.source,
                source_path=draft.source_path,
                context_project_slugs=list(draft.context_project_slugs),
                manual_tier=draft.manual_tier,
            )
        elif draft.action == "agent_chat":
            self.ask_agent_for_project(
                draft.chat_id,
                draft.user_id,
                project,
                prompt,
                source=draft.source,
                source_path=draft.source_path,
                context_project_slugs=list(draft.context_project_slugs),
            )
        else:
            parent_task_id = pending_task_id(draft.action, "continue_task")
            if parent_task_id:
                self.create_followup_task_for_parent(
                    draft.chat_id,
                    draft.user_id,
                    parent_task_id,
                    prompt,
                    source=draft.source,
                    source_path=draft.source_path,
                )

    def resume_prompt_drafts(self) -> None:
        for state in self.store.chat_states():
            if (
                state.chat_id in self.config.allowed_chat_ids
                and state.prompt_draft_parts
                and state.prompt_draft_action
            ):
                self.schedule_prompt_draft_flush(
                    state.chat_id,
                    state.prompt_draft_version,
                )

    def recover_interrupted_tasks(
        self,
        pid_alive: Callable[[int | None], bool] = is_process_alive,
    ) -> None:
        if not self.config.recover_interrupted_tasks:
            LOG.info("interrupted task recovery is disabled")
            return

        tasks = self.store.tasks(
            phases=PROCESS_PHASES,
            chat_ids=self.config.allowed_chat_ids,
        )
        queued = 0
        for task in tasks:
            process_alive = is_recorded_process_alive(task, pid_alive)
            if process_alive and not task.runtime_id:
                continue
            if process_alive and task.runtime_id == self.runner.runtime_id:
                continue
            if process_alive:
                terminate_process_group(task.pid)

            previous_phase = task.phase
            task.pid = None
            task.pid_start_time = ""
            if not task.codex_session_id:
                for log_path in (task.run_log_path, task.plan_log_path):
                    if log_path:
                        task.codex_session_id = extract_session_id(Path(log_path))
                        if task.codex_session_id:
                            break

            project = self.find_project(task.project_slug)
            if project is None:
                task.phase = "failed"
                task.error = (
                    "Interrupted task could not be recovered because project "
                    "disappeared from index."
                )
                task.recovery_attempts += 1
                self.store.save_task(task)
                self.send(task.chat_id, f"Task {task.id} failed: {task.error}")
                continue

            if previous_phase != "running":
                task.recovery_attempts += 1
            task.error = "Interrupted by bot restart; recovery queued."
            self.store.save_task(task)
            self.append_event(
                task.id,
                "state_change",
                "System",
                f"Recovery queued after interrupted {previous_phase}.",
            )
            status_message_id = self.send_codex_status(
                task,
                previous_phase,
                f"Восстанавливаю задачу {task.id} после рестарта бота.",
            )
            if previous_phase == "planning":
                self.spawn(self.plan_task, task.id, status_message_id)
            elif previous_phase == "agent_running":
                self.spawn(self.run_agent_chat, task.id, status_message_id)
            elif previous_phase == "running":
                self.spawn(self.execute_task, task.id, status_message_id, True)
            queued += 1

        if queued:
            LOG.info("queued %d interrupted task recoveries", queued)

    def start_slack_watcher(self) -> None:
        if self.slack_watcher is None:
            if self.config.slack_token:
                LOG.info("slack watcher is disabled by configuration")
            return

        thread = threading.Thread(
            target=self.slack_watcher.run,
            args=(self.stop_event, self.send_slack_notification),
            daemon=True,
        )
        self.worker_threads.add(thread)
        thread.start()
        LOG.info(
            "slack watcher started: dms=%s configured_channels=%d targets=%d",
            self.config.slack_watch_dms,
            len(self.config.slack_channel_ids),
            len(self.config.slack_target_chat_ids),
        )

    def start_slack_desktop_watcher(self) -> None:
        if self.slack_desktop_watcher is None:
            return

        thread = threading.Thread(
            target=self.slack_desktop_watcher.run,
            args=(self.stop_event, self.send_slack_notification),
            daemon=True,
        )
        self.worker_threads.add(thread)
        thread.start()
        LOG.info(
            "slack desktop watcher started: targets=%d",
            len(self.config.slack_target_chat_ids),
        )

    def send_slack_notification(self, notification: SlackNotification) -> None:
        text = render_slack_notification(notification)
        for chat_id in self.config.slack_target_chat_ids:
            if chat_id in self.config.allowed_chat_ids:
                self.send(chat_id, text)

    def handle_intent(
        self,
        chat_id: int,
        user_id: int | None,
        intent: IntentResult,
        original_text: str,
    ) -> bool:
        if intent.intent == "select_project" and intent.project_query:
            return self.select_project_conversational(chat_id, intent.project_query)
        if intent.intent == "add_alias" and intent.project_query and intent.alias:
            self.propose_alias(chat_id, intent.alias, intent.project_query)
            return True
        if intent.intent == "remember" and intent.memory_text:
            self.propose_memory_note(chat_id, user_id, intent.memory_text)
            return True
        if intent.intent == "show_memory":
            self.show_memory(chat_id)
            return True
        if intent.intent == "show_brain":
            self.show_task_brain(chat_id, intent.task_id or "last")
            return True
        if intent.intent == "show_logs":
            task = self.recent_context_task(chat_id, intent.task_id)
            if task is None:
                self.send(chat_id, "Не нашел задачу для логов.", reply_markup=tasks_keyboard())
                return True
            self.logs(chat_id, task.id)
            return True
        if intent.intent == "show_status":
            task = self.recent_context_task(chat_id, intent.task_id)
            if task is not None:
                self.show_task(chat_id, task.id)
            else:
                self.show_processes(chat_id)
            return True
        if intent.intent == "show_plan":
            self.show_task_plan(chat_id, intent.task_id or "last")
            return True
        if intent.intent == "show_safety":
            self.show_task_safety(chat_id, intent.task_id or "last")
            return True
        if intent.intent == "show_review":
            self.show_task_review(chat_id, intent.task_id or "last")
            return True
        if intent.intent == "continue_task":
            if intent.task_text:
                project = self.current_project(chat_id)
                result = self.task_resolver.resolve_message(
                    chat_id,
                    f"продолжи {intent.task_text}",
                    project_slug=project.slug if project else None,
                )
                if result.task is not None:
                    self.send(
                        chat_id,
                        "Нашёл похожую задачу:\n\n"
                        f"{short_task(result.task)}\n\nПродолжаю её.",
                        reply_markup=completed_task_keyboard(result.task.id)
                        if result.task.phase == "completed"
                        else task_progress_keyboard(result.task.id, result.task.phase),
                    )
                    if result.task.phase == "completed":
                        self.start_continue_input(chat_id, result.task.id)
                    return True
                if result.matches:
                    self.show_task_matches(chat_id, result.matches, "Нашёл несколько похожих задач:")
                    return True
                self.send(chat_id, "Не нашёл похожую задачу. Создам новую, если отправите задачу обычным текстом.")
                return True
            task = self.recent_context_task(
                chat_id,
                intent.task_id,
                phases={"completed"},
            )
            if task is None:
                self.send(chat_id, "Не нашёл completed задачу для продолжения.", reply_markup=tasks_keyboard())
                return True
            self.start_continue_input(chat_id, task.id)
            return True
        if intent.intent == "search_tasks" and intent.task_text:
            matches = self.store.search_tasks(intent.task_text, chat_id=chat_id, limit=5)
            if not matches:
                self.send(chat_id, "Задачи не найдены.", reply_markup=tasks_keyboard())
                return True
            self.show_task_matches(chat_id, matches, "Нашёл задачи:")
            return True
        if intent.intent == "confirm_execution":
            task = self.recent_context_task(
                chat_id,
                intent.task_id,
                phases={"planned"},
            )
            if task is None:
                self.send(chat_id, "Не нашел planned задачу для запуска.", reply_markup=tasks_keyboard())
                return True
            self.confirm_task(chat_id, task.id)
            return True
        if intent.intent == "cancel_task":
            task = self.recent_context_task(
                chat_id,
                intent.task_id,
                phases={"created", "planning", "planned", "running", "agent_running"},
            )
            if task is None:
                self.send(chat_id, "Не нашел активную задачу для отмены.", reply_markup=tasks_keyboard())
                return True
            self.cancel_task(chat_id, task.id)
            return True
        if intent.intent == "list_projects":
            self.list_projects(chat_id, "")
            return True
        if intent.intent == "list_tasks":
            self.show_tasks(chat_id, "active")
            return True
        if intent.intent == "agent_chat":
            self.queue_agent_prompt(chat_id, user_id, intent.task_text or original_text, source="text", source_path="")
            return True
        if intent.intent == "create_task":
            self.queue_task_prompt_resolving_project(
                chat_id,
                user_id,
                intent.task_text or original_text,
                action="new_task",
                source="text",
                source_path="",
            )
            return True
        return False

    def select_project_conversational(self, chat_id: int, query: str) -> bool:
        queries = split_project_queries(query)
        if not queries:
            self.send(chat_id, "Укажите проект.", reply_markup=project_keyboard(list(self.projects), 0))
            return True

        projects: list[ProjectInfo] = []
        errors: list[str] = []
        suggestion_rows: list[list[dict[str, str]]] = []
        with self._projects_lock:
            all_projects = list(self.projects)

        for item in queries:
            result = self.resolve_project_query(item)
            if result.project is not None:
                projects.append(result.project)
                continue
            errors.append(render_resolution_error(result))
            for suggestion in result.suggestions[:6]:
                try:
                    index = all_projects.index(suggestion)
                except ValueError:
                    continue
                suggestion_rows.append(
                    [{"text": suggestion.slug[:32], "callback_data": f"proj:{index}"}]
                )

        if errors:
            rows = suggestion_rows or project_keyboard(all_projects, 0)["inline_keyboard"]
            self.send(
                chat_id,
                "Не смог однозначно выбрать проект.\n\n" + "\n\n".join(errors),
                reply_markup={"inline_keyboard": rows},
            )
            return True

        self.select_projects_by_records(chat_id, projects)
        return True

    def propose_alias(self, chat_id: int, alias: str, project_query: str) -> None:
        result = self.resolve_project_query(project_query)
        if result.project is None:
            self.send(chat_id, render_resolution_error(result), reply_markup=project_keyboard(list(self.projects), 0))
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_alias_name = alias.strip()
        state.pending_alias_project_slug = result.project.slug
        state.pending_action = "alias_confirm"
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            f"Добавить alias?\n{state.pending_alias_name} → {result.project.slug}",
            reply_markup=alias_confirm_keyboard(),
        )

    def save_pending_alias(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        if not state.pending_alias_name or not state.pending_alias_project_slug:
            self.send(chat_id, "Нет alias на подтверждение.")
            return
        try:
            self.aliases.add_alias(state.pending_alias_name, state.pending_alias_project_slug)
        except ValueError as exc:
            self.send(chat_id, f"Alias не сохранен: {exc}")
            return
        text = f"Alias saved: {state.pending_alias_name} → {state.pending_alias_project_slug}"
        state.pending_alias_name = ""
        state.pending_alias_project_slug = ""
        state.pending_action = None
        self.store.save_chat_state(state)
        self.send(chat_id, text, reply_markup=main_menu_keyboard(state.agent_mode))

    def cancel_pending_alias(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        state.pending_alias_name = ""
        state.pending_alias_project_slug = ""
        if state.pending_action == "alias_confirm":
            state.pending_action = None
        self.store.save_chat_state(state)
        self.send(chat_id, "Alias не сохранен.", reply_markup=main_menu_keyboard(state.agent_mode))

    def propose_memory_note(
        self,
        chat_id: int,
        user_id: int | None,
        text: str,
        project_slug: str | None = None,
    ) -> None:
        del user_id
        project = self.current_project(chat_id) if project_slug is None else self.find_project(project_slug)
        if project is None:
            self.send(chat_id, "Сначала выберите проект.", reply_markup=project_keyboard(list(self.projects), 0))
            return
        sanitized = self.memory.sanitize(text)
        if not sanitized.text:
            self.send(chat_id, "Не сохраняю заметку: она пустая или похожа на secret.")
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_memory_project_slug = project.slug
        state.pending_memory_text = sanitized.text
        state.pending_action = f"memory_confirm:{project.slug}"
        self.store.save_chat_state(state)
        warning = (
            f"\n\nОтфильтровано строк как potential secrets: {sanitized.removed_lines}"
            if sanitized.removed_lines
            else ""
        )
        self.send(
            chat_id,
            f"Сохранить в память проекта {project.slug}?\n\n{sanitized.text[:1200]}{warning}",
            reply_markup=memory_confirm_keyboard(project.slug),
        )

    def save_pending_memory(self, chat_id: int, user_id: int | None) -> None:
        state = self.store.load_chat_state(chat_id)
        if not state.pending_memory_project_slug or not state.pending_memory_text:
            self.send(chat_id, "Нет заметки на подтверждение.")
            return
        sanitized = self.memory.append_project_memory(
            state.pending_memory_project_slug,
            state.pending_memory_text,
            user_id=user_id,
        )
        project_slug = state.pending_memory_project_slug
        last_task_id = state.last_task_id
        state.pending_memory_project_slug = ""
        state.pending_memory_text = ""
        state.pending_action = None
        self.store.save_chat_state(state)
        if not sanitized.text:
            self.send(chat_id, "Не сохранил заметку: после фильтрации нечего сохранять.")
            return
        if last_task_id:
            task = self.store.load_task(last_task_id)
            if task is not None and task.chat_id == chat_id:
                self.append_event(task.id, "memory", "Scribe", f"Project memory updated for {project_slug}.")
        self.send(chat_id, f"Память проекта обновлена: {project_slug}")

    def edit_pending_memory(self, chat_id: int, project_slug: str) -> None:
        state = self.store.load_chat_state(chat_id)
        state.pending_action = f"memory_edit:{project_slug}"
        state.pending_memory_project_slug = project_slug
        self.store.save_chat_state(state)
        self.send(chat_id, "Отправьте исправленный текст для памяти проекта.")

    def skip_pending_memory(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        state.pending_memory_project_slug = ""
        state.pending_memory_text = ""
        if state.pending_action and state.pending_action.startswith("memory_"):
            state.pending_action = None
        self.store.save_chat_state(state)
        self.send(chat_id, "Заметка не сохранена.")

    def handle_plain_text(self, chat_id: int, user_id: int | None, text: str) -> None:
        state = self.store.load_chat_state(chat_id)
        answer_task_id = pending_task_id(state.pending_action, "answer_task")
        if answer_task_id:
            state.pending_action = None
            self.store.save_chat_state(state)
            self.answer_task(chat_id, f"{answer_task_id} {text}")
            return

        memory_project_slug = pending_task_id(state.pending_action, "memory_edit")
        if memory_project_slug:
            self.propose_memory_note(chat_id, user_id, text, project_slug=memory_project_slug)
            return

        followup_task_id = pending_task_id(state.pending_action, "followup_context")
        if followup_task_id:
            self.append_followup_context(
                chat_id,
                followup_task_id,
                text,
                source="text",
                source_path="",
            )
            return

        continue_parent_id = pending_task_id(state.pending_action, "continue_task")
        if continue_parent_id:
            self.queue_followup_prompt(
                chat_id,
                user_id,
                continue_parent_id,
                text,
                source="text",
                source_path="",
            )
            return

        if state.prompt_draft_parts and state.prompt_draft_action:
            project = self.find_project(state.prompt_draft_project_slug or ROOT_PROJECT_SLUG)
            if project is None:
                self.send(chat_id, "Draft context disappeared from index. Run /refresh.")
                return
            self.queue_prompt_draft(
                chat_id,
                user_id,
                project,
                text,
                action=state.prompt_draft_action,
                source=state.prompt_draft_source,
                source_path=state.prompt_draft_source_path,
                context_project_slugs=state.prompt_draft_context_project_slugs,
            )
            return

        if state.pending_action in {"new_task", "direct_task"}:
            self.queue_task_prompt_resolving_project(
                chat_id,
                user_id,
                text,
                action=state.pending_action,
                source="text",
                source_path="",
            )
            return

        intent = self.intent_router.route(text, self.intent_context(chat_id))
        if state.agent_mode and intent.intent == "create_task":
            intent = IntentResult("agent_chat", 0.7, task_text=text)
        if self.handle_intent(chat_id, user_id, intent, text):
            return

        project = self.current_project(chat_id)
        if project is None:
            self.send(chat_id, "No context selected and root context is missing. Run /refresh.")
            return

        if state.agent_mode:
            self.queue_prompt_draft(
                chat_id,
                user_id,
                project,
                text,
                action="agent_chat",
                source="text",
                source_path="",
            )
            return

        self.send(
            chat_id,
            "Не понял намерение. Выберите действие:",
            reply_markup=main_menu_keyboard(state.agent_mode),
        )

    def handle_voice_message(
        self,
        chat_id: int,
        user_id: int | None,
        message: dict[str, Any],
    ) -> None:
        voice = message.get("voice") or {}
        file_id = voice.get("file_id")
        file_unique_id = voice.get("file_unique_id") or file_id or "voice"
        if not file_id:
            self.send(chat_id, "Voice message does not contain a Telegram file id.")
            return

        try:
            file_info = self.api.get_file(file_id)
            telegram_path = file_info["file_path"]
            suffix = Path(telegram_path).suffix or ".oga"
            destination = (
                self.config.state_dir
                / "inbox"
                / str(chat_id)
                / f"{int(time.time())}-{file_unique_id}{suffix}"
            )
            self.api.download_file(telegram_path, destination)
            transcript = transcribe_audio(
                self.config.transcribe_command,
                destination,
                self.config.transcribe_timeout_seconds,
            )
        except TranscriptionError as exc:
            self.send(
                chat_id,
                f"Voice received, but transcription is not available: {exc}\n"
                "Configure CODEX_TELEGRAM_TRANSCRIBE_CMD to enable voice tasks.",
            )
            return
        except Exception as exc:
            LOG.exception("voice handling failed")
            self.send(chat_id, f"Voice handling failed: {exc}")
            return

        self.send(chat_id, f"Voice transcript:\n{transcript[:1200]}")
        state = self.store.load_chat_state(chat_id)
        followup_task_id = pending_task_id(state.pending_action, "followup_context")
        if followup_task_id:
            self.append_followup_context(
                chat_id,
                followup_task_id,
                transcript,
                source="voice",
                source_path=str(destination),
            )
            return

        continue_parent_id = pending_task_id(state.pending_action, "continue_task")
        if continue_parent_id:
            self.queue_followup_prompt(
                chat_id,
                user_id,
                continue_parent_id,
                transcript,
                source="voice",
                source_path=str(destination),
            )
            return

        project = self.current_project(chat_id)
        if project is None:
            self.send(chat_id, "No context selected and root context is missing. Run /refresh.")
            return

        if state.pending_action == "new_task":
            self.queue_task_prompt_resolving_project(
                chat_id,
                user_id,
                transcript,
                action="new_task",
                source="voice",
                source_path=str(destination),
            )
        elif state.pending_action == "direct_task":
            self.queue_task_prompt_resolving_project(
                chat_id,
                user_id,
                transcript,
                action="direct_task",
                source="voice",
                source_path=str(destination),
            )
        elif state.agent_mode:
            self.ask_agent(
                chat_id,
                user_id,
                transcript,
                source="voice",
                source_path=str(destination),
            )
        else:
            self.send(
                chat_id,
                "Voice was transcribed. Press New task or enable Agent mode first.",
                reply_markup=main_menu_keyboard(state.agent_mode),
            )

    def completed_task_for_continuation(
        self,
        chat_id: int,
        task_id: str,
    ) -> TaskRecord | None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return None
        if task.phase != "completed":
            self.send(chat_id, f"Task is {task.phase}; only completed tasks can be continued.")
            return None
        # Codex only reveals its session id in the run log; Claude stores its own.
        if not task.codex_session_id and task.run_log_path:
            recovered = extract_session_id(Path(task.run_log_path))
            if recovered:
                task.codex_session_id = recovered
                self.store.save_task(task)
        # A missing session only costs conversation history: the continuation
        # prompt still carries the parent task context, so the follow-up runs
        # as a fresh conversation instead of being refused.
        return task

    def active_followup_for_parent(
        self,
        chat_id: int,
        parent_task_id: str,
    ) -> TaskRecord | None:
        for task in self.store.recent_tasks(chat_id, limit=100, phases=None):
            if (
                task.parent_task_id == parent_task_id
                and task.phase in BLOCKING_FOLLOWUP_PHASES
            ):
                return task
        return None

    def ensure_followup_draft(
        self,
        chat_id: int,
        user_id: int | None,
        parent_task_id: str,
    ) -> TaskRecord | None:
        parent = self.completed_task_for_continuation(chat_id, parent_task_id)
        if parent is None:
            return None

        active = self.active_followup_for_parent(chat_id, parent.id)
        if active is not None:
            if active.phase == "created":
                return active
            self.send(chat_id, f"Continuation already active: {active.id} ({active.phase}).")
            return None

        project = self.find_project(parent.project_slug)
        if project is None:
            self.send(chat_id, "Project disappeared from index.")
            return None

        return self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt="",
            context_project_slugs=normalize_context_project_slugs(
                project.slug,
                parent.context_project_slugs,
            ),
            kind="followup_task",
            source="text",
            source_path="",
            parent_task_id=parent.id,
            codex_session_id=parent.codex_session_id,
            claude_session_id=parent.claude_session_id,
        )

    def start_continue_input(self, chat_id: int, task_id: str) -> None:
        task = self.completed_task_for_continuation(chat_id, task_id)
        if task is None:
            return

        active = self.active_followup_for_parent(chat_id, task.id)
        if active is not None:
            if active.phase == "created":
                self.send(
                    chat_id,
                    f"Continuation draft already exists: {active.id}. "
                    "Add context/files or run it.",
                    reply_markup=followup_draft_keyboard(task.id, active.id),
                )
            else:
                self.send(chat_id, f"Continuation already active: {active.id} ({active.phase}).")
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_action = continue_task_action(task.id)
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            f"Send follow-up instruction for task {task.id}.",
            reply_markup=completed_task_keyboard(task.id),
        )

    def start_followup_context_input(
        self,
        chat_id: int,
        user_id: int | None,
        parent_task_id: str,
    ) -> None:
        task = self.ensure_followup_draft(chat_id, user_id, parent_task_id)
        if task is None:
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_action = followup_context_action(task.id)
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            f"Send extra context or follow-up instruction for continuation draft {task.id}.",
            reply_markup=followup_draft_keyboard(task.parent_task_id, task.id),
        )

    def append_followup_context(
        self,
        chat_id: int,
        task_id: str,
        text: str,
        source: str,
        source_path: str,
    ) -> None:
        part = text.strip()
        if not part:
            self.send(chat_id, "Continuation context is empty.")
            return

        task = self.store.load_task(task_id)
        if (
            task is None
            or task.chat_id != chat_id
            or task.kind != "followup_task"
            or not task.parent_task_id
        ):
            self.send(chat_id, "Continuation draft not found.")
            return
        if task.phase != "created":
            self.send(chat_id, f"Continuation draft is {task.phase}; cannot add context now.")
            return

        if task.prompt.strip():
            task.prompt = PROMPT_DRAFT_SEPARATOR.join([task.prompt.strip(), part])
            task.source = "mixed" if task.source != source else task.source
            if task.source == "mixed":
                task.source_path = ""
        else:
            task.prompt = part
            task.source = source
            task.source_path = source_path
        self.store.save_task(task)
        self.send(
            chat_id,
            f"Continuation context saved for draft {task.id}. "
            "Add more context/files or run continuation.",
            reply_markup=followup_draft_keyboard(task.parent_task_id, task.id),
        )

    def start_followup_file_attachment_input(
        self,
        chat_id: int,
        user_id: int | None,
        parent_task_id: str,
    ) -> None:
        task = self.ensure_followup_draft(chat_id, user_id, parent_task_id)
        if task is None:
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_action = f"attach_files:{task.id}"
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            "Send documents or photos for this continuation draft. "
            "When done, add context or run continuation.",
            reply_markup=followup_draft_keyboard(task.parent_task_id, task.id),
        )

    def run_continuation_draft(self, chat_id: int, parent_task_id: str) -> None:
        parent = self.completed_task_for_continuation(chat_id, parent_task_id)
        if parent is None:
            return

        state = self.store.load_chat_state(chat_id)
        if state.prompt_draft_action == continue_task_action(parent.id):
            self.flush_prompt_draft(chat_id, expected_version=state.prompt_draft_version)
            return

        task = self.active_followup_for_parent(chat_id, parent.id)
        if task is None:
            self.send(
                chat_id,
                "No continuation draft yet. Add context or files first.",
                reply_markup=completed_task_keyboard(parent.id),
            )
            return
        if task.phase != "created":
            self.send(chat_id, f"Continuation already active: {task.id} ({task.phase}).")
            return
        if not task.prompt.strip() and not task.attachments:
            self.send(
                chat_id,
                "Continuation draft is empty. Add context or files first.",
                reply_markup=followup_draft_keyboard(parent.id, task.id),
            )
            return

        if not task.prompt.strip():
            task.prompt = DEFAULT_CONTINUATION_PROMPT
            task.source = "attachments"
            self.store.save_task(task)

        if (
            pending_task_id(state.pending_action, "followup_context") == task.id
            or pending_task_id(state.pending_action, "attach_files") == task.id
            or state.pending_action == continue_task_action(parent.id)
        ):
            state.pending_action = None
            self.store.save_chat_state(state)

        status_message_id = self.send_codex_status(
            task,
            "running",
            f"Continuation draft {task.id} started from {parent.id}. Resuming Codex session.",
            reply_markup=running_task_keyboard(task.id),
        )
        self.append_event(
            task.id,
            "state_change",
            "CodexDev",
            f"Continuation started from {parent.id}.",
            {"parent_task_id": parent.id},
        )
        self.remember_task_context(task, "status")
        self.spawn(self.execute_followup_task, task.id, status_message_id)

    def queue_followup_prompt(
        self,
        chat_id: int,
        user_id: int | None,
        parent_task_id: str,
        prompt: str,
        source: str,
        source_path: str,
    ) -> None:
        parent = self.completed_task_for_continuation(chat_id, parent_task_id)
        if parent is None:
            return

        active = self.active_followup_for_parent(chat_id, parent.id)
        if active is not None:
            self.send(chat_id, f"Continuation already active: {active.id} ({active.phase}).")
            return

        project = self.find_project(parent.project_slug)
        if project is None:
            self.send(chat_id, "Project disappeared from index.")
            return

        self.queue_prompt_draft(
            chat_id,
            user_id,
            project,
            prompt,
            action=continue_task_action(parent.id),
            source=source,
            source_path=source_path,
        )

    def continue_task(self, chat_id: int, user_id: int | None, args: str) -> None:
        task_id, _, prompt = args.partition(" ")
        task_id = task_id.strip()
        prompt = prompt.strip()
        if not task_id:
            self.send(chat_id, "Usage: /continue <task_id> <text>")
            return
        if not prompt:
            self.start_continue_input(chat_id, task_id)
            return

        self.queue_followup_prompt(
            chat_id,
            user_id,
            task_id,
            prompt,
            source="text",
            source_path="",
        )

    def start_answer_input(self, chat_id: int, task_id: str) -> None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return
        if task.phase not in ANSWERABLE_PHASES:
            self.send(chat_id, f"Task is {task.phase}; cannot add clarification now.")
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_action = f"answer_task:{task.id}"
        self.store.save_chat_state(state)
        if task.phase in PROCESS_PHASES:
            self.send(
                chat_id,
                f"Send context for active task {task.id}.",
                reply_markup=running_task_keyboard(task.id),
            )
            return
        self.send(
            chat_id,
            f"Send clarification text for task {task.id}.",
            reply_markup=inline_task_keyboard(task.id),
        )

    def start_file_attachment_input(self, chat_id: int, task_id: str) -> None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return
        if task.phase not in ATTACHABLE_PHASES:
            self.send(chat_id, f"Task is {task.phase}; cannot add files now.")
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_action = f"attach_files:{task.id}"
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            "Send documents or photos for this task. "
            "When done, press Execute or Answer clarification.",
            reply_markup=inline_task_keyboard(task.id),
        )

    def handle_file_message(
        self,
        chat_id: int,
        user_id: int | None,
        message: dict[str, Any],
    ) -> None:
        del user_id
        state = self.store.load_chat_state(chat_id)
        task_id = pending_task_id(state.pending_action, "attach_files")
        if not task_id:
            self.send(chat_id, "Press Add files on a planned task before sending files.")
            return

        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return
        if task.phase not in ATTACHABLE_PHASES:
            self.send(chat_id, f"Task is {task.phase}; cannot add files now.")
            return

        try:
            attachment = self.download_task_attachment(task, message)
        except Exception as exc:
            LOG.exception("file attachment failed")
            self.send(chat_id, f"File attachment failed: {exc}")
            return

        task = self.store.add_attachment(task.id, attachment)
        if task is None:
            self.send(chat_id, "Task not found.")
            return

        count = len(task.attachments)
        if task.kind == "followup_task" and task.parent_task_id and task.phase == "created":
            self.send(
                chat_id,
                f"Attached context file to continuation draft {task.id}: {attachment['file_name']}\n"
                f"Total attachments: {count}\n"
                "Send more files, add context, or run continuation.",
                reply_markup=followup_draft_keyboard(task.parent_task_id, task.id),
            )
            return

        self.send(
            chat_id,
            f"Attached file to task {task.id}: {attachment['file_name']}\n"
            f"Total attachments: {count}\n"
            "Send more files, or press Execute / Answer clarification.",
            reply_markup=inline_task_keyboard(task.id),
        )

    def download_task_attachment(
        self,
        task: TaskRecord,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        file_payload = self.extract_attachment_payload(message)
        if file_payload is None:
            raise ValueError("Message does not contain a supported document or photo.")

        kind = file_payload["kind"]
        file_id = file_payload["file_id"]
        file_unique_id = file_payload["file_unique_id"]
        file_name = file_payload["file_name"]
        mime_type = file_payload["mime_type"]
        file_size = file_payload["file_size"]
        if file_size and file_size > MAX_ATTACHMENT_SIZE_BYTES:
            raise ValueError("File is too large. Maximum size is 20 MB.")

        file_info = self.api.get_file(file_id)
        telegram_path = str(file_info["file_path"])
        telegram_size = file_info.get("file_size") or file_size
        if telegram_size and int(telegram_size) > MAX_ATTACHMENT_SIZE_BYTES:
            raise ValueError("File is too large. Maximum size is 20 MB.")

        suffix = Path(telegram_path).suffix or Path(file_name).suffix or ".bin"
        safe_unique_id = safe_attachment_name(str(file_unique_id or file_id))
        safe_name = safe_attachment_name(file_name)
        if not Path(safe_name).suffix:
            safe_name = f"{safe_name}{suffix}"
        destination = (
            self.store.task_dir(task.id)
            / "attachments"
            / f"{time.time_ns()}-{safe_unique_id}-{safe_name}"
        )
        self.api.download_file(telegram_path, destination)

        actual_size = destination.stat().st_size
        if actual_size > MAX_ATTACHMENT_SIZE_BYTES:
            destination.unlink(missing_ok=True)
            raise ValueError("File is too large. Maximum size is 20 MB.")

        caption = str(message.get("caption") or "").strip()
        return {
            "kind": kind,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "file_name": safe_name,
            "mime_type": mime_type,
            "file_size": actual_size,
            "file_path": str(destination),
            "caption": caption[:1000],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def extract_attachment_payload(self, message: dict[str, Any]) -> dict[str, Any] | None:
        document = message.get("document")
        if document:
            file_id = document.get("file_id")
            if not file_id:
                return None
            file_unique_id = document.get("file_unique_id") or file_id
            file_name = document.get("file_name") or f"document-{file_unique_id}"
            return {
                "kind": "document",
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "file_name": file_name,
                "mime_type": document.get("mime_type") or "application/octet-stream",
                "file_size": int(document["file_size"]) if document.get("file_size") else None,
            }

        photos = message.get("photo") or []
        if photos:
            photo = max(
                photos,
                key=lambda item: item.get("file_size") or item.get("width", 0) * item.get("height", 0),
            )
            file_id = photo.get("file_id")
            if not file_id:
                return None
            file_unique_id = photo.get("file_unique_id") or file_id
            return {
                "kind": "photo",
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "file_name": f"photo-{file_unique_id}.jpg",
                "mime_type": "image/jpeg",
                "file_size": int(photo["file_size"]) if photo.get("file_size") else None,
            }

        return None

    def ask_agent(
        self,
        chat_id: int,
        user_id: int | None,
        prompt: str,
        source: str,
        source_path: str,
    ) -> None:
        prompt = prompt.strip()
        if not prompt:
            self.send(chat_id, "Usage: /ask <text>")
            return
        project = self.current_project(chat_id)
        if project is None:
            self.send(chat_id, "No context selected and root context is missing. Run /refresh.")
            return

        self.ask_agent_for_project(chat_id, user_id, project, prompt, source, source_path)

    def queue_agent_prompt(
        self,
        chat_id: int,
        user_id: int | None,
        prompt: str,
        source: str,
        source_path: str,
    ) -> None:
        prompt = prompt.strip()
        if not prompt:
            self.send(chat_id, "Usage: /ask <text>")
            return
        project = self.current_project(chat_id)
        if project is None:
            self.send(chat_id, "No context selected and root context is missing. Run /refresh.")
            return
        self.queue_prompt_draft(
            chat_id,
            user_id,
            project,
            prompt,
            action="agent_chat",
            source=source,
            source_path=source_path,
        )

    def ask_agent_for_project(
        self,
        chat_id: int,
        user_id: int | None,
        project: ProjectInfo,
        prompt: str,
        source: str,
        source_path: str,
        context_project_slugs: list[str] | None = None,
    ) -> None:
        prompt = prompt.strip()
        if not prompt:
            self.send(chat_id, "Usage: /ask <text>")
            return
        task_context_slugs = normalize_context_project_slugs(
            project.slug,
            context_project_slugs or self.task_context_project_slugs(chat_id, project),
        )

        task = self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=prompt,
            context_project_slugs=task_context_slugs,
            kind="agent_chat",
            source=source,
            source_path=source_path,
        )
        self.append_event(
            task.id,
            "agent_message",
            "PM",
            f"Принял read-only agent вопрос для проекта {project.slug}.",
        )
        self.remember_task_context(task, "status")
        status_message_id = self.send_codex_status(task, "agent_running")
        self.spawn(self.run_agent_chat, task.id, status_message_id)

    def run_agent_chat(self, task_id: str, status_message_id: int | None = None) -> None:
        task = self.store.load_task(task_id)
        if task is None:
            return
        project = self.find_project(task.project_slug)
        if project is None:
            task.phase = "failed"
            task.error = "Project disappeared from index."
            self.store.save_task(task)
            self.finish_codex_status(
                LiveStatusHandle(task.chat_id, status_message_id),
                task,
            )
            self.send(task.chat_id, f"Agent failed: {task.error}")
            return

        provider = self.effective_task_provider(task.chat_id)
        log_path = self.store.task_dir(task.id) / "agent.log"
        self.append_event(task.id, "state_change", "Architect", "Read-only agent chat started.")
        status = self.start_codex_status(
            task,
            "agent_running",
            log_path,
            message_id=status_message_id,
        )
        task = self.runner.run_agent_chat(task, project, provider=provider)
        self.remember_task_context(task, "status")
        self.finish_codex_status(status, task)
        final = ""
        if task.final_path and Path(task.final_path).exists():
            final = Path(task.final_path).read_text(encoding="utf-8", errors="replace").strip()

        if task.phase == "agent_completed":
            self.append_event(task.id, "state_change", "System", "Read-only agent response completed.")
            self.send(
                task.chat_id,
                final or "Agent response is empty.",
                reply_markup=agent_response_keyboard(task.id),
            )
        else:
            self.append_event(task.id, "state_change", "System", f"Read-only agent failed: {task.error}")
            log_tail = tail_file(task.run_log_path, max_lines=60)
            self.send(task.chat_id, f"Agent failed: {task.error}\n\nLog tail:\n{log_tail}")

    def create_task_from_agent(self, chat_id: int, agent_task_id: str) -> None:
        agent_task = self.store.load_task(agent_task_id)
        if agent_task is None or agent_task.chat_id != chat_id:
            self.send(chat_id, "Agent message not found.")
            return
        project = self.find_project(agent_task.project_slug)
        if project is None:
            self.send(chat_id, "Project disappeared from index.")
            return
        self.create_task_for_project(
            chat_id,
            agent_task.user_id,
            project,
            agent_task.prompt,
            source=agent_task.source,
            source_path=agent_task.source_path,
            context_project_slugs=agent_task.context_project_slugs,
        )

    def create_task(self, chat_id: int, user_id: int | None, args: str) -> None:
        project, prompt = self.parse_task_args(chat_id, args)
        if project is None or not prompt:
            self.send(
                chat_id,
                "Usage: /task <project> <text>\n"
                "Or set /project <query> first and then use /task <text>.",
            )
            return

        self.queue_prompt_draft(
            chat_id,
            user_id,
            project,
            prompt,
            action="new_task",
            source="text",
            source_path="",
        )

    def create_direct_task(self, chat_id: int, user_id: int | None, args: str) -> None:
        manual_tier, remaining = extract_tier_override(args)
        project, prompt = self.parse_task_args(chat_id, remaining)
        if project is None or not prompt:
            self.send(
                chat_id,
                "Usage: /run [tier=auto|cheap|standard|strong|max] <project> <text>\n"
                "Or set /project <query> first and then use /run <text>.",
            )
            return

        self.queue_prompt_draft(
            chat_id,
            user_id,
            project,
            prompt,
            action="direct_task",
            source="text",
            source_path="",
            manual_tier=manual_tier,
        )

    def create_task_for_project(
        self,
        chat_id: int,
        user_id: int | None,
        project: ProjectInfo,
        prompt: str,
        source: str,
        source_path: str,
        context_project_slugs: list[str] | None = None,
    ) -> None:
        task_context_slugs = normalize_context_project_slugs(
            project.slug,
            context_project_slugs or self.task_context_project_slugs(chat_id, project),
        )
        task = self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=prompt,
            context_project_slugs=task_context_slugs,
            kind="task",
            source=source,
            source_path=source_path,
        )
        self.append_event(
            task.id,
            "agent_message",
            "PM",
            f"Создал задачу для проекта {project.slug}.",
            {"context_project_slugs": task_context_slugs},
        )
        state = self.store.load_chat_state(chat_id)
        state.pending_action = None
        state.last_task_id = task.id
        self.store.save_chat_state(state)
        status_message_id = self.send_codex_status(
            task,
            "planning",
            f"Задача создана для {project.slug}. Запускаю read-only planning.",
            reply_markup=created_task_keyboard(task.id),
        )
        self.spawn(self.plan_task, task.id, status_message_id)

    def create_direct_task_for_project(
        self,
        chat_id: int,
        user_id: int | None,
        project: ProjectInfo,
        prompt: str,
        source: str,
        source_path: str,
        context_project_slugs: list[str] | None = None,
        manual_tier: str = "auto",
    ) -> None:
        task_context_slugs = normalize_context_project_slugs(
            project.slug,
            context_project_slugs or self.task_context_project_slugs(chat_id, project),
        )
        task = self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=prompt,
            context_project_slugs=task_context_slugs,
            kind="direct_task",
            source=source,
            source_path=source_path,
        )
        task.model_routing["manual_tier"] = manual_tier
        self.store.save_task(task)
        self.append_event(
            task.id,
            "agent_message",
            "PM",
            f"Создал direct execution task для проекта {project.slug}.",
            {"context_project_slugs": task_context_slugs},
        )
        state = self.store.load_chat_state(chat_id)
        state.pending_action = None
        state.last_task_id = task.id
        self.store.save_chat_state(state)
        provider = self.effective_task_provider(chat_id)
        if self.effective_orchestrator_mode(chat_id):
            executor_name = executor_display_name(provider)
            activity = (
                "Orchestrator mode включён.\n\n"
                "1. ModelRouter выберет минимально достаточный tier.\n"
                "2. Claude подготовит план, если это нужно.\n"
                f"3. {executor_name} выполнит реализацию.\n"
                "4. Claude проверит diff, если review нужен."
            )
        else:
            activity = (
                f"Direct execution task {task.id} created for {project.slug}. "
                f"Запускаю {provider} execution."
            )
        status_message_id = self.send_codex_status(
            task,
            "running",
            activity,
            reply_markup=running_task_keyboard(task.id),
        )
        self.spawn(self.execute_task, task.id, status_message_id)

    def create_followup_task_for_parent(
        self,
        chat_id: int,
        user_id: int | None,
        parent_task_id: str,
        prompt: str,
        source: str,
        source_path: str,
    ) -> None:
        parent = self.completed_task_for_continuation(chat_id, parent_task_id)
        if parent is None:
            return

        active = self.active_followup_for_parent(chat_id, parent.id)
        if active is not None:
            self.send(chat_id, f"Continuation already active: {active.id} ({active.phase}).")
            return

        project = self.find_project(parent.project_slug)
        if project is None:
            self.send(chat_id, "Project disappeared from index.")
            return

        task = self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=prompt,
            context_project_slugs=normalize_context_project_slugs(
                project.slug,
                parent.context_project_slugs,
            ),
            kind="followup_task",
            source=source,
            source_path=source_path,
            parent_task_id=parent.id,
            codex_session_id=parent.codex_session_id,
            claude_session_id=parent.claude_session_id,
        )
        self.append_event(
            task.id,
            "agent_message",
            "PM",
            f"Создал continuation task от {parent.id}.",
            {"parent_task_id": parent.id},
        )
        state = self.store.load_chat_state(chat_id)
        if state.pending_action == continue_task_action(parent.id):
            state.pending_action = None
        state.last_task_id = task.id
        self.store.save_chat_state(state)
        status_message_id = self.send_codex_status(
            task,
            "running",
            f"Continuation task {task.id} created from {parent.id}. Resuming Codex session.",
            reply_markup=running_task_keyboard(task.id),
        )
        self.spawn(self.execute_followup_task, task.id, status_message_id)

    def plan_task(self, task_id: str, status_message_id: int | None = None) -> None:
        task = self.store.load_task(task_id)
        if task is None:
            return
        project = self.find_project(task.project_slug)
        if project is None:
            task.phase = "failed"
            task.error = "Project disappeared from index."
            self.store.save_task(task)
            self.finish_codex_status(
                LiveStatusHandle(task.chat_id, status_message_id),
                task,
            )
            self.send(task.chat_id, f"Task {task.id} failed: {task.error}")
            return

        provider = self.effective_task_provider(task.chat_id)
        log_path = self.store.task_dir(task.id) / "plan.log"
        self.append_event(task.id, "state_change", "Architect", "Запущено read-only planning.")
        self.remember_task_context(task, "status")
        status = self.start_codex_status(
            task,
            "planning",
            log_path,
            message_id=status_message_id,
        )
        task = self.runner.run_planning(task, project, provider=provider)
        self.remember_task_context(task, "plan" if task.phase == "planned" else "status")
        self.finish_codex_status(status, task)
        if task.phase == "planned":
            self.append_event(task.id, "state_change", "Architect", "Planning completed.")
            self.append_event(
                task.id,
                "agent_message",
                executor_agent_name(provider),
                "Ожидаю подтверждения выполнения.",
            )
            self.send(
                task.chat_id,
                f"Plan for task {task.id}:\n\n{task.plan_text}",
                reply_markup=inline_task_keyboard(task.id),
            )
        else:
            self.append_event(
                task.id,
                "state_change",
                "Architect",
                f"Planning failed: {task.error}",
            )
            log_tail = tail_file(task.plan_log_path, max_lines=40)
            self.send(
                task.chat_id,
                f"Planning failed for task {task.id}: {task.error}\n\nLog tail:\n{log_tail}",
                reply_markup=failed_task_keyboard(task.id),
            )

    def answer_task(self, chat_id: int, args: str) -> None:
        task_id, _, answer = args.partition(" ")
        if not task_id or not answer.strip():
            self.send(chat_id, "Usage: /answer <task_id> <text>")
            return

        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return
        if task.phase not in ANSWERABLE_PHASES:
            self.send(chat_id, f"Task is {task.phase}; cannot add clarification now.")
            return

        task.clarifications.append(answer.strip())
        state = self.store.load_chat_state(chat_id)
        if pending_task_id(state.pending_action, "answer_task") == task.id:
            state.pending_action = None
            self.store.save_chat_state(state)
        if task.phase in PROCESS_PHASES:
            self.store.save_task(task)
            self.append_event(task.id, "agent_message", "PM", "Context added to active task.")
            self.send_codex_status(
                task,
                task.phase,
                f"Контекст сохранен для активной задачи {task.id}. Новая задача не создана.",
                reply_markup=running_task_keyboard(task.id),
            )
            return

        task.phase = "created"
        task.plan_text = ""
        task.error = ""
        self.store.save_task(task)
        self.append_event(task.id, "agent_message", "PM", "Clarification added; planning will restart.")
        status_message_id = self.send_codex_status(
            task,
            "planning",
            f"Уточнение сохранено для {task.id}. Перезапускаю planning.",
            reply_markup=created_task_keyboard(task.id),
        )
        self.spawn(self.plan_task, task.id, status_message_id)

    def confirm_task(self, chat_id: int, task_id: str) -> None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return
        if task.phase != "planned":
            self.send(chat_id, f"Task {task.id} is {task.phase}; it can be confirmed only after planning.")
            return

        state = self.store.load_chat_state(chat_id)
        if pending_task_id(state.pending_action, "attach_files") == task.id:
            state.pending_action = None
            self.store.save_chat_state(state)
        provider = self.effective_task_provider(chat_id)
        agent_name = executor_agent_name(provider)
        self.append_event(task.id, "state_change", agent_name, "Execution confirmed by user.")
        status_message_id = self.send_codex_status(
            task,
            "running",
            f"Задача {task.id} подтверждена. Запускаю {provider} execution.",
            reply_markup=running_task_keyboard(task.id),
        )
        self.spawn(self.execute_task, task.id, status_message_id)

    def execute_task(
        self,
        task_id: str,
        status_message_id: int | None = None,
        recover: bool = False,
    ) -> None:
        task = self.store.load_task(task_id)
        if task is None:
            return
        project = self.find_project(task.project_slug)
        if project is None:
            task.phase = "failed"
            task.error = "Project disappeared from index."
            self.store.save_task(task)
            self.finish_codex_status(
                LiveStatusHandle(task.chat_id, status_message_id),
                task,
            )
            self.send(task.chat_id, f"Task {task.id} failed: {task.error}")
            return

        log_path = self.store.task_dir(task.id) / "run.log"
        provider = self.effective_task_provider(task.chat_id)
        agent_name = executor_agent_name(provider)
        task.executor_provider = provider
        self.store.save_task(task)
        self.append_event(task.id, "state_change", agent_name, "Execution started.")
        pre_snapshot = self.record_git_safety_snapshot(task, "pre-execution")
        if pre_snapshot.is_dirty:
            self.send(
                task.chat_id,
                f"⚠️ Safety warning for {task.id}: {snapshot_summary(pre_snapshot)}",
                reply_markup=running_task_keyboard(task.id),
            )
        status = self.start_codex_status(
            task,
            "running",
            log_path,
            message_id=status_message_id,
        )
        orchestrator_enabled = self.effective_orchestrator_mode(task.chat_id)
        if orchestrator_enabled:
            task = self.orchestrator.execute(
                task,
                project,
                orchestrator_enabled=True,
                recover=recover,
                executor_provider=provider,
                notify=lambda text: self.send(
                    task.chat_id,
                    text,
                    reply_markup=running_task_keyboard(task.id),
                ),
                append_event=lambda task_id, event_type, agent, message, data=None: self.append_event(
                    task_id,
                    event_type,
                    agent,
                    message,
                    data,
                ),
            )
        elif recover:
            task = self.runner.run_recovery_execution(task, project, provider=provider)
        else:
            task = self.runner.run_execution(task, project, provider=provider)
        post_snapshot = self.record_git_safety_snapshot(task, "post-execution")
        if self.effective_memory_mode(task.chat_id):
            self.memory_engine.remember_completed_task(
                task,
                final_text=read_task_final(task),
                snapshot=post_snapshot,
            )
        self.remember_task_context(task, "status")
        self.finish_codex_status(status, task)
        final = read_task_final(task)

        if task.phase == "completed":
            self.append_event(task.id, "state_change", "System", "Execution completed successfully.")
            message = f"Task {task.id} completed.\n\n{final or 'Final response is empty.'}"
            if post_snapshot.is_dirty:
                message += f"\n\nSafety: {snapshot_summary(post_snapshot)}"
            reply_markup = completed_task_keyboard(task.id)
        elif task.phase == "needs_human":
            self.append_event(task.id, "state_change", "System", f"Needs human review: {task.error}")
            message = (
                "Claude reviewer всё ещё видит проблемы после "
                f"{task.orchestrator_review_rounds or self.config.orchestrator_max_review_rounds} "
                "раундов исправлений.\nНужна ручная проверка.\n\n"
                f"Summary:\n{task.error or final or '-'}"
            )
            reply_markup = failed_task_keyboard(task.id)
        else:
            self.append_event(task.id, "state_change", "System", f"Execution failed: {task.error}")
            log_tail = tail_file(task.run_log_path, max_lines=60)
            message = (
                f"Task {task.id} failed: {task.error}\n\n"
                f"Final:\n{final or '-'}\n\nLog tail:\n{log_tail}"
            )
            reply_markup = failed_task_keyboard(task.id)
        self.send(task.chat_id, message, reply_markup=reply_markup)

    def execute_followup_task(
        self,
        task_id: str,
        status_message_id: int | None = None,
    ) -> None:
        task = self.store.load_task(task_id)
        if task is None:
            return
        project = self.find_project(task.project_slug)
        if project is None:
            task.phase = "failed"
            task.error = "Project disappeared from index."
            self.store.save_task(task)
            self.finish_codex_status(
                LiveStatusHandle(task.chat_id, status_message_id),
                task,
            )
            self.send(task.chat_id, f"Task {task.id} failed: {task.error}")
            return

        provider = self.effective_task_provider(task.chat_id)
        log_path = self.store.task_dir(task.id) / "run.log"
        self.append_event(
            task.id,
            "state_change",
            executor_agent_name(provider),
            "Continuation execution started.",
        )
        pre_snapshot = self.record_git_safety_snapshot(task, "pre-execution")
        if pre_snapshot.is_dirty:
            self.send(
                task.chat_id,
                f"⚠️ Safety warning for {task.id}: {snapshot_summary(pre_snapshot)}",
                reply_markup=running_task_keyboard(task.id),
            )
        status = self.start_codex_status(
            task,
            "running",
            log_path,
            message_id=status_message_id,
        )
        task = self.runner.run_followup_execution(task, project, provider=provider)
        post_snapshot = self.record_git_safety_snapshot(task, "post-execution")
        if self.effective_memory_mode(task.chat_id):
            self.memory_engine.remember_completed_task(
                task,
                final_text=read_task_final(task),
                snapshot=post_snapshot,
            )
        self.remember_task_context(task, "status")
        self.finish_codex_status(status, task)
        final = read_task_final(task)

        if task.phase == "completed":
            self.append_event(task.id, "state_change", "System", "Continuation completed successfully.")
            message = f"Task {task.id} completed.\n\n{final or 'Final response is empty.'}"
            if post_snapshot.is_dirty:
                message += f"\n\nSafety: {snapshot_summary(post_snapshot)}"
            reply_markup = completed_task_keyboard(task.id)
        else:
            self.append_event(task.id, "state_change", "System", f"Continuation failed: {task.error}")
            log_tail = tail_file(task.run_log_path, max_lines=60)
            message = (
                f"Task {task.id} failed: {task.error}\n\n"
                f"Final:\n{final or '-'}\n\nLog tail:\n{log_tail}"
            )
            reply_markup = failed_task_keyboard(task.id)
        self.send(task.chat_id, message, reply_markup=reply_markup)

    def cancel_task(self, chat_id: int, task_id: str) -> None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return

        if task.pid and task.phase in PROCESS_PHASES:
            terminate_process_group(task.pid)
        task.phase = "canceled"
        task.error = "Canceled by user."
        task.pid = None
        self.store.save_task(task)
        self.append_event(task.id, "state_change", "System", "Task cancelled by user.")
        self.remember_task_context(task, "status")
        state = self.store.load_chat_state(chat_id)
        if (
            pending_task_id(state.pending_action, "attach_files") == task.id
            or pending_task_id(state.pending_action, "answer_task") == task.id
            or pending_task_id(state.pending_action, "followup_context") == task.id
            or (
                bool(task.parent_task_id)
                and state.pending_action == continue_task_action(task.parent_task_id)
            )
        ):
            state.pending_action = None
            self.store.save_chat_state(state)
        self.send(chat_id, f"Task {task.id} canceled.")

    def project_from_task_record(self, task: TaskRecord) -> ProjectInfo:
        return ProjectInfo(
            slug=task.project_slug,
            name=task.project_name,
            path=task.project_path,
            base=str(Path(task.project_path).parent),
            is_git=is_git_repo(task.project_path),
            branch=None,
            origin=None,
            languages=[],
            markers=[],
            docs=[],
            test_hints=[],
        )

    def force_push_target_project(self, task: TaskRecord) -> ProjectInfo:
        primary = self.find_project(task.project_slug)
        if primary is not None and is_git_repo(primary.path):
            return primary
        if is_git_repo(task.project_path):
            return primary or self.project_from_task_record(task)

        candidates: list[ProjectInfo] = []
        for slug in task.context_project_slugs:
            if slug in {ROOT_PROJECT_SLUG, task.project_slug}:
                continue
            project = self.find_project(slug)
            if (
                project is not None
                and project.slug not in {item.slug for item in candidates}
                and is_git_repo(project.path)
            ):
                candidates.append(project)

        if len(candidates) == 1:
            return candidates[0]
        return primary or self.project_from_task_record(task)

    def force_push_task(self, chat_id: int, task_id: str) -> None:
        task = self.recent_context_task(chat_id, task_id, phases={"completed"})
        if task is None:
            self.send(chat_id, "Task not found or not completed.", reply_markup=tasks_keyboard())
            return

        project = self.force_push_target_project(task)
        force_task = self.store.create_task(
            chat_id=chat_id,
            user_id=task.user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=(
                f"Force push the current task branch for completed Telegram task {task.id}. "
                "Use git push --force-with-lease only after verifying branch and upstream."
            ),
            context_project_slugs=normalize_context_project_slugs(
                project.slug,
                task.context_project_slugs,
            ),
            kind="force_push_task",
            source="button",
            source_path="",
            parent_task_id=task.id,
        )
        self.append_event(
            task.id,
            "safety",
            "System",
            f"Force push agent launched as task {force_task.id}.",
            {"force_push_task_id": force_task.id, "project_path": force_task.project_path},
        )
        self.append_event(
            force_task.id,
            "state_change",
            "PM",
            f"Force push requested from completed task {task.id}.",
            {"parent_task_id": task.id},
        )
        state = self.store.load_chat_state(chat_id)
        state.last_task_id = force_task.id
        self.store.save_chat_state(state)
        status_message_id = self.send_codex_status(
            force_task,
            "running",
            (
                f"Force push task {force_task.id} created from {task.id}. "
                f"Запускаю {executor_display_name(self.effective_task_provider(chat_id))} "
                "с правами danger-full-access."
            ),
            reply_markup=running_task_keyboard(force_task.id),
        )
        self.spawn(self.execute_force_push_task, force_task.id, status_message_id)

    def execute_force_push_task(
        self,
        task_id: str,
        status_message_id: int | None = None,
    ) -> None:
        task = self.store.load_task(task_id)
        if task is None:
            return
        parent = self.store.load_task(task.parent_task_id) if task.parent_task_id else None
        project = self.find_project(task.project_slug) or self.project_from_task_record(task)

        provider = self.effective_task_provider(task.chat_id)
        log_path = self.store.task_dir(task.id) / "force-push.log"
        self.append_event(
            task.id,
            "state_change",
            executor_agent_name(provider),
            "Force push agent started.",
        )
        status = self.start_codex_status(
            task,
            "running",
            log_path,
            message_id=status_message_id,
        )
        task = self.runner.run_force_push_agent(task, project, parent, provider=provider)
        self.remember_task_context(task, "status")
        self.finish_codex_status(status, task)
        final = read_task_final(task)
        parent_task_id = parent.id if parent is not None else task.id

        if task.phase == "completed":
            self.append_event(task.id, "state_change", "System", "Force push agent completed.")
            message = f"Force push task {task.id} completed.\n\n{final or 'Final response is empty.'}"
            reply_markup = completed_task_keyboard(parent_task_id)
        else:
            self.append_event(task.id, "state_change", "System", f"Force push agent failed: {task.error}")
            log_tail = tail_file(task.run_log_path, max_lines=60)
            message = (
                f"Force push task {task.id} failed: {task.error}\n\n"
                f"Final:\n{final or '-'}\n\nLog tail:\n{log_tail}"
            )
            reply_markup = failed_task_keyboard(task.id)
        self.send(task.chat_id, message, reply_markup=reply_markup)

    def handle_task_callback(
        self,
        chat_id: int,
        user_id: int | None,
        action: str,
        task_id: str,
    ) -> None:
        task = self.recent_context_task(chat_id, task_id)
        if task is None:
            self.send(chat_id, "Task not found.", reply_markup=tasks_keyboard())
            return
        if action == "brain":
            self.show_task_brain(chat_id, task.id)
        elif action == "plan":
            self.show_task_plan(chat_id, task.id)
        elif action == "status":
            self.show_task(chat_id, task.id)
        elif action == "logs":
            self.logs(chat_id, task.id)
        elif action == "safety":
            self.show_task_safety(chat_id, task.id)
        elif action == "review":
            self.show_task_review(chat_id, task.id)
        elif action == "trace":
            self.show_task_trace(chat_id, task.id)
        elif action == "routing":
            self.show_task_routing(chat_id, task.id)
        elif action == "execute":
            self.confirm_task(chat_id, task.id)
        elif action == "continue":
            self.start_continue_input(chat_id, task.id)
        elif action == "cancel":
            self.cancel_task(chat_id, task.id)
        elif action == "files":
            self.start_file_attachment_input(chat_id, task.id)
        elif action == "answer":
            self.start_answer_input(chat_id, task.id)
        elif action == "remember":
            self.propose_task_memory(chat_id, user_id, task.id)
        elif action == "force_push":
            self.force_push_task(chat_id, task.id)

    def show_task_brain(self, chat_id: int, task_id: str) -> None:
        task = self.recent_context_task(chat_id, task_id)
        if task is None:
            self.send(chat_id, "Task not found.", reply_markup=tasks_keyboard())
            return
        events = self.events.list_events(task.id, limit=50)
        self.set_last_view(chat_id, "brain")
        self.remember_task_context(task, "brain")
        self.send(
            chat_id,
            render_task_events(task.id, events),
            reply_markup=self.task_view_keyboard(task),
        )

    def show_task_plan(self, chat_id: int, task_id: str) -> None:
        task = self.recent_context_task(chat_id, task_id)
        if task is None:
            self.send(chat_id, "Task not found.", reply_markup=tasks_keyboard())
            return
        plan = task.plan_text.strip() or f"Plan is not available yet. Phase: {task.phase}"
        self.set_last_view(chat_id, "plan")
        self.remember_task_context(task, "plan")
        self.send(chat_id, f"📋 План {task.id}:\n\n{plan[:3500]}", reply_markup=self.task_view_keyboard(task))

    def show_task_safety(self, chat_id: int, task_id: str) -> None:
        task = self.recent_context_task(chat_id, task_id)
        if task is None:
            self.send(chat_id, "Task not found.", reply_markup=tasks_keyboard())
            return
        task_dir = self.store.task_dir(task.id)
        snapshots = [
            snapshot
            for snapshot in [
                read_git_snapshot(task_dir, "pre-execution"),
                read_git_snapshot(task_dir, "post-execution"),
            ]
            if snapshot is not None
        ]
        if not snapshots:
            current = capture_git_snapshot(task.project_path, "current")
            snapshots = [current]
        lines = [f"🛡 Safety for {task.id}"]
        for snapshot in snapshots:
            lines.extend(["", f"{snapshot.label}: {snapshot_summary(snapshot)}"])
            if snapshot.status_short:
                lines.append("Status:")
                lines.append(snapshot.status_short[:1200])
            if snapshot.diff_stat:
                lines.append("Diff stat:")
                lines.append(snapshot.diff_stat[:1200])
            if snapshot.error:
                lines.append(f"Git note: {snapshot.error[:500]}")
        self.set_last_view(chat_id, "safety")
        self.remember_task_context(task, "safety")
        self.send(chat_id, "\n".join(lines)[:4000], reply_markup=self.task_view_keyboard(task))

    def show_task_review(self, chat_id: int, task_id: str) -> None:
        task = self.recent_context_task(chat_id, task_id)
        if task is None:
            self.send(chat_id, "Task not found.", reply_markup=tasks_keyboard())
            return
        self.append_event(task.id, "review", "Reviewer", "Review requested from Telegram UI.")
        self.set_last_view(chat_id, "review")
        self.remember_task_context(task, "review")
        self.send(
            chat_id,
            f"🔍 Review for {task.id}\nReview role is prepared as metadata. Automated reviewer execution is not enabled in this increment.",
            reply_markup=self.task_view_keyboard(task),
        )

    def show_task_trace(self, chat_id: int, task_id: str) -> None:
        task = self.recent_context_task(chat_id, task_id)
        if task is None:
            self.send(chat_id, "Task not found.", reply_markup=tasks_keyboard())
            return
        trace_path = self.store.task_dir(task.id) / "trace.json"
        if not trace_path.exists():
            self.send(chat_id, f"Trace for {task.id}: not available yet.", reply_markup=self.task_view_keyboard(task))
            return
        state = self.store.load_chat_state(chat_id)
        trace = trace_path.read_text(encoding="utf-8", errors="replace")
        if not state.debug_mode:
            try:
                payload = json.loads(trace)
                text = "\n".join(
                    [
                        f"Trace {task.id}",
                        f"complexity: {payload.get('complexity')}",
                        f"selected_flow: {payload.get('selected_flow')}",
                        f"review_verdict: {payload.get('review_verdict') or '-'}",
                        f"final_status: {payload.get('final_status')}",
                    ]
                )
            except json.JSONDecodeError:
                text = trace[:1200]
        else:
            text = trace[:3500]
        self.set_last_view(chat_id, "trace")
        self.remember_task_context(task, "trace")
        self.send(chat_id, text, reply_markup=self.task_view_keyboard(task))

    def show_task_routing(self, chat_id: int, task_id: str) -> None:
        task = self.recent_context_task(chat_id, task_id)
        if task is None:
            self.send(chat_id, "Task not found.", reply_markup=tasks_keyboard())
            return
        if not task.model_routing:
            decision = self.model_router.route(task)
            text = render_routing_decision(decision)
        else:
            text = render_task_routing(task.model_routing)
        self.set_last_view(chat_id, "routing")
        self.remember_task_context(task, "routing")
        self.send(chat_id, text[:3500], reply_markup=self.task_view_keyboard(task))

    def task_view_keyboard(self, task: TaskRecord) -> dict[str, Any]:
        return task_progress_keyboard(task.id, task.phase)

    def record_git_safety_snapshot(self, task: TaskRecord, label: str) -> GitSnapshot:
        snapshot = capture_git_snapshot(task.project_path, label)
        path = write_git_snapshot(self.store.task_dir(task.id), snapshot)
        self.append_event(
            task.id,
            "safety",
            "Safety",
            snapshot_summary(snapshot),
            {
                "label": label,
                "artifact_path": str(path),
                "is_git_repo": snapshot.is_git_repo,
                "branch": snapshot.branch,
                "dirty_files": snapshot.dirty_files,
            },
        )
        return snapshot

    def show_tasks(self, chat_id: int, status_filter: str) -> None:
        phases = TASK_FILTERS.get(status_filter)
        if status_filter not in TASK_FILTERS:
            status_filter = "active"
            phases = TASK_FILTERS[status_filter]
        project = self.current_project(chat_id)
        project_slug = project.slug if project else None
        tasks = self.store.recent_tasks(
            chat_id,
            limit=12,
            phases=phases,
            project_slug=project_slug,
        )
        if not tasks:
            self.send(
                chat_id,
                f"No tasks for filter: {status_filter}\nContext: {project_slug or 'any'}",
                reply_markup=tasks_keyboard(),
            )
            return

        lines = [f"Tasks: {status_filter}", f"Context: {project_slug or 'any'}"]
        rows: list[list[dict[str, str]]] = []
        for task in tasks:
            lines.append("")
            lines.append(short_task(task))
            rows.append(
                [
                    {
                        "text": f"{task.phase}: {task.id[-9:]}",
                        "callback_data": f"task:{task.id}",
                    }
                ]
            )
        rows.extend(tasks_keyboard()["inline_keyboard"])
        self.send(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": rows})

    def show_task_matches(
        self,
        chat_id: int,
        tasks: list[TaskRecord],
        title: str,
    ) -> None:
        rows: list[list[dict[str, str]]] = []
        lines = [title]
        for index, task in enumerate(tasks, start=1):
            lines.extend(["", f"{index}. {short_task(task)}"])
            rows.append(
                [
                    {
                        "text": f"{index}. {task.phase}: {task.id[-9:]}",
                        "callback_data": f"task:{task.id}",
                    }
                ]
            )
        rows.append([{"text": "📋 Задачи", "callback_data": "tasks:active"}])
        self.send(chat_id, "\n".join(lines)[:4000], reply_markup={"inline_keyboard": rows})

    def show_task(self, chat_id: int, task_id: str) -> None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return

        lines = [
            f"Task {task.id}",
            f"Kind: {task.kind}",
            f"Phase: {task.phase}",
            f"Project: {task.project_slug}",
            f"Source: {task.source}",
            f"Parent: {task.parent_task_id}" if task.parent_task_id else "Parent: -",
            f"Attachments: {len(task.attachments)}",
            "",
            task.prompt[:1500],
        ]
        for attachment in task.attachments[:5]:
            lines.append(f"- {attachment.get('file_name', 'attachment')}")
        self.remember_task_context(task, "status")
        if task.kind == "followup_task" and task.parent_task_id and task.phase == "created":
            reply_markup = followup_draft_keyboard(task.parent_task_id, task.id)
        else:
            reply_markup = self.task_view_keyboard(task)
        self.send(chat_id, "\n".join(lines), reply_markup=reply_markup)

    def status(self, chat_id: int, task_id: str) -> None:
        if task_id:
            self.show_task(chat_id, task_id)
            return

        self.show_processes(chat_id)

    def show_processes(self, chat_id: int) -> None:
        tasks = self.store.recent_tasks(
            chat_id,
            limit=50,
            phases=PROCESS_PHASES,
            project_slug=None,
        )
        self.send(
            chat_id,
            process_summary(tasks),
            reply_markup=main_menu_keyboard(
                self.store.load_chat_state(chat_id).agent_mode
            ),
        )

    def logs(self, chat_id: int, task_id: str) -> None:
        task = self.store.load_task(task_id)
        if task is None or task.chat_id != chat_id:
            self.send(chat_id, "Task not found.")
            return

        log_path = task.run_log_path or task.plan_log_path
        self.set_last_view(chat_id, "logs")
        self.remember_task_context(task, "logs")
        self.send(
            chat_id,
            f"Log tail for {task.id}:\n\n{tail_file(log_path)}",
            reply_markup=self.task_view_keyboard(task),
        )

    def run(self) -> None:
        self.load_or_build_index()
        self.configure_telegram_menu()
        self.send_startup_message()
        self.recover_interrupted_tasks()
        self.resume_prompt_drafts()
        self.start_slack_watcher()
        self.start_slack_desktop_watcher()

        while not self.stop_event.is_set():
            try:
                updates = self.api.get_updates(self.offset, self.config.poll_timeout_seconds)
                for update in updates:
                    self.offset = int(update["update_id"]) + 1
                    self.handle_update(update)
                self.cleanup_threads()
            except Exception:
                LOG.exception("polling failed")
                time.sleep(5)

    def send_startup_message(self) -> None:
        text = (
            "Codex Telegram bot started.\n"
            f"Indexed projects: {len(self.projects)}\n"
            "Use /help."
        )
        for chat_id in self.config.allowed_chat_ids:
            state = self.store.load_chat_state(chat_id)
            self.send(chat_id, text, reply_markup=main_menu_keyboard(state.agent_mode))

    def stop(self, *_args: Any) -> None:
        self.stop_event.set()
        raise SystemExit(0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local Codex Telegram bot.")
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Rebuild project index and exit.",
    )
    parser.add_argument(
        "--daily-summary",
        action="store_true",
        help="Send the previous workday activity summary and exit.",
    )
    parser.add_argument(
        "--summary-date",
        type=date.fromisoformat,
        help="Summarize this date instead of the previous workday (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the daily summary without sending it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.daily_summary:
        from .daily_summary import run_daily_summary_from_env

        result = run_daily_summary_from_env(
            target=args.summary_date,
            dry_run=args.dry_run,
        )
        print(result if args.dry_run else "Daily summary processing completed.")
        return 0

    config = Config.from_env()
    configure_logging(config.state_dir)

    if args.reindex:
        projects = build_index(
            config.project_roots,
            config.index_dir,
            config.workspace_root,
        )
        print(f"Indexed {len(projects)} projects into {config.index_dir}")
        return 0

    config.validate_for_bot()
    bot = CodexTelegramBot(config)
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
