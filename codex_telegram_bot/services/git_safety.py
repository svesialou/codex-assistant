from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ..task_store import now_iso


GIT_TIMEOUT_SECONDS = 10
GIT_PUSH_TIMEOUT_SECONDS = 120
PROTECTED_FORCE_PUSH_BRANCHES = {"main", "master"}


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


@dataclass(frozen=True)
class GitForcePushResult:
    ok: bool
    branch: str = ""
    upstream: str = ""
    command: str = ""
    output: str = ""
    error: str = ""


def run_git(
    project_path: str | Path,
    args: list[str],
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
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


def force_push_current_branch(project_path: str | Path) -> GitForcePushResult:
    path = Path(project_path)
    if not is_git_repo(path):
        return GitForcePushResult(ok=False, error="Git repo not detected.")

    branch, branch_error = git_output(path, ["branch", "--show-current"])
    if branch_error:
        return GitForcePushResult(ok=False, error=branch_error)
    if not branch:
        return GitForcePushResult(ok=False, error="Current branch is detached or unknown.")
    if branch in PROTECTED_FORCE_PUSH_BRANCHES:
        return GitForcePushResult(
            ok=False,
            branch=branch,
            error=f"Force push is blocked for protected branch: {branch}.",
        )

    upstream, upstream_error = git_output(
        path,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
    )
    if upstream_error or not upstream:
        return GitForcePushResult(
            ok=False,
            branch=branch,
            error=upstream_error or "Current branch has no upstream configured.",
        )

    remote, _, remote_branch = upstream.partition("/")
    if not remote or not remote_branch:
        return GitForcePushResult(
            ok=False,
            branch=branch,
            upstream=upstream,
            error=f"Unsupported upstream format: {upstream}.",
        )

    args = ["push", "--force-with-lease", remote, f"HEAD:{remote_branch}"]
    command = "git " + " ".join(args)
    try:
        result = run_git(path, args, timeout=GIT_PUSH_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitForcePushResult(
            ok=False,
            branch=branch,
            upstream=upstream,
            command=command,
            error=str(exc),
        )

    output = "\n".join(
        item for item in [result.stdout.strip(), result.stderr.strip()] if item
    )
    if result.returncode != 0:
        return GitForcePushResult(
            ok=False,
            branch=branch,
            upstream=upstream,
            command=command,
            output=output,
            error=f"git push exited with code {result.returncode}.",
        )
    return GitForcePushResult(
        ok=True,
        branch=branch,
        upstream=upstream,
        command=command,
        output=output,
    )
