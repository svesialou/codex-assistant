from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_telegram_bot.codex_runner import (
    CodexRunner,
    continuation_prompt,
    execution_prompt,
    extract_session_id,
    force_push_prompt,
    interrupted_execution_prompt,
    planning_prompt,
    read_task_context,
    read_text_tail,
)
from codex_telegram_bot.config import Config
from codex_telegram_bot.project_index import ProjectInfo
from codex_telegram_bot.services.executors import (
    MODE_EXECUTE,
    MODE_FORCE_PUSH,
    MODE_RESUME,
    build_executors,
)
from codex_telegram_bot.task_store import TaskRecord, TaskStore


class CodexRunnerTest(unittest.TestCase):
    def test_base_command_uses_exec_supported_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                bot_token="token",
                allowed_chat_ids={1},
                allowed_user_ids=set(),
                project_roots=[],
                index_dir=Path(tmp) / "index",
                state_dir=Path(tmp) / "state",
                codex_bin="codex",
                model=None,
                poll_timeout_seconds=30,
                prompt_debounce_seconds=60,
                plan_timeout_seconds=60,
                run_timeout_seconds=None,
                transcribe_command=None,
                transcribe_timeout_seconds=300,
                env_file=Path(tmp) / "telegram.env",
            )
            runner = CodexRunner(config, TaskStore(config.state_dir))

            command = runner.executor_for("codex").build(
                MODE_EXECUTE,
                project_path="/home/stanislavv",
                output_path=Path(tmp) / "out.md",
            )
            env = runner._command_env("codex")

        self.assertNotIn("-a", command.argv)
        self.assertIn("--skip-git-repo-check", command.argv)
        self.assertIn('approval_policy="never"', command.argv)
        self.assertIn("danger-full-access", command.argv)
        self.assertFalse(command.final_answer_on_stdout)
        self.assertEqual(env["CODEX_TELEGRAM_SUPPRESS_STOP_HOOK"], "1")

    def test_prompts_include_attached_file_paths(self) -> None:
        task = TaskRecord(
            id="task-1",
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="analyze file",
            attachments=[
                {
                    "kind": "document",
                    "file_name": "report.txt",
                    "mime_type": "text/plain",
                    "file_size": 11,
                    "file_path": "/tmp/task/report.txt",
                    "caption": "input data",
                }
            ],
        )

        plan_prompt = planning_prompt(task, "")
        run_prompt = execution_prompt(task, "")
        continuation = continuation_prompt(task, "", None)

        self.assertIn("/tmp/task/report.txt", plan_prompt)
        self.assertIn("/tmp/task/report.txt", run_prompt)
        self.assertIn("/tmp/task/report.txt", continuation)
        self.assertIn("input data", run_prompt)
        self.assertIn("input data", continuation)

    def test_read_task_context_includes_active_project_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            agents_dir = index_dir / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "demo.md").write_text("Demo context", encoding="utf-8")
            (agents_dir / "api.md").write_text("API context", encoding="utf-8")
            project = ProjectInfo(
                slug="demo",
                name="demo",
                path="/tmp/demo",
                base="/tmp",
                is_git=False,
                branch=None,
                origin=None,
                languages=[],
                markers=[],
                docs=[],
                test_hints=[],
            )
            task = TaskRecord(
                id="task-1",
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path="/tmp/demo",
                prompt="do work",
                context_project_slugs=["demo", "api"],
            )

            context = read_task_context(index_dir, task, project)

        self.assertIn("Active projects: demo, api", context)
        self.assertIn("Demo context", context)
        self.assertIn("API context", context)

    def test_direct_execution_prompt_records_planning_was_skipped(self) -> None:
        task = TaskRecord(
            id="task-1",
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="implement directly",
            kind="direct_task",
        )

        run_prompt = execution_prompt(task, "")

        self.assertIn("direct execution mode without read-only planning", run_prompt)

    def test_extract_session_id_reads_codex_log_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            log_path.write_text(
                "OpenAI Codex v0.135.0\n"
                "session id: 019e7842-d41f-72b3-9394-911a9c490cb4\n",
                encoding="utf-8",
            )

            session_id = extract_session_id(log_path)

        self.assertEqual(session_id, "019e7842-d41f-72b3-9394-911a9c490cb4")

    def test_resume_command_uses_saved_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                bot_token="token",
                allowed_chat_ids={1},
                allowed_user_ids=set(),
                project_roots=[],
                index_dir=Path(tmp) / "index",
                state_dir=Path(tmp) / "state",
                codex_bin="codex",
                model="gpt-test",
                poll_timeout_seconds=30,
                prompt_debounce_seconds=60,
                plan_timeout_seconds=60,
                run_timeout_seconds=None,
                transcribe_command=None,
                transcribe_timeout_seconds=300,
                env_file=Path(tmp) / "telegram.env",
            )
            runner = CodexRunner(config, TaskStore(config.state_dir))

            command = runner.executor_for("codex").build(
                MODE_RESUME,
                project_path="/tmp/demo",
                output_path=Path(tmp) / "out.md",
                session_id="019e7842-d41f-72b3-9394-911a9c490cb4",
            )

        self.assertEqual(command.argv[:3], ["codex", "exec", "resume"])
        self.assertIn('approval_policy="never"', command.argv)
        self.assertIn('sandbox_mode="danger-full-access"', command.argv)
        self.assertIn("gpt-test", command.argv)
        self.assertEqual(
            command.argv[-2:],
            ["019e7842-d41f-72b3-9394-911a9c490cb4", "-"],
        )

    def test_force_push_command_bypasses_approval_policy_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                bot_token="token",
                allowed_chat_ids={1},
                allowed_user_ids=set(),
                project_roots=[],
                index_dir=Path(tmp) / "index",
                state_dir=Path(tmp) / "state",
                codex_bin="codex",
                model=None,
                poll_timeout_seconds=30,
                prompt_debounce_seconds=60,
                plan_timeout_seconds=60,
                run_timeout_seconds=None,
                transcribe_command=None,
                transcribe_timeout_seconds=300,
                env_file=Path(tmp) / "telegram.env",
            )
            runner = CodexRunner(config, TaskStore(config.state_dir))

            command = runner.executor_for("codex").build(
                MODE_FORCE_PUSH,
                project_path="/tmp/demo",
                output_path=Path(tmp) / "out.md",
            )

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command.argv)
        self.assertIn("--ignore-rules", command.argv)
        self.assertEqual(command.argv[-1], "-")

    def test_claude_execution_writes_stdout_to_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            config = Config(
                bot_token="token",
                allowed_chat_ids={1},
                allowed_user_ids=set(),
                project_roots=[],
                index_dir=Path(tmp) / "index",
                state_dir=Path(tmp) / "state",
                codex_bin="codex",
                model=None,
                poll_timeout_seconds=30,
                prompt_debounce_seconds=60,
                plan_timeout_seconds=60,
                run_timeout_seconds=30,
                transcribe_command=None,
                transcribe_timeout_seconds=300,
                env_file=Path(tmp) / "telegram.env",
                claude_executor_command=(
                    f"{sys.executable} -c \"import sys; "
                    "sys.stdin.read(); print('CLAUDE_DONE')\""
                ),
            )
            runner = CodexRunner(config, TaskStore(config.state_dir))
            project = ProjectInfo(
                slug="demo",
                name="demo",
                path=str(project_dir),
                base=tmp,
                is_git=False,
                branch=None,
                origin=None,
                languages=[],
                markers=[],
                docs=[],
                test_hints=[],
            )
            task = TaskRecord(
                id="task-1",
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path=str(project_dir),
                prompt="implement with claude",
            )
            runner.store.save_task(task)

            result = runner.run_execution(task, project, provider="claude")
            final_text = Path(result.final_path).read_text(encoding="utf-8").strip()

        self.assertEqual(result.phase, "completed")
        self.assertEqual(result.executor_provider, "claude")
        self.assertEqual(final_text, "CLAUDE_DONE")

    def test_continuation_prompt_includes_parent_and_followup(self) -> None:
        parent = TaskRecord(
            id="parent-1",
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="initial task",
        )
        task = TaskRecord(
            id="task-2",
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="continue with tests",
            kind="followup_task",
            parent_task_id=parent.id,
            codex_session_id="019e7842-d41f-72b3-9394-911a9c490cb4",
        )

        prompt = continuation_prompt(task, "", parent)

        self.assertIn("Parent task:", prompt)
        self.assertIn("initial task", prompt)
        self.assertIn("continue with tests", prompt)

    def test_force_push_prompt_requires_safe_explicit_push(self) -> None:
        parent = TaskRecord(
            id="parent-1",
            chat_id=10,
            user_id=20,
            project_slug="root",
            project_name="workspace-root",
            project_path="/home/stanislavv",
            prompt="finish task",
            run_log_path="/tmp/task/run.log",
            final_path="/tmp/task/final.md",
        )
        task = TaskRecord(
            id="force-1",
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="force push parent branch",
            kind="force_push_task",
            parent_task_id=parent.id,
        )

        prompt = force_push_prompt(task, "Demo context", parent)

        self.assertIn("Force push button", prompt)
        self.assertIn("git push --force-with-lease", prompt)
        self.assertIn("Do not force-push protected branches `main` or `master`", prompt)
        self.assertIn("/tmp/task/run.log", prompt)

    def test_force_push_agent_command_bypasses_approval_policy_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "project"
            project_dir.mkdir()
            args_path = root / "codex-args.txt"
            codex_bin = root / "codex"
            codex_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib\n"
                "import sys\n"
                f"pathlib.Path({str(args_path)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
                "sys.stdin.read()\n",
                encoding="utf-8",
            )
            codex_bin.chmod(0o755)
            config = Config(
                bot_token="token",
                allowed_chat_ids={1},
                allowed_user_ids=set(),
                project_roots=[],
                index_dir=root / "index",
                state_dir=root / "state",
                codex_bin=str(codex_bin),
                model=None,
                poll_timeout_seconds=30,
                prompt_debounce_seconds=60,
                plan_timeout_seconds=60,
                run_timeout_seconds=30,
                transcribe_command=None,
                transcribe_timeout_seconds=300,
                env_file=root / "telegram.env",
            )
            runner = CodexRunner(config, TaskStore(config.state_dir))
            project = ProjectInfo(
                slug="demo",
                name="demo",
                path=str(project_dir),
                base=str(root),
                is_git=True,
                branch="feature/demo",
                origin=None,
                languages=[],
                markers=[],
                docs=[],
                test_hints=[],
            )
            task = TaskRecord(
                id="force-1",
                chat_id=10,
                user_id=20,
                project_slug="demo",
                project_name="demo",
                project_path=str(project_dir),
                prompt="force push",
                kind="force_push_task",
                parent_task_id="parent-1",
            )
            runner.store.save_task(task)

            result = runner.run_force_push_agent(task, project, None, provider="codex")
            args = args_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.phase, "completed")
        self.assertIn('approval_policy="never"', args)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertIn("--ignore-rules", args)
        self.assertEqual(args[-1], "-")

    def test_interrupted_execution_prompt_includes_recovery_context(self) -> None:
        task = TaskRecord(
            id="task-1",
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path="/tmp/demo",
            prompt="finish implementation",
            phase="running",
            run_log_path="/tmp/task/run.log",
            final_path="/tmp/task/final.md",
            codex_session_id="019e7842-d41f-72b3-9394-911a9c490cb4",
            recovery_attempts=1,
        )
        task.plan_text = "Change the smallest set of files."

        prompt = interrupted_execution_prompt(
            task,
            "Project context",
            "edited file A",
            "partial final",
        )

        self.assertIn("interrupted by a bot, daemon, or container restart", prompt)
        self.assertIn("Do not repeat already completed changes", prompt)
        self.assertIn("/tmp/task/run.log", prompt)
        self.assertIn("partial final", prompt)
        self.assertIn("Changed:", prompt)

    def test_read_text_tail_limits_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            path.write_text("a" * 100 + "tail", encoding="utf-8")

            tail = read_text_tail(path, max_chars=8)

        self.assertEqual(tail, "aaaatail")


