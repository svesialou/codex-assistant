# Codex Assistant

Local Telegram long-poll bot for starting Codex tasks in local projects.
Project directories are configured through environment files during install.

The bot itself is Python stdlib-only. The default runtime is a host daemon:
`systemd --user` when available, otherwise a small background supervisor script.
Codex tasks therefore run through the host `codex` CLI, not inside Docker.

## What Is Stored Where

- Repository: source code, tests, optional Docker config, install scripts.
- `~/.codex/secrets/telegram.env`: Telegram and optional Slack secrets.
- `~/.codex/telegram-bot`: task state, attachments, logs, chat state.
- `~/.codex/project-index`: generated project index and per-project context.
- `~/.codex/auth.json` and `~/.codex/config.toml`: Codex CLI auth/config reused by host Codex tasks.

Do not commit `~/.codex`, `.env`, logs, sqlite files, or task state.

## Install On A PC

Prerequisites:

- Git
- A configured Codex CLI account in `~/.codex` on the host
- Python 3.12+
- Telegram bot token and allowed chat id

Clone anywhere and install:

```sh
git clone git@github.com:kaselrap/codex-assistant.git ~/src/codex-assistant
cd ~/src/codex-assistant
./scripts/install.sh
```

Configure secrets and project roots:

```sh
nano ~/.codex/secrets/telegram.env
```

Required values:

```sh
CODEX_TELEGRAM_BOT_TOKEN=123456:telegram-token
CODEX_TELEGRAM_CHAT_ID=123456789
```

Access control:

```sh
CODEX_TELEGRAM_ALLOWED_CHAT_IDS=123456789
CODEX_TELEGRAM_ALLOWED_USER_IDS=123456789
```

For a private chat, `CODEX_TELEGRAM_CHAT_ID` is enough: the bot uses the same
positive id as the allowed user id by default. For group chats, set
`CODEX_TELEGRAM_ALLOWED_USER_IDS` explicitly; otherwise the bot refuses to start
instead of accepting commands from everyone in the group.

Start the host daemon:

```sh
~/.codex/scripts/codex-telegram-bot-control.sh restart
~/.codex/scripts/codex-telegram-bot-control.sh status
~/.codex/scripts/codex-telegram-bot-control.sh log
```

`restart` prefers `systemd --user` and automatically installs
`codex-telegram-bot.service` with `Restart=on-failure`. If user systemd is not
available, the control script starts `codex-telegram-bot-supervisor.sh` in the
background; it restarts the bot after crashes. Use
`~/.codex/scripts/codex-telegram-bot-control.sh enable` to enable user autostart
on systems with `systemd --user`.

Runtime modes:

```sh
CODEX_TELEGRAM_RUN_MODE=auto       # default: systemd if available, else supervisor
CODEX_TELEGRAM_RUN_MODE=systemd    # require systemd --user
CODEX_TELEGRAM_RUN_MODE=supervisor # force the host supervisor
CODEX_TELEGRAM_RUN_MODE=process    # one background process without restart loop
CODEX_TELEGRAM_RUN_MODE=docker     # optional Docker runtime
```

## Optional Docker Runtime

Docker is still available as an explicit opt-in:

```sh
CODEX_TELEGRAM_RUN_MODE=docker ~/.codex/scripts/codex-telegram-bot-control.sh restart
```

Compose mounts Codex config and one host workspace root into the container:

- `${CODEX_HOST_HOME}/.codex` -> `/home/codex/.codex`
- `${CODEX_HOST_WORKSPACE_ROOT}` -> same absolute path inside the container

The generated repository-local `.env` file stores only local paths and UID/GID:

```sh
CODEX_HOST_HOME=/home/your-user
CODEX_HOST_WORKSPACE_ROOT=/home/your-user
CODEX_TELEGRAM_PROJECT_DIRS=/home/your-user/Projects:/home/your-user/MyProjects
HOST_UID=1000
HOST_GID=1000
CODEX_CLI_VERSION=0.135.0
```

If your projects live elsewhere, set `CODEX_HOST_WORKSPACE_ROOT` to their common
parent and `CODEX_TELEGRAM_PROJECT_DIRS` to colon-separated absolute project
roots under that mounted parent. Example:

```sh
CODEX_HOST_WORKSPACE_ROOT=/data/work
CODEX_TELEGRAM_PROJECT_DIRS=/data/work/company:/data/work/personal
```

