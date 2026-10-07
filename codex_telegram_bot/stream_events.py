"""Incremental parser for Claude Code `--output-format stream-json` run logs.

The runner appends Claude's JSONL stream (plus stray stderr lines) to the task
run log. This module folds those events into a small snapshot: the latest
activity, launched subagents with their status/result, and the final answer.

Claude in print mode may emit several `result` events per run: background
subagents finish after the first turn and the main agent then continues. The
final answer is therefore the last non-empty `result`, available only once the
process exits.
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUBAGENT_TOOL_NAMES = {"Agent", "Task"}
SUBAGENT_RUNNING = "running"
SUBAGENT_COMPLETED = "completed"
SUBAGENT_FAILED = "failed"
SUBAGENT_UNFINISHED = "unfinished"
# Async-launch acknowledgement returned as the Agent tool_result for
# background subagents. It is not the subagent's real result.
ASYNC_LAUNCH_MARKER = "async agent launched"
MAX_ACTIVITY_CHARS = 320
# Foreground subagent reports come wrapped by the Claude Code harness:
# a "[Subagent hand-back] ... The report follows:" preamble, an indented
# report, then `agentId: ...` and a `<usage>` block.
HANDBACK_PREFIX = "[Subagent hand-back]"
HANDBACK_REPORT_MARKER = "The report follows:"
USAGE_BLOCK_RE = re.compile(r"<usage>.*?</usage>", re.DOTALL)


@dataclass
class SubagentState:
    tool_use_id: str
    description: str = ""
    agent_type: str = ""
    status: str = SUBAGENT_RUNNING
    activity: str = ""
    result: str = ""
    result_sent: bool = False

    @property
    def finished(self) -> bool:
        return self.status != SUBAGENT_RUNNING


@dataclass
class StreamState:
    session_id: str = ""
    activity: str = ""
    final_text: str = ""
    final_is_error: bool = False
    events: int = 0
    subagents: dict[str, SubagentState] = field(default_factory=dict)

    def running_subagents(self) -> list[SubagentState]:
        return [agent for agent in self.subagents.values() if not agent.finished]


class StreamTracker:
    """Reads new complete lines of a stream-json log and updates `state`."""

    def __init__(self, path: Path, offset: int | None = None) -> None:
        self.path = path
        if offset is None:
            try:
                offset = path.stat().st_size
            except OSError:
                offset = 0
        self.offset = offset
        self.state = StreamState()

    def poll(self) -> bool:
        """Consume newly appended lines. Returns True when any event was parsed."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return False
        end = data.rfind(b"\n")
        if end < 0:
            return False
        self.offset += end + 1
        changed = False
        for raw_line in data[: end + 1].splitlines():
            event = parse_event_line(raw_line)
            if event is not None:
                apply_event(self.state, event)
                changed = True
        return changed

    def mark_unfinished(self) -> list[SubagentState]:
        """Flag subagents still running after the process exited."""
        unfinished = self.state.running_subagents()
        for agent in unfinished:
            agent.status = SUBAGENT_UNFINISHED
        return unfinished


def parse_event_line(raw_line: bytes | str) -> dict[str, Any] | None:
    if isinstance(raw_line, bytes):
        raw_line = raw_line.decode("utf-8", errors="replace")
    line = raw_line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None
    return event


def apply_event(state: StreamState, event: dict[str, Any]) -> None:
    state.events += 1
    event_type = event.get("type")
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id and not state.session_id:
        state.session_id = session_id

    if event_type == "system":
        _apply_system_event(state, event)
    elif event_type == "assistant":
        _apply_assistant_event(state, event)
    elif event_type == "user":
        _apply_user_event(state, event)
    elif event_type == "result":
        result = event.get("result")
        if isinstance(result, str) and result.strip():
            state.final_text = result.strip()
            state.final_is_error = bool(event.get("is_error"))


def _subagent(state: StreamState, tool_use_id: str) -> SubagentState:
    agent = state.subagents.get(tool_use_id)
    if agent is None:
        agent = SubagentState(tool_use_id=tool_use_id)
        state.subagents[tool_use_id] = agent
    return agent


