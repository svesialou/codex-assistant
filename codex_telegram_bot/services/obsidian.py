from __future__ import annotations

import re
from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..task_store import TaskRecord
from .redaction import RedactionService


DAILY_NOTES_DIR = "Daily"
TASKS_DIR = "Tasks"
PROJECTS_DIR = "Projects"
BACKFILL_PHASES = {"completed", "agent_completed"}
SKIPPED_DIRS = {".obsidian", ".trash", ".git"}
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_SNIPPET_CHARS = 200


@dataclass(frozen=True)
class ObsidianMatch:
    path: Path
    line_number: int
    line: str


class ObsidianVault:
    def __init__(self, root: Path | None) -> None:
        self.root = root

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def is_ready(self) -> bool:
        return self.root is not None and self.root.is_dir()

    def daily_note_path(self, now: datetime) -> Path:
        return self.root / DAILY_NOTES_DIR / f"{now:%Y-%m-%d}.md"

    def append_note(self, text: str, now: datetime) -> Path:
        path = self.daily_note_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = [f"- {now:%H:%M} {line}" for line in text.strip().splitlines()]
        block = "\n".join(lines) + "\n"
        separator = "\n" if existing and not existing.endswith("\n") else ""
        path.write_text(existing + separator + block, encoding="utf-8")
        return path

    def search(self, query: str, limit: int = 10) -> list[ObsidianMatch]:
        needle = query.strip().lower()
        if not needle or not self.is_ready():
            return []

        matches: list[ObsidianMatch] = []
        for path in sorted(self.root.rglob("*.md")):
            relative = path.relative_to(self.root)
            if any(part in SKIPPED_DIRS for part in relative.parts):
                continue
            if not path.is_file() or path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    matches.append(ObsidianMatch(relative, number, line.strip()[:MAX_SNIPPET_CHARS]))
                    if len(matches) >= limit:
                        return matches
        return matches


def obsidian_agent_context(vault_dir: Path | None, project_slug: str | None = None) -> str:
    if vault_dir is None or not vault_dir.is_dir():
        return ""
    lines = [
        "## Global Obsidian Vault",
        "",
        f"- path: {vault_dir}",
        f"- daily notes: {vault_dir / DAILY_NOTES_DIR}/YYYY-MM-DD.md",
        "- The vault is shared across all projects. Use it for cross-project notes and context.",
        f"- project hubs: {vault_dir / PROJECTS_DIR}/<project>.md (memory and task list per project)",
        f"- task notes: {vault_dir / TASKS_DIR}/<project>/<task_id>.md (full request, plan, result)",
    ]
    if project_slug:
        hub = vault_dir / PROJECTS_DIR / f"{safe_part(project_slug)}.md"
        lines.append(f"- this project's previous sessions and memory: {hub}")
    lines.append("- Read or search it with plain file tools. Do not store secrets in it.")
    return "\n".join(lines)



def safe_part(value: str) -> str:
    return re.sub(r"[^\w.-]+", "-", value).strip("-.") or "unknown"


def task_note_name(task: TaskRecord) -> str:
    return f"{TASKS_DIR}/{safe_part(task.project_slug)}/{safe_part(task.id)}"


def task_project_slugs(task: TaskRecord) -> list[str]:
    """Primary project plus every project the task used as context, without duplicates."""
    slugs: list[str] = []
    for slug in [task.project_slug, *task.context_project_slugs]:
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def task_title(task: TaskRecord, redactor: RedactionService) -> str:
    request = redactor.redact_text(task.original_user_request or task.prompt).text
    return " ".join(request.split())[:80] or task.id


def render_task_note(task: TaskRecord, final_text: str, redactor: RedactionService) -> str:
    def clean(text: str) -> str:
        return redactor.redact_text(text or "").text

    slug = safe_part(task.project_slug)
    lines = [
        "---",
        f"task_id: {task.id}",
        f"project: {slug}",
        f"phase: {task.phase}",
        f"kind: {task.kind}",
        f"provider: {task.executor_provider or '-'}",
        f"created: {task.created_at}",
        f"updated: {task.updated_at}",
        "---",
        "",
        f"# {task_title(task, redactor)}",
        "",
        f"Project: [[{PROJECTS_DIR}/{slug}]]",
    ]
    context = [other for other in task_project_slugs(task) if other != task.project_slug]
    if context:
        lines.append("Context projects: " + ", ".join(f"[[{PROJECTS_DIR}/{safe_part(item)}]]" for item in context))
    if task.parent_task_id:
        lines.append(f"Parent task: [[{safe_part(task.parent_task_id)}]]")

    sections = [
        ("Request", clean(task.original_user_request or task.prompt)),
        ("Clarifications", "\n".join(f"- {clean(item)}" for item in task.clarifications)),
        ("Plan", clean(task.plan_text)),
        ("Result", clean(final_text)),
        ("Summary", clean(task.summary)),
        ("Diff summary", clean(task.last_diff_summary)),
        ("Files touched", "\n".join(f"- {item}" for item in task.files_touched)),
        ("Next actions", "\n".join(f"- {clean(item)}" for item in task.next_actions)),
    ]
    for title, body in sections:
        if body.strip():
            lines.extend(["", f"## {title}", "", body.strip()])
    artifacts = [
        f"- final answer: {task.final_path}" if task.final_path else "",
        f"- run log: {task.run_log_path}" if task.run_log_path else "",
        f"- plan log: {task.plan_log_path}" if task.plan_log_path else "",
        f"- prompt: {task.prompt_path}" if task.prompt_path else "",
        f"- codex session: {task.codex_session_id}" if task.codex_session_id else "",
        f"- claude session: {task.claude_session_id}" if task.claude_session_id else "",
        *(f"- attachment: {item.get('file_name', '')} ({item.get('file_path', '')})" for item in task.attachments),
    ]
    artifacts = [line for line in artifacts if line]
    if artifacts:
        lines.extend(["", "## Artifacts", "", *artifacts])
    return "\n".join(lines) + "\n"


