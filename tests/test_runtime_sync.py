from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.runtime_sync import (
    compare_runtime_files,
    render_runtime_drift,
)


class RuntimeSyncTest(unittest.TestCase):
    def test_compare_runtime_files_detects_same_and_different_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            installed = root / "installed"
            (source / "codex_telegram_bot").mkdir(parents=True)
            (installed / "codex_telegram_bot").mkdir(parents=True)
            same = "codex_telegram_bot/task_store.py"
            different = "codex_telegram_bot/bot.py"
            missing = "codex_telegram_bot/codex_runner.py"
            (source / same).write_text("same\n", encoding="utf-8")
            (installed / same).write_text("same\n", encoding="utf-8")
            (source / different).write_text("source\n", encoding="utf-8")
            (installed / different).write_text("installed\n", encoding="utf-8")
            (source / missing).write_text("source only\n", encoding="utf-8")

            result = compare_runtime_files(source, installed, [same, different, missing])
            summary = render_runtime_drift(result)

        by_path = {item.relative_path: item for item in result}
        self.assertTrue(by_path[same].same)
        self.assertFalse(by_path[different].same)
        self.assertFalse(by_path[missing].installed_exists)
        self.assertIn("2/3 checked files differ", summary)
        self.assertIn("different", summary)
        self.assertIn("installed missing", summary)

    def test_render_runtime_drift_reports_clean_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            installed = root / "installed"
            source.mkdir()
            installed.mkdir()
            (source / "file.py").write_text("same\n", encoding="utf-8")
            (installed / "file.py").write_text("same\n", encoding="utf-8")

            summary = render_runtime_drift(
                compare_runtime_files(source, installed, ["file.py"])
            )

        self.assertEqual(summary, "Runtime drift: none across 1 checked files.")


if __name__ == "__main__":
    unittest.main()
