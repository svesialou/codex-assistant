from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.voice import (
    TranscriptionError,
    build_transcribe_command,
    transcribe_audio,
)


class VoiceTest(unittest.TestCase):
    def test_build_transcribe_command_replaces_placeholder(self) -> None:
        path = Path("/tmp/audio.oga")
        self.assertEqual(
            build_transcribe_command("tool --file {file}", path),
            ["tool", "--file", "/tmp/audio.oga"],
        )

    def test_build_transcribe_command_appends_path_without_placeholder(self) -> None:
        path = Path("/tmp/audio.oga")
        self.assertEqual(build_transcribe_command("tool", path), ["tool", "/tmp/audio.oga"])

    def test_transcribe_audio_requires_command(self) -> None:
        with self.assertRaises(TranscriptionError):
            transcribe_audio(None, Path("/tmp/audio.oga"), 1)

    def test_transcribe_audio_returns_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "transcribe.sh"
            script.write_text("#!/usr/bin/env bash\necho transcript\n", encoding="utf-8")
            script.chmod(0o755)

            self.assertEqual(transcribe_audio(str(script), Path("/tmp/audio.oga"), 5), "transcript")


if __name__ == "__main__":
    unittest.main()
