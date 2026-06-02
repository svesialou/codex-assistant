from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


class TranscriptionError(RuntimeError):
    pass


def build_transcribe_command(template: str, audio_path: Path) -> list[str]:
    parts = shlex.split(template)
    if not parts:
        raise TranscriptionError("Transcription command is empty.")

    path_value = str(audio_path)
    if any("{file}" in part for part in parts):
        return [part.replace("{file}", path_value) for part in parts]
    return [*parts, path_value]


def transcribe_audio(
    command_template: str | None,
    audio_path: Path,
    timeout_seconds: int,
) -> str:
    if not command_template:
        raise TranscriptionError(
            "Voice message was downloaded, but CODEX_TELEGRAM_TRANSCRIBE_CMD is not configured."
        )

    command = build_transcribe_command(command_template, audio_path)
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscriptionError(f"Transcription command failed: {exc}") from exc

    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip()
        raise TranscriptionError(
            f"Transcription command exited with {completed.returncode}: {error[:1000]}"
        )

    transcript = completed.stdout.strip()
    if not transcript:
        raise TranscriptionError("Transcription command returned empty text.")
    return transcript
