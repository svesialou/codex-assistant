# Codex Assistant

Local Telegram long-poll bot for starting Codex tasks in projects under
`~/Projects` and `~/MyProjects`.

The bot itself is Python stdlib-only. Docker is the recommended runtime because
`restart: unless-stopped` keeps the bot running after crashes and daemon restarts.

## What Is Stored Where

- Repository: source code, tests, Docker config, install scripts.
- `~/.codex/secrets/telegram.env`: Telegram and optional Slack secrets.
- `~/.codex/telegram-bot`: task state, attachments, logs, chat state.
- `~/.codex/project-index`: generated project index and per-project context.
- `~/.codex/auth.json` and `~/.codex/config.toml`: Codex CLI auth/config reused by the container.

Do not commit `~/.codex`, `.env`, logs, sqlite files, or task state.

## Install On A PC

Prerequisites:

- Docker with Compose v2
- Git
- A configured Codex CLI account in `~/.codex` on the host
- Telegram bot token and allowed chat id

Clone and install:

```sh
git clone git@github.com:kaselrap/codex-assistant.git ~/Projects/codex-assistant
cd ~/Projects/codex-assistant
./scripts/install.sh
```

Fill secrets:

```sh
nano ~/.codex/secrets/telegram.env
```

Required values:

```sh
CODEX_TELEGRAM_BOT_TOKEN=123456:telegram-token
CODEX_TELEGRAM_CHAT_ID=123456789
```

Optional hardening:

```sh
CODEX_TELEGRAM_ALLOWED_CHAT_IDS=123456789
CODEX_TELEGRAM_ALLOWED_USER_IDS=123456789
```

Start through Docker:

```sh
~/.codex/scripts/codex-telegram-bot-control.sh restart
~/.codex/scripts/codex-telegram-bot-control.sh status
~/.codex/scripts/codex-telegram-bot-control.sh log
```

The control script uses Docker automatically when the cloned repository and
`docker-compose.yml` are available. Set `CODEX_TELEGRAM_RUN_MODE=process` only
when you explicitly want the old background-process mode.

## Docker Runtime

Compose mounts the host workspace into the container:

- `${CODEX_HOST_HOME}/.codex` -> `/home/codex/.codex`
- `${CODEX_HOST_HOME}/Projects` -> `/home/codex/Projects`
- `${CODEX_HOST_HOME}/MyProjects` -> `/home/codex/MyProjects`

The generated `.env` file stores only local paths and UID/GID:

```sh
CODEX_HOST_HOME=/home/your-user
HOST_UID=1000
HOST_GID=1000
CODEX_CLI_VERSION=0.135.0
```

On container startup, Compose rebuilds the project index with container paths
before starting the long-poll bot. This avoids stale host paths in Docker mode.

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
python -m unittest discover -s tests
```

Run the bot without Docker:

```sh
PYTHONPATH=. python -m codex_telegram_bot
```

Rebuild the project index:

```sh
PYTHONPATH=. python -m codex_telegram_bot --reindex
```

## Configuration

Optional Telegram/Codex settings in `~/.codex/secrets/telegram.env`:

```sh
CODEX_TELEGRAM_PROJECT_DIRS="$HOME/Projects:$HOME/MyProjects"
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
