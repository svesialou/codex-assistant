from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.project_index import ProjectInfo
from codex_telegram_bot.services.project_aliases import ProjectAliasStore
from codex_telegram_bot.services.project_resolver import ProjectResolver
from codex_telegram_bot.services.task_resolver import TaskResolver
from codex_telegram_bot.task_store import TaskStore


def project(slug: str) -> ProjectInfo:
    return ProjectInfo(
        slug=slug,
        name=slug,
        path=f"/tmp/{slug}",
        base="/tmp",
        is_git=False,
        branch=None,
        origin=None,
        languages=[],
        markers=[],
        docs=[],
        test_hints=[],
    )


class ProjectAndTaskResolverTest(unittest.TestCase):
    def test_project_resolver_uses_explicit_project_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aliases = ProjectAliasStore(Path(tmp) / "index")
            resolver = ProjectResolver(aliases)
            result = resolver.resolve(
                "поправь баг в проекте billing-api",
                [project("billing-api"), project("bot")],
            )

        self.assertIsNotNone(result.project)
        self.assertEqual(result.project.slug, "billing-api")
        self.assertGreater(result.confidence, 0.8)

    def test_project_resolver_uses_explicit_slug_hint_over_other_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aliases = ProjectAliasStore(Path(tmp) / "index")
            resolver = ProjectResolver(aliases)
            result = resolver.resolve(
                "перезапустить agent-sdlc. Правильный slug - metrics-etl-php",
                [project("root"), project("agent-sdlc"), project("metrics-etl-php")],
                default_project=project("root"),
            )

        self.assertIsNotNone(result.project)
        self.assertEqual(result.project.slug, "metrics-etl-php")

    def test_project_resolver_asks_when_root_context_has_no_project_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aliases = ProjectAliasStore(Path(tmp) / "index")
            resolver = ProjectResolver(aliases)
            result = resolver.resolve(
                "восстановить работу задачи BLA-447",
                [project("root"), project("metrics-etl-php"), project("agent-sdlc")],
                default_project=project("root"),
            )

        self.assertIsNone(result.project)
        self.assertEqual(
            [candidate.slug for candidate in result.candidates],
            ["metrics-etl-php", "agent-sdlc"],
        )

    def test_project_resolver_reports_ambiguous_project_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aliases = ProjectAliasStore(Path(tmp) / "index")
            resolver = ProjectResolver(aliases)
            result = resolver.resolve(
                "проверь metrics",
                [project("metrics-etl-php"), project("metrics-api")],
            )

        self.assertIsNone(result.project)
        self.assertEqual(
            [candidate.slug for candidate in result.candidates],
            ["metrics-etl-php", "metrics-api"],
        )

    def test_task_resolver_finds_continue_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            task = store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="billing-api",
                project_name="billing-api",
                project_path="/tmp/billing",
                prompt="Fix authorization token refresh",
            )
            task.phase = "completed"
            store.save_task(task)

            result = TaskResolver(store).resolve_message(
                10,
                "продолжи задачу про token refresh",
                project_slug="billing-api",
            )

        self.assertEqual(result.action, "resume_existing")
        self.assertIsNotNone(result.task)
        self.assertEqual(result.task.id, task.id)


if __name__ == "__main__":
    unittest.main()
