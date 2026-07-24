from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..project_index import ProjectInfo
from .project_aliases import ProjectAliasStore


PROJECT_HINT_RE = re.compile(
    r"(?:в|для|по|про)\s+(?:проект(?:е|а|ом)?\s+)?"
    r"(?P<project>[A-Za-zА-Яа-я0-9._/-]{2,80})|"
    r"(?:project|проект(?:е|а|ом)?)\s+"
    r"(?P<project2>[A-Za-zА-Яа-я0-9._/-]{2,80})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProjectResolveResult:
    project: ProjectInfo | None
    confidence: float
    reason: str
    candidates: list[ProjectInfo]


class ProjectResolver:
    def __init__(self, aliases: ProjectAliasStore) -> None:
        self.aliases = aliases

    def resolve(
        self,
        text: str,
        projects: Iterable[ProjectInfo],
        default_project: ProjectInfo | None = None,
    ) -> ProjectResolveResult:
        project_list = list(projects)
        explicit = self.extract_project_query(text)
        if explicit:
            result = self.aliases.resolve(project_list, explicit)
            if result.project is not None:
                return ProjectResolveResult(
                    result.project,
                    0.91,
                    f"explicit project hint: {explicit}",
                    [],
                )
            if result.matches:
                return ProjectResolveResult(
                    None,
                    0.52,
                    f"ambiguous project hint: {explicit}",
                    result.matches[:6],
                )
            if result.suggestions:
                return ProjectResolveResult(
                    None,
                    0.35,
                    f"project hint not found: {explicit}",
                    result.suggestions[:6],
                )

        if default_project is not None:
            return ProjectResolveResult(
                default_project,
                0.7,
                "selected chat project",
                [],
            )
        return ProjectResolveResult(None, 0.0, "no project context", project_list[:6])

    def extract_project_query(self, text: str) -> str | None:
        match = None
        for item in PROJECT_HINT_RE.finditer(text):
            match = item
        if match is None:
            return None
        value = match.group("project") or match.group("project2") or ""
        return value.strip(" .,!?:;`'\"")
