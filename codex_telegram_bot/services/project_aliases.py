from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..project_index import ProjectInfo


ALIAS_RE = re.compile(r"^[A-Za-zА-Яа-я0-9._-]{1,64}$")
DEFAULT_ALIASES = {
    "assistant": "codex-assistant",
    "bot": "codex-assistant",
    "codex-bot": "codex-assistant",
    "бот": "codex-assistant",
}


@dataclass(frozen=True)
class ProjectResolution:
    project: ProjectInfo | None
    query: str
    matched_by: str
    matches: list[ProjectInfo]
    suggestions: list[ProjectInfo]

    @property
    def is_ambiguous(self) -> bool:
        return self.project is None and bool(self.matches)


class ProjectAliasStore:
    def __init__(self, index_dir: Path) -> None:
        self.path = index_dir / "aliases.json"
        self._lock = threading.RLock()

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return dict(DEFAULT_ALIASES)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        aliases: dict[str, str] = {}
        for alias, slug in data.items():
            if isinstance(alias, str) and isinstance(slug, str) and alias.strip() and slug.strip():
                aliases[normalize_alias(alias)] = slug.strip()
        return aliases

    def save(self, aliases: dict[str, str]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            normalized = {
                normalize_alias(alias): slug
                for alias, slug in sorted(aliases.items())
                if alias.strip() and slug.strip()
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def list_aliases(self) -> dict[str, str]:
        return self.load()

    def add_alias(self, alias: str, project_slug: str) -> None:
        alias_key = normalize_alias(alias)
        validate_alias(alias_key)
        aliases = self.load()
        aliases[alias_key] = project_slug
        self.save(aliases)

    def remove_alias(self, alias: str) -> bool:
        alias_key = normalize_alias(alias)
        aliases = self.load()
        existed = alias_key in aliases
        if existed:
            del aliases[alias_key]
            self.save(aliases)
        return existed

    def resolve(
        self,
        projects: Iterable[ProjectInfo],
        query: str,
    ) -> ProjectResolution:
        normalized = query.strip().lower()
        project_list = list(projects)
        if not normalized:
            return ProjectResolution(None, query, "empty", [], [])

        by_slug = {project.slug.lower(): project for project in project_list}
        aliases = self.load()

        alias_target = aliases.get(normalized)
        if alias_target:
            project = by_slug.get(alias_target.lower())
            if project is not None:
                return ProjectResolution(project, query, "alias", [], [])

        project = by_slug.get(normalized)
        if project is not None:
            return ProjectResolution(project, query, "slug", [], [])

        exact_name_matches = [
            project for project in project_list if project.name.lower() == normalized
        ]
        if len(exact_name_matches) == 1:
            return ProjectResolution(exact_name_matches[0], query, "name", [], [])
        if len(exact_name_matches) > 1:
            return ProjectResolution(None, query, "name", exact_name_matches, exact_name_matches[:6])

        substring_matches = [
            project
            for project in project_list
            if normalized in project.slug.lower()
            or normalized in project.name.lower()
            or normalized in project.path.lower()
        ]
        if len(substring_matches) == 1:
            return ProjectResolution(substring_matches[0], query, "substring", [], [])
        return ProjectResolution(
            None,
            query,
            "substring",
            substring_matches,
            (substring_matches or project_list)[:6],
        )


def normalize_alias(alias: str) -> str:
    return alias.strip().lower()


def validate_alias(alias: str) -> None:
    if not ALIAS_RE.match(alias):
        raise ValueError("Alias must be 1-64 letters, digits, dot, underscore, or dash.")


def render_aliases(aliases: dict[str, str]) -> str:
    if not aliases:
        return "Project aliases: none"
    lines = ["Project aliases:"]
    for alias, slug in sorted(aliases.items()):
        lines.append(f"{alias} → {slug}")
    return "\n".join(lines)


def render_resolution_error(result: ProjectResolution) -> str:
    if result.is_ambiguous:
        lines = [f"Project query is ambiguous: {result.query}"]
        lines.extend(f"- {project.slug} | {project.path}" for project in result.matches[:6])
        return "\n".join(lines)
    if result.suggestions:
        lines = [f"Project not found: {result.query}", "Suggestions:"]
        lines.extend(f"- {project.slug} | {project.path}" for project in result.suggestions[:6])
        return "\n".join(lines)
    return f"Project not found: {result.query}"
