from __future__ import annotations

import unittest

from codex_telegram_bot.telegram_api import TelegramAPI
from codex_telegram_bot.telegram_format import (
    balance_code_fences,
    markdown_to_telegram_html,
)


class TelegramFormatTest(unittest.TestCase):
    def test_converts_agent_final_answer_markdown(self) -> None:
        text = (
            "## Итог\n"
            "- **Changed:** `bot.py` <3 & co\n"
            "- snake_case_name stays *as is*\n"
            "```python\n"
            "if a < b and c:\n"
            "```"
        )

        html = markdown_to_telegram_html(text)

        self.assertEqual(
            html,
            "<b>Итог</b>\n"
            "• <b>Changed:</b> <code>bot.py</code> &lt;3 &amp; co\n"
            "• snake_case_name stays *as is*\n"
            "<pre><code>if a &lt; b and c:</code></pre>",
        )

    def test_unclosed_fence_is_closed_and_reopened_across_chunks(self) -> None:
        chunks = balance_code_fences(["text\n```\ncode 1", "code 2\n```\nafter"])

        self.assertEqual(chunks, ["text\n```\ncode 1\n```", "```\ncode 2\n```\nafter"])


class ParseErrorAPI(TelegramAPI):
    def __init__(self) -> None:
        super().__init__("token")
        self.requests: list[dict] = []

    def request(self, method: str, payload: dict) -> dict:
        self.requests.append(payload)
        if payload.get("parse_mode") == "HTML":
            raise RuntimeError("Telegram sendMessage failed: 400 Bad Request: can't parse entities")
        return {"ok": True, "result": {"message_id": 7}}


class TelegramApiFormattingTest(unittest.TestCase):
    def test_send_message_uses_html_and_falls_back_to_plain_text(self) -> None:
        api = ParseErrorAPI()

        message_id = api.send_message(10, "**Changed:** done")

        self.assertEqual(message_id, 7)
        self.assertEqual(api.requests[0]["text"], "<b>Changed:</b> done")
        self.assertEqual(api.requests[0]["parse_mode"], "HTML")
        self.assertEqual(api.requests[1]["text"], "**Changed:** done")
        self.assertNotIn("parse_mode", api.requests[1])


if __name__ == "__main__":
    unittest.main()
