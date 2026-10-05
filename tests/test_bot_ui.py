from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import codex_telegram_bot.bot as bot_module
from codex_telegram_bot.bot import (
    BOT_COMMANDS,
    CodexTelegramBot,
    PROJECT_CHOICE_TTL_SECONDS,
    PROJECT_PAGE_SIZE,
    command_parts,
    completed_task_keyboard,
    followup_draft_keyboard,
    inline_task_keyboard,
    main_menu_keyboard,
    process_summary,
    running_task_keyboard,
    settings_keyboard,
    tasks_keyboard,
)
from codex_telegram_bot.config import Config
from codex_telegram_bot.project_index import ProjectInfo
from codex_telegram_bot.task_store import TaskRecord


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict | None]] = []
        self.edited: list[tuple[int, int, str, dict | None]] = []
        self.answered: list[tuple[str, str]] = []
        self.next_message_id = 1

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int:
        self.sent.append((chat_id, text, reply_markup))
        message_id = self.next_message_id
        self.next_message_id += 1
        return message_id

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        self.edited.append((chat_id, message_id, text, reply_markup))

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.answered.append((callback_query_id, text))

    def get_file(self, file_id: str) -> dict:
        self.assert_file_id = file_id
        return {"file_path": "documents/report.txt", "file_size": 11}

    def download_file(self, file_path: str, destination: Path) -> None:
        self.assert_file_path = file_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"hello world")


def test_config(tmp: str) -> Config:
    root = Path(tmp)
    return Config(
        bot_token="token",
        allowed_chat_ids={10},
        allowed_user_ids=set(),
        project_roots=[],
        index_dir=root / "index",
        state_dir=root / "state",
        codex_bin="codex",
        model=None,
        poll_timeout_seconds=30,
        prompt_debounce_seconds=60,
        plan_timeout_seconds=60,
        run_timeout_seconds=None,
        transcribe_command=None,
        transcribe_timeout_seconds=300,
        env_file=root / "telegram.env",
    )


def demo_project(slug: str = "demo") -> ProjectInfo:
    return ProjectInfo(
        slug=slug,
        name=slug,
        path=f"/tmp/{slug}",
        base="/tmp",
        is_git=False,
        branch=None,
        origin=None,
        languages=["Python"],
        markers=[],
        docs=[],
        test_hints=[],
    )


