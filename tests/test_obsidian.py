from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from codex_telegram_bot.services.obsidian import (
    ObsidianVault,
    append_hub_entry,
    backfill_vault,
    obsidian_agent_context,
    refresh_project_memory,
    task_project_slugs,
)
from codex_telegram_bot.services.redaction import RedactionService
from codex_telegram_bot.task_store import TaskRecord


class ObsidianVaultTest(unittest.TestCase):
    def test_append_note_creates_daily_note_and_appends_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))
            now = datetime(2026, 10, 11, 9, 5)

            path = vault.append_note("first line\nsecond line", now)
            vault.append_note("later", datetime(2026, 10, 11, 18, 30))
            content = path.read_text(encoding="utf-8")

        self.assertEqual(path.name, "2026-10-11.md")
        self.assertEqual(path.parent.name, "Daily")
        self.assertEqual(
            content,
            "- 09:05 first line\n- 09:05 second line\n- 18:30 later\n",
        )

    def test_search_skips_obsidian_internal_dirs_and_limits_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Notes").mkdir()
            (root / ".obsidian").mkdir()
            (root / "Notes" / "a.md").write_text("Plan: ship Obsidian\nOther\n", encoding="utf-8")
            (root / "Notes" / "b.md").write_text("obsidian again\n", encoding="utf-8")
            (root / ".obsidian" / "hidden.md").write_text("obsidian config\n", encoding="utf-8")

            matches = ObsidianVault(root).search("OBSIDIAN", limit=10)
            limited = ObsidianVault(root).search("obsidian", limit=1)

        self.assertEqual(
            [(str(match.path), match.line_number) for match in matches],
            [("Notes/a.md", 1), ("Notes/b.md", 1)],
        )
        self.assertEqual(len(limited), 1)

    def test_missing_vault_is_not_ready_and_search_returns_nothing(self) -> None:
        vault = ObsidianVault(None)

        self.assertFalse(vault.is_ready())
        self.assertEqual(vault.search("anything"), [])

    def test_agent_context_is_empty_without_existing_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"

            self.assertEqual(obsidian_agent_context(None), "")
            self.assertEqual(obsidian_agent_context(missing), "")

    def test_agent_context_points_agents_at_global_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            context = obsidian_agent_context(root)

        self.assertIn(f"- path: {root}", context)
        self.assertIn("shared across all projects", context)
        self.assertIn(f"{root}/Projects/<project>.md", context)

    def test_agent_context_names_current_project_hub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            context = obsidian_agent_context(root, "demo")

        self.assertIn(f"- this project's previous sessions and memory: {root}/Projects/demo.md", context)

    def test_task_note_lists_artifacts_and_session(self) -> None:
        task = TaskRecord(
            id="t-11", chat_id=1, user_id=1, project_slug="demo",
            project_name="demo", project_path="/tmp/demo", prompt="ship it", phase="completed",
            final_path="/state/t-11/final.md", codex_session_id="sess-1",
            attachments=[{"file_name": "spec.pdf", "file_path": "/state/t-11/spec.pdf"}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))
            backfill_vault(vault, [task], lambda item: "done", lambda slug: "")
            note = (Path(tmp) / "Tasks/demo/t-11.md").read_text(encoding="utf-8")

        self.assertIn("## Artifacts", note)
        self.assertIn("- final answer: /state/t-11/final.md", note)
        self.assertIn("- codex session: sess-1", note)
        self.assertIn("- attachment: spec.pdf (/state/t-11/spec.pdf)", note)

    def test_backfill_writes_full_untruncated_notes_and_project_hub(self) -> None:
        long_final = "Result line.\n" + "x" * 5000
        task = TaskRecord(
            id="t-1",
            chat_id=1,
            user_id=1,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="fix the bug\n" + "details " * 200,
            phase="completed",
            plan_text="1. do it",
        )
        request_only = TaskRecord(
            id="t-2", chat_id=1, user_id=1, project_slug="demo",
            project_name="demo", project_path="/tmp/demo", prompt="nothing", phase="completed",
        )
        failed = TaskRecord(
            id="t-3", chat_id=1, user_id=1, project_slug="demo",
            project_name="demo", project_path="/tmp/demo", prompt="boom", phase="failed",
        )
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))

            counts = backfill_vault(
                vault,
                [task, request_only, failed],
                lambda item: long_final if item.id == "t-1" else "",
                lambda slug: "- remembered fact\npassword=hunter2",
            )
            note = (Path(tmp) / "Tasks/demo/t-1.md").read_text(encoding="utf-8")
            hub = (Path(tmp) / "Projects/demo.md").read_text(encoding="utf-8")
            files = sorted(path.name for path in (Path(tmp) / "Tasks/demo").iterdir())

        self.assertEqual(counts, (2, 1))
        self.assertEqual(files, ["t-1.md", "t-2.md"])
        self.assertIn("x" * 5000, note)
        self.assertIn(("details " * 200).rstrip(), note)
        self.assertIn("## Plan", note)
        self.assertIn("[[Tasks/demo/t-1]]", hub)
        self.assertIn("- remembered fact", hub)
        self.assertNotIn("hunter2", hub)

    def test_append_hub_entry_is_idempotent(self) -> None:
        task = TaskRecord(
            id="t-9", chat_id=1, user_id=1, project_slug="demo",
            project_name="demo", project_path="/tmp/demo", prompt="do thing",
        )
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))
            redactor = RedactionService()

            append_hub_entry(vault, task, redactor)
            append_hub_entry(vault, task, redactor)
            hub = (Path(tmp) / "Projects/demo.md").read_text(encoding="utf-8")

        self.assertEqual(hub.count("[[Tasks/demo/t-9]]"), 1)

    def test_refresh_project_memory_keeps_task_list(self) -> None:
        task = TaskRecord(
            id="t-5", chat_id=1, user_id=1, project_slug="demo",
            project_name="demo", project_path="/tmp/demo", prompt="ship it",
        )
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))
            append_hub_entry(vault, task, RedactionService())

            refresh_project_memory(vault, "demo", "- new fact")
            hub = (Path(tmp) / "Projects/demo.md").read_text(encoding="utf-8")

        self.assertIn("## Memory\n\n- new fact", hub)
        self.assertEqual(hub.count("## Tasks"), 1)
        self.assertIn("[[Tasks/demo/t-5]]", hub)

    def test_task_is_linked_to_every_context_project(self) -> None:
        task = TaskRecord(
            id="t-7", chat_id=1, user_id=1, project_slug="root",
            project_name="workspace-root", project_path="/home/stanislavv", prompt="check repo",
            phase="completed", context_project_slugs=["root", "codex-assistant"],
        )
        self.assertEqual(task_project_slugs(task), ["root", "codex-assistant"])
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))
            backfill_vault(vault, [task], lambda item: "done", lambda slug: "")
            hub = (Path(tmp) / "Projects/codex-assistant.md").read_text(encoding="utf-8")

        self.assertIn("[[Tasks/root/t-7]]", hub)

    def test_session_only_project_gets_hub_with_sessions_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))
            backfill_vault(
                vault, [], lambda item: "", lambda slug: "",
                session_lines={"manual-app": ["- 2026-10-11 codex s-1: fix it (/x.jsonl)"]},
            )
            hub = (Path(tmp) / "Projects/manual-app.md").read_text(encoding="utf-8")

        self.assertIn("## Sessions\n\n- 2026-10-11 codex s-1: fix it (/x.jsonl)", hub)

    def test_context_projects_are_linked_in_note_and_related_hubs(self) -> None:
        task = TaskRecord(
            id="t-12", chat_id=1, user_id=1, project_slug="app",
            project_name="app", project_path="/home/u/app", prompt="use lib", phase="completed",
            context_project_slugs=["app", "lib"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            vault = ObsidianVault(Path(tmp))
            backfill_vault(vault, [task], lambda item: "done", lambda slug: "")
            note = (Path(tmp) / "Tasks/app/t-12.md").read_text(encoding="utf-8")
            app_hub = (Path(tmp) / "Projects/app.md").read_text(encoding="utf-8")
            lib_hub = (Path(tmp) / "Projects/lib.md").read_text(encoding="utf-8")

        self.assertIn("Context projects: [[Projects/lib]]", note)
        self.assertIn("## Related projects\n\n- [[Projects/lib]] — 1 shared", app_hub)
        self.assertIn("- [[Projects/app]] — 1 shared", lib_hub)


if __name__ == "__main__":
    unittest.main()
