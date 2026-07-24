from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from codex_telegram_bot.slack import (
    SlackDesktopNotificationStore,
    SlackDesktopNotificationWatcher,
    SlackStateStore,
    SlackWatcher,
    render_slack_notification,
    slack_desktop_notification_from_dbus_block,
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

    def test_repeated_message_id_is_sent_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeSlackAPI()
            api.conversations = [{"id": "D1", "is_im": True, "user": "U_ALICE"}]
            api.histories["D1"] = []
            watcher = make_watcher(tmp, api)

            watcher.poll_once()
            api.histories["D1"] = [
                {"ts": "102.000000", "user": "U_ALICE", "text": "new message"},
                {"ts": "102.000000", "user": "U_ALICE", "text": "new message"},
            ]
            first = watcher.poll_once()

            store = SlackStateStore(Path(tmp))
            state = store.load()
            state.channel_last_seen["D1"] = "101.000000"
            store.save(state)
            second = watcher.poll_once()

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].text, "new message")
        self.assertEqual(second, [])


class SlackDesktopNotificationTest(unittest.TestCase):
    @staticmethod
    def slack_block(body: str) -> list[str]:
        return [
            "method call time=1.0 sender=:1.10 -> destination=:1.20 "
            "path=/org/freedesktop/Notifications; "
            "interface=org.freedesktop.Notifications; member=Notify\n",
            '   string "Slack"\n',
            "   uint32 0\n",
            '   string "slack"\n',
            '   string "Alice"\n',
            f'   string "{body}"\n',
            "   array [\n",
            "   ]\n",
            "   array [\n",
            "      dict entry(\n",
            '         string "desktop-entry"\n',
            '         variant             string "slack"\n',
            "      )\n",
            "   ]\n",
            "   int32 -1\n",
        ]

    def test_parses_slack_dbus_notify_block(self) -> None:
        notification = slack_desktop_notification_from_dbus_block(
            self.slack_block("first line\\nsecond line")
        )

        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertEqual(notification.title, "Slack tray notification")
        self.assertEqual(notification.sender, "Alice")
        self.assertEqual(notification.channel_name, "system tray")
        self.assertEqual(notification.text, "first line\nsecond line")
        self.assertEqual(notification.permalink, "")

    def test_ignores_non_slack_dbus_notify_block(self) -> None:
        notification = slack_desktop_notification_from_dbus_block(
            [
                "method call time=1.0 sender=:1.10 -> destination=:1.20 "
                "path=/org/freedesktop/Notifications; "
                "interface=org.freedesktop.Notifications; member=Notify\n",
                '   string "Firefox"\n',
                "   uint32 0\n",
                '   string "firefox"\n',
                '   string "Docs"\n',
                '   string "Slack documentation changed"\n',
                "   array [\n",
                "   ]\n",
                "   array [\n",
                "   ]\n",
                "   int32 -1\n",
            ]
        )

        self.assertIsNone(notification)

    def test_desktop_watcher_dedupes_persisted_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SlackDesktopNotificationStore(Path(tmp))
            watcher = SlackDesktopNotificationWatcher(state_store=store)
            notifications = []

            watcher._emit_block(self.slack_block("same message"), notifications.append)
            watcher = SlackDesktopNotificationWatcher(state_store=store)
            watcher._emit_block(self.slack_block("same message"), notifications.append)

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].text, "same message")

    def test_desktop_watcher_sends_new_notification_after_old_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            watcher = SlackDesktopNotificationWatcher(
                state_store=SlackDesktopNotificationStore(Path(tmp)),
            )
            notifications = []

            watcher._emit_block(self.slack_block("old message"), notifications.append)
            watcher._emit_block(self.slack_block("new message"), notifications.append)

        self.assertEqual([item.text for item in notifications], ["old message", "new message"])


if __name__ == "__main__":
    unittest.main()
