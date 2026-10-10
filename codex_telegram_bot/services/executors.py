"""Config-driven CLI invocation for every supported task executor provider.

Each provider knows how to turn an abstract run mode into a concrete argv plus
the two bits of plumbing the runner needs to differ on: where the final answer
lands, and how a session id is obtained.
"""

from __future__ import annotations

import shlex
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, normalize_task_provider

# Read-only inspection: planning and agent chat.
MODE_PLAN = "plan"
# Writable execution of a fresh conversation.
MODE_EXECUTE = "execute"
# Writable execution continuing an existing conversation.
MODE_RESUME = "resume"
# Writable execution with every guard-rail disabled.
MODE_FORCE_PUSH = "force_push"

MODES = (MODE_PLAN, MODE_EXECUTE, MODE_RESUME, MODE_FORCE_PUSH)


@dataclass(frozen=True)
class ExecutorCommand:
    """A ready-to-spawn executor invocation."""

    argv: list[str] = field(default_factory=list)
    # Session id this run will use. Empty when the provider only reveals it
    # afterwards (Codex prints it into the run log).
    session_id: str = ""
    # Codex writes the final answer itself via `-o`; Claude prints it on stdout
    # and the runner has to redirect it into the output file.
    final_answer_on_stdout: bool = False
    # Claude in `--output-format stream-json` mode: stdout is a JSONL event
    # stream that goes into the run log; the final answer is extracted from it.
    final_answer_from_stream: bool = False
    # Whether the session id has to be recovered by parsing the run log.
    session_id_from_log: bool = True


class Executor:
    """Builds CLI invocations for one task provider."""

    name = ""

    def __init__(self, config: Config) -> None:
        self.config = config

    def unavailable_reason(self) -> str:
        """Empty string when the provider can run, otherwise a user-facing reason."""
        raise NotImplementedError

    def supports_resume(self) -> bool:
        raise NotImplementedError

    def build(
        self,
        mode: str,
        *,
        project_path: str,
        output_path: Path,
        model: str | None = None,
        session_id: str = "",
        effort: str | None = None,
    ) -> ExecutorCommand:
        raise NotImplementedError


def _binary_missing(binary: str) -> bool:
    if not binary:
        return True
    if shutil.which(binary) is not None:
        return False
    return not Path(binary).exists()


class CodexExecutor(Executor):
    name = "codex"

    def unavailable_reason(self) -> str:
        if _binary_missing(self.config.codex_bin):
            return f"Codex binary not found: {self.config.codex_bin or '-'}"
        return ""

    def supports_resume(self) -> bool:
        return True

    def _base_argv(
        self,
        project_path: str,
        output_path: Path,
        model: str | None,
    ) -> list[str]:
        argv = [
            self.config.codex_bin,
            "exec",
            "-C",
            project_path,
            "--skip-git-repo-check",
            "-c",
            'approval_policy="never"',
            "-o",
            str(output_path),
        ]
        if model:
            argv.extend(["-m", model])
        return argv

    def _effort_args(self, effort: str | None) -> list[str]:
        return ["-c", f'model_reasoning_effort="{effort}"'] if effort else []

    def build(
        self,
        mode: str,
        *,
        project_path: str,
        output_path: Path,
        model: str | None = None,
        session_id: str = "",
        effort: str | None = None,
    ) -> ExecutorCommand:
        selected_model = model or self.config.model

        if mode == MODE_RESUME:
            if not session_id:
                raise ValueError("Codex resume requires a session id.")
            argv = [
                self.config.codex_bin,
                "exec",
                "resume",
                "--skip-git-repo-check",
                "-c",
                'approval_policy="never"',
                "-c",
                'sandbox_mode="danger-full-access"',
                "-o",
                str(output_path),
            ]
            if selected_model:
                argv.extend(["-m", selected_model])
            argv.extend(self._effort_args(effort))
            argv.extend([session_id, "-"])
            return ExecutorCommand(argv=argv, session_id=session_id)

        argv = self._base_argv(project_path, output_path, selected_model)
        argv.extend(self._effort_args(effort))
        if mode == MODE_FORCE_PUSH:
            argv.extend(["--dangerously-bypass-approvals-and-sandbox", "--ignore-rules", "-"])
        elif mode == MODE_PLAN:
            argv.extend(["-s", "read-only", "-"])
        elif mode == MODE_EXECUTE:
            argv.extend(["-s", "danger-full-access", "-"])
        else:
            raise ValueError(f"Unsupported Codex run mode: {mode}")
        return ExecutorCommand(argv=argv)


