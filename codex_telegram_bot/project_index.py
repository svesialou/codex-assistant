from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


IGNORE_DIRS = {
    ".cache",
    ".git",
    ".idea",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "cache",
    "dist",
    "node_modules",
    "storage",
    "tmp",
    "var",
    "vendor",
    "venv",
}

MARKER_FILES = [
    "AGENTS.md",
    "AGENTS.override.md",
    ".codex/AGENTS.md",
    ".codex/config.toml",
    "README.md",
    "README.rst",
    "README",
    "Makefile",
    "go.mod",
    "composer.json",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Dockerfile",
]

DISCOVERY_MARKER_FILES = [
    "AGENTS.md",
    "AGENTS.override.md",
    ".codex/config.toml",
    "go.mod",
    "composer.json",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Dockerfile",
]

DOC_FILES = [
    "AGENTS.md",
    "AGENTS.override.md",
    ".codex/AGENTS.md",
    ".codex/config.toml",
    "README.md",
    "README.rst",
    "README",
    "Makefile",
]

LANGUAGE_MARKERS = {
    "go.mod": "Go",
    "composer.json": "PHP",
    "package.json": "TypeScript/JavaScript",
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "Dockerfile": "Docker",
}

ROOT_PROJECT_SLUG = "root"


@dataclass(frozen=True)
class ProjectInfo:
    slug: str
    name: str
    path: str
    base: str
    is_git: bool
    branch: str | None
    origin: str | None
    languages: list[str]
    markers: list[str]
    docs: list[str]
    test_hints: list[str]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return slug.lower() or "project"


def project_slug(path: Path, bases: Iterable[Path]) -> str:
    for base in bases:
        try:
            return slugify(str(path.relative_to(base.expanduser())).replace("/", "__"))
        except ValueError:
            continue
    return slugify(path.name)


def is_ignored(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)


def has_marker(path: Path) -> bool:
    return (path / ".git").is_dir() or any(
        (path / marker).exists() for marker in DISCOVERY_MARKER_FILES
    )


