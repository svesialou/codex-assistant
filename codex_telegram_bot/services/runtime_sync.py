from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_RUNTIME_SYNC_FILES = (
    "codex_telegram_bot/bot.py",
    "codex_telegram_bot/codex_runner.py",
    "codex_telegram_bot/task_store.py",
    "tests/test_bot_ui.py",
    "tests/test_codex_runner.py",
    "tests/test_task_store.py",
)


@dataclass(frozen=True)
class RuntimeFileDrift:
    relative_path: str
    source_exists: bool
    installed_exists: bool
    same: bool
    source_sha256: str = ""
    installed_sha256: str = ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_runtime_files(
    source_root: Path,
    installed_root: Path,
    relative_paths: Iterable[str] = DEFAULT_RUNTIME_SYNC_FILES,
) -> list[RuntimeFileDrift]:
    drifts: list[RuntimeFileDrift] = []
    for relative_path in relative_paths:
        source_path = source_root / relative_path
        installed_path = installed_root / relative_path
        source_exists = source_path.exists()
        installed_exists = installed_path.exists()
        source_hash = file_sha256(source_path) if source_exists else ""
        installed_hash = file_sha256(installed_path) if installed_exists else ""
        drifts.append(
            RuntimeFileDrift(
                relative_path=relative_path,
                source_exists=source_exists,
                installed_exists=installed_exists,
                same=source_exists and installed_exists and source_hash == installed_hash,
                source_sha256=source_hash,
                installed_sha256=installed_hash,
            )
        )
    return drifts


def render_runtime_drift(drifts: list[RuntimeFileDrift]) -> str:
    if not drifts:
        return "Runtime drift: no files checked."

    changed = [item for item in drifts if not item.same]
    if not changed:
        return f"Runtime drift: none across {len(drifts)} checked files."

    lines = [f"Runtime drift: {len(changed)}/{len(drifts)} checked files differ."]
    for item in changed:
        if not item.source_exists:
            state = "source missing"
        elif not item.installed_exists:
            state = "installed missing"
        else:
            state = "different"
        lines.append(f"- {item.relative_path}: {state}")
    return "\n".join(lines)
