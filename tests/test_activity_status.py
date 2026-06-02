from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.activity_status import (
    extract_codex_activity,
    render_codex_status,
)
from codex_telegram_bot.task_store import TaskRecord


class ActivityStatusTest(unittest.TestCase):
    def test_extract_codex_activity_uses_latest_safe_codex_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "agent.log"
            log_path.write_text(
                "\n".join(
                    [
                        "OpenAI Codex v0.135.0",
                        "user",
                        "original prompt",
                        "codex",
                        "Читаю локальные инструкции.",
                        "codex",
                        "Проверяю planning flow.",
                    ]
                ),
                encoding="utf-8",
            )

            activity = extract_codex_activity(log_path)

        self.assertEqual(activity, "Проверяю planning flow.")

    def test_extract_codex_activity_masks_secret_like_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "agent.log"
            log_path.write_text(
                "codex\n"
                "Проверяю CODEX_TELEGRAM_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n",
                encoding="utf-8",
            )

            activity = extract_codex_activity(log_path)

        self.assertIsNotNone(activity)
        self.assertIn("CODEX_TELEGRAM_BOT_TOKEN=<hidden>", activity or "")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", activity or "")

    def test_render_codex_status_is_short_and_task_scoped(self) -> None:
        task = TaskRecord(
            id="task-1",
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="do work",
        )

        text = render_codex_status(task, "planning", "Читаю файлы." * 500)

        self.assertIn("Статус Codex", text)
        self.assertIn("Задача: task-1", text)
        self.assertIn("Проект: demo", text)
        self.assertLessEqual(len(text), 1800)


if __name__ == "__main__":
    unittest.main()
