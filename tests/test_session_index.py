from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.redaction import RedactionService
from codex_telegram_bot.services.session_index import (
    collect_relations,
    collect_session_lines,
    project_slug_for_cwd,
    scan_sessions,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class SessionIndexTest(unittest.TestCase):
    def test_scan_reads_codex_and_claude_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(
                root / "codex/2026/10/11/rollout-a.jsonl",
                [
                    {"type": "session_meta", "timestamp": "2026-10-11T09:00:00Z",
                     "payload": {"id": "codex-1", "cwd": "/work/app"}},
                    {"type": "response_item", "payload": {"role": "user", "content": [
                        {"type": "input_text", "text": "fix the login"}]}},
                ],
            )
            write_jsonl(
                root / "claude/-work-app/s-2.jsonl",
                [
                    {"type": "queue-operation"},
                    {"type": "user", "sessionId": "claude-2", "cwd": "/work/app",
                     "timestamp": "2026-10-10T08:00:00Z",
                     "message": {"content": "add tests"}},
                ],
            )

            refs = scan_sessions(root / "codex", root / "claude")

        self.assertEqual({(ref.source, ref.session_id, ref.cwd) for ref in refs},
                         {("codex", "codex-1", "/work/app"), ("claude", "claude-2", "/work/app")})
        self.assertEqual({ref.first_prompt for ref in refs}, {"fix the login", "add tests"})

    def test_cwd_maps_to_deepest_indexed_project(self) -> None:
        projects = [("root", "/home/u"), ("org-app", "/home/u/Projects/org/app")]

        self.assertEqual(project_slug_for_cwd("/home/u/Projects/org/app/src", projects), "org-app")
        self.assertEqual(project_slug_for_cwd("/home/u/notes", projects), "root")
        self.assertEqual(project_slug_for_cwd("/elsewhere/tool", projects), "tool")

    def test_collect_skips_bot_sessions_and_scratch_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(
                root / "codex/a.jsonl",
                [{"type": "session_meta", "timestamp": "2026-10-11T09:00:00Z",
                  "payload": {"id": "bot-run", "cwd": "/home/u/app"}}],
            )
            write_jsonl(
                root / "codex/b.jsonl",
                [{"type": "session_meta", "timestamp": "2026-10-11T10:00:00Z",
                  "payload": {"id": "manual", "cwd": "/home/u/app"}}],
            )
            write_jsonl(
                root / "codex/c.jsonl",
                [{"type": "session_meta", "timestamp": "2026-10-11T11:00:00Z",
                  "payload": {"id": "scratch", "cwd": "/tmp/e2e"}}],
            )
            refs = scan_sessions(root / "codex", None)

        lines = collect_session_lines(refs, [("app", "/home/u/app")], {"bot-run"}, RedactionService())

        self.assertEqual(list(lines), ["app"])
        self.assertEqual(len(lines["app"]), 1)
        self.assertIn("manual", lines["app"][0])

    def test_session_is_linked_to_every_project_it_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [("root", "/home/stanislavv"), ("app", "/home/stanislavv/MyProjects/app"),
                        ("lib", "/home/stanislavv/Projects/org/lib")]
            write_jsonl(
                root / "codex/s.jsonl",
                [{"type": "session_meta", "timestamp": "2026-10-11T09:00:00Z",
                  "payload": {"id": "multi", "cwd": "/home/stanislavv"}},
                 {"type": "response_item", "payload": {"role": "user", "content": [
                     {"type": "input_text", "text": "compare /home/stanislavv/MyProjects/app/a.py"}]}},
                 {"type": "event_msg", "payload": {"text": "also /home/stanislavv/Projects/org/lib/b.py"}}],
            )
            refs = scan_sessions(root / "codex", None, projects)

        lines = collect_session_lines(refs, projects, set(), RedactionService())

        self.assertEqual(set(lines), {"root", "app", "lib"})
        self.assertIn("also in [[Projects/app]], [[Projects/lib]]", lines["root"][0])
        self.assertIn("[[Projects/root]]", lines["app"][0])

    def test_relations_count_sessions_shared_between_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [("root", "/home/stanislavv"), ("app", "/home/stanislavv/MyProjects/app"),
                        ("lib", "/home/stanislavv/Projects/org/lib")]
            write_jsonl(
                root / "codex/s.jsonl",
                [{"type": "session_meta", "timestamp": "2026-10-11T09:00:00Z",
                  "payload": {"id": "multi", "cwd": "/home/stanislavv"}},
                 {"type": "event_msg", "payload": {"text": "/home/stanislavv/MyProjects/app/a.py and /home/stanislavv/Projects/org/lib/b.py"}}],
            )
            refs = scan_sessions(root / "codex", None, projects)

        relations = collect_relations(refs, projects, set())

        self.assertEqual(relations["app"]["lib"], 1)
        self.assertEqual(relations["lib"]["app"], 1)
        self.assertEqual(relations["root"]["app"], 1)

    def test_missing_roots_yield_no_sessions(self) -> None:
        self.assertEqual(scan_sessions(Path("/no/such/codex"), Path("/no/such/claude")), [])


if __name__ == "__main__":
    unittest.main()
