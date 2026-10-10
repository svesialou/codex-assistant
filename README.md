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
`codex-telegram-bot.service` with `Restart=always`. If user systemd is not
available, the control script starts `codex-telegram-bot-supervisor.sh` in the
background; it restarts the bot after crashes. Use
`~/.codex/scripts/codex-telegram-bot-control.sh enable` to enable user autostart
on systems with `systemd --user`.

On systems with `systemd --user`, `enable` also installs and starts
`codex-telegram-bot-watchdog.timer`. The timer runs once per minute and starts
the bot if the service is inactive or failed. This covers clean stops that are
not treated as failures by systemd. If `restart` is invoked from a Codex task
owned by the bot service, the control script schedules a deferred restart in a
separate transient user unit so the current Codex process can finish before the
bot service is restarted.

`enable` also installs `codex-network-watchdog.timer`. It checks real internet
connectivity once per minute with HTTP probes before trusting the local
NetworkManager state. If the machine reports a wired/VPN connection but the
internet is unreachable, it reconnects matching active NetworkManager
connections and then lets the bot watchdog start or recover the bot. Reconnects
are throttled by a five minute cooldown.

Useful host daemon commands:

```sh
~/.codex/scripts/codex-telegram-bot-control.sh enable
~/.codex/scripts/codex-telegram-bot-control.sh watchdog-status
~/.codex/scripts/codex-telegram-bot-control.sh watchdog-check
~/.codex/scripts/codex-telegram-bot-control.sh network-watchdog-status
~/.codex/scripts/codex-telegram-bot-control.sh network-status
~/.codex/scripts/codex-telegram-bot-control.sh disable
```

`disable` stops and disables the bot service, bot watchdog timer, and network
watchdog timer.

Network watchdog configuration:

```sh
CODEX_NETWORK_RECONNECT_ENABLED=0         # check only, never reconnect
CODEX_NETWORK_RECONNECT_COOLDOWN_SECONDS=300
CODEX_NETWORK_CHECK_URLS="https://api.telegram.org http://connectivity-check.ubuntu.com/"
CODEX_NETWORK_RECONNECT_TYPES="vpn:tun:802-3-ethernet:802-11-wireless"
CODEX_NETWORK_RECONNECT_CONNECTIONS="netplan-enp3s0"
```

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
CODEX_CLI_VERSION=0.145.0
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

The installed host runner auto-syncs runtime code from the source repository
recorded in `~/.codex/codex-assistant.repo` before starting the bot. Disable it
only for troubleshooting:

