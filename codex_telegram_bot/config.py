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


def parse_optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def parse_path_list(value: str | None, default: Iterable[Path]) -> list[Path]:
    if not value:
        return [path.expanduser() for path in default]

    sep = ";" if ";" in value else ":"
    return [
        Path(os.path.expandvars(item.strip())).expanduser()
        for item in value.split(sep)
        if item.strip()
    ]


def normalize_tier(value: str | None, default: str = "auto") -> str:
    tier = (value or default).strip().lower()
    if tier not in {"auto", "cheap", "standard", "strong", "max"}:
        raise ValueError(f"Invalid model tier: {value}")
    return tier


TASK_PROVIDERS = ("codex", "claude")
DEFAULT_TASK_PROVIDER = "claude"
DEFAULT_CLAUDE_EXEC_COMMAND = (
    "claude -p --output-format stream-json --verbose --permission-mode bypassPermissions"
)
# Planning and agent chat must not touch the workspace.
DEFAULT_CLAUDE_READONLY_COMMAND = (
    "claude -p --output-format stream-json --verbose --permission-mode plan"
)
# Read-only reviewer transport for router "full" verify when CODEX_CLAUDE_CMD
# is not configured.
DEFAULT_CLAUDE_VERIFY_COMMAND = "claude -p --permission-mode plan --model {model}"


def normalize_task_provider(
    value: str | None,
    default: str = DEFAULT_TASK_PROVIDER,
) -> str:
    provider = (value or default).strip().lower()
    if provider not in TASK_PROVIDERS:
        raise ValueError(f"Invalid task provider: {value}")
    return provider


@dataclass(frozen=True)
class ClaudeModelConfig:
    cheap: str | None = None
    standard: str | None = None
    strong: str | None = None

    def for_tier(self, tier: str) -> str | None:
        if tier == "cheap":
            return self.cheap
        if tier == "standard":
            return self.standard or self.cheap
        if tier in {"strong", "max"}:
            return self.strong or self.standard or self.cheap
        return None


@dataclass(frozen=True)
class CodexModelConfig:
    cheap: str | None = None
    standard: str | None = None
    strong: str | None = None
    max: str | None = None

    def for_tier(self, tier: str) -> str | None:
        if tier == "cheap":
            return self.cheap or self.standard
        if tier == "standard":
            return self.standard or self.cheap
        if tier == "strong":
            return self.strong or self.standard or self.cheap
        if tier == "max":
            return self.max or self.strong or self.standard or self.cheap
        return self.standard or self.cheap


@dataclass(frozen=True)
class ModelConfig:
    claude: ClaudeModelConfig = field(default_factory=ClaudeModelConfig)
    codex: CodexModelConfig = field(default_factory=CodexModelConfig)


