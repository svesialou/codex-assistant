from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


INSTALLED_RUNTIME_PATH = Path.home() / ".codex" / "tools" / "codex_telegram_bot"
DEFAULT_SOURCE_PATH = Path.home() / "Projects" / "codex-assistant"


@dataclass(frozen=True)
class RuntimeIdentity:
    source_path: str
    installed_path: str
    package_path: str
    version_marker: str


def runtime_identity(package_file: str) -> RuntimeIdentity:
    package_path = Path(package_file).resolve()
    package_root = package_path.parents[1]
    configured_source = Path(
        os.environ.get("CODEX_TELEGRAM_SOURCE_REPO", str(DEFAULT_SOURCE_PATH))
    ).expanduser()
    source_path = configured_source if configured_source.exists() else package_root
    return RuntimeIdentity(
        source_path=str(source_path),
        installed_path=str(INSTALLED_RUNTIME_PATH),
        package_path=str(package_path),
        version_marker="agent-foundation-mvp",
    )


def render_runtime_identity(identity: RuntimeIdentity) -> str:
    return (
        "Runtime identity\n"
        f"Source path: {identity.source_path}\n"
        f"Installed path: {identity.installed_path}\n"
        f"Package path: {identity.package_path}\n"
        f"Version marker: {identity.version_marker}"
    )