def write_task_note(
    vault: ObsidianVault,
    task: TaskRecord,
    final_text: str,
    redactor: RedactionService,
) -> Path:
    path = vault.root / f"{task_note_name(task)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_task_note(task, final_text, redactor), encoding="utf-8")
    return path


def hub_entry(task: TaskRecord, redactor: RedactionService) -> str:
    return f"- [[{task_note_name(task)}]] {task.created_at[:10]} {task_title(task, redactor)}"


def append_hub_entry(
    vault: ObsidianVault,
    task: TaskRecord,
    redactor: RedactionService,
    project_slug: str | None = None,
) -> None:
    slug = project_slug or task.project_slug
    path = vault.root / PROJECTS_DIR / f"{safe_part(slug)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else f"# {slug}\n\n## Tasks\n"
    if f"[[{task_note_name(task)}]]" in existing:
        return
    path.write_text(existing.rstrip("\n") + "\n" + hub_entry(task, redactor) + "\n", encoding="utf-8")


def write_project_hub(
    vault: ObsidianVault,
    project_slug: str,
    memory_text: str,
    entries: Iterable[str],
    session_lines: Iterable[str] = (),
    related_lines: Iterable[str] = (),
) -> Path:
    path = vault.root / PROJECTS_DIR / f"{safe_part(project_slug)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {project_slug}", ""]
    if memory_text.strip():
        lines.extend(["## Memory", "", memory_text.strip(), ""])
    lines.extend(["## Tasks", "", *entries])
    sessions = list(session_lines)
    if sessions:
        lines.extend(["", "## Sessions", "", *sessions])
    related = list(related_lines)
    if related:
        lines.extend(["", "## Related projects", "", *related])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def refresh_project_memory(vault: ObsidianVault, project_slug: str, memory_text: str) -> None:
    """Replace the Memory section of an existing project hub, keeping its task list."""
    path = vault.root / PROJECTS_DIR / f"{safe_part(project_slug)}.md"
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8")
    _, _, tasks_part = existing.partition("\n## Tasks")
    head = f"# {project_slug}\n\n"
    if memory_text.strip():
        head += f"## Memory\n\n{memory_text.strip()}\n\n"
    path.write_text(head + "## Tasks" + tasks_part, encoding="utf-8")


INDEX_HEURISTICS = [
    "- Task -> project: the task's project plus its context projects.",
    "- Session -> project: the deepest indexed project containing the session's working directory.",
    "- A session is also linked to every other indexed project whose absolute path appears in its text.",
    "- Sessions the bot ran itself are skipped (they are already task notes); /tmp sessions are skipped.",
    "- A project gets a hub only if it has tasks or sessions.",
]


def write_index(vault: ObsidianVault) -> Path:
    """Vault entry point: heuristics and a per-project summary with links to each hub."""
    lines = ["# Index", "", "## How links are built", "", *INDEX_HEURISTICS, "", "## Projects", ""]
    for hub in sorted((vault.root / PROJECTS_DIR).glob("*.md")):
        text = hub.read_text(encoding="utf-8")
        tasks = text.count("\n- [[Tasks/")
        sessions = len(re.findall(r"^- \d{4}-\d{2}-\d{2} ", text.partition("\n## Sessions")[2], re.M))
        lines.append(f"- [[{PROJECTS_DIR}/{hub.stem}]] tasks={tasks} sessions={sessions}")
    path = vault.root / "Index.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def backfill_vault(
    vault: ObsidianVault,
    tasks: list[TaskRecord],
    read_final,
    read_project_memory,
    redactor: RedactionService | None = None,
    session_lines: dict[str, list[str]] | None = None,
    relations: dict[str, Counter[str]] | None = None,
) -> tuple[int, int]:
    """Write full task notes and project hubs for existing tasks. Returns (tasks, projects)."""
    redactor = redactor or RedactionService()
    session_lines = session_lines or {}
    relations = {slug: Counter(counts) for slug, counts in (relations or {}).items()}
    by_project: dict[str, list[TaskRecord]] = {}
    for task in sorted(tasks, key=lambda item: item.created_at):
        if task.phase not in BACKFILL_PHASES:
            continue
        write_task_note(vault, task, read_final(task), redactor)
        slugs = task_project_slugs(task)
        for slug in slugs:
            by_project.setdefault(slug, []).append(task)
            for other in slugs:
                if other != slug:
                    relations.setdefault(slug, Counter())[other] += 1

    for slug in sorted(set(by_project) | set(session_lines)):
        related = [
            f"- [[{PROJECTS_DIR}/{safe_part(other)}]] — {count} shared"
            for other, count in relations.get(slug, Counter()).most_common()
        ]
        write_project_hub(
            vault,
            slug,
            redactor.redact_text(read_project_memory(slug)).text,
            [hub_entry(task, redactor) for task in by_project.get(slug, [])],
            session_lines.get(slug, []),
            related,
        )
    return sum(len(items) for items in by_project.values()), len(set(by_project) | set(session_lines))
