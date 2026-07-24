from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence


LINEAR_MCP_NAME = "linear"
LINEAR_MCP_URL = "https://mcp.linear.app/mcp"
LEGACY_COMMANDS = {
    "auth-callback",
    "auth-url",
    "issue-create",
    "issue-get",
    "issue-status",
    "states",
    "teams",
    "whoami",
}


class LinearMCPError(RuntimeError):
    pass


def codex_command() -> str:
    command = shutil.which("codex")
    if not command:
        raise LinearMCPError("codex CLI was not found in PATH.")
    return command


def run_codex_mcp(args: Sequence[str]) -> int:
    command = [codex_command(), "mcp", *args]
    return subprocess.run(command, check=False).returncode


def command_status(_args: argparse.Namespace) -> int:
    return run_codex_mcp(["get", LINEAR_MCP_NAME])


def command_login(_args: argparse.Namespace) -> int:
    return run_codex_mcp(["login", LINEAR_MCP_NAME])


def command_setup(_args: argparse.Namespace) -> int:
    return run_codex_mcp(["add", LINEAR_MCP_NAME, "--url", LINEAR_MCP_URL])


def print_legacy_message(command: str) -> None:
    print(
        f"error: '{command}' is no longer supported by codex-linear.",
        file=sys.stderr,
    )
    print(
        "Linear is integrated through the Codex MCP server, not through "
        "a local Linear GraphQL/OAuth helper.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("Run once:", file=sys.stderr)
    print(f"  codex mcp login {LINEAR_MCP_NAME}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Then start a new Codex session and use the Linear MCP tools.", file=sys.stderr)
    print(
        "Useful tool names include get_issue, list_teams, "
        "list_issue_statuses, and save_issue.",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compatibility helper for the Codex Linear MCP integration."
    )
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", help="Show Codex MCP Linear configuration.")
    status.set_defaults(func=command_status)

    login = subparsers.add_parser(
        "login",
        help="Authenticate the configured Linear MCP server.",
    )
    login.set_defaults(func=command_login)

    setup = subparsers.add_parser("setup", help="Add the official Linear MCP server to Codex.")
    setup.set_defaults(func=command_setup)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in LEGACY_COMMANDS:
        print_legacy_message(argv[0])
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    try:
        return int(args.func(args))
    except (LinearMCPError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
