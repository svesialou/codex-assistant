from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DEFAULT_ENV_FILE = "~/.codex/secrets/telegram.env"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            values[key] = parsed[0] if parsed else ""
        except ValueError:
            values[key] = value.strip("'\"")

    return values


def merged_env() -> dict[str, str]:
    env = dict(os.environ)
    env_file = Path(env.get("CODEX_TELEGRAM_ENV_FILE", DEFAULT_ENV_FILE)).expanduser()
    file_values = load_env_file(env_file)

    merged = dict(file_values)
    merged.update(env)
    merged["CODEX_TELEGRAM_ENV_FILE"] = str(env_file)
    return merged


def parse_csv_ints(value: str | None) -> set[int]:
    if not value:
        return set()

    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        result.add(int(item))
    return result


def parse_csv_strings(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()

    result: list[str] = []
    seen: set[str] = set()
    for item in value.split(","):
        item = item.strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return tuple(result)


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def parse_path_list(value: str | None, default: Iterable[Path]) -> list[Path]:
    if not value:
        return [path.expanduser() for path in default]

    sep = ";" if ";" in value else ":"
    return [
        Path(os.path.expandvars(item.strip())).expanduser()
        for item in value.split(sep)
        if item.strip()
    ]


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_chat_ids: set[int]
    allowed_user_ids: set[int]
    project_roots: list[Path]
    index_dir: Path
    state_dir: Path
    codex_bin: str
    model: str | None
    poll_timeout_seconds: int
    prompt_debounce_seconds: float
    plan_timeout_seconds: int
    run_timeout_seconds: int | None
    transcribe_command: str | None
    transcribe_timeout_seconds: int
    env_file: Path
    recover_interrupted_tasks: bool = True
    workspace_root: Path | None = None
    slack_token: str | None = None
    slack_watch_dms: bool = True
    slack_channel_ids: tuple[str, ...] = ()
    slack_poll_interval_seconds: int = 60
    slack_history_limit: int = 20
    slack_target_chat_ids: set[int] = field(default_factory=set)
    slack_desktop_notifications: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        env = merged_env()
        bot_token = env.get("CODEX_TELEGRAM_BOT_TOKEN", "").strip()
        chat_ids = env.get("CODEX_TELEGRAM_ALLOWED_CHAT_IDS") or env.get(
            "CODEX_TELEGRAM_CHAT_ID"
        )

        run_timeout = int(env.get("CODEX_TELEGRAM_RUN_TIMEOUT_SECONDS", "0"))
        allowed_chat_ids = parse_csv_ints(chat_ids)
        allowed_user_ids = parse_csv_ints(
            env.get("CODEX_TELEGRAM_ALLOWED_USER_IDS")
            or env.get("CODEX_TELEGRAM_USER_ID")
        )
        if not allowed_user_ids:
            allowed_user_ids = {chat_id for chat_id in allowed_chat_ids if chat_id > 0}

        slack_target_chat_ids = parse_csv_ints(env.get("CODEX_SLACK_TELEGRAM_CHAT_IDS"))
        if not slack_target_chat_ids:
            slack_target_chat_ids = parse_csv_ints(chat_ids)
        return cls(
            bot_token=bot_token,
            allowed_chat_ids=allowed_chat_ids,
            allowed_user_ids=allowed_user_ids,
            project_roots=parse_path_list(
                env.get("CODEX_TELEGRAM_PROJECT_DIRS"),
                [Path("~/Projects"), Path("~/MyProjects")],
            ),
            index_dir=Path(
                os.path.expandvars(
                    env.get("CODEX_TELEGRAM_INDEX_DIR", "~/.codex/project-index")
                )
            ).expanduser(),
            state_dir=Path(
                os.path.expandvars(
                    env.get("CODEX_TELEGRAM_STATE_DIR", "~/.codex/telegram-bot")
                )
            ).expanduser(),
            codex_bin=env.get("CODEX_TELEGRAM_CODEX_BIN", "codex"),
            model=env.get("CODEX_TELEGRAM_MODEL") or None,
            poll_timeout_seconds=int(env.get("CODEX_TELEGRAM_POLL_TIMEOUT", "30")),
            prompt_debounce_seconds=float(
                env.get("CODEX_TELEGRAM_PROMPT_DEBOUNCE_SECONDS", "3")
            ),
            plan_timeout_seconds=int(
                env.get("CODEX_TELEGRAM_PLAN_TIMEOUT_SECONDS", "1800")
            ),
            run_timeout_seconds=run_timeout if run_timeout > 0 else None,
            transcribe_command=env.get("CODEX_TELEGRAM_TRANSCRIBE_CMD") or None,
            transcribe_timeout_seconds=int(
                env.get("CODEX_TELEGRAM_TRANSCRIBE_TIMEOUT_SECONDS", "300")
            ),
            env_file=Path(env["CODEX_TELEGRAM_ENV_FILE"]),
            recover_interrupted_tasks=parse_bool(
                env.get("CODEX_TELEGRAM_RECOVER_INTERRUPTED_TASKS"),
                True,
            ),
            workspace_root=Path(
                os.path.expandvars(
                    env.get("CODEX_TELEGRAM_WORKSPACE_ROOT", str(Path.home()))
                )
            ).expanduser(),
            slack_token=(
                env.get("CODEX_SLACK_TOKEN")
                or env.get("CODEX_SLACK_USER_TOKEN")
                or env.get("CODEX_SLACK_BOT_TOKEN")
                or None
            ),
            slack_watch_dms=parse_bool(env.get("CODEX_SLACK_WATCH_DMS"), True),
            slack_channel_ids=parse_csv_strings(
                env.get("CODEX_SLACK_PINNED_CHANNEL_IDS")
                or env.get("CODEX_SLACK_CHANNEL_IDS")
            ),
            slack_poll_interval_seconds=max(
                5,
                int(env.get("CODEX_SLACK_POLL_INTERVAL_SECONDS", "60")),
            ),
            slack_history_limit=max(
                1,
                int(env.get("CODEX_SLACK_HISTORY_LIMIT", "20")),
            ),
            slack_target_chat_ids=slack_target_chat_ids,
            slack_desktop_notifications=parse_bool(
                env.get("CODEX_SLACK_DESKTOP_NOTIFICATIONS"),
                True,
            ),
        )

    def slack_enabled(self) -> bool:
        return bool(
            self.slack_token
            and self.slack_target_chat_ids
            and (self.slack_watch_dms or self.slack_channel_ids)
        )

    def slack_desktop_enabled(self) -> bool:
        return bool(self.slack_desktop_notifications and self.slack_target_chat_ids)

    def validate_for_bot(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "CODEX_TELEGRAM_BOT_TOKEN is required. "
                "Put it into ~/.codex/secrets/telegram.env."
            )
        if not self.allowed_chat_ids:
            raise ValueError(
                "CODEX_TELEGRAM_ALLOWED_CHAT_IDS or CODEX_TELEGRAM_CHAT_ID is required."
            )
        if (
            any(chat_id < 0 for chat_id in self.allowed_chat_ids)
            and not self.allowed_user_ids
        ):
            raise ValueError(
                "CODEX_TELEGRAM_ALLOWED_USER_IDS is required for group chats."
            )
