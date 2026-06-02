from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from codex_telegram_bot.slack import (
    SlackStateStore,
    SlackWatcher,
    render_slack_notification,
    slack_ts_gt,
    slack_ts_key,
)


class FakeSlackAPI:
    def __init__(self) -> None:
        self.conversations: list[dict[str, Any]] = []
        self.channel_infos: dict[str, dict[str, Any]] = {}
        self.histories: dict[str, list[dict[str, Any]]] = {}
        self.users: dict[str, dict[str, Any]] = {
            "U_ME": {"id": "U_ME", "name": "me"},
            "U_ALICE": {
                "id": "U_ALICE",
                "name": "alice",
                "profile": {"display_name": "Alice"},
            },
            "U_BOB": {
                "id": "U_BOB",
                "name": "bob",
                "profile": {"real_name": "Bob"},
            },
        }

    def auth_test(self) -> dict[str, Any]:
        return {"user_id": "U_ME"}

    def list_conversations(self, types: str) -> list[dict[str, Any]]:
        assert types == "im,mpim"
        return self.conversations

    def conversation_info(self, channel_id: str) -> dict[str, Any]:
        return self.channel_infos[channel_id]

    def conversation_history(
        self,
        channel_id: str,
        oldest: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        messages = [
            message
            for message in self.histories.get(channel_id, [])
            if oldest is None or slack_ts_gt(str(message.get("ts") or ""), oldest)
        ]
        messages.sort(key=lambda message: slack_ts_key(message.get("ts")), reverse=True)
        return messages[:limit]

    def user_info(self, user_id: str) -> dict[str, Any]:
        return self.users[user_id]

    def permalink(self, channel_id: str, message_ts: str) -> str:
        return f"https://slack.example/{channel_id}/{message_ts}"


def make_watcher(
    tmp: str,
    api: FakeSlackAPI,
    channel_ids: tuple[str, ...] = (),
) -> SlackWatcher:
    return SlackWatcher(
        api=api,  # type: ignore[arg-type]
        state_store=SlackStateStore(Path(tmp)),
        watch_dms=True,
        channel_ids=channel_ids,
        history_limit=20,
        poll_interval_seconds=60,
        clock=lambda: 101.0,
    )


class SlackWatcherTest(unittest.TestCase):
    def test_first_poll_baselines_existing_dm_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeSlackAPI()
            api.conversations = [{"id": "D1", "is_im": True, "user": "U_ALICE"}]
            api.histories["D1"] = [
                {"ts": "100.000000", "user": "U_ALICE", "text": "old message"}
            ]
            watcher = make_watcher(tmp, api)

            first = watcher.poll_once()
            api.histories["D1"].append(
                {"ts": "102.000000", "user": "U_ALICE", "text": "new message"}
            )
            second = watcher.poll_once()
            third = watcher.poll_once()

        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].title, "Slack DM")
        self.assertEqual(second[0].sender, "Alice")
        self.assertEqual(second[0].text, "new message")
        self.assertEqual(third, [])

    def test_configured_channel_sends_pinned_chat_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeSlackAPI()
            api.channel_infos["C1"] = {"id": "C1", "name": "backend", "is_channel": True}
            api.histories["C1"] = [
                {"ts": "200.000000", "user": "U_ALICE", "text": "old channel message"}
            ]
            watcher = make_watcher(tmp, api, channel_ids=("C1",))

            watcher.poll_once()
            api.histories["C1"].append(
                {"ts": "201.000000", "user": "U_BOB", "text": "deploy finished"}
            )
            notifications = watcher.poll_once()

        self.assertEqual(len(notifications), 1)
        rendered = render_slack_notification(notifications[0])
        self.assertIn("Slack pinned chat", rendered)
        self.assertIn("From: Bob", rendered)
        self.assertIn("Where: backend", rendered)
        self.assertIn("deploy finished", rendered)
        self.assertIn("https://slack.example/C1/201.000000", rendered)

    def test_own_messages_are_skipped_and_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeSlackAPI()
            api.conversations = [{"id": "D1", "is_im": True, "user": "U_ALICE"}]
            api.histories["D1"] = []
            watcher = make_watcher(tmp, api)

            watcher.poll_once()
            api.histories["D1"].append(
                {"ts": "102.000000", "user": "U_ME", "text": "my reply"}
            )
            own_messages = watcher.poll_once()
            api.histories["D1"].append(
                {"ts": "103.000000", "user": "U_ALICE", "text": "alice reply"}
            )
            other_messages = watcher.poll_once()

        self.assertEqual(own_messages, [])
        self.assertEqual(len(other_messages), 1)
        self.assertEqual(other_messages[0].text, "alice reply")


if __name__ == "__main__":
    unittest.main()