@dataclass(frozen=True)
class OrchestratorBudgetConfig:
    enabled: bool = True
    default_task_tier: str = "auto"
    max_tier_without_confirmation: str = "strong"
    allow_max_tier: bool = True
    require_confirmation_for_max_tier: bool = True
    monthly_token_budget: int | None = None
    daily_token_budget: int | None = None
    per_task_token_budget: dict[str, int] = field(
        default_factory=lambda: {
            "trivial": 2000,
            "small": 8000,
            "medium": 25000,
            "large": 60000,
            "critical": 100000,
        }
    )


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
    orchestrator_default_mode: bool = True
    orchestrator_fallback_to_codex: bool = True
    orchestrator_strict_mode: bool = False
    orchestrator_debate: bool = False
    orchestrator_max_review_rounds: int = 2
    orchestrator_model_routing: str = "auto"
    orchestrator_default_tier: str = "cheap"
    orchestrator_max_auto_tier: str = "strong"
    orchestrator_require_confirm_for_max: bool = True
    router_learning: bool = True
    router_auto_escalate: bool = True
    task_provider: str = DEFAULT_TASK_PROVIDER
    claude_enabled: bool = True
    claude_command: str | None = None
    claude_executor_command: str = DEFAULT_CLAUDE_EXEC_COMMAND
    claude_readonly_command: str = DEFAULT_CLAUDE_READONLY_COMMAND
    claude_resume_enabled: bool = True
    claude_timeout_seconds: int = 300
    claude_max_tokens: int = 8000
    memory_enabled: bool = True
    memory_auto_extract_after_task: bool = True
    memory_require_confirmation_for_sensitive: bool = True
    memory_max_items_per_project_context: int = 12
    memory_max_similar_tasks: int = 5
    memory_redact_secrets: bool = True
    models: ModelConfig = field(default_factory=ModelConfig)
    orchestrator_budget: OrchestratorBudgetConfig = field(
        default_factory=OrchestratorBudgetConfig
    )

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
        legacy_codex_model = env.get("CODEX_TELEGRAM_MODEL") or None
        codex_models = CodexModelConfig(
            cheap=env.get("CODEX_CHEAP_MODEL") or None,
            standard=env.get("CODEX_STANDARD_MODEL") or legacy_codex_model,
            strong=env.get("CODEX_STRONG_MODEL") or legacy_codex_model,
            max=env.get("CODEX_MAX_MODEL") or None,
        )
        claude_models = ClaudeModelConfig(
            cheap=env.get("CLAUDE_CHEAP_MODEL") or None,
            standard=env.get("CLAUDE_STANDARD_MODEL")
            or env.get("CLAUDE_ARCHITECT_MODEL")
            or env.get("CLAUDE_REVIEWER_MODEL")
            or None,
            strong=env.get("CLAUDE_STRONG_MODEL") or None,
        )
        per_task_budget = OrchestratorBudgetConfig().per_task_token_budget
        for key in list(per_task_budget):
            override = parse_optional_int(
                env.get(f"CODEX_ORCHESTRATOR_BUDGET_{key.upper()}_TOKENS")
            )
            if override is not None:
                per_task_budget[key] = override
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
            model=legacy_codex_model,
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
            orchestrator_default_mode=parse_bool(
                env.get("CODEX_TELEGRAM_ORCHESTRATOR")
                or env.get("CODEX_ORCHESTRATOR"),
                True,
            ),
            orchestrator_fallback_to_codex=parse_bool(
                env.get("CODEX_ORCHESTRATOR_FALLBACK_TO_CODEX"),
                True,
            ),
            orchestrator_strict_mode=parse_bool(
                env.get("CODEX_ORCHESTRATOR_STRICT_MODE"),
                False,
            ),
            orchestrator_debate=parse_bool(
                env.get("CODEX_ORCHESTRATOR_DEBATE"),
                False,
            ),
            orchestrator_max_review_rounds=max(
                0,
                int(env.get("CODEX_ORCHESTRATOR_MAX_REVIEW_ROUNDS", "2")),
            ),
            orchestrator_model_routing=(
                env.get("CODEX_ORCHESTRATOR_MODEL_ROUTING", "auto").strip().lower()
                or "auto"
            ),
            orchestrator_default_tier=normalize_tier(
                env.get("CODEX_ORCHESTRATOR_DEFAULT_TIER"),
                "cheap",
            ),
            orchestrator_max_auto_tier=normalize_tier(
                env.get("CODEX_ORCHESTRATOR_MAX_AUTO_TIER"),
                "strong",
            ),
            orchestrator_require_confirm_for_max=parse_bool(
                env.get("CODEX_ORCHESTRATOR_REQUIRE_CONFIRM_FOR_MAX"),
                True,
            ),
            router_learning=parse_bool(env.get("CODEX_ROUTER_LEARNING"), True),
            router_auto_escalate=parse_bool(env.get("CODEX_ROUTER_AUTO_ESCALATE"), True),
            task_provider=normalize_task_provider(
                env.get("CODEX_TELEGRAM_TASK_PROVIDER")
                or env.get("CODEX_TASK_PROVIDER"),
                DEFAULT_TASK_PROVIDER,
            ),
            claude_enabled=parse_bool(env.get("CODEX_CLAUDE_ENABLED"), True),
            claude_command=(
                env.get("CODEX_CLAUDE_CMD")
                or env.get("CODEX_TELEGRAM_CLAUDE_CMD")
                or env.get("CLAUDE_CMD")
                or None
            ),
            claude_executor_command=(
                env.get("CODEX_CLAUDE_EXEC_CMD")
                or env.get("CODEX_TELEGRAM_CLAUDE_EXEC_CMD")
                or DEFAULT_CLAUDE_EXEC_COMMAND
            ),
            claude_readonly_command=(
                env.get("CODEX_CLAUDE_READONLY_CMD")
                or env.get("CODEX_TELEGRAM_CLAUDE_READONLY_CMD")
                or DEFAULT_CLAUDE_READONLY_COMMAND
            ),
            claude_resume_enabled=parse_bool(
                env.get("CODEX_CLAUDE_RESUME_ENABLED"),
                True,
            ),
            claude_timeout_seconds=max(
                1,
                int(env.get("CODEX_CLAUDE_TIMEOUT_SECONDS", "300")),
            ),
            claude_max_tokens=max(
                1,
                int(env.get("CODEX_CLAUDE_MAX_TOKENS", "8000")),
            ),
            memory_enabled=parse_bool(env.get("CODEX_MEMORY_ENABLED"), True),
            memory_auto_extract_after_task=parse_bool(
                env.get("CODEX_MEMORY_AUTO_EXTRACT_AFTER_TASK"),
                True,
            ),
            memory_require_confirmation_for_sensitive=parse_bool(
                env.get("CODEX_MEMORY_REQUIRE_CONFIRMATION_FOR_SENSITIVE"),
                True,
            ),
            memory_max_items_per_project_context=max(
                1,
                int(env.get("CODEX_MEMORY_MAX_ITEMS_PER_PROJECT_CONTEXT", "12")),
            ),
            memory_max_similar_tasks=max(
                1,
                int(env.get("CODEX_MEMORY_MAX_SIMILAR_TASKS", "5")),
            ),
            memory_redact_secrets=parse_bool(env.get("CODEX_MEMORY_REDACT_SECRETS"), True),
            models=ModelConfig(claude=claude_models, codex=codex_models),
            orchestrator_budget=OrchestratorBudgetConfig(
                enabled=parse_bool(env.get("CODEX_ORCHESTRATOR_BUDGET_ENABLED"), True),
                default_task_tier=normalize_tier(
                    env.get("CODEX_ORCHESTRATOR_BUDGET_DEFAULT_TASK_TIER"),
                    "auto",
                ),
                max_tier_without_confirmation=normalize_tier(
                    env.get("CODEX_ORCHESTRATOR_MAX_TIER_WITHOUT_CONFIRMATION"),
                    "strong",
                ),
                allow_max_tier=parse_bool(
                    env.get("CODEX_ORCHESTRATOR_ALLOW_MAX_TIER"),
                    True,
                ),
                require_confirmation_for_max_tier=parse_bool(
                    env.get("CODEX_ORCHESTRATOR_REQUIRE_CONFIRM_FOR_MAX"),
                    True,
                ),
                monthly_token_budget=parse_optional_int(
                    env.get("CODEX_ORCHESTRATOR_MONTHLY_TOKEN_BUDGET")
                ),
                daily_token_budget=parse_optional_int(
                    env.get("CODEX_ORCHESTRATOR_DAILY_TOKEN_BUDGET")
                ),
                per_task_token_budget=per_task_budget,
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
