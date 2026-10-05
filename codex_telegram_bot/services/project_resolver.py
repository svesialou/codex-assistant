from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..project_index import ProjectInfo, ROOT_PROJECT_SLUG
from .project_aliases import ProjectAliasStore, ProjectResolution


PROJECT_HINT_RE = re.compile(
    r"(?:в|для|по|про)\s+"
    r"(?:(?:проект(?:е|а|ом)?|repo|repository|репо|репозитор(?:ий|ии|ия|ием)?)\s+)?"
    r"(?P<project>[A-Za-zА-Яа-я0-9._/-]{2,80})|"
    r"(?:project|проект(?:е|а|ом)?)\s+"
    r"(?P<project2>[A-Za-zА-Яа-я0-9._/-]{2,80})|"
    r"(?:slug|repo|repository|репо|репозитор(?:ий|ии|ия|ием)?)\s*(?:[:=-]\s*)?"
    r"(?P<project3>[A-Za-zА-Яа-я0-9._/-]{2,80})",
    re.IGNORECASE,
)
PROJECT_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9._/-]{1,79}")
PROJECT_TOKEN_STOP_WORDS = {
    "and",
    "codex",
    "project",
    "repo",
    "repository",
    "task",
    "the",
    "в",
    "для",
    "задача",
    "задачи",
    "и",
    "нужно",
    "по",
    "проект",
    "проекта",
    "репо",
    "репозиторий",
}


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

        inferred = self.resolve_from_text_tokens(text, project_list)
        if inferred.project is not None:
            return ProjectResolveResult(
                inferred.project,
                0.84,
                f"project token: {inferred.query}",
                [],
            )
        if inferred.matches:
            return ProjectResolveResult(
                None,
                0.5,
                "ambiguous project tokens",
                inferred.matches[:6],
            )

        if default_project is not None and default_project.slug != ROOT_PROJECT_SLUG:
            return ProjectResolveResult(
                default_project,
                0.7,
                "selected chat project",
                [],
            )
        if default_project is not None and default_project.slug == ROOT_PROJECT_SLUG:
            return ProjectResolveResult(
                None,
                0.2,
                "root context needs target project clarification",
                [project for project in project_list if project.slug != ROOT_PROJECT_SLUG][:6],
            )
        return ProjectResolveResult(None, 0.0, "no project context", project_list[:6])

    def extract_project_query(self, text: str) -> str | None:
        match = None
        for item in PROJECT_HINT_RE.finditer(text):
            match = item
        if match is None:
            return None
        value = match.group("project") or match.group("project2") or match.group("project3") or ""
        return value.strip(" .,!?:;`'\"")

    def resolve_from_text_tokens(
        self,
        text: str,
        projects: Iterable[ProjectInfo],
    ) -> ProjectResolution:
        project_list = list(projects)
        matches: list[ProjectInfo] = []
        seen_slugs: set[str] = set()
        for token in project_tokens(text):
            result = self.aliases.resolve(project_list, token)
            if result.project is not None and result.project.slug != ROOT_PROJECT_SLUG:
                if result.project.slug not in seen_slugs:
                    matches.append(result.project)
                    seen_slugs.add(result.project.slug)
                continue
            for project in result.matches:
                if project.slug == ROOT_PROJECT_SLUG or project.slug in seen_slugs:
                    continue
                matches.append(project)
                seen_slugs.add(project.slug)

        if len(matches) == 1:
            return self.aliases.resolve(project_list, matches[0].slug)
        if matches:
            return ProjectResolution(None, text, "tokens", matches, matches[:6])
        return ProjectResolution(None, text, "tokens", [], [])


def project_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in PROJECT_TOKEN_RE.finditer(text):
        token = match.group(0).strip("._-/").lower()
        if (
            not token
            or token in PROJECT_TOKEN_STOP_WORDS
            or token.isdigit()
            or token in seen
        ):
            continue
        tokens.append(match.group(0))
        seen.add(token)
    return tokens
