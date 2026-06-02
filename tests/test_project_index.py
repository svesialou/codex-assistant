from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.project_index import (
    ROOT_PROJECT_SLUG,
    build_index,
    discover_project_roots,
    load_index,
    resolve_project,
    search_projects,
)


class ProjectIndexTest(unittest.TestCase):
    def test_build_index_creates_project_and_agent_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Projects"
            project = base / "api-service"
            nested = project / "worker"
            output = Path(tmp) / "index"
            nested.mkdir(parents=True)
            (project / ".git").mkdir()
            (project / "README.md").write_text("# API Service\n", encoding="utf-8")
            (project / "go.mod").write_text("module example.test/api\n", encoding="utf-8")
            (nested / "AGENTS.md").write_text("# Worker Rules\n", encoding="utf-8")

            projects = build_index([base], output)
            slugs = {item.slug for item in projects}

            self.assertIn(ROOT_PROJECT_SLUG, slugs)
            self.assertIn("api-service", slugs)
            self.assertIn("api-service__worker", slugs)
            self.assertTrue((output / "projects.json").exists())
            self.assertTrue((output / "PROJECTS.md").exists())
            self.assertTrue((output / "agents" / "api-service.md").exists())
            self.assertIn(
                "API Service",
                (output / "agents" / "api-service.md").read_text(encoding="utf-8"),
            )

            loaded = load_index(output)
            resolved = resolve_project(loaded, "api-service")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.name, "api-service")

    def test_discovery_ignores_vendor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Projects"
            vendor = base / "app" / "vendor" / "package"
            vendor.mkdir(parents=True)
            (vendor / "composer.json").write_text("{}", encoding="utf-8")

            roots = discover_project_roots([base])

            self.assertNotIn(vendor.resolve(), roots)
            self.assertIn((base / "app").resolve(), roots)

    def test_search_projects_matches_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Projects"
            project = base / "php-app"
            output = Path(tmp) / "index"
            project.mkdir(parents=True)
            (project / "composer.json").write_text("{}", encoding="utf-8")

            projects = build_index([base], output)
            matches = search_projects(projects, "php")

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].slug, "php-app")


if __name__ == "__main__":
    unittest.main()
