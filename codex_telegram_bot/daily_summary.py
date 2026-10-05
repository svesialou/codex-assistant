from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from .config import merged_env
from .services.redaction import RedactionService
from .task_store import TaskRecord, TaskStore, now_iso
from .telegram_api import TelegramAPI


DEFAULT_TIMEZONE = "Europe/Minsk"
DONE_PHASES = {"completed", "agent_completed"}
AUTOMATION_PROMPT_MARKERS = (
    "You are preparing a Codex task that came from a local Telegram bot.",
    "The user confirmed this Telegram task.",
    "The user explicitly started this Telegram task",
    "The user asked to continue a completed Telegram Codex task.",
    "You are Codex running as a read-only AI agent for a Telegram chat.",
)
WEEKDAY_NAMES = {
    0: "понедельник",
    1: "вторник",
    2: "среду",
    3: "четверг",
    4: "пятницу",
}
MONTH_NAMES = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


@dataclass(frozen=True)
class Activity:
    request: str
    result: str
    phase: str
    created_at: str
    priority: int = 0

    @property
    def done(self) -> bool:
        return self.phase in DONE_PHASES


def previous_workday(today: date) -> date:
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def happened_on(value: str, target: date, local_timezone: ZoneInfo) -> bool:
    parsed = parse_timestamp(value)
    return parsed is not None and parsed.astimezone(local_timezone).date() == target


def normalize_text(value: str, max_chars: int | None = None) -> str:
    text = " ".join(value.split())
    if max_chars is not None and len(text) > max_chars:
        return text[: max(1, max_chars - 1)].rstrip() + "…"
    return text


