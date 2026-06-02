from __future__ import annotations

import unittest

from codex_telegram_bot.telegram_api import TelegramAPI, split_message


class FakeTelegramAPI(TelegramAPI):
    def __init__(self) -> None:
        super().__init__("token")
        self.requests: list[tuple[str, dict]] = []

    def request(self, method: str, payload: dict) -> dict:
        self.requests.append((method, payload))
        return {"ok": True, "result": {"message_id": len(self.requests)}}


class TelegramApiTest(unittest.TestCase):
    def test_split_message_keeps_short_text(self) -> None:
        self.assertEqual(split_message("hello"), ["hello"])

    def test_split_message_chunks_long_text(self) -> None:
        chunks = split_message("a" * 10, limit=4)
        self.assertEqual(chunks, ["aaaa", "aaaa", "aa"])

    def test_send_message_returns_first_message_id(self) -> None:
        api = FakeTelegramAPI()

        message_id = api.send_message(10, "hello")

        self.assertEqual(message_id, 1)
        self.assertEqual(api.requests[0][0], "sendMessage")

    def test_edit_message_text_uses_telegram_edit_method(self) -> None:
        api = FakeTelegramAPI()

        api.edit_message_text(10, 22, "updated")

        self.assertEqual(api.requests[0][0], "editMessageText")
        self.assertEqual(api.requests[0][1]["chat_id"], 10)
        self.assertEqual(api.requests[0][1]["message_id"], 22)
        self.assertEqual(api.requests[0][1]["text"], "updated")

    def test_set_my_commands_uses_telegram_method(self) -> None:
        api = FakeTelegramAPI()

        api.set_my_commands([{"command": "menu", "description": "Show main menu"}])

        self.assertEqual(api.requests[0][0], "setMyCommands")
        self.assertEqual(
            api.requests[0][1]["commands"],
            [{"command": "menu", "description": "Show main menu"}],
        )

    def test_set_chat_menu_button_uses_telegram_method(self) -> None:
        api = FakeTelegramAPI()

        api.set_chat_menu_button({"type": "commands"}, chat_id=10)

        self.assertEqual(api.requests[0][0], "setChatMenuButton")
        self.assertEqual(api.requests[0][1]["chat_id"], 10)
        self.assertEqual(api.requests[0][1]["menu_button"], {"type": "commands"})


if __name__ == "__main__":
    unittest.main()
