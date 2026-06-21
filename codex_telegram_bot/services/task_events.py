from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from ..agent_roles import role_emoji
from ..task_store import TaskStore, now_iso


EVENT_TYPES = {
    "agent_message",
    "tool_call",
    "state_change",
    "safety",
    "memory",
    "review",
}


@dataclass(frozen=True)
class TaskEvent:
    id: str
    task_id: str
    created_at: str
    type: str
    agent: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class TaskEventLog:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    def events_path(self, task_id: str):
        return self.store.task_dir(task_id) / "events.jsonl"

    def append_event(
        self,
        task_id: str,
        event_type: str,
        agent: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unsupported task event type: {event_type}")

        event = TaskEvent(
            id=f"evt_{now_iso().replace(':', '').replace('+', 'z')}_{secrets.token_hex(3)}",
            task_id=task_id,
            created_at=now_iso(),
            type=event_type,
            agent=agent,
            message=message.strip(),
            data=dict(data or {}),
        )
        path = self.events_path(task_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        return event

    def list_events(self, task_id: str, limit: int = 50) -> list[TaskEvent]:
        path = self.events_path(task_id)
        if not path.exists():
            return []

        events: list[TaskEvent] = []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        for line in lines[-max(1, limit) :]:
            try:
                payload = json.loads(line)
                events.append(TaskEvent(**payload))
            except (TypeError, json.JSONDecodeError):
                continue
        return events


def render_task_events(task_id: str, events: list[TaskEvent]) -> str:
    lines = [f"🧠 Мозги команды по {task_id}"]
    if not events:
        lines.append("")
        lines.append("Пока нет agent events.")
        return "\n".join(lines)

    for event in events:
        emoji = role_emoji(event.agent)
        lines.append(f"{emoji} {event.agent}: {event.message}")
    return "\n".join(lines)
