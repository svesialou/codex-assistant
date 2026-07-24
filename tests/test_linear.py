from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from codex_telegram_bot import linear


class LinearMCPCompatibilityTest(unittest.TestCase):
    def test_legacy_command_does_not_require_linear_oauth_settings(self) -> None:
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = linear.main(["whoami"])

        self.assertEqual(code, 2)
        self.assertIn("Codex MCP server", stderr.getvalue())
        self.assertIn("codex mcp login linear", stderr.getvalue())
        self.assertNotIn("CODEX_LINEAR_CLIENT_ID", stderr.getvalue())

    def test_status_delegates_to_codex_mcp_get(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], check: bool) -> mock.Mock:
            calls.append(command)
            return mock.Mock(returncode=0)

        with mock.patch(
            "codex_telegram_bot.linear.shutil.which",
            return_value="/usr/bin/codex",
        ):
            with mock.patch(
                "codex_telegram_bot.linear.subprocess.run",
                side_effect=fake_run,
            ):
                code = linear.main(["status"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, [["/usr/bin/codex", "mcp", "get", "linear"]])

    def test_help_without_command_is_successful(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            code = linear.main([])

        self.assertEqual(code, 0)
        self.assertIn("status", stdout.getvalue())
        self.assertIn("login", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
