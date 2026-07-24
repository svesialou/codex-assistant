from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.project_index import ProjectInfo, build_index
from codex_telegram_bot.services.project_aliases import ProjectAliasStore


def project(slug: str, name: str | None = None, path: str | None = None) -> ProjectInfo:
    return ProjectInfo(
        slug=slug,
        name=name or slug,
        path=path or f"/tmp/{slug}",
        base="/tmp",
        is_git=False,
        branch=None,
        origin=None,
        languages=[],
        markers=[],
        docs=[],
        test_hints=[],
    )


class ProjectAliasStoreTest(unittest.TestCase):
    def test_exact_alias_beats_substring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectAliasStore(Path(tmp))
            store.add_alias("bot", "codex-assistant")
            projects = [
                project("codex-assistant"),
                project("company__bot-tools", name="bot-tools"),
            ]

            result = store.resolve(projects, "bot")

        self.assertIsNotNone(result.project)
        self.assertEqual(result.project.slug, "codex-assistant")
        self.assertEqual(result.matched_by, "alias")

    def test_alias_resolves_to_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectAliasStore(Path(tmp))
            store.add_alias("assistant", "codex-assistant")

            result = store.resolve([project("codex-assistant")], "assistant")

        self.assertIsNotNone(result.project)
        self.assertEqual(result.project.slug, "codex-assistant")

    def test_unknown_alias_returns_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectAliasStore(Path(tmp))

            result = store.resolve([project("codex-assistant")], "missing")

        self.assertIsNone(result.project)
        self.assertEqual([item.slug for item in result.suggestions], ["codex-assistant"])

    def test_reindex_does_not_delete_aliases_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "Projects"
            source = base / "codex-assistant"
            index = root / "index"
            source.mkdir(parents=True)
            (source / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            store = ProjectAliasStore(index)
            store.add_alias("bot", "codex-assistant")

            build_index([base], index)

            self.assertEqual(store.list_aliases()["bot"], "codex-assistant")


if __name__ == "__main__":
    unittest.main()