class ClaudeExecutor(Executor):
    name = "claude"

    def unavailable_reason(self) -> str:
        if not self.config.claude_enabled:
            return "Claude provider is disabled (CODEX_CLAUDE_ENABLED=0)."
        if not self.config.claude_executor_command.strip():
            return "Claude executor command is empty."
        try:
            argv = shlex.split(self.config.claude_executor_command)
        except ValueError as exc:
            return f"Claude executor command is not parseable: {exc}"
        if not argv:
            return "Claude executor command is empty."
        if _binary_missing(argv[0]):
            return f"Claude binary not found: {argv[0]}"
        return ""

    def supports_resume(self) -> bool:
        return self.config.claude_resume_enabled

    def _template_argv(self, mode: str, model: str | None) -> tuple[list[str], bool]:
        if mode == MODE_PLAN:
            template = (
                self.config.claude_readonly_command
                or self.config.claude_executor_command
            )
        else:
            template = self.config.claude_executor_command

        has_model_placeholder = "{model}" in template
        if has_model_placeholder:
            template = template.format(model=model or "")
        return shlex.split(template), has_model_placeholder

    def build(
        self,
        mode: str,
        *,
        project_path: str,
        output_path: Path,
        model: str | None = None,
        session_id: str = "",
        effort: str | None = None,
    ) -> ExecutorCommand:
        del project_path, output_path  # Claude runs in cwd and prints to stdout.
        if mode not in MODES:
            raise ValueError(f"Unsupported Claude run mode: {mode}")

        selected_model = model or self.config.models.claude.standard
        argv, has_model_placeholder = self._template_argv(mode, selected_model)
        if not argv:
            raise ValueError("Claude executor command is empty.")
        if selected_model and not has_model_placeholder:
            argv.extend(["--model", selected_model])
        if effort and "--effort" not in argv:
            argv.extend(["--effort", effort])
        stream_json = uses_stream_json(argv)
        if stream_json and "--verbose" not in argv:
            # Claude rejects stream-json in print mode without --verbose.
            argv.append("--verbose")

        if mode == MODE_RESUME:
            if not session_id:
                raise ValueError("Claude resume requires a session id.")
            argv.extend(["--resume", session_id])
            used_session_id = session_id
        else:
            # Claude only reports its session id in JSON output modes, so assign
            # one up front to keep follow-ups resumable in plain text mode.
            used_session_id = str(uuid.uuid4())
            argv.extend(["--session-id", used_session_id])

        return ExecutorCommand(
            argv=argv,
            session_id=used_session_id,
            final_answer_on_stdout=not stream_json,
            final_answer_from_stream=stream_json,
            session_id_from_log=False,
        )


def uses_stream_json(argv: list[str]) -> bool:
    for index, arg in enumerate(argv):
        if arg == "--output-format=stream-json":
            return True
        if arg == "--output-format" and argv[index + 1 : index + 2] == ["stream-json"]:
            return True
    return False


def executor_display_name(provider: str) -> str:
    return "Claude" if provider == "claude" else "Codex"


def executor_agent_name(provider: str) -> str:
    return f"{executor_display_name(provider)}Dev"


def build_executors(config: Config) -> dict[str, Executor]:
    return {
        CodexExecutor.name: CodexExecutor(config),
        ClaudeExecutor.name: ClaudeExecutor(config),
    }


def resolve_executor(
    executors: dict[str, Executor],
    provider: str | None,
    default: str = "codex",
) -> Executor:
    return executors[normalize_task_provider(provider, default)]
