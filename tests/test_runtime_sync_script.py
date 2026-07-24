from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class RuntimeSyncScriptTest(unittest.TestCase):
    def test_sync_script_installs_source_runtime_into_tool_dir(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "codex-telegram-bot-sync.sh"
        if not script.exists():
            script = Path.home() / ".codex" / "scripts" / "codex-telegram-bot-sync.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            tool_dir = root / "codex" / "tools" / "codex_telegram_bot"
            (source / "codex_telegram_bot").mkdir(parents=True)
            (source / "tests").mkdir()
            (tool_dir / "codex_telegram_bot").mkdir(parents=True)
            (source / "codex_telegram_bot" / "__init__.py").write_text(
                "source\n",
                encoding="utf-8",
            )
            (source / "tests" / "test_placeholder.py").write_text(
                "source test\n",
                encoding="utf-8",
            )
            (source / "README.md").write_text("readme\n", encoding="utf-8")
            (source / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (tool_dir / "codex_telegram_bot" / "__init__.py").write_text(
                "stale\n",
                encoding="utf-8",
            )

            env = dict(os.environ)
            env["CODEX_HOME"] = str(root / "codex")
            env["CODEX_ASSISTANT_REPO"] = str(source)
            result = subprocess.run(
                [str(script), "--quiet"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env=env,
            )

            installed = (tool_dir / "codex_telegram_bot" / "__init__.py").read_text(
                encoding="utf-8"
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(installed, "source\n")


if __name__ == "__main__":
    unittest.main()
