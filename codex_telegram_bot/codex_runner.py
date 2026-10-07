from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
from pathlib import Path

from .config import DEFAULT_TASK_PROVIDER, Config, normalize_task_provider
from .project_index import ProjectInfo
from .services.executors import (
    MODE_EXECUTE,
    MODE_FORCE_PUSH,
    MODE_PLAN,
    MODE_RESUME,
    Executor,
    build_executors,
    resolve_executor,
)
from .stream_events import StreamTracker
from .task_store import TaskRecord, TaskStore, process_start_time

SESSION_ID_RE = re.compile(r"(?im)^session id:\s*([^\s]+)")
SESSION_ID_READ_BYTES = 64 * 1024


def read_context(index_dir: Path, project_slug: str, max_chars: int = 12000) -> str:
    path = index_dir / "agents" / f"{project_slug}.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n..."


def task_context_slugs(task: TaskRecord, primary_slug: str) -> list[str]:
    slugs: list[str] = []
    for slug in [primary_slug, *task.context_project_slugs]:
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs or [primary_slug]


def read_task_context(
    index_dir: Path,
    task: TaskRecord,
    primary_project: ProjectInfo,
    max_chars: int = 20000,
) -> str:
    slugs = task_context_slugs(task, primary_project.slug)
    if len(slugs) == 1:
        return read_context(index_dir, slugs[0])

    per_project_limit = max(2000, max_chars // len(slugs))
    sections = [
        "# Active Development Context",
        "",
        f"- Primary project: `{primary_project.slug}`",
        f"- Active projects: {', '.join(slugs)}",
    ]
    for slug in slugs:
        context = read_context(index_dir, slug, max_chars=per_project_limit)
        if not context:
            continue
        sections.extend(["", f"## Project Context: {slug}", "", context.rstrip()])
    return "\n".join(sections).rstrip() + "\n"


def attachment_context(task: TaskRecord) -> str:
    if not task.attachments:
        return "- none"

    lines: list[str] = []
    for index, attachment in enumerate(task.attachments, start=1):
        file_name = str(attachment.get("file_name") or "attachment")
        file_path = str(attachment.get("file_path") or "")
        kind = str(attachment.get("kind") or "file")
        mime_type = str(attachment.get("mime_type") or "unknown")
        file_size = attachment.get("file_size")
        size_text = f", {file_size} bytes" if file_size else ""
        line = f"- {index}. {file_name} ({kind}, {mime_type}{size_text}): {file_path}"
        caption = str(attachment.get("caption") or "").strip()
        if caption:
            line += f"\n  Caption: {caption}"
        lines.append(line)
    return "\n".join(lines)


def truncate_for_prompt(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n..."


def read_text_tail(path: str | Path, max_chars: int = 4000) -> str:
    if not path:
        return ""

    file_path = Path(path)
    try:
        with file_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_chars * 4))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:].lstrip()