class ProviderRoutingTest(unittest.TestCase):
    """Every runner entry point must honour the selected executor provider.

    Regression guard: planning, agent chat, follow-ups and force push used to
    ignore the provider and always spawn Codex, so picking Claude in Telegram
    silently kept using a Codex session.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir()
        self.claude_args = self.root / "claude-args.txt"
        self.codex_marker = self.root / "codex-was-called.txt"

        self.claude_bin = self._write_script(
            "claude",
            f"pathlib.Path({str(self.claude_args)!r}).write_text("
            "'\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
            "sys.stdin.read()\n"
            "print('CLAUDE_OUTPUT')\n",
        )
        self.codex_bin = self._write_script(
            "codex",
            f"pathlib.Path({str(self.codex_marker)!r}).write_text('called', encoding='utf-8')\n"
            "sys.stdin.read()\n",
        )

    def _write_script(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(
            "#!/usr/bin/env python3\nimport pathlib\nimport sys\n" + body,
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _runner(self) -> CodexRunner:
        config = Config(
            bot_token="token",
            allowed_chat_ids={1},
            allowed_user_ids=set(),
            project_roots=[],
            index_dir=self.root / "index",
            state_dir=self.root / "state",
            codex_bin=str(self.codex_bin),
            model=None,
            poll_timeout_seconds=30,
            prompt_debounce_seconds=60,
            plan_timeout_seconds=30,
            run_timeout_seconds=30,
            transcribe_command=None,
            transcribe_timeout_seconds=300,
            env_file=self.root / "telegram.env",
            claude_executor_command=f"{self.claude_bin} -p",
            claude_readonly_command=f"{self.claude_bin} -p --permission-mode plan",
        )
        return CodexRunner(config, TaskStore(config.state_dir))

    def _project(self) -> ProjectInfo:
        return ProjectInfo(
            slug="demo",
            name="demo",
            path=str(self.project_dir),
            base=str(self.root),
            is_git=False,
            branch=None,
            origin=None,
            languages=[],
            markers=[],
            docs=[],
            test_hints=[],
        )

    def _task(self, runner: CodexRunner, task_id: str, **kwargs: object) -> TaskRecord:
        task = TaskRecord(
            id=task_id,
            chat_id=10,
            user_id=20,
            project_slug="demo",
            project_name="demo",
            project_path=str(self.project_dir),
            prompt="do the work",
            **kwargs,
        )
        runner.store.save_task(task)
        return task

    def _claude_argv(self) -> list[str]:
        return self.claude_args.read_text(encoding="utf-8").splitlines()

    def test_planning_with_claude_never_spawns_codex(self) -> None:
        runner = self._runner()
        task = self._task(runner, "plan-1")

        result = runner.run_planning(task, self._project(), provider="claude")

        self.assertFalse(self.codex_marker.exists())
        self.assertEqual(result.phase, "planned")
        self.assertEqual(result.executor_provider, "claude")
        self.assertIn("CLAUDE_OUTPUT", result.plan_text)
        # Planning must stay read-only.
        self.assertIn("plan", self._claude_argv())

    def test_agent_chat_with_claude_never_spawns_codex(self) -> None:
        runner = self._runner()
        task = self._task(runner, "chat-1", kind="agent_chat")

        result = runner.run_agent_chat(task, self._project(), provider="claude")

        self.assertFalse(self.codex_marker.exists())
        self.assertEqual(result.phase, "agent_completed")
        self.assertEqual(result.executor_provider, "claude")

    def test_force_push_with_claude_never_spawns_codex(self) -> None:
        runner = self._runner()
        task = self._task(runner, "force-1", kind="force_push_task")

        result = runner.run_force_push_agent(
            task,
            self._project(),
            None,
            provider="claude",
        )

        self.assertFalse(self.codex_marker.exists())
        self.assertEqual(result.phase, "completed")
        self.assertEqual(result.executor_provider, "claude")

    def test_claude_execution_records_session_and_followup_resumes_it(self) -> None:
        runner = self._runner()
        project = self._project()
        task = self._task(runner, "task-1")

        executed = runner.run_execution(task, project, provider="claude")
        session_id = executed.claude_session_id

        self.assertTrue(session_id)
        self.assertIn("--session-id", self._claude_argv())
        self.assertEqual(executed.codex_session_id, "")

        followup = runner.run_followup_execution(executed, project, provider="claude")
        argv = self._claude_argv()

        self.assertFalse(self.codex_marker.exists())
        self.assertEqual(followup.phase, "completed")
        self.assertIn("--resume", argv)
        self.assertIn(session_id, argv)

    def test_followup_without_session_falls_back_to_fresh_run(self) -> None:
        runner = self._runner()
        task = self._task(runner, "followup-1", kind="followup_task", parent_task_id="parent-1")

        result = runner.run_followup_execution(task, self._project(), provider="claude")
        argv = self._claude_argv()

        self.assertFalse(self.codex_marker.exists())
        self.assertEqual(result.phase, "completed")
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)

    def test_unavailable_provider_fails_with_reason_instead_of_other_provider(self) -> None:
        runner = self._runner()
        runner.config = replace(runner.config, claude_enabled=False)
        runner.executors = build_executors(runner.config)
        task = self._task(runner, "task-2")

        result = runner.run_execution(task, self._project(), provider="claude")

        self.assertFalse(self.codex_marker.exists())
        self.assertEqual(result.phase, "failed")
        self.assertIn("disabled", result.error)


if __name__ == "__main__":
    unittest.main()
