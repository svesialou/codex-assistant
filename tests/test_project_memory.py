from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.project_memory import ProjectMemoryStore


class ProjectMemoryStoreTest(unittest.TestCase):
    def test_append_project_memory_filters_secret_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProjectMemoryStore(root / "index", root / "state")

            result = store.append_project_memory(
                "demo",
                "Source repo authoritative\npassword=do-not-save",
                user_id=20,
            )
            memory = store.read_project_memory("demo")
            user_memory = store.load_user_memory(20)

        self.assertEqual(result.removed_lines, 1)
        self.assertIn("Source repo authoritative", memory)
        self.assertNotIn("password", memory.lower())
        self.assertEqual(user_memory.project_notes_saved, 1)

    def test_empty_after_filter_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProjectMemoryStore(root / "index", root / "state")

            result = store.append_project_memory("demo", "token=secret")

        self.assertEqual(result.text, "")
        self.assertFalse(store.project_memory_path("demo").exists())


if __name__ == "__main__":
    unittest.main()
