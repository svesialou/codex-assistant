from __future__ import annotations

import html
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any


LOG = logging.getLogger("codex_telegram_bot.slack")
SLACK_API_BASE = "https://slack.com/api"
SKIPPED_MESSAGE_SUBTYPES = {
    "channel_archive",
    "channel_join",
    "channel_leave",
    "channel_name",
    "channel_purpose",
    "channel_topic",
    "channel_unarchive",
    "message_changed",
    "message_deleted",
}


class SlackRateLimited(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"Slack API rate limited; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class SlackChannel:
    id: str
    name: str
    kind: str
    user_id: str = ""
    configured: bool = False


@dataclass(frozen=True)
class SlackNotification:
    title: str
    sender: str
    channel_name: str
    text: str
    permalink: str


@dataclass
class SlackState:
    initialized: bool = False
    last_poll_ts: str = ""
    channel_last_seen: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlackState":
        return cls(
            initialized=bool(data.get("initialized")),
            last_poll_ts=str(data.get("last_poll_ts") or ""),
            channel_last_seen={
                str(channel): str(ts)
                for channel, ts in (data.get("channel_last_seen") or {}).items()
                if channel and ts
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SlackStateStore:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "slack" / "state.json"

    def load(self) -> SlackState:
        if not self.path.exists():
            return SlackState()
        try:
            return SlackState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return SlackState()

    def save(self, state: SlackState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)


class SlackAPI:
    def __init__(self, token: str, timeout_seconds: int = 30) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        url = f"{SLACK_API_BASE}/{method}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") or "60"
                raise SlackRateLimited(int(retry_after)) from exc
            raise RuntimeError(
                f"Slack {method} failed: HTTP {exc.code} {self._error_from_response(exc)}"
            ) from exc

        if not payload.get("ok"):
            error = str(payload.get("error") or "unknown_error")
            raise RuntimeError(f"Slack {method} failed: {error}")
        return payload

    @staticmethod
    def _error_from_response(exc: urllib.error.HTTPError) -> str:
        body = exc.read().decode(errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body[:200]
        return str(payload.get("error") or payload.get("message") or "")[:200]

    def auth_test(self) -> dict[str, Any]:
        return self.request("auth.test")

    def list_conversations(self, types: str) -> list[dict[str, Any]]:
        conversations: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload = self.request(
                "conversations.list",
                {
                    "types": types,
                    "exclude_archived": "true",
                    "limit": 200,
                    "cursor": cursor,
                },
            )
            conversations.extend(payload.get("channels") or [])
            cursor = (payload.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return conversations

    def conversation_info(self, channel_id: str) -> dict[str, Any]:
        payload = self.request("conversations.info", {"channel": channel_id})
        return payload.get("channel") or {}

    def conversation_history(
        self,
        channel_id: str,
        oldest: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        payload = self.request(
            "conversations.history",
            {
                "channel": channel_id,
                "oldest": oldest,
                "inclusive": "false" if oldest else None,
                "limit": limit,
            },
        )
        return payload.get("messages") or []

    def user_info(self, user_id: str) -> dict[str, Any]:
        payload = self.request("users.info", {"user": user_id})
        return payload.get("user") or {}

    def permalink(self, channel_id: str, message_ts: str) -> str:
        payload = self.request(
            "chat.getPermalink",
            {"channel": channel_id, "message_ts": message_ts},
        )
        return str(payload.get("permalink") or "")


class SlackWatcher:
    def __init__(
        self,
        api: SlackAPI,
        state_store: SlackStateStore,
        watch_dms: bool,
        channel_ids: tuple[str, ...],
        history_limit: int,
        poll_interval_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api = api
        self.state_store = state_store
        self.watch_dms = watch_dms
        self.channel_ids = channel_ids
        self.history_limit = history_limit
        self.poll_interval_seconds = poll_interval_seconds
        self.clock = clock
        self._self_user_id: str | None = None
        self._user_cache: dict[str, str] = {}

    def run(
        self,
        stop_event: Event,
        send_notification: Callable[[SlackNotification], None],
    ) -> None:
        while not stop_event.is_set():
            wait_seconds = self.poll_interval_seconds
            try:
                for notification in self.poll_once():
                    send_notification(notification)
            except SlackRateLimited as exc:
                wait_seconds = max(wait_seconds, exc.retry_after_seconds)
                LOG.warning("slack polling rate limited; retry_after=%s", wait_seconds)
            except Exception:
                LOG.exception("slack polling failed")
            stop_event.wait(wait_seconds)

    def poll_once(self) -> list[SlackNotification]:
        self_user_id = self.self_user_id()
        state = self.state_store.load()
        poll_started_ts = slack_time(self.clock())
        initializing = not state.initialized
        notifications: list[SlackNotification] = []

        for channel in self.watched_channels():
            last_seen = state.channel_last_seen.get(channel.id)
            oldest = None if initializing else (last_seen or state.last_poll_ts or None)
            messages = self.api.conversation_history(
                channel.id,
                oldest=oldest,
                limit=self.history_limit,
            )

            if not messages:
                if not last_seen:
                    state.channel_last_seen[channel.id] = state.last_poll_ts or poll_started_ts
                continue

            newest_ts = newest_message_ts(messages)
            if initializing:
                state.channel_last_seen[channel.id] = newest_ts or poll_started_ts
                continue

            floor_ts = last_seen or state.last_poll_ts
            for message in sorted(messages, key=lambda item: slack_ts_key(item.get("ts"))):
                ts = str(message.get("ts") or "")
                if floor_ts and not slack_ts_gt(ts, floor_ts):
                    continue
                if not self.should_notify(channel, message, self_user_id):
                    continue
                notifications.append(self.build_notification(channel, message))

            if newest_ts:
                state.channel_last_seen[channel.id] = max_slack_ts(last_seen, newest_ts)

        state.initialized = True
        state.last_poll_ts = poll_started_ts
        self.state_store.save(state)
        return notifications

    def self_user_id(self) -> str:
        if self._self_user_id is None:
            auth = self.api.auth_test()
            self._self_user_id = str(auth.get("user_id") or "")
        return self._self_user_id

    def watched_channels(self) -> list[SlackChannel]:
        channels: dict[str, SlackChannel] = {}
        if self.watch_dms:
            for payload in self.api.list_conversations("im,mpim"):
                channel = self.channel_from_payload(payload, configured=False)
                if channel is not None:
                    channels[channel.id] = channel

        for channel_id in self.channel_ids:
            if channel_id in channels:
                channels[channel_id] = replace(channels[channel_id], configured=True)
                continue
            try:
                payload = self.api.conversation_info(channel_id)
            except Exception:
                LOG.exception("failed to load configured slack channel id=%s", channel_id)
                continue
            channel = self.channel_from_payload(payload, configured=True)
            if channel is not None:
                channels[channel.id] = channel

        return list(channels.values())

    def channel_from_payload(
        self,
        payload: dict[str, Any],
        configured: bool,
    ) -> SlackChannel | None:
        channel_id = str(payload.get("id") or "")
        if not channel_id:
            return None

        user_id = str(payload.get("user") or "")
        if payload.get("is_im"):
            kind = "dm"
            name = self.user_name(user_id) if user_id else "direct message"
        elif payload.get("is_mpim"):
            kind = "mpim"
            name = str(payload.get("name") or "group direct message")
        elif payload.get("is_private"):
            kind = "private_channel"
            name = str(payload.get("name") or payload.get("name_normalized") or channel_id)
        else:
            kind = "channel"
            name = str(payload.get("name") or payload.get("name_normalized") or channel_id)

        return SlackChannel(
            id=channel_id,
            name=name,
            kind=kind,
            user_id=user_id,
            configured=configured,
        )

    def should_notify(
        self,
        channel: SlackChannel,
        message: dict[str, Any],
        self_user_id: str,
    ) -> bool:
        if not message.get("ts") or message.get("hidden"):
            return False
        if message.get("subtype") in SKIPPED_MESSAGE_SUBTYPES:
            return False
        if self_user_id and message.get("user") == self_user_id:
            return False
        return channel.kind in {"dm", "mpim"} or channel.configured

    def build_notification(
        self,
        channel: SlackChannel,
        message: dict[str, Any],
    ) -> SlackNotification:
        permalink = ""
        try:
            permalink = self.api.permalink(channel.id, str(message.get("ts") or ""))
        except Exception:
            LOG.exception("failed to build slack permalink channel=%s", channel.id)

        return SlackNotification(
            title=notification_title(channel),
            sender=self.message_sender(message),
            channel_name=channel.name,
            text=message_text(message),
            permalink=permalink,
        )

    def message_sender(self, message: dict[str, Any]) -> str:
        user_id = str(message.get("user") or "")
        if user_id:
            return self.user_name(user_id)

        bot_profile = message.get("bot_profile") or {}
        if bot_profile.get("name"):
            return str(bot_profile["name"])
        if message.get("username"):
            return str(message["username"])
        if message.get("bot_id"):
            return str(message["bot_id"])
        return "Slack"

    def user_name(self, user_id: str) -> str:
        if not user_id:
            return "Slack"
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        try:
            user = self.api.user_info(user_id)
        except Exception:
            LOG.exception("failed to load slack user id=%s", user_id)
            self._user_cache[user_id] = user_id
            return user_id

        profile = user.get("profile") or {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        self._user_cache[user_id] = str(name)
        return self._user_cache[user_id]


def render_slack_notification(notification: SlackNotification) -> str:
    lines = [
        notification.title,
        f"From: {notification.sender}",
        f"Where: {notification.channel_name}",
        "",
        notification.text,
    ]
    if notification.permalink:
        lines.extend(["", notification.permalink])
    return "\n".join(lines)


def notification_title(channel: SlackChannel) -> str:
    if channel.configured:
        return "Slack pinned chat"
    if channel.kind == "mpim":
        return "Slack group DM"
    return "Slack DM"


def message_text(message: dict[str, Any], limit: int = 1200) -> str:
    text = str(message.get("text") or "").replace("\x00", "").strip()
    if not text and message.get("files"):
        names = [
            str(file.get("name") or file.get("title") or "file")
            for file in message.get("files", [])
        ]
        text = f"Shared file: {', '.join(names)}"
    if not text:
        text = "Message has no text."

    text = html.unescape(text)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}..."


def slack_time(timestamp: float) -> str:
    return f"{timestamp:.6f}"


def slack_ts_key(value: Any) -> tuple[int, int]:
    raw = str(value or "0")
    seconds, _, fraction = raw.partition(".")
    try:
        seconds_int = int(seconds or "0")
    except ValueError:
        seconds_int = 0
    fraction = "".join(character for character in fraction if character.isdigit())
    fraction_int = int((fraction + "000000")[:6] or "0")
    return seconds_int, fraction_int


def slack_ts_gt(left: str, right: str) -> bool:
    return slack_ts_key(left) > slack_ts_key(right)


def max_slack_ts(left: str | None, right: str | None) -> str:
    if not left:
        return right or ""
    if not right:
        return left
    return left if slack_ts_key(left) >= slack_ts_key(right) else right


def newest_message_ts(messages: list[dict[str, Any]]) -> str:
    newest = ""
    for message in messages:
        newest = max_slack_ts(newest, str(message.get("ts") or ""))
    return newest
