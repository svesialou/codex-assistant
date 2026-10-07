from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .telegram_format import balance_code_fences, markdown_to_telegram_html


TELEGRAM_TEXT_LIMIT = 4096


class TelegramAPI:
    def __init__(self, bot_token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{bot_token}"

    def request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = urllib.parse.urlencode(
            {
                key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in payload.items()
                if value is not None
            }
        ).encode()
        request = urllib.request.Request(f"{self.base_url}/{method}", data=data)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"Telegram {method} failed: {exc.code} {body}") from exc

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = self.request("getUpdates", payload)
        if not response.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {response}")
        return response.get("result", [])

    def get_file(self, file_id: str) -> dict[str, Any]:
        response = self.request("getFile", {"file_id": file_id})
        if not response.get("ok"):
            raise RuntimeError(f"Telegram getFile failed: {response}")
        return response["result"]

    def download_file(self, file_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.file_base_url}/{file_path}"
        with urllib.request.urlopen(url, timeout=120) as response:
            destination.write_bytes(response.read())

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        first_message_id: int | None = None
        for chunk in balance_code_fences(split_message(text)):
            response = self.request_formatted(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "disable_web_page_preview": True,
                    "reply_markup": reply_markup,
                },
                chunk,
            )
            if first_message_id is None:
                result = response.get("result") or {}
                message_id = result.get("message_id")
                if isinstance(message_id, int):
                    first_message_id = message_id
            reply_markup = None
            time.sleep(0.05)
        return first_message_id

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.request_formatted(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
            text,
        )

    def request_formatted(
        self,
        method: str,
        payload: dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        """Send Markdown-ish text as Telegram HTML, falling back to plain text."""
        formatted = markdown_to_telegram_html(text)
        if len(formatted) <= TELEGRAM_TEXT_LIMIT:
            try:
                return self.request(
                    method,
                    {**payload, "text": formatted, "parse_mode": "HTML"},
                )
            except RuntimeError as exc:
                if "can't parse entities" not in str(exc).lower():
                    raise
        return self.request(method, {**payload, "text": text})

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.request(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        response = self.request("setMyCommands", {"commands": commands})
        if not response.get("ok"):
            raise RuntimeError(f"Telegram setMyCommands failed: {response}")

    def set_chat_menu_button(
        self,
        menu_button: dict[str, Any],
        chat_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {"menu_button": menu_button}
        if chat_id is not None:
            payload["chat_id"] = chat_id
        response = self.request("setChatMenuButton", payload)
        if not response.get("ok"):
            raise RuntimeError(f"Telegram setChatMenuButton failed: {response}")


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