For roots that do not share a practical parent, use a broader common parent
such as `/home/your-user`, or use the default host daemon runtime.

On container startup, Compose rebuilds the project index with container paths
before starting the long-poll bot. Because the workspace root is mounted to the
same absolute path, Codex edits the same paths on host and in Docker mode. The
Telegram `Root` context uses `CODEX_HOST_WORKSPACE_ROOT` in Docker mode.

Useful commands:

```sh
docker compose config
docker compose build
docker compose up -d --build
docker compose logs --tail=120 codex-telegram-bot
docker compose down
```

## Local Development

Run tests:

```sh
python3 -m unittest discover -s tests
```

Run the bot without Docker:

```sh
PYTHONPATH=. python3 -m codex_telegram_bot
```

Rebuild the project index:

```sh
PYTHONPATH=. python3 -m codex_telegram_bot --reindex
```

## Configuration

Optional Telegram/Codex settings in `~/.codex/secrets/telegram.env`:

```sh
CODEX_TELEGRAM_PROJECT_DIRS="/home/your-user/Projects:/home/your-user/MyProjects"
CODEX_TELEGRAM_WORKSPACE_ROOT="/home/your-user"
CODEX_TELEGRAM_INDEX_DIR="$HOME/.codex/project-index"
CODEX_TELEGRAM_STATE_DIR="$HOME/.codex/telegram-bot"
CODEX_TELEGRAM_CODEX_BIN=codex
CODEX_TELEGRAM_MODEL=gpt-5.5
CODEX_TELEGRAM_PROMPT_DEBOUNCE_SECONDS=3
CODEX_TELEGRAM_PLAN_TIMEOUT_SECONDS=1800
CODEX_TELEGRAM_RUN_TIMEOUT_SECONDS=0
CODEX_TELEGRAM_TRANSCRIBE_CMD='your-transcriber-command {file}'
CODEX_TELEGRAM_TRANSCRIBE_TIMEOUT_SECONDS=300
```

`CODEX_TELEGRAM_RUN_TIMEOUT_SECONDS=0` means no execution timeout.
`CODEX_TELEGRAM_TRANSCRIBE_CMD` is optional. When set, Telegram voice messages
are downloaded and passed to the command. The command must print the transcript
to stdout.

Interrupted task recovery:

```sh
CODEX_TELEGRAM_RECOVER_INTERRUPTED_TASKS=1
```

Recovery is enabled by default. On bot startup, stale `planning`, `running`, and
`agent_running` tasks from allowed chats are queued again. Running executions use
the saved Codex session id when available and a recovery prompt that asks Codex
to continue from the current workspace instead of blindly repeating completed
work.

Optional Slack forwarding:

```sh
CODEX_SLACK_TOKEN=xoxp-or-xoxb-token
CODEX_SLACK_WATCH_DMS=1
CODEX_SLACK_PINNED_CHANNEL_IDS=C0123456789,G0123456789
CODEX_SLACK_TELEGRAM_CHAT_IDS=123456789
CODEX_SLACK_POLL_INTERVAL_SECONDS=60
CODEX_SLACK_HISTORY_LIMIT=20
```

Slack channel IDs are configured explicitly. Use a Slack token with read access
for the conversation types you need.

## Telegram Commands

```text
/menu
/projects [query]
/refresh
/project <query>
/context <project>
/new
/agent on|off
/ask <text>
/task <project> <text>
/task <text>
/run <project> <text>
/run <text>
/answer <task_id> <text>
/confirm <task_id>
/continue <task_id> <text>
/cancel <task_id>
/status [task_id]
/processes
/logs <task_id>
```

## Execution Flow

1. Select `Root` or a project with buttons.
2. `New task` or `/task` creates a task in the selected context.
3. Consecutive text messages are joined after a short quiet window.
4. The bot runs `codex exec` with read-only sandbox for planning.
5. The plan message has buttons to attach files, answer clarifications, execute, or cancel.
6. `/answer` appends clarification and reruns read-only planning.
7. `/confirm` starts real `codex exec` with `danger-full-access`.
8. The final Codex answer is sent to Telegram, and logs stay on disk.
9. Completed tasks with a Codex session can be continued with `/continue`.

Direct execution is available through `/run` or the `Run task` button.