def _apply_system_event(state: StreamState, event: dict[str, Any]) -> None:
    subtype = event.get("subtype")
    tool_use_id = event.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return
    if subtype == "task_started":
        agent = _subagent(state, tool_use_id)
        agent.description = agent.description or str(event.get("description") or "")
        agent.agent_type = agent.agent_type or str(event.get("subagent_type") or "")
    elif subtype == "task_progress" and tool_use_id in state.subagents:
        description = str(event.get("description") or "")
        if description:
            state.subagents[tool_use_id].activity = short_text(description)
    elif subtype == "task_notification" and tool_use_id in state.subagents:
        agent = state.subagents[tool_use_id]
        status = str(event.get("status") or "")
        agent.status = SUBAGENT_COMPLETED if status == "completed" else SUBAGENT_FAILED
        summary = event.get("summary")
        if not agent.result and isinstance(summary, str):
            agent.result = summary.strip()


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _apply_assistant_event(state: StreamState, event: dict[str, Any]) -> None:
    parent_id = event.get("parent_tool_use_id")
    parent = state.subagents.get(parent_id) if isinstance(parent_id, str) else None
    for block in _content_blocks(event):
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            if parent is not None:
                # The subagent's last text is its report to the main agent.
                parent.result = text
                parent.activity = short_text(text)
            elif parent_id is None:
                state.activity = short_text(text)
        elif block_type == "tool_use":
            name = str(block.get("name") or "")
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            if parent_id is None and name in SUBAGENT_TOOL_NAMES:
                agent = _subagent(state, str(block.get("id") or ""))
                agent.description = str(tool_input.get("description") or agent.description)
                agent.agent_type = str(tool_input.get("subagent_type") or agent.agent_type)
                state.activity = f"Запускает агента: {agent.description or agent.agent_type or 'agent'}"
                continue
            label = tool_activity(name, tool_input)
            if parent is not None:
                parent.activity = label
            elif parent_id is None:
                state.activity = label


def _apply_user_event(state: StreamState, event: dict[str, Any]) -> None:
    if event.get("parent_tool_use_id") is not None:
        return
    for block in _content_blocks(event):
        if block.get("type") != "tool_result":
            continue
        agent = state.subagents.get(str(block.get("tool_use_id") or ""))
        if agent is None:
            continue
        text = tool_result_text(block.get("content"))
        if ASYNC_LAUNCH_MARKER in text.lower():
            continue
        # Foreground subagent: its tool_result is the final report.
        agent.status = SUBAGENT_FAILED if block.get("is_error") else SUBAGENT_COMPLETED
        report = clean_subagent_report(text)
        if report:
            agent.result = report


def clean_subagent_report(text: str) -> str:
    if text.lstrip().startswith(HANDBACK_PREFIX):
        marker = text.find(HANDBACK_REPORT_MARKER)
        if marker >= 0:
            text = text[marker + len(HANDBACK_REPORT_MARKER) :]
    text = USAGE_BLOCK_RE.sub("", text)
    lines = [line for line in text.splitlines() if not line.strip().startswith("agentId:")]
    return textwrap.dedent("\n".join(lines)).strip()


def tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def tool_activity(name: str, tool_input: dict[str, Any]) -> str:
    detail = ""
    for key in ("description", "file_path", "pattern", "command", "query", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            detail = value.strip().splitlines()[0]
            break
    label = f"{name}: {detail}" if detail else name
    return short_text(label)


def short_text(text: str, limit: int = MAX_ACTIVITY_CHARS) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clean = " ".join(lines[:3])
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."



def readable_log_line(raw_line: str) -> str | None:
    """Turn a stream-json log line into a short human line; None hides noise."""
    event = parse_event_line(raw_line)
    if event is None:
        return raw_line
    event_type = event.get("type")
    if event_type == "system":
        subtype = event.get("subtype")
        description = short_text(str(event.get("description") or ""), 160)
        if subtype == "init":
            return "[session started]"
        if subtype == "task_started":
            return f"[agent started] {description}"
        if subtype == "task_progress":
            return f"[agent] {description}"
        if subtype == "task_notification":
            return f"[agent {event.get('status') or 'finished'}]"
        return None
    if event_type == "result":
        prefix = "[result error]" if event.get("is_error") else "[result]"
        return f"{prefix} {short_text(str(event.get('result') or ''), 400)}".rstrip()
    if event_type not in ("assistant", "user"):
        return None
    prefix = "[agent] " if event.get("parent_tool_use_id") else ""
    lines: list[str] = []
    for block in _content_blocks(event):
        block_type = block.get("type")
        if block_type == "text":
            lines.append(f"{prefix}{short_text(str(block.get('text') or ''))}")
        elif block_type == "tool_use":
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            lines.append(f"{prefix}$ {tool_activity(str(block.get('name') or ''), tool_input)}")
        elif block_type == "tool_result":
            text = short_text(tool_result_text(block.get("content")), 200)
            lines.append(f"{prefix}  -> {text}")
    return "\n".join(line for line in lines if line.strip()) or None
