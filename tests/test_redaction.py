from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bot.config import Config
from codex_telegram_bot.services.llm_provider import ClaudeProvider, LlmRequest
from codex_telegram_bot.services.redaction import RedactionService


class RedactionServiceTest(unittest.TestCase):
    def test_redacts_secret_lines_and_values(self) -> None:
        result = RedactionService().redact_text(
            "normal line\nAPI_TOKEN=secret-value\nprivate key here\n"
        )

        self.assertIn("normal line", result.text)
        self.assertIn("API_TOKEN=[REDACTED]", result.text)
        self.assertNotIn("secret-value", result.text)
        self.assertIn("[REDACTED: potential secret line]", result.text)
        self.assertEqual(result.redacted_lines, 2)

    def test_claude_provider_passes_model_through_env_without_forced_flags(self) -> None:
        config = Config(
            bot_token="token",
            allowed_chat_ids={1},
            allowed_user_ids=set(),
            project_roots=[],
            index_dir=Path("/tmp/index"),
            state_dir=Path("/tmp/state"),
            codex_bin="codex",
            model=None,
            poll_timeout_seconds=30,
            prompt_debounce_seconds=60,
            plan_timeout_seconds=60,
            run_timeout_seconds=None,
            transcribe_command=None,
            transcribe_timeout_seconds=300,
            env_file=Path("/tmp/telegram.env"),
            claude_command="claude-wrapper",
        )
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]

            class Completed:
                returncode = 0
                stdout = "{}"
                stderr = ""

            return Completed()

        with patch("codex_telegram_bot.services.llm_provider.subprocess.run", fake_run):
            response = ClaudeProvider(config).complete(
                LlmRequest(role="architect", prompt="hello", model="claude-standard")
            )

        self.assertEqual(response.text, "{}")
        self.assertEqual(captured["command"], ["claude-wrapper"])
        self.assertEqual(captured["env"]["LLM_MODEL"], "claude-standard")


if __name__ == "__main__":
    unittest.main()