def extract_session_id(log_path: Path) -> str:
    try:
        with log_path.open("rb") as handle:
            text = handle.read(SESSION_ID_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""

    match = SESSION_ID_RE.search(text)
    return match.group(1).strip() if match else ""


def stop_process_group(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        return


def wait_after_stop(process: subprocess.Popen[bytes], timeout: int = 30) -> int:
    stop_process_group(process.pid)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process_group(process.pid, signal.SIGKILL)
        return process.wait(timeout=timeout)


def planning_prompt(task: TaskRecord, context: str) -> str:
    clarifications = "\n".join(f"- {item}" for item in task.clarifications) or "- none"
    attachments = attachment_context(task)
    return f"""You are preparing a Codex task that came from a local Telegram bot.

Do not edit files. Do not run destructive commands. Inspect the repository only as needed.
The user communicates in Russian; answer in Russian.

Project:
- name: {task.project_name}
- path: {task.project_path}
- slug: {task.project_slug}

Project agent context:
{context}

User task:
{task.prompt}

Known clarifications:
{clarifications}

Attached files:
{attachments}

Treat attached files as user-provided local inputs. Use their paths only when needed, do not log file contents or secrets.

Return a concise planning response with exactly these sections:

Requirements:
- Ask 0-3 concrete questions only if they materially affect implementation.
- If enough context is available, write "No blocking questions."

Recommended path:
- 3-7 bullets with the smallest safe implementation path.

Risks:
- Important risk areas, or "No special risks."

Confirmation:
- Tell the user to send /answer {task.id} <text> if requirements need clarification.
- Tell the user to send /confirm {task.id} when ready to execute.
- Also mention that Telegram buttons can add files, answer clarifications, or execute the task.
"""


def execution_prompt(task: TaskRecord, context: str) -> str:
    clarifications = "\n".join(f"- {item}" for item in task.clarifications) or "- none"
    if task.kind == "direct_task" and not task.plan_text.strip():
        intro = "The user explicitly started this Telegram task in direct execution mode. Execute it in the selected project."
        plan = "This task was started in direct execution mode without read-only planning."
    else:
        intro = "The user confirmed this Telegram task. Execute it in the selected project."
        plan = task.plan_text.strip() or "No prior plan was recorded."
    attachments = attachment_context(task)
    prepared_prompt = task.prepared_codex_prompt.strip()
    codex_task = prepared_prompt or task.prompt
    prepared_block = (
        f"\nPrepared prompt from orchestrator:\n{prepared_prompt}\n"
        if prepared_prompt
        else ""
    )
    return f"""{intro}

Follow global and project AGENTS instructions. Before editing, inspect relevant repo files and local instructions.
Keep the change minimal and focused. Ask only if blocked by a risky decision that cannot be inferred.
If you launch subagents, wait until every one of them has finished before writing the final answer.
Run relevant checks where practical. Final answer must be in Russian and use:

- Changed:
- Why:
- Verified:
- Risks / Notes:

Project:
- name: {task.project_name}
- path: {task.project_path}
- slug: {task.project_slug}

Project agent context:
{context}

Original task:
{task.prompt}
{prepared_block}
Task to execute:
{codex_task}

Clarifications:
{clarifications}

Attached files:
{attachments}

Treat attached files as user-provided local inputs. Use their paths only when needed, do not log file contents or secrets.

Planning response already shown to user:
{plan}
"""


def interrupted_execution_prompt(
    task: TaskRecord,
    context: str,
    previous_log_tail: str,
    previous_final: str,
) -> str:
    clarifications = "\n".join(f"- {item}" for item in task.clarifications) or "- none"
    attachments = attachment_context(task)
    plan = task.plan_text.strip() or "No prior plan was recorded."
    log_tail = previous_log_tail.strip() or "-"
    final_text = previous_final.strip() or "-"
    return f"""The previous Codex process for this Telegram task was interrupted by a bot, daemon, or container restart.

Continue from the current workspace state. First inspect git status, relevant files, existing logs, and the existing final answer if useful.
Do not assume that no work was done before the restart. Do not repeat already completed changes.
Do not revert unrelated changes or user changes. If the requested work is already complete, verify it and produce the final response.
If you launch subagents, wait until every one of them has finished before writing the final answer.
Run relevant checks where practical. Final answer must be in Russian and use:

- Changed:
- Why:
- Verified:
- Risks / Notes:

Project:
- name: {task.project_name}
- path: {task.project_path}
- slug: {task.project_slug}

Project agent context:
{context}

Telegram task state:
- id: {task.id}
- kind: {task.kind}
- parent task id: {task.parent_task_id or "-"}
- recovery attempt: {task.recovery_attempts}
- previous run log path: {task.run_log_path or "-"}
- previous final answer path: {task.final_path or "-"}
- executor session id available: {"yes" if (task.codex_session_id or task.claude_session_id) else "no"}

Original task:
{task.prompt}

Clarifications:
{clarifications}

Attached files:
{attachments}

Treat attached files as user-provided local inputs. Use their paths only when needed, do not log file contents or secrets.

Planning response already shown to user:
{plan}

Previous run log tail:
{log_tail}

Existing final answer, if any:
{final_text}
"""


def continuation_prompt(
    task: TaskRecord,
    context: str,
    parent_task: TaskRecord | None,
) -> str:
    clarifications = "\n".join(f"- {item}" for item in task.clarifications) or "- none"
    attachments = attachment_context(task)
    parent_id = parent_task.id if parent_task is not None else task.parent_task_id or "-"
    parent_prompt = (
        truncate_for_prompt(parent_task.prompt)
        if parent_task is not None
        else "Parent task record is unavailable."
    )
    return f"""The user asked to continue a completed Telegram Codex task. Continue in the resumed Codex session.

Follow global and project AGENTS instructions. Before editing, inspect relevant repo files and local instructions when needed.
Keep the change minimal and focused. Ask only if blocked by a risky decision that cannot be inferred.
If you launch subagents, wait until every one of them has finished before writing the final answer.
Run relevant checks where practical. Final answer must be in Russian and use:

- Changed:
- Why:
- Verified:
- Risks / Notes:

Project:
- name: {task.project_name}
- path: {task.project_path}
- slug: {task.project_slug}

Project agent context:
{context}

Parent task:
- id: {parent_id}
- original task: {parent_prompt}

Follow-up instruction:
{task.prompt}

Clarifications:
{clarifications}

Attached files:
{attachments}

Treat attached files as user-provided local inputs. Use their paths only when needed, do not log file contents or secrets.
"""


def force_push_prompt(
    task: TaskRecord,
    context: str,
    parent_task: TaskRecord | None,
) -> str:
    parent_id = parent_task.id if parent_task is not None else task.parent_task_id or "-"
    parent_project = (
        f"{parent_task.project_name} ({parent_task.project_path})"
        if parent_task is not None
        else "-"
    )
    parent_log_path = parent_task.run_log_path if parent_task is not None else ""
    parent_final_path = parent_task.final_path if parent_task is not None else ""
    return f"""This Telegram action was explicitly triggered by the completed task's Force push button.

Execute only the force-push action needed for the completed task. Do not edit files. Do not commit.
Use `git push --force-with-lease`, not plain `--force`.
Before pushing, inspect the current branch and upstream. Do not force-push protected branches `main` or `master`.
If the target repository is ambiguous, stop and explain the ambiguity instead of guessing.
The user communicates in Russian; final answer must be in Russian and use:

- Changed:
- Why:
- Verified:
- Risks / Notes:

Force push task:
- id: {task.id}
- project name: {task.project_name}
- project path: {task.project_path}
- project slug: {task.project_slug}

Parent completed task:
- id: {parent_id}
- project: {parent_project}
- run log path: {parent_log_path or "-"}
- final answer path: {parent_final_path or "-"}

If `project path` is not a git repository, use the parent task artifacts above and the project context below to find the repository changed by the completed task.

Project agent context:
{context}

Original force-push instruction:
{task.prompt}
"""


def agent_chat_prompt(task: TaskRecord, context: str) -> str:
    return f"""You are Codex running as a read-only AI agent for a Telegram chat.

Do not edit files. Do not run destructive commands. Inspect local context only when it is useful.
The user communicates in Russian; answer in Russian.

Current workspace:
- name: {task.project_name}
- path: {task.project_path}
- slug: {task.project_slug}

Project agent context:
{context}

User message:
{task.prompt}

Answer directly and concisely. If the message is a task request, clarify the likely implementation path and tell the user they can create a task from this message with the Telegram button.
"""


class CodexRunner:
    def __init__(
        self,
        config: Config,
        store: TaskStore,
        runtime_id: str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.runtime_id = runtime_id or secrets.token_hex(8)
        self.executors = build_executors(config)

    def _command_env(self, provider: str, model: str | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_TELEGRAM_SUPPRESS_STOP_HOOK"] = "1"
        if provider == "claude" and model:
            env["LLM_MODEL"] = model
            env["CLAUDE_MODEL"] = model
        return env

    def executor_for(self, provider: str) -> Executor:
        return resolve_executor(self.executors, provider, self.config.task_provider)

    def provider_unavailable_reason(self, provider: str) -> str:
        return self.executor_for(provider).unavailable_reason()

    def _fail_task(self, task: TaskRecord, error: str) -> TaskRecord:
        task.phase = "failed"
        task.error = error
        self.store.save_task(task)
        return task

    def _run_provider(
        self,
        task: TaskRecord,
        *,
        provider: str,
        mode: str,
        prompt_path: Path,
        output_path: Path,
        log_path: Path,
        model: str | None,
        timeout_seconds: int | None,
        success_phase: str,
        timeout_error: str,
        failed_error_prefix: str,
    ) -> TaskRecord:
        """Spawn one executor run and fold its outcome back into the task."""
        executor = self.executor_for(provider)
        unavailable = executor.unavailable_reason()
        if unavailable:
            return self._fail_task(task, unavailable)

        try:
            command = executor.build(
                mode,
                project_path=task.project_path,
                output_path=output_path,
                model=model,
                session_id=task.session_id_for(provider),
            )
        except ValueError as exc:
            return self._fail_task(task, str(exc))

        if not command.argv:
            return self._fail_task(task, f"{executor.name} executor command is empty.")

        if command.session_id:
            task.set_session_id(provider, command.session_id)
            self.store.save_task(task)

        with prompt_path.open("rb") as stdin, log_path.open("ab") as log:
            log.write(f"\n[{executor.name} executor started: mode={mode}]\n".encode("utf-8"))
            log.flush()
            stream_offset = log.tell()
            if command.final_answer_on_stdout:
                stdout_target = output_path.open("wb")
                stderr_target = log
            else:
                stdout_target = log
                stderr_target = subprocess.STDOUT
            try:
                try:
                    process = subprocess.Popen(
                        command.argv,
                        stdin=stdin,
                        stdout=stdout_target,
                        stderr=stderr_target,
                        cwd=task.project_path,
                        start_new_session=True,
                        env=self._command_env(provider, model),
                    )
                except OSError as exc:
                    return self._fail_task(
                        task,
                        f"{executor.name} execution failed to start: {exc}",
                    )
                self._mark_process_started(task, process)
                try:
                    returncode = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    returncode = wait_after_stop(process)
                    task.phase = "failed"
                    task.error = timeout_error
                else:
                    task.returncode = returncode
                    if returncode == 0:
                        task.phase = success_phase
                    else:
                        task.phase = "failed"
                        task.error = f"{failed_error_prefix} with exit code {returncode}."
            finally:
                if stdout_target is not log:
                    stdout_target.close()

        if command.final_answer_from_stream:
            self._write_stream_final(task, log_path, stream_offset, output_path)
        if command.session_id_from_log:
            task.set_session_id(provider, extract_session_id(log_path))
        self._mark_process_finished(task)
        return task

    def _write_stream_final(
        self,
        task: TaskRecord,
        log_path: Path,
        offset: int,
        output_path: Path,
    ) -> None:
        tracker = StreamTracker(log_path, offset=offset)
        tracker.poll()
        state = tracker.state
        final = state.final_text
        unfinished = tracker.mark_unfinished()
        if unfinished:
            names = ", ".join(
                agent.description or agent.agent_type or agent.tool_use_id
                for agent in unfinished
            )
            final = f"{final}\n\n⚠️ Агенты не завершили работу до выхода процесса: {names}".strip()
        # Always rewrite: a reused final path must not keep a previous answer.
        output_path.write_text(final + "\n" if final else "", encoding="utf-8")
        if task.phase == "failed" and state.final_is_error and state.final_text:
            task.error = f"{task.error} {state.final_text}".strip()

    def _resolve_mode(self, provider: str, task: TaskRecord, writable: bool) -> str:
        """Resume when the provider can and a session exists, otherwise start fresh."""
        if not writable:
            return MODE_PLAN
        executor = self.executor_for(provider)
        if executor.supports_resume() and task.session_id_for(provider):
            return MODE_RESUME
        return MODE_EXECUTE

    def _mark_process_started(
        self,
        task: TaskRecord,
        process: subprocess.Popen[bytes],
    ) -> None:
        task.pid = process.pid
        task.pid_start_time = process_start_time(process.pid)
        task.runtime_id = self.runtime_id
        self.store.save_task(task)

    def _mark_process_finished(self, task: TaskRecord) -> None:
        task.pid = None
        task.pid_start_time = ""
        self.store.save_task(task)

    def run_planning(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        model: str | None = None,
        provider: str = DEFAULT_TASK_PROVIDER,
    ) -> TaskRecord:
        provider = normalize_task_provider(provider)
        task.phase = "planning"
        task.returncode = None
        task.error = ""
        task.executor_provider = provider
        task_dir = self.store.task_dir(task.id)
        plan_path = task_dir / "plan.md"
        log_path = task_dir / "plan.log"
        prompt_path = task_dir / "plan-prompt.md"
        task.plan_log_path = str(log_path)
        self.store.save_task(task)

        context = read_task_context(self.config.index_dir, task, project)
        prompt_path.write_text(planning_prompt(task, context), encoding="utf-8")

        task = self._run_provider(
            task,
            provider=provider,
            mode=MODE_PLAN,
            prompt_path=prompt_path,
            output_path=plan_path,
            log_path=log_path,
            model=model,
            timeout_seconds=self.config.plan_timeout_seconds,
            success_phase="planned",
            timeout_error="Planning timed out.",
            failed_error_prefix="Planning failed",
        )

        if task.phase == "planned":
            if plan_path.exists():
                task.plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
            else:
                task.phase = "failed"
                task.error = "Planning produced no plan output."
            self.store.save_task(task)
        return task

    def run_execution(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        model: str | None = None,
        provider: str = DEFAULT_TASK_PROVIDER,
    ) -> TaskRecord:
        provider = normalize_task_provider(provider)
        task.phase = "running"
        task.returncode = None
        task.error = ""
        task.executor_provider = provider
        task_dir = self.store.task_dir(task.id)
        final_path = task_dir / "final.md"
        log_path = task_dir / "run.log"
        prompt_path = task_dir / "run-prompt.md"
        task.final_path = str(final_path)
        task.run_log_path = str(log_path)
        task.prompt_path = str(prompt_path)
        self.store.save_task(task)

        context = read_task_context(self.config.index_dir, task, project)
        prompt_path.write_text(execution_prompt(task, context), encoding="utf-8")

        return self._run_provider(
            task,
            provider=provider,
            mode=MODE_EXECUTE,
            prompt_path=prompt_path,
            output_path=final_path,
            log_path=log_path,
            model=model,
            timeout_seconds=self.config.run_timeout_seconds,
            success_phase="completed",
            timeout_error="Execution timed out.",
            failed_error_prefix="Execution failed",
        )

    def run_recovery_execution(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        model: str | None = None,
        provider: str = DEFAULT_TASK_PROVIDER,
    ) -> TaskRecord:
        provider = normalize_task_provider(provider)
        task.phase = "running"
        task.returncode = None
        task.error = ""
        task.executor_provider = provider
        task.recovery_attempts += 1
        task_dir = self.store.task_dir(task.id)
        final_path = Path(task.final_path) if task.final_path else task_dir / "final.md"
        log_path = Path(task.run_log_path) if task.run_log_path else task_dir / "run.log"
        prompt_path = task_dir / "recovery-prompt.md"
        previous_log_tail = read_text_tail(log_path)
        previous_final = read_text_tail(final_path)
        task.final_path = str(final_path)
        task.run_log_path = str(log_path)
        task.prompt_path = str(prompt_path)
        if provider == "codex" and not task.codex_session_id:
            task.codex_session_id = extract_session_id(log_path)
        self.store.save_task(task)

        context = read_task_context(self.config.index_dir, task, project)
        prompt_path.write_text(
            interrupted_execution_prompt(
                task,
                context,
                previous_log_tail,
                previous_final,
            ),
            encoding="utf-8",
        )

        return self._run_provider(
            task,
            provider=provider,
            mode=self._resolve_mode(provider, task, writable=True),
            prompt_path=prompt_path,
            output_path=final_path,
            log_path=log_path,
            model=model,
            timeout_seconds=self.config.run_timeout_seconds,
            success_phase="completed",
            timeout_error="Recovered execution timed out.",
            failed_error_prefix="Recovered execution failed",
        )

    def run_followup_execution(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        model: str | None = None,
        provider: str = DEFAULT_TASK_PROVIDER,
    ) -> TaskRecord:
        provider = normalize_task_provider(provider)
        task.phase = "running"
        task.returncode = None
        task.error = ""
        task.executor_provider = provider
        task_dir = self.store.task_dir(task.id)
        final_path = task_dir / "final.md"
        log_path = task_dir / "run.log"
        prompt_path = task_dir / "run-prompt.md"
        task.final_path = str(final_path)
        task.run_log_path = str(log_path)
        task.prompt_path = str(prompt_path)
        self.store.save_task(task)

        context = read_task_context(self.config.index_dir, task, project)
        parent_task = self.store.load_task(task.parent_task_id) if task.parent_task_id else None
        prompt_path.write_text(
            continuation_prompt(task, context, parent_task),
            encoding="utf-8",
        )

        # The continuation prompt carries the parent task context, so a missing
        # session only costs conversation history, not the follow-up itself.
        return self._run_provider(
            task,
            provider=provider,
            mode=self._resolve_mode(provider, task, writable=True),
            prompt_path=prompt_path,
            output_path=final_path,
            log_path=log_path,
            model=model,
            timeout_seconds=self.config.run_timeout_seconds,
            success_phase="completed",
            timeout_error="Continuation timed out.",
            failed_error_prefix="Continuation failed",
        )

    def run_force_push_agent(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        parent_task: TaskRecord | None,
        model: str | None = None,
        provider: str = DEFAULT_TASK_PROVIDER,
    ) -> TaskRecord:
        provider = normalize_task_provider(provider)
        task.phase = "running"
        task.returncode = None
        task.error = ""
        task.executor_provider = provider
        task_dir = self.store.task_dir(task.id)
        final_path = task_dir / "force-push-final.md"
        log_path = task_dir / "force-push.log"
        prompt_path = task_dir / "force-push-prompt.md"
        task.final_path = str(final_path)
        task.run_log_path = str(log_path)
        task.prompt_path = str(prompt_path)
        self.store.save_task(task)

        context = read_task_context(self.config.index_dir, task, project)
        prompt_path.write_text(
            force_push_prompt(task, context, parent_task),
            encoding="utf-8",
        )

        return self._run_provider(
            task,
            provider=provider,
            mode=MODE_FORCE_PUSH,
            prompt_path=prompt_path,
            output_path=final_path,
            log_path=log_path,
            model=model,
            timeout_seconds=self.config.run_timeout_seconds,
            success_phase="completed",
            timeout_error="Force push agent timed out.",
            failed_error_prefix="Force push agent failed",
        )

    def run_revision_execution(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        round_number: int,
        model: str | None = None,
        provider: str = DEFAULT_TASK_PROVIDER,
    ) -> TaskRecord:
        provider = normalize_task_provider(provider)
        task.phase = "running"
        task.returncode = None
        task.error = ""
        task.executor_provider = provider
        task_dir = self.store.task_dir(task.id)
        final_path = task_dir / f"final-revision-{round_number}.md"
        log_path = task_dir / "run.log"
        prompt_path = task_dir / f"revision-{round_number}-prompt.md"
        task.final_path = str(final_path)
        task.run_log_path = str(log_path)
        task.prompt_path = str(prompt_path)
        self.store.save_task(task)

        context = read_task_context(self.config.index_dir, task, project)
        prompt_path.write_text(execution_prompt(task, context), encoding="utf-8")

        return self._run_provider(
            task,
            provider=provider,
            mode=self._resolve_mode(provider, task, writable=True),
            prompt_path=prompt_path,
            output_path=final_path,
            log_path=log_path,
            model=model,
            timeout_seconds=self.config.run_timeout_seconds,
            success_phase="completed",
            timeout_error="Revision execution timed out.",
            failed_error_prefix="Revision execution failed",
        )

    def run_agent_chat(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        model: str | None = None,
        provider: str = DEFAULT_TASK_PROVIDER,
    ) -> TaskRecord:
        provider = normalize_task_provider(provider)
        task.phase = "agent_running"
        task.returncode = None
        task.error = ""
        task.executor_provider = provider
        task_dir = self.store.task_dir(task.id)
        final_path = task_dir / "agent.md"
        log_path = task_dir / "agent.log"
        prompt_path = task_dir / "agent-prompt.md"
        task.final_path = str(final_path)
        task.run_log_path = str(log_path)
        task.prompt_path = str(prompt_path)
        self.store.save_task(task)

        context = read_task_context(self.config.index_dir, task, project)
        prompt_path.write_text(agent_chat_prompt(task, context), encoding="utf-8")

        return self._run_provider(
            task,
            provider=provider,
            mode=MODE_PLAN,
            prompt_path=prompt_path,
            output_path=final_path,
            log_path=log_path,
            model=model,
            timeout_seconds=self.config.plan_timeout_seconds,
            success_phase="agent_completed",
            timeout_error="Agent response timed out.",
            failed_error_prefix="Agent response failed",
        )
