from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .redaction import RedactionService


MAX_SCAN_LINES = 200
MAX_TITLE_CHARS = 200
SCRATCH_DIR = "/tmp"
# Absolute paths of local repos inside session text, e.g. /home/u/Projects/org/app/src/x.py.
PROJECT_PATH_RE = re.compile(r"/home/stanislavv/(?:MyProjects|Projects)/[^\s\"'`<>\\]+")


@dataclass(frozen=True)
class SessionRef:
    source: str
    session_id: str
    cwd: str
    started_at: str
    path: Path
    first_prompt: str
    mentions: tuple[str, ...] = field(default=())


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item["text"]) for item in content if isinstance(item, dict) and item.get("text")
        )
    return ""


def _codex_session(path: Path) -> SessionRef | None:
    session_id = cwd = started_at = prompt = ""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= MAX_SCAN_LINES:
                break
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
            if item.get("type") == "session_meta":
                session_id = str(payload.get("id") or "")
                cwd = str(payload.get("cwd") or "")
                started_at = str(payload.get("timestamp") or "")
            elif item.get("type") == "response_item" and payload.get("role") == "user" and not prompt:
                prompt = _text_of(payload.get("content")).strip()
            if session_id and cwd and prompt:
                break
    if not session_id or not cwd:
        return None
    return SessionRef("codex", session_id, cwd, started_at, path, prompt)


def _claude_session(path: Path) -> SessionRef | None:
    session_id = cwd = started_at = prompt = ""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= MAX_SCAN_LINES:
                break
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") != "user":
                continue
            session_id = session_id or str(item.get("sessionId") or "")
            cwd = cwd or str(item.get("cwd") or "")
            started_at = started_at or str(item.get("timestamp") or "")
            if not prompt:
                prompt = _text_of((item.get("message") or {}).get("content")).strip()
            if session_id and cwd and prompt:
                break
    if not session_id or not cwd:
        return None
    return SessionRef("claude", session_id, cwd, started_at, path, prompt)


def _indexed_slug(path: str, projects: list[tuple[str, str]]) -> str:
    """Slug of the deepest indexed project that contains path, or empty string."""
    best_slug = ""
    best_len = -1
    for slug, root in projects:
        root = root.rstrip("/")
        if (path == root or path.startswith(root + "/")) and len(root) > best_len:
            best_slug, best_len = slug, len(root)
    return best_slug


def _mentioned_slugs(path: Path, projects: list[tuple[str, str]]) -> tuple[str, ...]:
    """Indexed projects whose paths appear in the session text, most mentioned first."""
    counts: Counter[str] = Counter()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            for match in PROJECT_PATH_RE.findall(line):
                slug = _indexed_slug(match, projects)
                if slug:
                    counts[slug] += 1
    return tuple(slug for slug, _ in counts.most_common())


def scan_sessions(
    codex_root: Path | None,
    claude_root: Path | None,
    projects: Iterable[tuple[str, str]] = (),
) -> list[SessionRef]:
    project_list = list(projects)
    found: list[tuple[SessionRef, Path]] = []
    if codex_root is not None and codex_root.is_dir():
        for path in sorted(codex_root.rglob("*.jsonl")):
            ref = _codex_session(path)
            if ref is not None:
                found.append((ref, path))
    if claude_root is not None and claude_root.is_dir():
        for path in sorted(claude_root.glob("*/*.jsonl")):
            ref = _claude_session(path)
            if ref is not None:
                found.append((ref, path))
    return [
        SessionRef(
            ref.source, ref.session_id, ref.cwd, ref.started_at, ref.path, ref.first_prompt,
            _mentioned_slugs(path, project_list),
        )
        for ref, path in found
    ]


def project_slug_for_cwd(cwd: str, projects: Iterable[tuple[str, str]]) -> str:
    """Slug of the deepest indexed project that contains cwd, else the cwd basename."""
    return _indexed_slug(cwd, list(projects)) or PurePosixPath(cwd).name or "unknown"


def session_hub_line(ref: SessionRef, redactor: RedactionService, related: Iterable[str] = ()) -> str:
    title = " ".join(redactor.redact_text(ref.first_prompt).text.split())[:MAX_TITLE_CHARS]
    line = f"- {ref.started_at[:10]} {ref.source} {ref.session_id}: {title or '-'} ({ref.path})"
    links = [f"[[Projects/{slug}]]" for slug in related]
    return f"{line} — also in {', '.join(links)}" if links else line


def _involved(ref: SessionRef, project_list: list[tuple[str, str]]) -> list[str]:
    primary = project_slug_for_cwd(ref.cwd, project_list)
    return list(dict.fromkeys([primary, *ref.mentions]))


def _is_scratch(cwd: str) -> bool:
    return cwd == SCRATCH_DIR or cwd.startswith(SCRATCH_DIR + "/")


def collect_session_lines(
    refs: Iterable[SessionRef],
    projects: Iterable[tuple[str, str]],
    known_session_ids: set[str],
    redactor: RedactionService,
) -> dict[str, list[str]]:
    """Hub lines per project for sessions the bot did not run itself.

    A session is listed in the hub of its working directory's project and in the hub of
    every other indexed project whose path appears in its text.
    """
    project_list = list(projects)
    lines: dict[str, list[str]] = {}
    for ref in sorted(refs, key=lambda item: item.started_at):
        if ref.session_id in known_session_ids or _is_scratch(ref.cwd):
            continue
        involved = _involved(ref, project_list)
        for slug in involved:
            others = [other for other in involved if other != slug]
            lines.setdefault(slug, []).append(session_hub_line(ref, redactor, others))
    return lines


def collect_relations(
    refs: Iterable[SessionRef],
    projects: Iterable[tuple[str, str]],
    known_session_ids: set[str],
) -> dict[str, Counter[str]]:
    """Per project: how many sessions also involved each other project."""
    project_list = list(projects)
    relations: dict[str, Counter[str]] = {}
    for ref in refs:
        if ref.session_id in known_session_ids or _is_scratch(ref.cwd):
            continue
        involved = _involved(ref, project_list)
        for slug in involved:
            for other in involved:
                if other != slug:
                    relations.setdefault(slug, Counter())[other] += 1
    return relations
