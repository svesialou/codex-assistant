from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.task_events import TaskEventLog, render_task_events
from codex_telegram_bot.task_store import TaskStore


class TaskEventLogTest(unittest.TestCase):
    def test_append_and_list_events(self) -> None:
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
            events = TaskEventLog(store)

            events.append_event(task.id, "agent_message", "PM", "Принял задачу")
            events.append_event(task.id, "state_change", "Architect", "Planning started")
            loaded = events.list_events(task.id)
            rendered = render_task_events(task.id, loaded)

        self.assertEqual([event.agent for event in loaded], ["PM", "Architect"])
        self.assertIn("🧠 PM: Принял задачу", rendered)
        self.assertIn("🏗 Architect: Planning started", rendered)

    def test_list_events_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            task = store.create_task(10, 20, "demo", "demo", "/tmp/demo", "do work")
            events = TaskEventLog(store)
            for index in range(3):
                events.append_event(task.id, "state_change", "System", f"event {index}")

            loaded = events.list_events(task.id, limit=2)

        self.assertEqual([event.message for event in loaded], ["event 1", "event 2"])


if __name__ == "__main__":
    unittest.main()
