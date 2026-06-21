from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ..task_store import now_iso


GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class GitSnapshot:
    label: str
    created_at: str
    project_path: str
    is_git_repo: bool
    branch: str
    status_short: str
    diff_stat: str
    dirty_files: int
    error: str = ""

    @property
    def is_dirty(self) -> bool:
        return self.dirty_files > 0


def run_git(project_path: str | Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


def git_output(project_path: str | Path, args: list[str]) -> tuple[str, str]:
    try:
        result = run_git(project_path, args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    if result.returncode != 0:
        return "", (result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip(), ""


def is_git_repo(project_path: str | Path) -> bool:
    output, _ = git_output(project_path, ["rev-parse", "--is-inside-work-tree"])
    return output == "true"


def capture_git_snapshot(project_path: str | Path, label: str) -> GitSnapshot:
    path = Path(project_path)
    if not is_git_repo(path):
        return GitSnapshot(
            label=label,
            created_at=now_iso(),
            project_path=str(path),
            is_git_repo=False,
            branch="",
            status_short="",
            diff_stat="",
            dirty_files=0,
        )

    branch, branch_error = git_output(path, ["branch", "--show-current"])
    status_short, status_error = git_output(path, ["status", "--short"])
    diff_stat, diff_error = git_output(path, ["diff", "--stat"])
    error = "; ".join(
        item for item in [branch_error, status_error, diff_error] if item
    )
    dirty_files = sum(1 for line in status_short.splitlines() if line.strip())
    return GitSnapshot(
        label=label,
        created_at=now_iso(),
        project_path=str(path),
        is_git_repo=True,
        branch=branch,
        status_short=status_short,
        diff_stat=diff_stat,
        dirty_files=dirty_files,
        error=error,
    )


def write_git_snapshot(task_dir: Path, snapshot: GitSnapshot) -> Path:
    path = task_dir / f"{snapshot.label}-git.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(snapshot), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def snapshot_summary(snapshot: GitSnapshot) -> str:
    if not snapshot.is_git_repo:
        return "Git repo not detected."
    dirty = f", dirty state: {snapshot.dirty_files} files" if snapshot.is_dirty else ", clean"
    branch = snapshot.branch or "detached/unknown"
    return f"Git branch {branch}{dirty}."


def read_git_snapshot(task_dir: Path, label: str) -> GitSnapshot | None:
    path = task_dir / f"{label}-git.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return GitSnapshot(**data)
    except (OSError, TypeError, json.JSONDecodeError):
        return None