class BotUiTest(unittest.TestCase):
    def test_command_parts_strips_bot_name(self) -> None:
        self.assertEqual(command_parts("/task@my_bot demo"), ("/task", "demo"))

    def test_main_menu_is_task_first(self) -> None:
        keyboard = main_menu_keyboard(agent_mode=True)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(labels, ["📋 Задачи", "▶️ Run custom", "⚙️ Настройки", "❓ Помощь"])

    def test_bot_commands_include_menu_first(self) -> None:
        self.assertEqual(BOT_COMMANDS[0]["command"], "menu")
        self.assertIn(
            "menu",
            {command["command"] for command in BOT_COMMANDS},
        )

    def test_tasks_keyboard_has_status_filters(self) -> None:
        keyboard = tasks_keyboard()
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("tasks:active", callbacks)
        self.assertIn("tasks:failed", callbacks)

    def test_settings_keyboard_has_task_provider_toggle(self) -> None:
        keyboard = settings_keyboard(
            orchestrator_enabled=True,
            debug_enabled=False,
            memory_enabled=True,
            task_provider="claude",
        )
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("settings:task_provider_toggle", callbacks)
        self.assertIn("Executor: CLAUDE", labels)

    def test_inline_task_keyboard_has_attachment_and_answer_actions(self) -> None:
        keyboard = inline_task_keyboard("task-1")
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]

        self.assertIn("task:files:task-1", callbacks)
        self.assertIn("task:answer:task-1", callbacks)
        self.assertIn("task:execute:task-1", callbacks)
        self.assertIn("task:brain:task-1", callbacks)

    def test_completed_task_keyboard_has_continue_action(self) -> None:
        keyboard = completed_task_keyboard("task-1")
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]

        self.assertIn("task:continue:task-1", callbacks)
        self.assertIn("contctx:task-1", callbacks)
        self.assertIn("contattach:task-1", callbacks)
        self.assertIn("task:brain:task-1", callbacks)
        self.assertIn("task:remember:task-1", callbacks)
        self.assertIn("task:force_push:task-1", callbacks)

    def test_running_task_keyboard_has_clarify_and_stop_actions(self) -> None:
        keyboard = running_task_keyboard("task-1")
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]

        self.assertIn("task:answer:task-1", callbacks)
        self.assertIn("task:cancel:task-1", callbacks)

    def test_followup_draft_keyboard_has_context_file_and_run_actions(self) -> None:
        keyboard = followup_draft_keyboard("parent-1", "draft-1")
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]

        self.assertIn("contctx:parent-1", callbacks)
        self.assertIn("contattach:parent-1", callbacks)
        self.assertIn("contrun:parent-1", callbacks)
        self.assertIn("cancel:draft-1", callbacks)

    def test_projects_next_callback_edits_existing_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            fake_api = FakeTelegramAPI()
            bot.api = fake_api
            bot.projects = [
                demo_project(f"demo-{index}")
                for index in range(PROJECT_PAGE_SIZE + 1)
            ]

            bot.handle_callback(
                {
                    "id": "callback-1",
                    "data": "projects:1",
                    "message": {"message_id": 42, "chat": {"id": 10}},
                    "from": {"id": 20},
                }
            )

        self.assertEqual(fake_api.sent, [])
        self.assertEqual(fake_api.answered, [("callback-1", "Received")])
        self.assertEqual(len(fake_api.edited), 1)
        chat_id, message_id, text, reply_markup = fake_api.edited[0]
        self.assertEqual(chat_id, 10)
        self.assertEqual(message_id, 42)
        self.assertIn("Projects page 2:", text)
        self.assertIsNotNone(reply_markup)

    def test_edit_message_ignores_not_modified_error(self) -> None:
        class NotModifiedAPI(FakeTelegramAPI):
            def edit_message_text(
                self,
                chat_id: int,
                message_id: int,
                text: str,
                reply_markup: dict | None = None,
            ) -> None:
                raise RuntimeError("Telegram editMessageText failed: message is not modified")

        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.api = NotModifiedAPI()

            edited = bot.edit_message(10, 42, "same text")

        self.assertTrue(edited)

    def test_live_status_edit_keeps_running_task_keyboard(self) -> None:
        class OneEditStopEvent:
            def __init__(self) -> None:
                self.calls = 0

            def wait(self, _timeout: float) -> bool:
                self.calls += 1
                return self.calls > 1

        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            fake_api = FakeTelegramAPI()
            bot.api = fake_api
            task = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="do work",
            )
            task.phase = "running"
            bot.store.save_task(task)

            original_extract = bot_module.extract_codex_activity
            original_monotonic = bot_module.time.monotonic
            monotonic_values = iter([0.0, 10.0, 10.0])
            bot_module.extract_codex_activity = lambda _path: "Inspecting files"
            bot_module.time.monotonic = lambda: next(monotonic_values)
            try:
                bot.watch_codex_status(
                    task,
                    "running",
                    Path(tmp) / "run.log",
                    42,
                    OneEditStopEvent(),
                )
            finally:
                bot_module.extract_codex_activity = original_extract
                bot_module.time.monotonic = original_monotonic

        self.assertEqual(len(fake_api.edited), 1)
        reply_markup = fake_api.edited[0][3]
        self.assertIsNotNone(reply_markup)
        callbacks = [
            button["callback_data"]
            for row in reply_markup["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"task:answer:{task.id}", callbacks)
        self.assertIn(f"task:cancel:{task.id}", callbacks)

    def test_process_summary_lists_live_processes_across_projects(self) -> None:
        tasks = [
            TaskRecord(
                id="task-1",
                chat_id=10,
                user_id=20,
                project_slug="api",
                project_name="api",
                project_path="/tmp/api",
                prompt="run api task",
                phase="running",
                pid=111,
            ),
            TaskRecord(
                id="task-2",
                chat_id=10,
                user_id=20,
                project_slug="worker",
                project_name="worker",
                project_path="/tmp/worker",
                prompt="run worker task",
                phase="planning",
                pid=222,
            ),
        ]

        summary = process_summary(tasks, pid_alive=lambda pid: pid == 111)

        self.assertIn("Active Codex processes across projects: 1", summary)
        self.assertIn("Project: api", summary)
        self.assertIn("pid=111", summary)
        self.assertIn("Stale active task records: 1", summary)

    def test_global_process_commands_do_not_require_selected_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)

            bot.dispatch_command(10, 20, "/processes", "")
            bot.dispatch_command(10, 20, "/status", "")

        self.assertEqual(
            sent,
            [
                "No active Codex processes across projects.",
                "No active Codex processes across projects.",
            ],
        )

    def test_orchestrator_commands_toggle_chat_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or 1

            bot.dispatch_command(10, 20, "/orchestrator_off", "")
            off_state = bot.store.load_chat_state(10)
            bot.dispatch_command(10, 20, "/orchestrator_on", "")
            on_state = bot.store.load_chat_state(10)
            bot.dispatch_command(10, 20, "/settings", "orchestrator status")

        self.assertFalse(off_state.orchestrator_mode)
        self.assertTrue(on_state.orchestrator_mode)
        self.assertIn("Orchestrator: OFF", sent[0])
        self.assertIn("Orchestrator: ON", sent[-1])

    def test_settings_provider_command_sets_chat_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or 1

            bot.dispatch_command(10, 20, "/settings", "provider claude")
            claude_state = bot.store.load_chat_state(10)
            bot.dispatch_command(10, 20, "/settings", "provider codex")
            codex_state = bot.store.load_chat_state(10)

        self.assertEqual(claude_state.task_provider, "claude")
        self.assertEqual(codex_state.task_provider, "codex")
        self.assertIn("Task provider: claude", sent[0])

    def test_recover_interrupted_tasks_queues_stale_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []

            def fake_send(
                chat_id: int,
                text: str,
                reply_markup: dict | None = None,
            ) -> int:
                sent.append(text)
                return len(sent)

            bot.send = fake_send
            bot.spawn = lambda target, *args: spawned.append((target, args))
            task = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="finish task",
                kind="direct_task",
            )
            task.phase = "running"
            task.pid = 999999
            task.pid_start_time = "old-start-time"
            task.run_log_path = str(bot.store.task_dir(task.id) / "run.log")
            Path(task.run_log_path).write_text(
                "OpenAI Codex\n"
                "session id: 019e7842-d41f-72b3-9394-911a9c490cb4\n",
                encoding="utf-8",
            )
            bot.store.save_task(task)

            bot.recover_interrupted_tasks(pid_alive=lambda pid: False)

            loaded = bot.store.load_task(task.id)

        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.pid)
        self.assertEqual(loaded.pid_start_time, "")
        self.assertEqual(
            loaded.codex_session_id,
            "019e7842-d41f-72b3-9394-911a9c490cb4",
        )
        self.assertEqual(spawned[0][0].__name__, "execute_task")
        self.assertEqual(spawned[0][1], (task.id, 1, True))
        self.assertIn("Восстанавливаю задачу", sent[0])

    def test_recover_interrupted_tasks_skips_live_recorded_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: 1
            bot.spawn = lambda target, *args: spawned.append((target, args))
            task = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="finish task",
            )
            task.phase = "planning"
            task.pid = 123
            bot.store.save_task(task)

            bot.recover_interrupted_tasks(pid_alive=lambda pid: pid == 123)

        self.assertEqual(spawned, [])

    def test_recover_interrupted_tasks_recovers_live_process_from_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            terminated: list[int | None] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or 1
            bot.spawn = lambda target, *args: spawned.append((target, args))
            task = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="finish task",
            )
            task.phase = "running"
            task.pid = 123
            task.runtime_id = "previous-runtime"
            bot.store.save_task(task)

            original_terminate = bot_module.terminate_process_group
            bot_module.terminate_process_group = terminated.append
            try:
                bot.recover_interrupted_tasks(pid_alive=lambda pid: pid == 123)
            finally:
                bot_module.terminate_process_group = original_terminate

            loaded = bot.store.load_task(task.id)

        self.assertEqual(terminated, [123])
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.pid)
        self.assertEqual(spawned[0][0].__name__, "execute_task")
        self.assertEqual(spawned[0][1], (task.id, 1, True))
        self.assertIn("Восстанавливаю задачу", sent[0])

    def test_file_attachment_after_planning_is_stored_on_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            fake_api = FakeTelegramAPI()
            bot.api = fake_api
            task = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="analyze file",
            )
            task.phase = "planned"
            bot.store.save_task(task)
            state = bot.store.load_chat_state(10)
            state.pending_action = f"attach_files:{task.id}"
            bot.store.save_chat_state(state)

            bot.handle_file_message(
                10,
                20,
                {
                    "caption": "important input",
                    "document": {
                        "file_id": "file-1",
                        "file_unique_id": "unique-1",
                        "file_name": "../report.txt",
                        "mime_type": "text/plain",
                        "file_size": 11,
                    },
                },
            )

            loaded = bot.store.load_task(task.id)
            attachment_exists = Path(loaded.attachments[0]["file_path"]).exists()

        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.attachments), 1)
        attachment = loaded.attachments[0]
        self.assertEqual(attachment["file_name"], "report.txt")
        self.assertEqual(attachment["caption"], "important input")
        self.assertTrue(attachment_exists)
        self.assertIn("Attached file to task", fake_api.sent[-1][1])

    def test_answer_pending_action_uses_next_text_as_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            task = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="do work",
            )
            task.phase = "planned"
            bot.store.save_task(task)
            state = bot.store.load_chat_state(10)
            state.pending_action = f"answer_task:{task.id}"
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "use the attached file")

            loaded = bot.store.load_task(task.id)
            loaded_state = bot.store.load_chat_state(10)

        self.assertEqual(loaded.clarifications, ["use the attached file"])
        self.assertEqual(loaded.phase, "created")
        self.assertIsNone(loaded_state.pending_action)
        self.assertEqual(spawned[0][1], (task.id, None))
        self.assertIn("Статус Codex", sent[-1])
        self.assertIn("Уточнение сохранено", sent[-1])

    def test_answer_button_on_running_task_saves_context_without_new_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            fake_api = FakeTelegramAPI()
            bot.api = fake_api
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.spawn = lambda target, *args: spawned.append((target, args))
            task = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="do work",
            )
            task.phase = "running"
            bot.store.save_task(task)

            bot.handle_callback(
                {
                    "id": "callback-1",
                    "data": f"task:answer:{task.id}",
                    "message": {"message_id": 42, "chat": {"id": 10}},
                    "from": {"id": 20},
                }
            )
            bot.handle_plain_text(10, 20, "use the current logs as context")

            tasks = bot.store.recent_tasks(10, limit=10)
            loaded = bot.store.load_task(task.id)
            loaded_state = bot.store.load_chat_state(10)

        self.assertEqual(len(tasks), 1)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.clarifications, ["use the current logs as context"])
        self.assertEqual(loaded.phase, "running")
        self.assertIsNone(loaded_state.pending_action)
        self.assertEqual(spawned, [])
        self.assertIn("Send context for active task", fake_api.sent[0][1])
        self.assertIn("Контекст сохранен", fake_api.sent[-1][1])
        self.assertNotIn("Prompt part", fake_api.sent[-1][1])

    def test_new_task_text_parts_are_buffered_into_one_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "demo"
            state.pending_action = "new_task"
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "first part")
            bot.handle_plain_text(10, 20, "second part")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)
            loaded_state = bot.store.load_chat_state(10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].prompt, "first part\n\nsecond part")
        self.assertEqual(tasks[0].kind, "task")
        self.assertIsNone(loaded_state.pending_action)
        self.assertEqual(loaded_state.prompt_draft_parts, [])
        self.assertEqual(spawned[0][1], (tasks[0].id, None))
        self.assertIn("Статус Codex", sent[-1])
        self.assertIn("Задача создана", sent[-1])

    def test_task_command_can_collect_following_plain_text_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            bot.send = lambda chat_id, text, reply_markup=None: None
            bot.spawn = lambda target, *args: None

            bot.dispatch_command(10, 20, "/task", "demo first part")
            bot.handle_plain_text(10, 20, "second part")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].prompt, "first part\n\nsecond part")

    def test_run_command_collects_parts_and_executes_without_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))

            bot.dispatch_command(10, 20, "/run", "demo first part")
            bot.handle_plain_text(10, 20, "second part")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)
            loaded_state = bot.store.load_chat_state(10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].kind, "direct_task")
        self.assertEqual(tasks[0].prompt, "first part\n\nsecond part")
        self.assertIsNone(loaded_state.pending_action)
        self.assertEqual(spawned[0][0].__name__, "execute_task")
        self.assertEqual(spawned[0][1], (tasks[0].id, None))
        self.assertIn("Статус Codex", sent[-1])
        self.assertIn("Orchestrator mode включён", sent[-1])

    def test_run_command_accepts_manual_tier_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            bot.send = lambda chat_id, text, reply_markup=None: None
            bot.spawn = lambda target, *args: None

            bot.dispatch_command(10, 20, "/run", "tier=cheap demo fix README typo")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)

        self.assertEqual(tasks[0].model_routing["manual_tier"], "cheap")

    def test_continue_command_collects_parts_and_resumes_parent_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            parent = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="initial work",
            )
            parent.phase = "completed"
            parent.codex_session_id = "019e7842-d41f-72b3-9394-911a9c490cb4"
            bot.store.save_task(parent)

            bot.dispatch_command(10, 20, "/continue", f"{parent.id} first follow-up")
            bot.handle_plain_text(10, 20, "second follow-up")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)
            followups = [task for task in tasks if task.parent_task_id == parent.id]
            loaded_state = bot.store.load_chat_state(10)

        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0].kind, "followup_task")
        self.assertEqual(followups[0].prompt, "first follow-up\n\nsecond follow-up")
        self.assertEqual(followups[0].codex_session_id, parent.codex_session_id)
        self.assertIsNone(loaded_state.pending_action)
        self.assertEqual(spawned[0][0].__name__, "execute_followup_task")
        self.assertEqual(spawned[0][1], (followups[0].id, None))
        self.assertIn("Continuation task", sent[-1])

    def test_continue_button_uses_next_text_as_followup_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            parent = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="initial work",
            )
            parent.phase = "completed"
            parent.codex_session_id = "019e7842-d41f-72b3-9394-911a9c490cb4"
            bot.store.save_task(parent)

            bot.start_continue_input(10, parent.id)
            bot.handle_plain_text(10, 20, "continue this task")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)
            followups = [task for task in tasks if task.parent_task_id == parent.id]

        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0].prompt, "continue this task")
        self.assertEqual(spawned[0][0].__name__, "execute_followup_task")
        self.assertIn("Send follow-up instruction", sent[0])

    def test_continue_button_works_for_claude_task_without_codex_session(self) -> None:
        """Regression: the Continue button refused any task Claude had executed,
        because it required a Codex session id that Claude never produces."""
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            parent = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="initial work",
            )
            parent.phase = "completed"
            parent.executor_provider = "claude"
            parent.claude_session_id = "75f09777-6e94-4c2e-8799-c3666c8533ed"
            bot.store.save_task(parent)

            bot.start_continue_input(10, parent.id)
            bot.handle_plain_text(10, 20, "continue with claude")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)
            followups = [task for task in tasks if task.parent_task_id == parent.id]

        self.assertFalse(any("cannot continue" in item for item in sent))
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0].prompt, "continue with claude")
        # The child must inherit the Claude session so the run can --resume it.
        self.assertEqual(followups[0].claude_session_id, parent.claude_session_id)
        self.assertEqual(spawned[0][0].__name__, "execute_followup_task")

    def test_continue_button_works_without_any_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            parent = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="initial work",
            )
            parent.phase = "completed"
            bot.store.save_task(parent)

            bot.start_continue_input(10, parent.id)
            bot.handle_plain_text(10, 20, "keep going")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)
            followups = [task for task in tasks if task.parent_task_id == parent.id]

        self.assertFalse(any("cannot continue" in item for item in sent))
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0].claude_session_id, "")
        self.assertEqual(followups[0].codex_session_id, "")
        self.assertEqual(spawned[0][0].__name__, "execute_followup_task")

    def test_followup_context_button_creates_draft_and_run_starts_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            parent = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="initial work",
            )
            parent.phase = "completed"
            parent.codex_session_id = "019e7842-d41f-72b3-9394-911a9c490cb4"
            bot.store.save_task(parent)

            bot.start_followup_context_input(10, 20, parent.id)
            bot.handle_plain_text(10, 20, "add regression tests")
            bot.run_continuation_draft(10, parent.id)

            tasks = bot.store.recent_tasks(10, limit=10)
            followups = [task for task in tasks if task.parent_task_id == parent.id]
            loaded_state = bot.store.load_chat_state(10)

        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0].kind, "followup_task")
        self.assertEqual(followups[0].prompt, "add regression tests")
        self.assertEqual(followups[0].codex_session_id, parent.codex_session_id)
        self.assertIsNone(loaded_state.pending_action)
        self.assertEqual(spawned[0][0].__name__, "execute_followup_task")
        self.assertEqual(spawned[0][1], (followups[0].id, None))
        self.assertIn("Continuation draft", sent[-1])

    def test_followup_files_are_attached_to_draft_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            fake_api = FakeTelegramAPI()
            bot.api = fake_api
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.spawn = lambda target, *args: spawned.append((target, args))
            parent = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="initial work",
            )
            parent.phase = "completed"
            parent.codex_session_id = "019e7842-d41f-72b3-9394-911a9c490cb4"
            bot.store.save_task(parent)

            bot.start_followup_file_attachment_input(10, 20, parent.id)
            bot.handle_file_message(
                10,
                20,
                {
                    "document": {
                        "file_id": "file-1",
                        "file_unique_id": "unique-1",
                        "file_name": "notes.txt",
                        "mime_type": "text/plain",
                        "file_size": 11,
                    },
                },
            )
            bot.run_continuation_draft(10, parent.id)

            tasks = bot.store.recent_tasks(10, limit=10)
            followups = [task for task in tasks if task.parent_task_id == parent.id]
            attachment_exists = Path(followups[0].attachments[0]["file_path"]).exists()

        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0].prompt, "Continue development using the attached context.")
        self.assertEqual(len(followups[0].attachments), 1)
        self.assertTrue(attachment_exists)
        self.assertEqual(spawned[0][0].__name__, "execute_followup_task")
        self.assertIn("Attached context file", fake_api.sent[-2][1])

    def test_continue_is_blocked_when_parent_followup_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            parent = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="initial work",
            )
            parent.phase = "completed"
            parent.codex_session_id = "019e7842-d41f-72b3-9394-911a9c490cb4"
            bot.store.save_task(parent)
            active = bot.store.create_task(
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="active follow-up",
                kind="followup_task",
                parent_task_id=parent.id,
                codex_session_id=parent.codex_session_id,
            )
            active.phase = "running"
            bot.store.save_task(active)

            bot.continue_task(10, 20, f"{parent.id} another follow-up")

            tasks = bot.store.recent_tasks(10, limit=10)
            followups = [task for task in tasks if task.parent_task_id == parent.id]

        self.assertEqual(len(followups), 1)
        self.assertIn("Continuation already active", sent[-1])

    def test_agent_mode_text_parts_are_buffered_into_one_agent_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "demo"
            state.agent_mode = True
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "part one")
            bot.handle_plain_text(10, 20, "part two")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].kind, "agent_chat")
        self.assertEqual(tasks[0].prompt, "part one\n\npart two")
        self.assertEqual(spawned[0][1], (tasks[0].id, None))
        self.assertIn("Статус Codex", sent[-1])
        self.assertIn("agent running", sent[-1])

    def test_agent_status_message_id_is_passed_to_worker_for_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.api = FakeTelegramAPI()
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.spawn = lambda target, *args: spawned.append((target, args))

            bot.ask_agent_for_project(
                10,
                20,
                demo_project(),
                "inspect repo",
                source="text",
                source_path="",
            )

            tasks = bot.store.recent_tasks(10, limit=10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(spawned[0][1], (tasks[0].id, 1))
        self.assertIn("Статус Codex", bot.api.sent[0][1])

    def test_prompt_draft_is_flushed_before_another_user_starts_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            bot.send = lambda chat_id, text, reply_markup=None: None
            bot.spawn = lambda target, *args: None
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "demo"
            state.pending_action = "new_task"
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "user 20 prompt")
            bot.handle_plain_text(10, 21, "user 21 prompt")
            bot.flush_prompt_draft(10)

            prompts = {task.prompt for task in bot.store.recent_tasks(10, limit=10)}

        self.assertEqual(prompts, {"user 20 prompt", "user 21 prompt"})

    def test_plain_text_selects_project_by_default_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project("codex-assistant")]
            sent: list[tuple[str, dict | None]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or 1

            bot.handle_plain_text(10, 20, "Работаем с ботом")

            state = bot.store.load_chat_state(10)

        self.assertEqual(state.selected_project_slug, "codex-assistant")
        self.assertEqual(state.active_project_slugs, ["codex-assistant"])
        self.assertIn("активный проект: codex-assistant", sent[-1][0])
        self.assertIsNotNone(sent[-1][1])

    def test_plain_text_creates_planned_task_with_next_action_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[tuple[str, dict | None]] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or len(sent)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "demo"
            state.active_project_slugs = ["demo"]
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "Добавь агентные роли и кнопку мозги")
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)
            callbacks = [
                button["callback_data"]
                for row in sent[-1][1]["inline_keyboard"]
                for button in row
            ]

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].prompt, "Добавь агентные роли и кнопку мозги")
        self.assertEqual(spawned[0][0].__name__, "plan_task")
        self.assertIn(f"task:brain:{tasks[0].id}", callbacks)
        self.assertIn(f"task:plan:{tasks[0].id}", callbacks)

    def test_pending_task_text_resolves_target_slug_instead_of_root_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project("root"), demo_project("metrics-etl-php")]
            sent: list[tuple[str, dict | None]] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or len(sent)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "root"
            state.active_project_slugs = ["root"]
            state.pending_action = "new_task"
            bot.store.save_chat_state(state)

            bot.handle_plain_text(
                10,
                20,
                "перезапустить agent-sdlc. Правильный slug - metrics-etl-php",
            )
            bot.flush_prompt_draft(10)

            tasks = bot.store.recent_tasks(10, limit=10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].project_slug, "metrics-etl-php")
        self.assertEqual(spawned[0][0].__name__, "plan_task")

    def test_pending_task_text_asks_for_project_when_root_context_is_unclear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project("root"), demo_project("metrics-etl-php")]
            sent: list[tuple[str, dict | None]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or len(sent)
            bot.spawn = lambda target, *args: None
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "root"
            state.active_project_slugs = ["root"]
            state.pending_action = "new_task"
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "восстановить работу задачи BLA-447")

            pending_tasks = bot.store.recent_tasks(10, limit=10)
            clarification = sent[-1]

            # Answering the clarification must resume the stored task text.
            bot.select_project_by_record(10, demo_project("metrics-etl-php"))
            bot.flush_prompt_draft(10)
            tasks = bot.store.recent_tasks(10, limit=10)

        self.assertEqual(pending_tasks, [])
        self.assertIn("Не понял, в каком проекте будут изменения", clarification[0])
        self.assertEqual(
            [row[0]["callback_data"] for row in clarification[1]["inline_keyboard"][:2]],
            ["proj:0", "proj:1"],
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].project_slug, "metrics-etl-php")
        self.assertEqual(tasks[0].prompt, "восстановить работу задачи BLA-447")

    def test_ambiguous_project_tokens_offer_candidates_and_resume_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [
                demo_project("root"),
                demo_project("metrics-etl-php"),
                demo_project("metrics-api"),
            ]
            sent: list[tuple[str, dict | None]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or len(sent)
            bot.spawn = lambda target, *args: None
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "root"
            state.active_project_slugs = ["root"]
            state.pending_action = "new_task"
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "почини metrics")

            clarification = sent[-1]
            bot.select_project_by_record(10, demo_project("metrics-api"))
            bot.flush_prompt_draft(10)
            tasks = bot.store.recent_tasks(10, limit=10)

        self.assertIn("Нужно уточнить проект", clarification[0])
        self.assertEqual(
            [row[0]["callback_data"] for row in clarification[1]["inline_keyboard"]],
            ["project:select:metrics-etl-php", "project:select:metrics-api"],
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].project_slug, "metrics-api")
        self.assertEqual(tasks[0].prompt, "почини metrics")

    def test_stale_project_choice_is_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project("root"), demo_project("metrics-etl-php")]
            bot.send = lambda chat_id, text, reply_markup=None: 1
            bot.spawn = lambda target, *args: None
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "root"
            state.active_project_slugs = ["root"]
            state.pending_action = "new_task"
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "восстановить работу задачи BLA-447")

            state = bot.store.load_chat_state(10)
            state.pending_project_choice_at -= PROJECT_CHOICE_TTL_SECONDS + 1
            bot.store.save_chat_state(state)

            bot.select_project_by_record(10, demo_project("metrics-etl-php"))
            bot.flush_prompt_draft(10)
            tasks = bot.store.recent_tasks(10, limit=10)
            cleared = bot.store.load_chat_state(10)

        self.assertEqual(tasks, [])
        self.assertEqual(cleared.pending_project_choice_text, "")

    def test_brain_callback_shows_task_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.api = FakeTelegramAPI()
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "do work")
            bot.append_event(task.id, "agent_message", "PM", "Принял задачу")

            bot.handle_callback(
                {
                    "id": "callback-1",
                    "data": f"task:brain:{task.id}",
                    "message": {"message_id": 42, "chat": {"id": 10}},
                    "from": {"id": 20},
                }
            )

        self.assertIn("Мозги команды", bot.api.sent[-1][1])
        self.assertIn("PM: Принял задачу", bot.api.sent[-1][1])

    def test_force_push_callback_launches_agent_in_context_repo_from_root_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.api = FakeTelegramAPI()
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            bot.projects = [
                ProjectInfo(
                    slug="root",
                    name="workspace-root",
                    path=str(root),
                    base=str(root),
                    is_git=False,
                    branch=None,
                    origin=None,
                    languages=[],
                    markers=[],
                    docs=[],
                    test_hints=[],
                ),
                ProjectInfo(
                    slug="demo",
                    name="demo",
                    path=str(repo),
                    base=str(root),
                    is_git=True,
                    branch="feature/demo",
                    origin=None,
                    languages=[],
                    markers=[],
                    docs=[],
                    test_hints=[],
                ),
            ]
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.spawn = lambda target, *args: spawned.append((target, args))
            task = bot.store.create_task(
                10,
                20,
                "root",
                "workspace-root",
                str(root),
                "do work in repo",
                context_project_slugs=["root", "demo"],
            )
            task.phase = "completed"
            bot.store.save_task(task)

            bot.handle_callback(
                {
                    "id": "callback-1",
                    "data": f"task:force_push:{task.id}",
                    "message": {"message_id": 42, "chat": {"id": 10}},
                    "from": {"id": 20},
                }
            )

            force_task_id = spawned[0][1][0]
            force_task = bot.store.load_task(force_task_id)

        self.assertEqual(spawned[0][0].__name__, "execute_force_push_task")
        self.assertIsNotNone(force_task)
        self.assertEqual(force_task.parent_task_id, task.id)
        self.assertEqual(force_task.kind, "force_push_task")
        self.assertEqual(force_task.project_slug, "demo")
        self.assertEqual(force_task.project_path, str(repo))
        self.assertIn("--force-with-lease", force_task.prompt)

    def test_plain_text_confirm_executes_last_planned_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[str] = []
            spawned: list[tuple[object, tuple[object, ...]]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append(text) or len(sent)
            bot.spawn = lambda target, *args: spawned.append((target, args))
            task = bot.store.create_task(10, 20, "demo", "demo", "/tmp/demo", "do work")
            task.phase = "planned"
            task.plan_text = "Plan"
            bot.store.save_task(task)
            state = bot.store.load_chat_state(10)
            state.last_task_id = task.id
            state.last_planned_task_id = task.id
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "делай")

        self.assertEqual(spawned[0][0].__name__, "execute_task")
        self.assertIn("подтверждена", sent[-1])

    def test_plain_text_remember_prompts_for_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            bot = CodexTelegramBot(config)
            bot.projects = [demo_project()]
            sent: list[tuple[str, dict | None]] = []
            bot.send = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or 1
            state = bot.store.load_chat_state(10)
            state.selected_project_slug = "demo"
            state.active_project_slugs = ["demo"]
            bot.store.save_chat_state(state)

            bot.handle_plain_text(10, 20, "Запомни: source repo главный")

            loaded_state = bot.store.load_chat_state(10)
            callbacks = [
                button["callback_data"]
                for row in sent[-1][1]["inline_keyboard"]
                for button in row
            ]

        self.assertEqual(loaded_state.pending_memory_project_slug, "demo")
        self.assertIn("memory:save_pending:demo", callbacks)
        self.assertIn("source repo главный", sent[-1][0])


if __name__ == "__main__":
    unittest.main()