```sh
CODEX_TELEGRAM_AUTO_SYNC=0 ~/.codex/scripts/codex-telegram-bot-control.sh restart
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

Optional orchestration/model routing settings:

```sh
CODEX_TELEGRAM_ORCHESTRATOR=1
CODEX_ORCHESTRATOR_MODEL_ROUTING=auto
CODEX_ORCHESTRATOR_DEFAULT_TIER=cheap
CODEX_ORCHESTRATOR_MAX_AUTO_TIER=strong
CODEX_ORCHESTRATOR_REQUIRE_CONFIRM_FOR_MAX=1
CODEX_ORCHESTRATOR_DEBATE=0
CODEX_ORCHESTRATOR_MAX_REVIEW_ROUNDS=2
CODEX_ORCHESTRATOR_FALLBACK_TO_CODEX=1
CODEX_TELEGRAM_TASK_PROVIDER=claude   # codex or claude (default: claude)
CODEX_CLAUDE_CMD='your-claude-wrapper'
CODEX_CLAUDE_EXEC_CMD='claude -p --output-format stream-json --verbose --permission-mode bypassPermissions'
CODEX_CLAUDE_READONLY_CMD='claude -p --output-format stream-json --verbose --permission-mode plan'
CODEX_CLAUDE_RESUME_ENABLED=1
CLAUDE_CHEAP_MODEL=
CLAUDE_STANDARD_MODEL=
CLAUDE_STRONG_MODEL=
CODEX_CHEAP_MODEL=
CODEX_STANDARD_MODEL=
CODEX_STRONG_MODEL=
CODEX_MAX_MODEL=
CODEX_ROUTER_LEARNING=1        # learning router for every executor run
CODEX_ROUTER_AUTO_ESCALATE=1   # retry one tier higher when verify fails
```

### Learning model router

With `CODEX_ROUTER_LEARNING=1` (default) every task, follow-up, and agent chat
goes through the router, also when orchestrator mode is off:

1. The regex `ModelRouter` classifies complexity and proposes a tier.
2. `RouterPolicy` picks the executor tier and effort
   (`cheap`/low, `standard`/medium, `strong`/high). It switches to a
   cheaper tier only after it is proven (≥5 runs, ≥80% smoothed success),
   skips tiers that keep failing, and explores one tier cheaper ~10% of the time.
   `large` never drops below `standard`, `critical` never below `strong`.
3. Verify by risk: `light` checks the exit code and a non-empty answer;
   `full` adds a read-only Claude review of the diff for high-risk work and
   for any run on a tier cheaper than the rule proposed.
4. On fail the run is retried once on the next tier, up to
   `CODEX_ORCHESTRATOR_MAX_AUTO_TIER` (`max` only when confirmation is off).
5. Every outcome, with tokens and cost parsed from the run log, is stored in
   `~/.codex/telegram-bot/router-stats.json`. On first start, task history is
   seeded as the `strong` baseline. `/router` shows the stats and savings.

Without `CLAUDE_*_MODEL` the Claude executor uses the CLI aliases `haiku`,
`sonnet`, and `opus` per tier. Full verify uses `CODEX_CLAUDE_CMD` when set,
otherwise `claude -p --permission-mode plan --model sonnet`.

### Executor providers

`CODEX_TELEGRAM_TASK_PROVIDER` selects the CLI that actually runs the work.
It defaults to `claude`. The Telegram Settings screen overrides it per chat
without editing the env file, and the chosen provider is used by **every**
runner path: planning, read-only agent chat, execution, interrupted-task
recovery, orchestrator revisions, follow-ups and force push.

Each provider is driven entirely by config:

| Setting | Applies to | Purpose |
| --- | --- | --- |
| `CODEX_TELEGRAM_CODEX_BIN` | codex | Codex binary |
| `CODEX_CLAUDE_EXEC_CMD` | claude | Writable runs (execution, revision, force push) |
| `CODEX_CLAUDE_READONLY_CMD` | claude | Read-only runs (planning, agent chat) |
| `CODEX_CLAUDE_RESUME_ENABLED` | claude | Allow `--resume` for follow-ups |
| `CODEX_CLAUDE_ENABLED` | claude | Disable the provider entirely |

Both Claude commands accept an optional `{model}` placeholder; without it the
selected model is appended as `--model <model>`.

Session continuity differs per provider. Codex prints its session id into the
run log and the bot parses it; Claude is given a generated `--session-id` up
front and follow-ups resume it with `--resume`. Session ids are stored per
provider (`codex_session_id` / `claude_session_id`), so switching providers
mid-task starts a fresh conversation instead of failing.

If the selected provider cannot run (binary missing, disabled, unparseable
command), the task fails with that reason. It never silently falls through to
the other provider. The orchestrator may still fall back to Codex when
`CODEX_ORCHESTRATOR_FALLBACK_TO_CODEX=1`, and only when Codex is itself usable.
Settings shows the live status of both providers.

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
CODEX_SLACK_DESKTOP_NOTIFICATIONS=1
CODEX_SLACK_TELEGRAM_CHAT_IDS=123456789
```

Desktop notification forwarding is enabled by default and does not use Slack
API. The bot listens to local `org.freedesktop.Notifications` events through
`dbus-monitor` and forwards Slack notifications that the desktop session already
shows in the tray. Only the sender/title and message text exposed by the local
notification are available.

Optional Slack API polling:

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

Optional Linear MCP integration for Codex tasks:

```sh
codex mcp add linear --url https://mcp.linear.app/mcp
codex mcp login linear
```

Check the local MCP configuration:

```sh
~/.codex/scripts/codex-linear.sh status
```

After OAuth is configured, start a new Codex session and use the Linear MCP
tools exposed by Codex. Useful Linear tool names include:

- `get_issue`
- `list_teams`
- `list_issue_statuses`
- `save_issue`

`codex-linear.sh` is kept only as a compatibility helper for MCP setup/status.
It no longer calls Linear GraphQL directly and does not use `CODEX_LINEAR_*`
OAuth settings. Do not paste OAuth callback URLs, authorization codes, or token
contents into chat or logs.

## Telegram Commands

```text
/menu
/projects [query]
/refresh
/project <query>
/context <project>
/new
/agent on|off
/orchestrator_on
/orchestrator_off
/orchestrator_status
/settings orchestrator on|off|status
/debug on|off
/ask <text>
/task <project> <text>
/task <text>
/run [tier=auto|cheap|standard|strong|max] <project> <text>
/run <text>
/answer <task_id> <text>
/confirm <task_id>
/continue <task_id> <text>
/cancel <task_id>
/status [task_id]
/processes
/logs <task_id>
/memory status|search|forget|summarize|export
/router
```

## Execution Flow

1. Send a task as plain text, or use `Run custom` / `/run` for manual prompts.
2. The bot resolves project context from aliases, recent context, and task text.
3. Consecutive text messages are joined after a short quiet window.
4. Planned tasks still use read-only Codex planning before `/confirm`.
5. Direct tasks use ModelRouter when orchestrator mode is on.
6. Cheap/simple tasks stay Codex-only; larger tasks may use Claude Architect and Reviewer.
7. The final Codex answer is sent to Telegram, and logs/trace stay on disk.
8. Completed tasks with a Codex session can be continued with `/continue`.

The main menu is task-first: `Tasks`, `Run custom`, `Settings`, and `Help`.
Advanced project, memory, trace, and model controls live under settings or task cards.
