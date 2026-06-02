#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
STATE_DIR="${CODEX_TELEGRAM_STATE_DIR:-${CODEX_HOME}/telegram-bot}"
PID_FILE="${STATE_DIR}/bot.pid"
LOG_DIR="${STATE_DIR}/logs"
LOG_FILE="${LOG_DIR}/service.log"
BOT_LOG_FILE="${LOG_DIR}/bot.log"
RUNNER="${CODEX_HOME}/scripts/codex-telegram-bot.sh"
SERVICE_NAME="codex-telegram-bot.service"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
SERVICE_FILE="${SYSTEMD_USER_DIR}/${SERVICE_NAME}"
REPO_FILE="${CODEX_ASSISTANT_REPO_FILE:-${CODEX_HOME}/codex-assistant.repo}"
DEFAULT_REPO="${HOME}/Projects/codex-assistant"
RUN_MODE="${CODEX_TELEGRAM_RUN_MODE:-auto}"

if [ -n "${CODEX_ASSISTANT_REPO:-}" ]; then
  REPO_DIR="${CODEX_ASSISTANT_REPO}"
elif [ -f "${REPO_FILE}" ]; then
  REPO_DIR="$(cat "${REPO_FILE}")"
else
  REPO_DIR="${DEFAULT_REPO}"
fi

COMPOSE_FILE="${CODEX_ASSISTANT_COMPOSE_FILE:-${REPO_DIR}/docker-compose.yml}"

mkdir -p "${LOG_DIR}"

is_running() {
  if [ ! -f "${PID_FILE}" ]; then
    return 1
  fi

  local pid
  pid="$(cat "${PID_FILE}")"
  [ -n "${pid}" ] && kill -0 "${pid}" >/dev/null 2>&1
}

docker_available() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

docker_mode() {
  case "${RUN_MODE}" in
    docker)
      return 0
      ;;
    process|legacy|systemd)
      return 1
      ;;
    auto)
      [ -f "${COMPOSE_FILE}" ] && docker_available
      ;;
    *)
      echo "Unknown CODEX_TELEGRAM_RUN_MODE=${RUN_MODE}" >&2
      return 1
      ;;
  esac
}

compose() {
  docker compose --project-directory "${REPO_DIR}" -f "${COMPOSE_FILE}" "$@"
}

docker_start() {
  if [ ! -f "${COMPOSE_FILE}" ]; then
    echo "Compose file not found: ${COMPOSE_FILE}" >&2
    return 1
  fi
  if ! docker_available; then
    echo "docker compose is not available" >&2
    return 1
  fi
  if is_running; then
    legacy_stop
  fi
  compose up -d --build
  compose ps
}

docker_stop() {
  if [ -f "${COMPOSE_FILE}" ] && docker_available; then
    compose down
  fi
}

docker_restart() {
  docker_stop
  docker_start
}

docker_status() {
  if [ ! -f "${COMPOSE_FILE}" ] || ! docker_available; then
    echo "codex telegram bot docker service is not available"
    return 1
  fi
  compose ps
}

docker_log() {
  local lines="${1:-80}"
  compose logs --tail="${lines}" codex-telegram-bot
}

systemd_available() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

service_known() {
  systemd_available && systemctl --user cat "${SERVICE_NAME}" >/dev/null 2>&1
}

write_service_file() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  mkdir -p "${SYSTEMD_USER_DIR}"
  cat >"${SERVICE_FILE}" <<SERVICE
[Unit]
Description=Codex Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h
ExecStart=%h/.codex/scripts/codex-telegram-bot.sh
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SERVICE
  systemctl --user daemon-reload
  echo "installed ${SERVICE_FILE}"
}