def discover_project_roots(base_dirs: Iterable[Path], max_depth: int = 4) -> list[Path]:
    roots: set[Path] = set()
    bases = [base.expanduser().resolve() for base in base_dirs if base.expanduser().exists()]

    for base in bases:
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                roots.add(child.resolve())

        stack: list[tuple[Path, int]] = [(base, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > max_depth:
                continue
            if depth > 0 and has_marker(current):
                roots.add(current.resolve())

            if depth == max_depth:
                continue

            try:
                children = sorted(current.iterdir())
            except OSError:
                continue

            for child in reversed(children):
                if not child.is_dir() or child.name in IGNORE_DIRS or child.name.startswith("."):
                    continue
                if is_ignored(child.relative_to(base)):
                    continue
                stack.append((child, depth + 1))

    return sorted(roots, key=lambda item: str(item).lower())


def git_value(path: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    value = completed.stdout.strip()
    return value or None


def detect_languages(path: Path) -> list[str]:
    languages = {
        language
        for marker, language in LANGUAGE_MARKERS.items()
        if (path / marker).exists()
    }

    if any(path.glob("*.go")) or (path / "cmd").is_dir() or (path / "internal").is_dir():
        languages.add("Go")
    if any(path.glob("*.php")) or (path / "src").is_dir() and (path / "composer.json").exists():
        languages.add("PHP")
    if (path / "src").is_dir() and (path / "package.json").exists():
        languages.add("TypeScript/JavaScript")
    if any(path.glob("*.py")):
        languages.add("Python")

    return sorted(languages)


def detect_test_hints(path: Path) -> list[str]:
    hints: list[str] = []
    if (path / "Makefile").exists():
        hints.append("make test")
    if (path / "go.mod").exists():
        hints.append("go test ./...")
    if (path / "composer.json").exists():
        hints.append("composer test")
        if (path / "vendor/bin/phpunit").exists():
            hints.append("vendor/bin/phpunit")
    if (path / "package.json").exists():
        hints.append("npm test")
    if (path / "pyproject.toml").exists() or (path / "requirements.txt").exists():
        hints.append("python -m pytest")
    return hints


def existing_relative_files(path: Path, candidates: Iterable[str]) -> list[str]:
    return [candidate for candidate in candidates if (path / candidate).exists()]


def project_base(path: Path, bases: Iterable[Path]) -> str:
    for base in bases:
        base = base.expanduser().resolve()
        try:
            path.relative_to(base)
            return str(base)
        except ValueError:
            continue
    return str(path.parent)


def inspect_project(path: Path, bases: Iterable[Path]) -> ProjectInfo:
    path = path.resolve()
    is_git = (path / ".git").exists()
    return ProjectInfo(
        slug=project_slug(path, bases),
        name=path.name,
        path=str(path),
        base=project_base(path, bases),
        is_git=is_git,
        branch=git_value(path, ["branch", "--show-current"]) if is_git else None,
        origin=git_value(path, ["remote", "get-url", "origin"]) if is_git else None,
        languages=detect_languages(path),
        markers=existing_relative_files(path, MARKER_FILES),
        docs=existing_relative_files(path, DOC_FILES),
        test_hints=detect_test_hints(path),
    )


def inspect_root_project(path: Path) -> ProjectInfo:
    path = path.expanduser().resolve()
    return ProjectInfo(
        slug=ROOT_PROJECT_SLUG,
        name="workspace-root",
        path=str(path),
        base=str(path),
        is_git=False,
        branch=None,
        origin=None,
        languages=[],
        markers=existing_relative_files(path, MARKER_FILES),
        docs=existing_relative_files(path, DOC_FILES),
        test_hints=[],
    )


def read_excerpt(path: Path, max_chars: int = 3500) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n..."


def render_agent_context(project: ProjectInfo) -> str:
    path = Path(project.path)
    lines = [
        f"# Project Agent Context: {project.name}",
        "",
        f"- Slug: `{project.slug}`",
        f"- Path: `{project.path}`",
        f"- Base: `{project.base}`",
        f"- Git: {'yes' if project.is_git else 'no'}",
    ]

    if project.branch:
        lines.append(f"- Branch: `{project.branch}`")
    if project.origin:
        lines.append(f"- Origin: `{project.origin}`")
    if project.languages:
        lines.append(f"- Languages: {', '.join(project.languages)}")
    if project.test_hints:
        lines.append(f"- Verification hints: {', '.join(project.test_hints)}")

    lines.extend(
        [
            "",
            "## Operating Notes",
            "",
            "- Read local instructions before editing: AGENTS.md, AGENTS.override.md, .codex/config.toml, README, Makefile, CI config, and docs that are relevant to the task.",
            "- Keep diffs focused and follow existing project patterns.",
            "- Ask before public API, database schema, auth/security, gRPC/protobuf, CI/CD, Kubernetes, or destructive git changes.",
            "- Run the narrowest meaningful verification command and report exact failures.",
        ]
    )

    if project.docs:
        lines.extend(["", "## Local Docs Excerpts"])
        for doc in project.docs:
            excerpt = read_excerpt(path / doc)
            if not excerpt:
                continue
            lines.extend(["", f"### {doc}", "", "```", excerpt, "```"])

    return "\n".join(lines).rstrip() + "\n"


def render_projects_markdown(projects: Iterable[ProjectInfo]) -> str:
    lines = ["# Codex Project Index", ""]
    for project in projects:
        languages = ", ".join(project.languages) if project.languages else "unknown"
        lines.append(f"## {project.slug}")
        lines.append("")
        lines.append(f"- Name: `{project.name}`")
        lines.append(f"- Path: `{project.path}`")
        lines.append(f"- Languages: {languages}")
        if project.branch:
            lines.append(f"- Branch: `{project.branch}`")
        if project.test_hints:
            lines.append(f"- Verification hints: {', '.join(project.test_hints)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_index(
    base_dirs: Iterable[Path],
    output_dir: Path,
    workspace_root: Path | None = None,
) -> list[ProjectInfo]:
    bases = [base.expanduser().resolve() for base in base_dirs]
    root = inspect_root_project(workspace_root or Path.home())
    projects = [root, *[inspect_project(path, bases) for path in discover_project_roots(bases)]]

    output_dir.mkdir(parents=True, exist_ok=True)
    agents_dir = output_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "projects.json").write_text(
        json.dumps([asdict(project) for project in projects], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "PROJECTS.md").write_text(
        render_projects_markdown(projects), encoding="utf-8"
    )

    current_agent_files = {path.name for path in agents_dir.glob("*.md")}
    expected_agent_files: set[str] = set()
    for project in projects:
        file_name = f"{project.slug}.md"
        expected_agent_files.add(file_name)
        (agents_dir / file_name).write_text(render_agent_context(project), encoding="utf-8")

    for stale_name in current_agent_files - expected_agent_files:
        (agents_dir / stale_name).unlink(missing_ok=True)

    return projects


def load_index(index_dir: Path) -> list[ProjectInfo]:
    path = index_dir / "projects.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ProjectInfo(**item) for item in data]


def resolve_project(projects: Iterable[ProjectInfo], query: str) -> ProjectInfo | None:
    normalized = query.strip().lower()
    if not normalized:
        return None

    project_list = list(projects)
    if normalized in {ROOT_PROJECT_SLUG, ".", "~", "/", "home", "workspace"}:
        for project in project_list:
            if project.slug == ROOT_PROJECT_SLUG:
                return project

    for project in project_list:
        if normalized in {project.slug.lower(), project.name.lower(), project.path.lower()}:
            return project

    matches = [
        project
        for project in project_list
        if normalized in project.slug.lower()
        or normalized in project.name.lower()
        or normalized in project.path.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def search_projects(projects: Iterable[ProjectInfo], query: str, limit: int = 20) -> list[ProjectInfo]:
    normalized = query.strip().lower()
    project_list = list(projects)
    if not normalized:
        return project_list[:limit]
    return [
        project
        for project in project_list
        if normalized in project.slug.lower()
        or normalized in project.name.lower()
        or normalized in project.path.lower()
        or any(normalized in language.lower() for language in project.languages)
    ][:limit]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Codex project index.")
    parser.add_argument(
        "--roots",
        default="~/Projects:~/MyProjects",
        help="Colon-separated project roots.",
    )
    parser.add_argument(
        "--output",
        default="~/.codex/project-index",
        help="Output directory.",
    )
    parser.add_argument(
        "--workspace-root",
        default="~",
        help="Root context path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    roots = [Path(item).expanduser() for item in args.roots.split(":") if item]
    projects = build_index(
        roots,
        Path(args.output).expanduser(),
        Path(args.workspace_root).expanduser(),
    )
    print(f"Indexed {len(projects)} projects into {Path(args.output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
