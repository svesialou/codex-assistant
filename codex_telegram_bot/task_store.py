from __future__ import annotations

import json
import os
import secrets
import signal
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


@dataclass
class TaskRecord:
    id: str
    chat_id: int
    user_id: int | None
    project_slug: str
    project_name: str
    project_path: str
    prompt: str
    context_project_slugs: list[str] = field(default_factory=list)
    kind: str = "task"
    source: str = "text"
    source_path: str = ""
    parent_task_id: str = ""
    codex_session_id: str = ""
    phase: str = "created"
    clarifications: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    plan_text: str = ""
    plan_log_path: str = ""
    run_log_path: str = ""
    final_path: str = ""
    prompt_path: str = ""
    pid: int | None = None
    pid_start_time: str = ""
    runtime_id: str = ""
    returncode: int | None = None
    error: str = ""
    recovery_attempts: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChatState:
    chat_id: int
    selected_project_slug: str | None = None
    active_project_slugs: list[str] = field(default_factory=list)
    agent_mode: bool = False
    agent_conversation_mode: bool = True
    pending_action: str | None = None
    prompt_draft_action: str | None = None
    prompt_draft_user_id: int | None = None
    prompt_draft_project_slug: str | None = None
    prompt_draft_context_project_slugs: list[str] = field(default_factory=list)
    prompt_draft_parts: list[str] = field(default_factory=list)
    prompt_draft_source: str = "text"
    prompt_draft_source_path: str = ""
    prompt_draft_version: int = 0
    last_task_id: str = ""
    last_planned_task_id: str = ""
    last_completed_task_id: str = ""
    last_task_with_session_id: str = ""
    last_shown_view: str = ""
    pending_memory_project_slug: str = ""
    pending_memory_text: str = ""
    pending_alias_name: str = ""
    pending_alias_project_slug: str = ""
    updated_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatState":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.tasks_dir = state_dir / "tasks"
        self._lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def create_task(
        self,
        chat_id: int,
        user_id: int | None,
        project_slug: str,
        project_name: str,
        project_path: str,
        prompt: str,
        context_project_slugs: list[str] | None = None,
        kind: str = "task",
        source: str = "text",
        source_path: str = "",
        parent_task_id: str = "",
        codex_session_id: str = "",
    ) -> TaskRecord:
        task_id = new_task_id()
        task = TaskRecord(
            id=task_id,
            chat_id=chat_id,
            user_id=user_id,
            project_slug=project_slug,
            project_name=project_name,
            project_path=project_path,
            prompt=prompt,
            context_project_slugs=list(context_project_slugs or [project_slug]),
            kind=kind,
            source=source,
            source_path=source_path,
            parent_task_id=parent_task_id,
            codex_session_id=codex_session_id,
        )
        self.save_task(task)
        return task

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def task_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "task.json"

    def save_task(self, task: TaskRecord) -> None:
        with self._lock:
            task.updated_at = now_iso()
            directory = self.task_dir(task.id)
            directory.mkdir(parents=True, exist_ok=True)
            tmp = directory / "task.json.tmp"
            tmp.write_text(
                json.dumps(task.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(directory / "task.json")

    def add_attachment(self, task_id: str, attachment: dict[str, Any]) -> TaskRecord | None:
        with self._lock:
            task = self.load_task(task_id)
            if task is None:
                return None
            task.attachments.append(attachment)
            self.save_task(task)
            return task

    def load_task(self, task_id: str) -> TaskRecord | None:
        path = self.task_path(task_id)
        if not path.exists():
            return None
        return TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def recent_tasks(
        self,
        chat_id: int,
        limit: int = 10,
        phases: set[str] | None = None,
        project_slug: str | None = None,
    ) -> list[TaskRecord]:
        return self.tasks(
            limit=limit,
            phases=phases,
            chat_ids={chat_id},
            project_slug=project_slug,
        )

    def tasks(
        self,
        limit: int | None = None,
        phases: set[str] | None = None,
        chat_ids: set[int] | None = None,
        project_slug: str | None = None,
    ) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        for path in self.tasks_dir.glob("*/task.json"):
            try:
                task = TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if chat_ids is not None and task.chat_id not in chat_ids:
                continue
            if phases is not None and task.phase not in phases:
                continue
            if project_slug is not None and task.project_slug != project_slug:
                continue
            tasks.append(task)
        tasks.sort(key=lambda item: item.updated_at, reverse=True)
        return tasks[:limit] if limit is not None else tasks

    def chat_state_path(self, chat_id: int) -> Path:
        return self.state_dir / "chats" / f"{chat_id}.json"

    def load_chat_state(self, chat_id: int) -> ChatState:
        path = self.chat_state_path(chat_id)
        if not path.exists():
            return ChatState(chat_id=chat_id)
        try:
            return ChatState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return ChatState(chat_id=chat_id)

    def chat_states(self) -> list[ChatState]:
        states: list[ChatState] = []
        for path in (self.state_dir / "chats").glob("*.json"):
            try:
                states.append(
                    ChatState.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return states

    def save_chat_state(self, state: ChatState) -> None:
        with self._lock:
            state.updated_at = now_iso()
            path = self.chat_state_path(state.chat_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)


def terminate_process_group(pid: int) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    except OSError:
        return


def is_process_alive(pid: int | None) -> bool:
    if pid is None:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_time(pid: int | None) -> str:
    if pid is None:
        return ""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""

    try:
        fields = stat.rsplit(")", 1)[1].strip().split()
        return fields[19]
    except (IndexError, ValueError):
        return ""


def is_recorded_process_alive(
    task: TaskRecord,
    pid_alive: Any = is_process_alive,
) -> bool:
    if task.pid is None:
        return False
    if task.pid_start_time:
        return process_start_time(task.pid) == task.pid_start_time
    return bool(pid_alive(task.pid))
