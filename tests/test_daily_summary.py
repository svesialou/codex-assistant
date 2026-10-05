from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_telegram_bot.daily_summary import (
    Activity,
    build_report,
    collect_codex_activities,
    marker_path,
    previous_workday,
    run_daily_summary,
)
from codex_telegram_bot.task_store import TaskStore


MINSK = ZoneInfo("Europe/Minsk")


class DailySummaryTest(unittest.TestCase):
    def test_previous_workday_skips_weekend(self) -> None:
        self.assertEqual(previous_workday(date(2026, 8, 3)), date(2026, 7, 31))
        self.assertEqual(previous_workday(date(2026, 8, 4)), date(2026, 8, 3))

    def test_report_deduplicates_planning_and_execution_and_redacts_secret(self) -> None:
        activities = [
            Activity(
                request="Implement BLA-10",
                result="Plan only",
                phase="agent_completed",
                created_at="2026-07-31T07:00:00+00:00",
                priority=1,
            ),
            Activity(
                request="Implement BLA-10",
                result="- Changed: Feature implemented.\n- Why: Required.",
                phase="completed",
                created_at="2026-07-31T08:00:00+00:00",
                priority=2,
            ),
            Activity(
                request="API_TOKEN=do-not-send",
                result="",
                phase="running",
                created_at="2026-07-31T09:00:00+00:00",
            ),
        ]

        report = build_report(date(2026, 7, 31), activities)

        self.assertIn("Рабочая сводка за пятницу, 31 июля 2026", report)
        self.assertEqual(report.count("Implement BLA-10"), 1)
        self.assertIn("Feature implemented.", report)
        self.assertNotIn("do-not-send", report)
        self.assertIn("Не доделано:", report)

    def test_report_explicitly_states_when_no_activity_was_found(self) -> None:
        report = build_report(date(2026, 8, 3), [])

        self.assertIn("Локальной активности Telegram/Codex не найдено", report)
        self.assertIn("Нет зафиксированных завершений", report)
        self.assertIn("Нет зафиксированных незавершённых действий", report)

    def test_collect_codex_activity_ignores_bot_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions" / "2026" / "07" / "31"
            sessions.mkdir(parents=True)
            direct = sessions / "direct.jsonl"
            automated = sessions / "automated.jsonl"
            direct.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-07-31T08:00:00+00:00",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": "Inspect BLA-20"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-07-31T08:10:00+00:00",
                                "type": "event_msg",
                                "payload": {
                                    "type": "agent_message",
                                    "phase": "final_answer",
                                    "message": "Inspection completed",
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            automated.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-31T09:00:00+00:00",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "You are preparing a Codex task that came from a local Telegram bot.",
                        },
                    }
                ),
                encoding="utf-8",
            )

            activities = collect_codex_activities(Path(tmp) / "sessions", date(2026, 7, 31), MINSK)

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].request, "Inspect BLA-20")
        self.assertTrue(activities[0].done)

    def test_run_sends_once_and_reports_unfinished_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            store = TaskStore(state_dir)
            completed = store.create_task(10, 20, "root", "Root", "/tmp", "Finish BLA-30")
            completed.created_at = "2026-07-31T08:00:00+00:00"
            completed.updated_at = "2026-07-31T08:30:00+00:00"
            completed.phase = "completed"
            completed.final_path = str(store.task_dir(completed.id) / "final.md")
            Path(completed.final_path).write_text(
                "- Changed: BLA-30 finished.\n- Why: Required.", encoding="utf-8"
            )
            store.save_task(completed)
            running = store.create_task(10, 20, "root", "Root", "/tmp", "Monitor BLA-31")
            running.created_at = "2026-07-31T09:00:00+00:00"
            running.updated_at = "2026-07-31T09:10:00+00:00"
            running.phase = "running"
            store.save_task(running)
            sent: list[tuple[int, str]] = []

            report = run_daily_summary(
                state_dir=state_dir,
                sessions_root=Path(tmp) / "sessions",
                chat_id=10,
                target=date(2026, 7, 31),
                local_timezone=MINSK,
                summarizer=None,
                sender=lambda chat_id, text: sent.append((chat_id, text)),
            )
            second = run_daily_summary(
                state_dir=state_dir,
                sessions_root=Path(tmp) / "sessions",
                chat_id=10,
                target=date(2026, 7, 31),
                local_timezone=MINSK,
                summarizer=None,
                sender=lambda chat_id, text: sent.append((chat_id, text)),
            )
            marker_created = marker_path(state_dir, date(2026, 7, 31)).exists()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 10)
        self.assertEqual(sent[0][1], report)
        self.assertIn("BLA-30 finished", report)
        self.assertIn("Monitor BLA-31", report)
        self.assertIn("already sent", second)
        self.assertTrue(marker_created)


if __name__ == "__main__":
    unittest.main()
