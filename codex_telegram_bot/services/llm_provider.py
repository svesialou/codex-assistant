from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol

from ..config import Config


class LlmProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmRequest:
    role: str
    prompt: str
    model: str | None = None
    max_tokens: int | None = None
    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class LlmResponse:
    text: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class LlmProvider(Protocol):
    def complete(self, request: LlmRequest) -> LlmResponse:
        ...


class ClaudeProvider:
    def __init__(self, config: Config) -> None:
        self.config = config

    def complete(self, request: LlmRequest) -> LlmResponse:
        if not self.config.claude_enabled:
            raise LlmProviderUnavailable("Claude provider is disabled.")
        if not self.config.claude_command:
            raise LlmProviderUnavailable("Claude command transport is not configured.")

        command_text = self.config.claude_command
        if "{model}" in command_text or "{max_tokens}" in command_text:
            command_text = command_text.format(
                model=request.model or "",
                max_tokens=request.max_tokens or "",
            )
        command = shlex.split(command_text)
        if not command:
            raise LlmProviderUnavailable("Claude command transport is empty.")
        env = os.environ.copy()
        if request.model:
            env["LLM_MODEL"] = request.model
            env["CLAUDE_MODEL"] = request.model
        if request.max_tokens:
            env["LLM_MAX_TOKENS"] = str(request.max_tokens)
            env["CLAUDE_MAX_TOKENS"] = str(request.max_tokens)

        try:
            completed = subprocess.run(
                command,
                input=request.prompt,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.config.claude_timeout_seconds,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LlmProviderUnavailable(f"Claude transport failed: {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()[:500]
            raise LlmProviderUnavailable(
                f"Claude transport exited with {completed.returncode}: {stderr}"
            )
        return LlmResponse(
            text=completed.stdout.strip(),
            model=request.model,
            input_tokens=estimate_tokens(request.prompt),
            output_tokens=estimate_tokens(completed.stdout),
        )


class CodexProvider:
    def __init__(self, config: Config) -> None:
        self.config = config

    def complete(self, request: LlmRequest) -> LlmResponse:
        command = [
            self.config.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "-c",
            'approval_policy="never"',
            "-s",
            "read-only",
            "-",
        ]
        if request.model:
            command.extend(["-m", request.model])
        try:
            completed = subprocess.run(
                command,
                input=request.prompt,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.config.plan_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LlmProviderUnavailable(f"Codex transport failed: {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()[:500]
            raise LlmProviderUnavailable(
                f"Codex transport exited with {completed.returncode}: {stderr}"
            )
        return LlmResponse(
            text=completed.stdout.strip(),
            model=request.model,
            input_tokens=estimate_tokens(request.prompt),
            output_tokens=estimate_tokens(completed.stdout),
        )


class MockProvider:
    def __init__(self, responses: list[LlmResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        if not self.responses:
            return LlmResponse(text="{}", model=request.model)
        response = self.responses.pop(0)
        if response.model is None and request.model:
            return LlmResponse(
                text=response.text,
                model=request.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        return response


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0