legacy_start() {
  if is_running; then
    echo "codex telegram bot is already running: $(cat "${PID_FILE}")"
    return 0
  fi

  nohup setsid "${RUNNER}" >>"${LOG_FILE}" 2>&1 &
  echo "$!" >"${PID_FILE}"
  sleep 1

  if is_running; then
    echo "codex telegram bot started: $(cat "${PID_FILE}")"
  else
    echo "codex telegram bot failed to start; see ${LOG_FILE}" >&2
    return 1
  fi
}

legacy_stop() {
  if ! is_running; then
    rm -f "${PID_FILE}"
    echo "codex telegram bot is not running"
    return 0
  fi

  local pid
  pid="$(cat "${PID_FILE}")"
  kill -- "-${pid}" >/dev/null 2>&1 || kill "${pid}" >/dev/null 2>&1 || true

  for _ in $(seq 1 20); do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      rm -f "${PID_FILE}"
      echo "codex telegram bot stopped"
      return 0
    fi
    sleep 0.5
  done

  kill -9 -- "-${pid}" >/dev/null 2>&1 || kill -9 "${pid}" >/dev/null 2>&1 || true
  rm -f "${PID_FILE}"
  echo "codex telegram bot killed"
}

legacy_status() {
  if is_running; then
    echo "codex telegram bot is running: $(cat "${PID_FILE}")"
  else
    echo "codex telegram bot is not running"
    return 1
  fi
}

start() {
  if docker_mode; then
    docker_start
    return
  fi
  if service_known; then
    if is_running; then
      legacy_stop
    fi
    systemctl --user start "${SERVICE_NAME}"
    systemctl --user --no-pager --full status "${SERVICE_NAME}"
    return
  fi

  legacy_start
}

stop() {
  if docker_mode; then
    docker_stop
    return
  fi
  if service_known; then
    systemctl --user stop "${SERVICE_NAME}"
  fi
  legacy_stop
}

restart() {
  if docker_mode; then
    docker_restart
    return
  fi
  stop
  start
}

status() {
  if docker_mode; then
    docker_status
    return
  fi
  if service_known; then
    systemctl --user --no-pager --full status "${SERVICE_NAME}"
    return
  fi

  legacy_status
}

log() {
  if docker_mode; then
    docker_log "${1:-80}"
    return
  fi
  if service_known; then
    journalctl --user -u "${SERVICE_NAME}" -n "${1:-80}" --no-pager
    if [ -f "${BOT_LOG_FILE}" ]; then
      echo
      echo "Bot file log: ${BOT_LOG_FILE}"
      tail -n "${1:-80}" "${BOT_LOG_FILE}"
    fi
    return
  fi

  tail -n "${1:-80}" "${LOG_FILE}"
}

install_service() {
  write_service_file
}

enable_service() {
  if docker_mode; then
    docker_start
    echo "Docker restart policy is active: unless-stopped"
    return
  fi
  write_service_file
  systemctl --user enable "${SERVICE_NAME}"
  echo "enabled ${SERVICE_NAME} for user autostart"
}

disable_service() {
  if docker_mode; then
    docker_stop
    echo "stopped docker service"
    return
  fi
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  systemctl --user disable --now "${SERVICE_NAME}" 2>/dev/null || true
  systemctl --user daemon-reload
  echo "disabled ${SERVICE_NAME}"
}

case "${1:-status}" in
  start)
    start
    ;;
  stop)
    stop
    ;;
  restart)
    restart
    ;;
  status)
    status
    ;;
  log|logs)
    log "${2:-80}"
    ;;
  install)
    install_service
    ;;
  enable|autostart)
    enable_service
    ;;
  disable)
    disable_service
    ;;
  systemd-status)
    systemctl --user --no-pager --full status "${SERVICE_NAME}"
    ;;
  docker-status)
    docker_status
    ;;
  docker-logs)
    docker_log "${2:-80}"
    ;;
  *)
    echo "Usage: $0 start|stop|restart|status|log [lines]|install|enable|autostart|disable|systemd-status|docker-status|docker-logs [lines]" >&2
    exit 2
    ;;
esac
