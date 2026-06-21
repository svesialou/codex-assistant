from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bot.config import (
    Config,
    load_env_file,
    parse_bool,
    parse_csv_ints,
    parse_csv_strings,
    parse_path_list,
)


class ConfigTest(unittest.TestCase):
    def test_load_env_file_supports_quotes_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telegram.env"
            path.write_text(
                """
# comment
export CODEX_TELEGRAM_BOT_TOKEN='token value'
CODEX_TELEGRAM_CHAT_ID=123
EMPTY=
""",
                encoding="utf-8",
            )

            values = load_env_file(path)

        self.assertEqual(values["CODEX_TELEGRAM_BOT_TOKEN"], "token value")
        self.assertEqual(values["CODEX_TELEGRAM_CHAT_ID"], "123")
        self.assertEqual(values["EMPTY"], "")

    def test_parse_csv_ints(self) -> None:
        self.assertEqual(parse_csv_ints("1, 2,,3"), {1, 2, 3})
        self.assertEqual(parse_csv_ints(""), set())

    def test_parse_csv_strings(self) -> None:
        self.assertEqual(parse_csv_strings("C1, C2,,C1"), ("C1", "C2"))
        self.assertEqual(parse_csv_strings(""), ())

    def test_parse_bool(self) -> None:
        self.assertTrue(parse_bool("yes"))
        self.assertTrue(parse_bool(None, default=True))
        self.assertFalse(parse_bool("0"))

    def test_parse_path_list(self) -> None:
        os.environ["CODEX_TEST_HOME"] = "/tmp/codex-test-home"
        paths = parse_path_list("/a:/b", [])
        self.assertEqual([str(path) for path in paths], ["/a", "/b"])
        expanded = parse_path_list("$CODEX_TEST_HOME/Projects", [])
        self.assertEqual(str(expanded[0]), "/tmp/codex-test-home/Projects")

    def test_private_chat_defaults_to_same_allowed_user_id(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/codex-test-home",
                "CODEX_TELEGRAM_BOT_TOKEN": "token",
                "CODEX_TELEGRAM_CHAT_ID": "123",
                "CODEX_TELEGRAM_WORKSPACE_ROOT": "/workspace",
            },
            clear=True,
        ):
            config = Config.from_env()

        self.assertEqual(config.allowed_chat_ids, {123})
        self.assertEqual(config.allowed_user_ids, {123})
        self.assertEqual(str(config.workspace_root), "/workspace")
        self.assertTrue(config.recover_interrupted_tasks)

    def test_interrupted_task_recovery_can_be_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/codex-test-home",
                "CODEX_TELEGRAM_BOT_TOKEN": "token",
                "CODEX_TELEGRAM_CHAT_ID": "123",
                "CODEX_TELEGRAM_RECOVER_INTERRUPTED_TASKS": "0",
            },
            clear=True,
        ):
            config = Config.from_env()

        self.assertFalse(config.recover_interrupted_tasks)

    def test_explicit_allowed_user_ids_override_private_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/codex-test-home",
                "CODEX_TELEGRAM_BOT_TOKEN": "token",
                "CODEX_TELEGRAM_CHAT_ID": "123",
                "CODEX_TELEGRAM_ALLOWED_USER_IDS": "456",
            },
            clear=True,
        ):
            config = Config.from_env()

        self.assertEqual(config.allowed_chat_ids, {123})
        self.assertEqual(config.allowed_user_ids, {456})

    def test_group_chat_requires_allowed_user_ids(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/codex-test-home",
                "CODEX_TELEGRAM_BOT_TOKEN": "token",
                "CODEX_TELEGRAM_CHAT_ID": "-100123",
            },
            clear=True,
        ):
            config = Config.from_env()

        with self.assertRaisesRegex(ValueError, "CODEX_TELEGRAM_ALLOWED_USER_IDS"):
            config.validate_for_bot()

    def test_slack_desktop_notifications_work_without_api_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/codex-test-home",
                "CODEX_TELEGRAM_BOT_TOKEN": "token",
                "CODEX_TELEGRAM_CHAT_ID": "123",
            },
            clear=True,
        ):
            config = Config.from_env()

        self.assertFalse(config.slack_enabled())
        self.assertTrue(config.slack_desktop_enabled())
        self.assertEqual(config.slack_target_chat_ids, {123})

    def test_slack_desktop_notifications_can_be_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/codex-test-home",
                "CODEX_TELEGRAM_BOT_TOKEN": "token",
                "CODEX_TELEGRAM_CHAT_ID": "123",
                "CODEX_SLACK_DESKTOP_NOTIFICATIONS": "0",
            },
            clear=True,
        ):
            config = Config.from_env()

        self.assertFalse(config.slack_desktop_enabled())


if __name__ == "__main__":
    unittest.main()
