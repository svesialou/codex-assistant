from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.git_safety import (
    capture_git_snapshot,
    force_push_current_branch,
    read_git_snapshot,
    snapshot_summary,
    write_git_snapshot,
)


def git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class GitSafetyTest(unittest.TestCase):
    def test_non_git_snapshot_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = capture_git_snapshot(tmp, "pre-execution")

        self.assertFalse(snapshot.is_git_repo)
        self.assertEqual(snapshot.dirty_files, 0)
        self.assertIn("not detected", snapshot_summary(snapshot))

    def test_git_snapshot_records_dirty_state_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "bot-test@example.test")
            git(repo, "config", "user.name", "Bot Test")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "base")
            (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")

            snapshot = capture_git_snapshot(repo, "pre-execution")
            artifact = write_git_snapshot(root / "task", snapshot)
            loaded = read_git_snapshot(root / "task", "pre-execution")
            artifact_exists = artifact.exists()

        self.assertTrue(snapshot.is_git_repo)
        self.assertGreater(snapshot.dirty_files, 0)
        self.assertTrue(artifact_exists)
        self.assertIsNotNone(loaded)
        self.assertIn("dirty state", snapshot_summary(snapshot))

    def test_force_push_blocks_protected_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init")
            git(repo, "config", "user.email", "bot-test@example.test")
            git(repo, "config", "user.name", "Bot Test")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "base")
            git(repo, "checkout", "-b", "main")

            result = force_push_current_branch(repo)

        self.assertFalse(result.ok)
        self.assertEqual(result.branch, "main")
        self.assertIn("protected branch", result.error)

    def test_force_push_current_branch_uses_force_with_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "repo"
            git(root, "init", "--bare", str(remote))
            git(root, "clone", str(remote), str(repo))
            git(repo, "config", "user.email", "bot-test@example.test")
            git(repo, "config", "user.name", "Bot Test")
            git(repo, "checkout", "-b", "feature/force-push")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "base")
            git(repo, "push", "-u", "origin", "HEAD")
            (repo / "README.md").write_text("rewritten\n", encoding="utf-8")
            git(repo, "commit", "--amend", "-am", "rewritten")

            result = force_push_current_branch(repo)
            local_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            remote_head = subprocess.run(
                ["git", "rev-parse", "refs/heads/feature/force-push"],
                cwd=remote,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.branch, "feature/force-push")
        self.assertEqual(result.upstream, "origin/feature/force-push")
        self.assertIn("--force-with-lease", result.command)
        self.assertEqual(remote_head, local_head)


if __name__ == "__main__":
    unittest.main()
