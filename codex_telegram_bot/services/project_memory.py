from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..task_store import now_iso


SECRET_LINE_RE = re.compile(
    r"(token|password|passwd|secret|api[_-]?key|private\s+key|auth\.json|ssh-rsa)",
    re.IGNORECASE,
)
MAX_MEMORY_DISPLAY_CHARS = 3500


@dataclass(frozen=True)
class MemorySanitization:
    text: str
    removed_lines: int = 0


@dataclass
class UserMemory:
    user_id: int
    project_notes_saved: int = 0
    updated_at: str = field(default_factory=now_iso)


class ProjectMemoryStore:
    def __init__(self, index_dir: Path, state_dir: Path) -> None:
        self.index_dir = index_dir
        self.state_dir = state_dir

    def project_memory_path(self, project_slug: str) -> Path:
        return self.index_dir / "memory" / f"{project_slug}.md"

    def user_memory_path(self, user_id: int) -> Path:
        return self.state_dir / "users" / f"{user_id}.json"

    def sanitize(self, text: str) -> MemorySanitization:
        kept: list[str] = []
        removed = 0
        for raw_line in text.strip().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if SECRET_LINE_RE.search(line):
                removed += 1
                continue
            kept.append(line)
        return MemorySanitization(text="\n".join(kept).strip(), removed_lines=removed)

    def append_project_memory(
        self,
        project_slug: str,
        text: str,
        user_id: int | None = None,
    ) -> MemorySanitization:
        sanitized = self.sanitize(text)
        if not sanitized.text:
            return sanitized

        path = self.project_memory_path(project_slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        block = f"- {sanitized.text.replace(chr(10), chr(10) + '  ')}"
        content = (existing.rstrip() + "\n" if existing.strip() else "") + block + "\n"
        path.write_text(content, encoding="utf-8")

        if user_id is not None:
            self.increment_user_saved_count(user_id)
        return sanitized

    def read_project_memory(self, project_slug: str, max_chars: int = MAX_MEMORY_DISPLAY_CHARS) -> str:
        path = self.project_memory_path(project_slug)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) <= max_chars:
            return text
        return text[-max_chars:].lstrip()

    def load_user_memory(self, user_id: int) -> UserMemory:
        path = self.user_memory_path(user_id)
        if not path.exists():
            return UserMemory(user_id=user_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return UserMemory(**data)
        except (OSError, TypeError, json.JSONDecodeError):
            return UserMemory(user_id=user_id)

    def save_user_memory(self, memory: UserMemory) -> None:
        memory.updated_at = now_iso()
        path = self.user_memory_path(memory.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(asdict(memory), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def increment_user_saved_count(self, user_id: int) -> None:
        memory = self.load_user_memory(user_id)
        memory.project_notes_saved += 1
        self.save_user_memory(memory)
