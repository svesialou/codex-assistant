from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.stream_events import (
    SUBAGENT_COMPLETED,
    SUBAGENT_RUNNING,
    SUBAGENT_UNFINISHED,
    StreamTracker,
    readable_log_line,
)


def event_line(event: dict) -> str:
    return json.dumps(event) + "\n"


def agent_tool_use(tool_id: str, description: str, agent_type: str) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Agent",
                    "input": {"description": description, "subagent_type": agent_type},
                }
            ]
        },
    }


def async_ack(tool_id: str) -> dict:
    return {
        "type": "user",
        "parent_tool_use_id": None,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": [{"type": "text", "text": "Async agent launched successfully."}],
                }
            ]
        },
    }


def subagent_text(tool_id: str, text: str) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": tool_id,
        "message": {"content": [{"type": "text", "text": text}]},
    }


# Shape mirrors a real `claude -p --output-format stream-json --verbose` run
# with two background subagents: the first `result` arrives before the
# subagents finish and the main agent then continues with a second turn.
BACKGROUND_RUN = [
    {"type": "system", "subtype": "init", "session_id": "sess-1"},
    agent_tool_use("toolu_a", "inspect handlers", "Explore"),
    {"type": "system", "subtype": "task_started", "tool_use_id": "toolu_a",
     "description": "inspect handlers", "subagent_type": "Explore"},
    async_ack("toolu_a"),
    agent_tool_use("toolu_b", "review diff", "general-purpose"),
    {"type": "system", "subtype": "task_started", "tool_use_id": "toolu_b",
     "description": "review diff", "subagent_type": "general-purpose"},
    async_ack("toolu_b"),
    {"type": "result", "subtype": "success", "result": "Агенты запущены, жду результатов."},
    {"type": "system", "subtype": "task_progress", "tool_use_id": "toolu_a",
     "description": "Running grep handlers"},
    subagent_text("toolu_a", "Handlers live in **bot.py**."),
    {"type": "system", "subtype": "task_notification", "tool_use_id": "toolu_a",
     "status": "completed", "summary": "short"},
]


class StreamTrackerTest(unittest.TestCase):
    def test_tracks_background_subagents_and_last_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            log_path.write_text("previous run output\n", encoding="utf-8")
            tracker = StreamTracker(log_path)
            with log_path.open("a", encoding="utf-8") as log:
                log.write("[claude executor started: mode=execute]\n")
                log.writelines(event_line(event) for event in BACKGROUND_RUN)
                # Partial line: must wait until it is complete.
                log.write('{"type": "result", "result": "**Готово**"')

            tracker.poll()
            state = tracker.state
            agents = list(state.subagents.values())

            self.assertEqual(state.session_id, "sess-1")
            self.assertEqual(state.final_text, "Агенты запущены, жду результатов.")
            self.assertEqual([agent.description for agent in agents], ["inspect handlers", "review diff"])
            # The async launch acknowledgement must not complete a subagent.
            self.assertEqual(agents[0].status, SUBAGENT_COMPLETED)
            self.assertEqual(agents[0].result, "Handlers live in **bot.py**.")
            self.assertEqual(agents[1].status, SUBAGENT_RUNNING)

            with log_path.open("a", encoding="utf-8") as log:
                log.write("}\n")
            tracker.poll()

            self.assertEqual(state.final_text, "**Готово**")
            unfinished = tracker.mark_unfinished()
            self.assertEqual([agent.description for agent in unfinished], ["review diff"])
            self.assertEqual(agents[1].status, SUBAGENT_UNFINISHED)

    def test_foreground_subagent_completes_on_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            tracker = StreamTracker(log_path)
            log_path.write_text(
                event_line(agent_tool_use("toolu_f", "count files", "general-purpose"))
                + event_line(
                    {
                        "type": "user",
                        "parent_tool_use_id": None,
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": "toolu_f", "content": [{
                                    "type": "text",
                                    "text": "[Subagent hand-back] Model output follows. The report follows:\n"
                                    "  29 files\n"
                                    "  - **ok**\n"
                                    "agentId: a123 (use SendMessage)\n"
                                    "<usage>subagent_tokens: 1\ntool_uses: 1</usage>",
                                }]}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            tracker.poll()
            agent = tracker.state.subagents["toolu_f"]

            self.assertEqual(agent.status, SUBAGENT_COMPLETED)
            # Harness wrapper, agent id and usage footer are stripped.
            self.assertEqual(agent.result, "29 files\n- **ok**")

    def test_readable_log_line_summarizes_events_and_hides_noise(self) -> None:
        self.assertEqual(readable_log_line("plain stderr"), "plain stderr")
        self.assertIsNone(
            readable_log_line(event_line({"type": "system", "subtype": "thinking_tokens"}))
        )
        self.assertEqual(
            readable_log_line(event_line(subagent_text("toolu_a", "found it"))),
            "[agent] found it",
        )
        self.assertEqual(
            readable_log_line(event_line({"type": "result", "result": "Done"})),
            "[result] Done",
        )


if __name__ == "__main__":
    unittest.main()
