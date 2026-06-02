from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.task_store import ChatState, TaskStore


class TaskStoreTest(unittest.TestCase):
    def test_create_load_and_list_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            task = store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="do work",
            )
            task.phase = "planned"
            store.save_task(task)

            loaded = store.load_task(task.id)
            recent = store.recent_tasks(10)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.phase, "planned")
        self.assertEqual([item.id for item in recent], [task.id])

    def test_chat_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            state = ChatState(
                chat_id=10,
                selected_project_slug="demo",
                prompt_draft_action="new_task",
                prompt_draft_user_id=20,
                prompt_draft_project_slug="demo",
                prompt_draft_parts=["part 1", "part 2"],
            )
            store.save_chat_state(state)

            loaded = store.load_chat_state(10)
            states = store.chat_states()

        self.assertEqual(loaded.selected_project_slug, "demo")
        self.assertEqual(loaded.prompt_draft_parts, ["part 1", "part 2"])
        self.assertEqual([item.chat_id for item in states], [10])

    def test_tasks_filters_by_chat_phase_and_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            first = store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="do work",
            )
            first.phase = "running"
            store.save_task(first)
            second = store.create_task(
                chat_id=11,
                user_id=21,
                project_slug="other",
                project_name="other",
                project_path="/tmp/other",
                prompt="other work",
            )
            second.phase = "planned"
            store.save_task(second)

            tasks = store.tasks(
                phases={"running"},
                chat_ids={10},
                project_slug="demo",
            )

        self.assertEqual([task.id for task in tasks], [first.id])

    def test_add_attachment_persists_on_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            task = store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="do work",
            )

            updated = store.add_attachment(
                task.id,
                {
                    "kind": "document",
                    "file_name": "report.txt",
                    "file_path": "/tmp/report.txt",
                },
            )
            loaded = store.load_task(task.id)

        self.assertIsNotNone(updated)
        self.assertEqual(loaded.attachments[0]["file_name"], "report.txt")


if __name__ == "__main__":
    unittest.main()