def read_task_result(task: TaskRecord) -> str:
    if task.final_path:
        try:
            text = Path(task.final_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text.strip():
            return text.strip()
    return task.error.strip()


def collect_task_activities(
    store: TaskStore,
    chat_id: int,
    target: date,
    local_timezone: ZoneInfo,
) -> list[Activity]:
    activities: list[Activity] = []
    for task in store.tasks(chat_ids={chat_id}):
        if not (
            happened_on(task.created_at, target, local_timezone)
            or happened_on(task.updated_at, target, local_timezone)
        ):
            continue
        request = task.original_user_request.strip() or task.prompt.strip()
        if not request:
            continue
        activities.append(
            Activity(
                request=request,
                result=read_task_result(task),
                phase=task.phase,
                created_at=task.created_at,
                priority=2 if task.kind != "agent_chat" else 1,
            )
        )
    return activities


def _event_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def parse_codex_session(path: Path, target: date, local_timezone: ZoneInfo) -> list[Activity]:
    activities: list[Activity] = []
    current_request = ""
    current_created_at = ""
    session_requests: list[str] = []

    try:
        lines = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return []

    with lines:
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue

            event_type = payload.get("type")
            if event_type == "user_message":
                request = _event_text(payload, "message")
                if not request or not happened_on(
                    str(record.get("timestamp") or ""), target, local_timezone
                ):
                    continue
                if current_request:
                    activities.append(
                        Activity(
                            request=current_request,
                            result="",
                            phase="running",
                            created_at=current_created_at,
                        )
                    )
                current_request = request
                current_created_at = str(record.get("timestamp") or "")
                session_requests.append(request)
                continue

            if (
                event_type == "agent_message"
                and payload.get("phase") == "final_answer"
                and current_request
            ):
                activities.append(
                    Activity(
                        request=current_request,
                        result=_event_text(payload, "message"),
                        phase="completed",
                        created_at=current_created_at,
                    )
                )
                current_request = ""
                current_created_at = ""

    if current_request:
        activities.append(
            Activity(
                request=current_request,
                result="",
                phase="running",
                created_at=current_created_at,
            )
        )

    if any(marker in request for request in session_requests for marker in AUTOMATION_PROMPT_MARKERS):
        return []
    return activities


def collect_codex_activities(
    sessions_root: Path,
    target: date,
    local_timezone: ZoneInfo,
) -> list[Activity]:
    activities: list[Activity] = []
    seen_paths: set[Path] = set()
    for offset in (-1, 0, 1):
        candidate = target + timedelta(days=offset)
        directory = sessions_root / f"{candidate:%Y}" / f"{candidate:%m}" / f"{candidate:%d}"
        for path in directory.glob("*.jsonl"):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            activities.extend(parse_codex_session(path, target, local_timezone))
    return activities


def deduplicate_activities(activities: Iterable[Activity]) -> list[Activity]:
    selected: dict[str, Activity] = {}
    for activity in activities:
        key = normalize_text(activity.request).casefold()
        if not key:
            continue
        existing = selected.get(key)
        score = (activity.priority, activity.done, bool(activity.result), len(activity.result))
        existing_score = (
            existing.priority,
            existing.done,
            bool(existing.result),
            len(existing.result),
        ) if existing is not None else None
        if existing_score is None or score > existing_score:
            selected[key] = activity
    return sorted(selected.values(), key=lambda item: item.created_at)


def redact_activities(activities: Iterable[Activity]) -> list[Activity]:
    redactor = RedactionService()
    result: list[Activity] = []
    for activity in activities:
        request = redactor.redact_text(activity.request, max_chars=1200).text
        outcome = redactor.redact_text(activity.result, max_chars=1600).text
        result.append(
            Activity(
                request=request,
                result=outcome,
                phase=activity.phase,
                created_at=activity.created_at,
                priority=activity.priority,
            )
        )
    return result


def format_report_date(target: date) -> str:
    weekday = WEEKDAY_NAMES.get(target.weekday(), target.strftime("%A"))
    return f"{weekday}, {target.day} {MONTH_NAMES[target.month]} {target.year}"


def _extract_changed(text: str) -> str:
    match = re.search(
        r"(?:^|\n)\s*-\s*Changed:\s*(.*?)(?=\n\s*-\s*(?:Why|Verified|Risks / Notes):|\Z)",
        text,
        flags=re.DOTALL,
    )
    if match:
        return normalize_text(match.group(1), max_chars=260)
    return normalize_text(text, max_chars=260)


def _limited_lines(values: Iterable[str], limit: int = 7) -> list[str]:
    unique: list[str] = []
    for value in values:
        normalized = normalize_text(value, max_chars=260)
        if normalized and normalized not in unique:
            unique.append(normalized)
    lines = [f"• {value}" for value in unique[:limit]]
    if len(unique) > limit:
        lines.append(f"• Ещё активностей: {len(unique) - limit}.")
    return lines


def fallback_summary(activities: list[Activity]) -> str:
    if not activities:
        return "\n".join(
            [
                "Чем занимались:",
                "• Локальной активности Telegram/Codex не найдено.",
                "",
                "Сделано:",
                "• Нет зафиксированных завершений.",
                "",
                "Не доделано:",
                "• Нет зафиксированных незавершённых действий.",
            ]
        )

    done = [item for item in activities if item.done]
    unfinished = [item for item in activities if not item.done]
    work_lines = _limited_lines(item.request for item in activities)
    done_lines = _limited_lines(
        _extract_changed(item.result) or item.request for item in done
    ) or ["• Нет зафиксированных завершений."]
    unfinished_lines = _limited_lines(
        f"{item.request} (статус: {item.phase})" for item in unfinished
    ) or ["• Незавершённых действий не зафиксировано."]
    return "\n".join(
        [
            "Чем занимались:",
            *work_lines,
            "",
            "Сделано:",
            *done_lines,
            "",
            "Не доделано:",
            *unfinished_lines,
        ]
    )


def summary_prompt(activities: list[Activity]) -> str:
    records: list[str] = []
    for index, activity in enumerate(activities, start=1):
        status = "сделано" if activity.done else f"не завершено ({activity.phase})"
        records.extend(
            [
                f"Активность {index}",
                f"Статус: {status}",
                f"Запрос: {normalize_text(activity.request, max_chars=1000)}",
                f"Результат: {normalize_text(activity.result, max_chars=1400) or '-'}",
                "",
            ]
        )
    source = "\n".join(records).rstrip()
    return f"""Сформируй краткую рабочую сводку на русском языке по локальной активности ниже.

Верни только три раздела с точными заголовками:
Чем занимались:
Сделано:
Не доделано:

Требования:
- В каждом разделе используй короткие пункты с маркером «•».
- Объедини связанные шаги в 3–7 смысловых направлений, не перечисляй каждое техническое действие отдельно.
- В «Сделано» укажи конкретные результаты только для записей со статусом «сделано».
- В «Не доделано» перечисли все незавершённые направления; если их нет, явно напиши об этом.
- Можно упоминать рабочие ключи вида BLA-123, но не упоминай внутренние ID бота, пути и названия проектов.
- Не додумывай факты и не включай секреты.
- Общая длина — не более 3000 символов.

Локальная активность:
{source}
"""


def summarize_with_codex(
    activities: list[Activity],
    codex_bin: str,
    workspace_root: Path,
    model: str | None = None,
    timeout_seconds: int = 900,
) -> str:
    with tempfile.TemporaryDirectory(prefix="codex-daily-summary-") as tmp:
        output_path = Path(tmp) / "summary.md"
        command = [
            codex_bin,
            "exec",
            "-C",
            str(workspace_root),
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-c",
            'approval_policy="never"',
            "-c",
            'model_reasoning_effort="low"',
            "-s",
            "read-only",
            "-o",
            str(output_path),
        ]
        if model:
            command.extend(["-m", model])
        command.append("-")
        env = os.environ.copy()
        env["CODEX_TELEGRAM_SUPPRESS_STOP_HOOK"] = "1"
        completed = subprocess.run(
            command,
            input=summary_prompt(activities),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            cwd=workspace_root,
            env=env,
            check=False,
        )
        if completed.returncode != 0 or not output_path.exists():
            raise RuntimeError(f"Codex summary failed with exit code {completed.returncode}")
        result = output_path.read_text(encoding="utf-8", errors="replace").strip()

    required = ("Чем занимались:", "Сделано:", "Не доделано:")
    if not result or any(section not in result for section in required):
        raise RuntimeError("Codex summary did not contain the required sections")
    return result[:3400].rstrip()


def build_report(
    target: date,
    activities: list[Activity],
    summarizer: Callable[[list[Activity]], str] | None = None,
) -> str:
    safe_activities = redact_activities(deduplicate_activities(activities))
    if safe_activities and summarizer is not None:
        try:
            body = summarizer(safe_activities)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            body = fallback_summary(safe_activities)
    else:
        body = fallback_summary(safe_activities)
    safe_body = RedactionService().redact_text(body, max_chars=3500).text
    return f"📋 Рабочая сводка за {format_report_date(target)}\n\n{safe_body}"


def marker_path(state_dir: Path, target: date) -> Path:
    return state_dir / "daily-summary" / f"sent-{target.isoformat()}.json"


def mark_sent(state_dir: Path, target: date, chat_id: int) -> None:
    path = marker_path(state_dir, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"target_date": target.isoformat(), "chat_id": chat_id, "sent_at": now_iso()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_daily_summary(
    *,
    state_dir: Path,
    sessions_root: Path,
    chat_id: int,
    target: date,
    local_timezone: ZoneInfo,
    summarizer: Callable[[list[Activity]], str] | None,
    sender: Callable[[int, str], object],
    dry_run: bool = False,
) -> str:
    marker = marker_path(state_dir, target)
    if marker.exists() and not dry_run:
        return f"Daily summary for {target.isoformat()} was already sent."

    store = TaskStore(state_dir)
    activities = collect_task_activities(store, chat_id, target, local_timezone)
    activities.extend(collect_codex_activities(sessions_root, target, local_timezone))
    report = build_report(target, activities, summarizer=summarizer)
    if dry_run:
        return report

    sender(chat_id, report)
    mark_sent(state_dir, target, chat_id)
    return report


def run_daily_summary_from_env(
    target: date | None = None,
    dry_run: bool = False,
) -> str:
    env = merged_env()
    timezone_name = env.get("CODEX_TELEGRAM_DAILY_SUMMARY_TIMEZONE", DEFAULT_TIMEZONE)
    local_timezone = ZoneInfo(timezone_name)
    today = datetime.now(local_timezone).date()
    target = target or previous_workday(today)

    chat_value = (
        env.get("CODEX_TELEGRAM_DAILY_SUMMARY_CHAT_ID")
        or env.get("CODEX_TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    try:
        chat_id = int(chat_value)
    except ValueError as exc:
        raise ValueError("Daily summary Telegram chat id is invalid") from exc
    if chat_id <= 0:
        raise ValueError("Daily summary requires a private Telegram chat id")

    bot_token = env.get("CODEX_TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token and not dry_run:
        raise ValueError("CODEX_TELEGRAM_BOT_TOKEN is required")

    state_dir = Path(
        os.path.expandvars(env.get("CODEX_TELEGRAM_STATE_DIR", "~/.codex/telegram-bot"))
    ).expanduser()
    codex_home = Path(env.get("CODEX_HOME", "~/.codex")).expanduser()
    sessions_root = codex_home / "sessions"
    workspace_root = Path(
        os.path.expandvars(env.get("CODEX_TELEGRAM_WORKSPACE_ROOT", str(Path.home())))
    ).expanduser()
    codex_bin = env.get("CODEX_TELEGRAM_CODEX_BIN", "codex")
    model = (
        env.get("CODEX_TELEGRAM_DAILY_SUMMARY_MODEL")
        or env.get("CODEX_CHEAP_MODEL")
        or None
    )
    summarizer = lambda activities: summarize_with_codex(
        activities,
        codex_bin=codex_bin,
        workspace_root=workspace_root,
        model=model,
    )
    api = TelegramAPI(bot_token) if bot_token else None
    sender = (
        (lambda target_chat_id, text: api.send_message(target_chat_id, text))
        if api is not None
        else (lambda _target_chat_id, _text: None)
    )
    return run_daily_summary(
        state_dir=state_dir,
        sessions_root=sessions_root,
        chat_id=chat_id,
        target=target,
        local_timezone=local_timezone,
        summarizer=summarizer,
        sender=sender,
        dry_run=dry_run,
    )
