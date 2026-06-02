from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.config import (
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


if __name__ == "__main__":
    unittest.main()
