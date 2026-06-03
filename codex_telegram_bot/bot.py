from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .activity_status import extract_codex_activity, render_codex_status
from .config import Config
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
    SlackNotification,
    SlackStateStore,
    SlackWatcher,
    render_slack_notification,
)
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
/ask <text> - ask Codex in read-only agent mode
/task <project> <text> - create a Codex task in a project
/task <text> - create a task in the selected default project
/run <project> <text> - execute a task directly without read-only planning
/run <text> - execute directly in the selected default project
/answer <task_id> <text> - add clarification and rerun planning
/confirm <task_id> - execute a planned task
/continue <task_id> <text> - continue a completed task in its Codex session
/cancel <task_id> - cancel a task or stop a running Codex process
/status [task_id] - show global active processes or one task
/processes - show active Codex processes across projects
/logs <task_id> - show recent execution log lines

Flow:
1. Choose root or project with buttons.
2. Press New Task or send /task.
3. Bot runs read-only Codex planning and asks for missing requirements.
4. Press Add files to attach documents/photos, or Answer clarification if needed.
5. Send /confirm or press Execute to start real execution.
Direct execution: press Run task or send /run for an explicit no-planning run.
"""

BOT_COMMANDS = [
    {"command": "menu", "description": "Show main menu"},
    {"command": "new", "description": "Start a planned Codex task"},
    {"command": "run", "description": "Run a task directly"},
    {"command": "task", "description": "Create a planned Codex task"},
    {"command": "projects", "description": "List indexed projects"},
    {"command": "project", "description": "Select default project"},
    {"command": "status", "description": "Show processes or task status"},
    {"command": "processes", "description": "Show active Codex processes"},
    {"command": "agent", "description": "Toggle agent mode"},
    {"command": "help", "description": "Show help"},
]

PROJECT_PAGE_SIZE = 8
TASK_FILTERS: dict[str, set[str] | None] = {
    "active": {"created", "planning", "planned", "running", "agent_running"},
    "planned": {"planned"},
    "running": {"planning", "running", "agent_running"},
    "done": {"completed", "agent_completed"},
    "failed": {"failed"},
    "all": None,
}
PROCESS_PHASES = {"planning", "running", "agent_running"}
BLOCKING_FOLLOWUP_PHASES = {"created", "planning", "planned", "running", "agent_running"}
ATTACHABLE_PHASES = {"created", "planned", "failed"}
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
                {"text": "Execute", "callback_data": f"confirm:{task_id}"},
                {"text": "Answer clarification", "callback_data": f"answer:{task_id}"},
            ],
            [
                {"text": "Add files", "callback_data": f"attach:{task_id}"},
                {"text": "Cancel", "callback_data": f"cancel:{task_id}"},
            ],
            [
                {"text": "Task", "callback_data": f"task:{task_id}"},
                {"text": "Menu", "callback_data": "menu"},
            ]
        ]
    }


def completed_task_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Continue", "callback_data": f"continue:{task_id}"},
                {"text": "Add context", "callback_data": f"contctx:{task_id}"},
            ],
            [
                {"text": "Add files", "callback_data": f"contattach:{task_id}"},
                {"text": "Run continuation", "callback_data": f"contrun:{task_id}"},
            ],
            [
                {"text": "Task", "callback_data": f"task:{task_id}"},
                {"text": "Logs", "callback_data": f"logs:{task_id}"},
            ],
            [{"text": "Tasks", "callback_data": "tasks:done"}, {"text": "Menu", "callback_data": "menu"}],
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


def main_menu_keyboard(agent_mode: bool) -> dict[str, Any]:
    agent_label = "Agent: on" if agent_mode else "Agent: off"
    return {
        "inline_keyboard": [
            [
                {"text": "Root", "callback_data": "root"},
                {"text": "Projects", "callback_data": "projects:0"},
            ],
            [
                {"text": "New task", "callback_data": "new"},
                {"text": "Run task", "callback_data": "runnew"},
            ],
            [
                {"text": "Tasks", "callback_data": "tasks:active"},
            ],
            [
                {"text": "Processes", "callback_data": "processes"},
            ],
            [
                {"text": agent_label, "callback_data": "agent:toggle"},
                {"text": "Refresh", "callback_data": "refresh"},
            ],
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
            [{"text": "New task", "callback_data": "new"}, {"text": "Menu", "callback_data": "menu"}],
        ]
    }


def agent_response_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Create task from this", "callback_data": f"taskfrom:{task_id}"}],
            [{"text": "New task", "callback_data": "new"}, {"text": "Menu", "callback_data": "menu"}],
        ]
    }


def command_parts(text: str) -> tuple[str, str]:
    head, _, tail = text.partition(" ")
    command = head.split("@", 1)[0].lower()
    return command, tail.strip()


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


def short_task(task: TaskRecord) -> str:
    suffix = f", rc={task.returncode}" if task.returncode is not None else ""
    return (
        f"{task.id}: {task.phase}{suffix}\n"
        f"Project: {task.project_slug}\n"
        f"Task: {task.prompt[:220]}"
    )


@dataclass(frozen=True)
class PromptDraft:
    chat_id: int
    user_id: int | None
    action: str
    project_slug: str
    parts: tuple[str, ...]
    source: str
    source_path: str


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
        self.projects: list[ProjectInfo] = []
        self.offset: int | None = None
        self.stop_event = threading.Event()
        self.worker_threads: set[threading.Thread] = set()
        self._projects_lock = threading.RLock()
        self._draft_lock = threading.RLock()
        self._draft_timers: dict[int, threading.Timer] = {}
        self.slack_watcher: SlackWatcher | None = None
        if config.slack_enabled() and config.slack_token is not None:
            self.slack_watcher = SlackWatcher(
                api=SlackAPI(config.slack_token),
                state_store=SlackStateStore(config.state_dir),
                watch_dms=config.slack_watch_dms,
                channel_ids=config.slack_channel_ids,
                history_limit=config.slack_history_limit,
                poll_interval_seconds=config.slack_poll_interval_seconds,
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
            return resolve_project(self.projects, query)

    def project_by_index(self, index: int) -> ProjectInfo | None:
        with self._projects_lock:
            if 0 <= index < len(self.projects):
                return self.projects[index]
        return None

    def current_project(self, chat_id: int) -> ProjectInfo | None:
        state = self.store.load_chat_state(chat_id)
        if state.selected_project_slug:
            project = self.find_project(state.selected_project_slug)
            if project is not None:
                return project
        return self.find_project(ROOT_PROJECT_SLUG)

    def current_context_label(self, chat_id: int) -> str:
        project = self.current_project(chat_id)
        if project is None:
            return "not selected"
        return f"{project.slug} ({project.path})"

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

    def send_codex_status(
        self,
        task: TaskRecord,
        phase: str,
        activity: str | None = None,
    ) -> int | None:
        return self.send(task.chat_id, render_codex_status(task, phase, activity))

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

            self.edit_message(task.chat_id, message_id, text)
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
        elif action == "tasks":
            self.show_tasks(chat_id, value or "active")
        elif action == "task":
            self.show_task(chat_id, value)
        elif action == "logs":
            self.logs(chat_id, value)
        elif action == "taskfrom":
            self.create_task_from_agent(chat_id, value)
        elif action == "agent":
            self.toggle_agent_mode(chat_id)
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
        elif command == "/new":
            self.start_new_task_input(chat_id)
        elif command == "/agent":
            self.set_agent_mode(chat_id, args)
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
        else:
            self.send(chat_id, "Unknown command. Use /help.")

    def show_menu(self, chat_id: int) -> None:
        state = self.store.load_chat_state(chat_id)
        mode = "on" if state.agent_mode else "off"
        pending = f"\nPending input: {state.pending_action}" if state.pending_action else ""
        self.send(
            chat_id,
            f"Menu\nContext: {self.current_context_label(chat_id)}\nAgent mode: {mode}{pending}",
            reply_markup=main_menu_keyboard(state.agent_mode),
        )

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
        state = self.store.load_chat_state(chat_id)
        state.selected_project_slug = project.slug
        state.pending_action = None
        self.store.save_chat_state(state)
        self.send(
            chat_id,
            f"Selected context: {project.slug}\n{project.path}",
            reply_markup=main_menu_keyboard(state.agent_mode),
        )

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
        project = self.find_project(query)
        if project is None:
            self.send(chat_id, "Project not found or query is ambiguous. Use /projects <query>.")
            return
        self.select_project_by_record(chat_id, project)

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
        state.prompt_draft_parts = []
        state.prompt_draft_source = "text"
        state.prompt_draft_source_path = ""
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
    ) -> None:
        part = text.strip()
        if not part:
            self.send(chat_id, "Prompt text is empty.")
            return

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
                        state.prompt_draft_source = source
                        state.prompt_draft_source_path = source_path
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
                parts=tuple(state.prompt_draft_parts),
                source=state.prompt_draft_source,
                source_path=state.prompt_draft_source_path,
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
            )
        elif draft.action == "direct_task":
            self.create_direct_task_for_project(
                draft.chat_id,
                draft.user_id,
                project,
                prompt,
                source=draft.source,
                source_path=draft.source_path,
            )
        elif draft.action == "agent_chat":
            self.ask_agent_for_project(
                draft.chat_id,
                draft.user_id,
                project,
                prompt,
                source=draft.source,
                source_path=draft.source_path,
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

    def send_slack_notification(self, notification: SlackNotification) -> None:
        text = render_slack_notification(notification)
        for chat_id in self.config.slack_target_chat_ids:
            if chat_id in self.config.allowed_chat_ids:
                self.send(chat_id, text)

    def handle_plain_text(self, chat_id: int, user_id: int | None, text: str) -> None:
        state = self.store.load_chat_state(chat_id)
        answer_task_id = pending_task_id(state.pending_action, "answer_task")
        if answer_task_id:
            state.pending_action = None
            self.store.save_chat_state(state)
            self.answer_task(chat_id, f"{answer_task_id} {text}")
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
            )
            return

        project = self.current_project(chat_id)
        if project is None:
            self.send(chat_id, "No context selected and root context is missing. Run /refresh.")
            return

        if state.pending_action == "new_task":
            self.queue_prompt_draft(
                chat_id,
                user_id,
                project,
                text,
                action="new_task",
                source="text",
                source_path="",
            )
            return

        if state.pending_action == "direct_task":
            self.queue_prompt_draft(
                chat_id,
                user_id,
                project,
                text,
                action="direct_task",
                source="text",
                source_path="",
            )
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
            "Use buttons, /new, /task, or enable Agent mode.",
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
            self.create_task_for_project(
                chat_id,
                user_id,
                project,
                transcript,
                source="voice",
                source_path=str(destination),
            )
        elif state.pending_action == "direct_task":
            self.create_direct_task_for_project(
                chat_id,
                user_id,
                project,
                transcript,
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
        if not task.codex_session_id and task.run_log_path:
            task.codex_session_id = extract_session_id(Path(task.run_log_path))
            if task.codex_session_id:
                self.store.save_task(task)
        if not task.codex_session_id:
            self.send(chat_id, f"Task {task.id} has no saved Codex session id; cannot continue it.")
            return None
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
            kind="followup_task",
            source="text",
            source_path="",
            parent_task_id=parent.id,
            codex_session_id=parent.codex_session_id,
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
        )
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
        if task.phase not in ATTACHABLE_PHASES:
            self.send(chat_id, f"Task is {task.phase}; cannot add clarification now.")
            return

        state = self.store.load_chat_state(chat_id)
        state.pending_action = f"answer_task:{task.id}"
        self.store.save_chat_state(state)
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
    ) -> None:
        prompt = prompt.strip()
        if not prompt:
            self.send(chat_id, "Usage: /ask <text>")
            return

        task = self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=prompt,
            kind="agent_chat",
            source=source,
            source_path=source_path,
        )
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

        log_path = self.store.task_dir(task.id) / "agent.log"
        status = self.start_codex_status(
            task,
            "agent_running",
            log_path,
            message_id=status_message_id,
        )
        task = self.runner.run_agent_chat(task, project)
        self.finish_codex_status(status, task)
        final = ""
        if task.final_path and Path(task.final_path).exists():
            final = Path(task.final_path).read_text(encoding="utf-8", errors="replace").strip()

        if task.phase == "agent_completed":
            self.send(
                task.chat_id,
                final or "Agent response is empty.",
                reply_markup=agent_response_keyboard(task.id),
            )
        else:
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
        project, prompt = self.parse_task_args(chat_id, args)
        if project is None or not prompt:
            self.send(
                chat_id,
                "Usage: /run <project> <text>\n"
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
        )

    def create_task_for_project(
        self,
        chat_id: int,
        user_id: int | None,
        project: ProjectInfo,
        prompt: str,
        source: str,
        source_path: str,
    ) -> None:
        task = self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=prompt,
            kind="task",
            source=source,
            source_path=source_path,
        )
        state = self.store.load_chat_state(chat_id)
        state.pending_action = None
        self.store.save_chat_state(state)
        status_message_id = self.send_codex_status(
            task,
            "planning",
            f"Задача создана для {project.slug}. Запускаю read-only planning.",
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
    ) -> None:
        task = self.store.create_task(
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project.slug,
            project_name=project.name,
            project_path=project.path,
            prompt=prompt,
            kind="direct_task",
            source=source,
            source_path=source_path,
        )
        state = self.store.load_chat_state(chat_id)
        state.pending_action = None
        self.store.save_chat_state(state)
        status_message_id = self.send_codex_status(
            task,
            "running",
            f"Direct execution task {task.id} created for {project.slug}. Запускаю Codex execution.",
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
            kind="followup_task",
            source=source,
            source_path=source_path,
            parent_task_id=parent.id,
            codex_session_id=parent.codex_session_id,
        )
        state = self.store.load_chat_state(chat_id)
        if state.pending_action == continue_task_action(parent.id):
            state.pending_action = None
            self.store.save_chat_state(state)
        status_message_id = self.send_codex_status(
            task,
            "running",
            f"Continuation task {task.id} created from {parent.id}. Resuming Codex session.",
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

        log_path = self.store.task_dir(task.id) / "plan.log"
        status = self.start_codex_status(
            task,
            "planning",
            log_path,
            message_id=status_message_id,
        )
        task = self.runner.run_planning(task, project)
        self.finish_codex_status(status, task)
        if task.phase == "planned":
            self.send(
                task.chat_id,
                f"Plan for task {task.id}:\n\n{task.plan_text}",
                reply_markup=inline_task_keyboard(task.id),
            )
        else:
            log_tail = tail_file(task.plan_log_path, max_lines=40)
            self.send(
                task.chat_id,
                f"Planning failed for task {task.id}: {task.error}\n\nLog tail:\n{log_tail}",
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
        if task.phase not in {"planned", "failed", "created"}:
            self.send(chat_id, f"Task is {task.phase}; cannot add clarification now.")
            return

        task.clarifications.append(answer.strip())
        task.phase = "created"
        task.plan_text = ""
        task.error = ""
        self.store.save_task(task)
        state = self.store.load_chat_state(chat_id)
        if pending_task_id(state.pending_action, "answer_task") == task.id:
            state.pending_action = None
            self.store.save_chat_state(state)
        status_message_id = self.send_codex_status(
            task,
            "planning",
            f"Уточнение сохранено для {task.id}. Перезапускаю planning.",
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
        status_message_id = self.send_codex_status(
            task,
            "running",
            f"Задача {task.id} подтверждена. Запускаю Codex execution.",
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
        status = self.start_codex_status(
            task,
            "running",
            log_path,
            message_id=status_message_id,
        )
        if recover:
            task = self.runner.run_recovery_execution(task, project)
        else:
            task = self.runner.run_execution(task, project)
        self.finish_codex_status(status, task)
        final = ""
        if task.final_path and Path(task.final_path).exists():
            final = Path(task.final_path).read_text(encoding="utf-8", errors="replace").strip()

        if task.phase == "completed":
            message = f"Task {task.id} completed.\n\n{final or 'Final response is empty.'}"
            reply_markup = completed_task_keyboard(task.id) if task.codex_session_id else None
        else:
            log_tail = tail_file(task.run_log_path, max_lines=60)
            message = (
                f"Task {task.id} failed: {task.error}\n\n"
                f"Final:\n{final or '-'}\n\nLog tail:\n{log_tail}"
            )
            reply_markup = None
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

        log_path = self.store.task_dir(task.id) / "run.log"
        status = self.start_codex_status(
            task,
            "running",
            log_path,
            message_id=status_message_id,
        )
        task = self.runner.run_followup_execution(task, project)
        self.finish_codex_status(status, task)
        final = ""
        if task.final_path and Path(task.final_path).exists():
            final = Path(task.final_path).read_text(encoding="utf-8", errors="replace").strip()

        if task.phase == "completed":
            message = f"Task {task.id} completed.\n\n{final or 'Final response is empty.'}"
            reply_markup = completed_task_keyboard(task.id) if task.codex_session_id else None
        else:
            log_tail = tail_file(task.run_log_path, max_lines=60)
            message = (
                f"Task {task.id} failed: {task.error}\n\n"
                f"Final:\n{final or '-'}\n\nLog tail:\n{log_tail}"
            )
            reply_markup = None
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
        rows: list[list[dict[str, str]]] = []
        full_keyboard = False
        if task.kind == "followup_task" and task.parent_task_id and task.phase == "created":
            rows.extend(followup_draft_keyboard(task.parent_task_id, task.id)["inline_keyboard"])
            full_keyboard = True
        elif task.phase == "planned":
            rows.append(
                [
                    {"text": "Execute", "callback_data": f"confirm:{task.id}"},
                    {"text": "Answer clarification", "callback_data": f"answer:{task.id}"},
                ]
            )
            rows.append(
                [
                    {"text": "Add files", "callback_data": f"attach:{task.id}"},
                    {"text": "Cancel", "callback_data": f"cancel:{task.id}"},
                ]
            )
        elif task.phase in {"planning", "running", "agent_running"}:
            rows.append([{"text": "Cancel", "callback_data": f"cancel:{task.id}"}])
        elif task.phase == "completed":
            rows.extend(completed_task_keyboard(task.id)["inline_keyboard"])
            full_keyboard = True
        if not full_keyboard:
            rows.append([{"text": "Logs", "callback_data": f"logs:{task.id}"}, {"text": "Tasks", "callback_data": "tasks:active"}])
            rows.append([{"text": "Menu", "callback_data": "menu"}])
        self.send(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": rows})

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
        self.send(chat_id, f"Log tail for {task.id}:\n\n{tail_file(log_path)}")

    def run(self) -> None:
        self.load_or_build_index()
        self.configure_telegram_menu()
        self.send_startup_message()
        self.recover_interrupted_tasks()
        self.resume_prompt_drafts()
        self.start_slack_watcher()

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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
