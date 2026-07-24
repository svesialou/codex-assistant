#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
ENV_FILE="${CODEX_TELEGRAM_ENV_FILE:-${CODEX_HOME}/secrets/telegram.env}"

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

STATE_DIR="${CODEX_TELEGRAM_STATE_DIR:-${CODEX_HOME}/telegram-bot}"
PID_FILE="${STATE_DIR}/bot.pid"
LOG_DIR="${STATE_DIR}/logs"
LOG_FILE="${LOG_DIR}/service.log"
BOT_LOG_FILE="${LOG_DIR}/bot.log"
RUNNER="${CODEX_HOME}/scripts/codex-telegram-bot.sh"
SUPERVISOR="${CODEX_HOME}/scripts/codex-telegram-bot-supervisor.sh"
SERVICE_NAME="codex-telegram-bot.service"
SERVICE_BASE="${SERVICE_NAME%.service}"
WATCHDOG_SERVICE_NAME="${SERVICE_BASE}-watchdog.service"
WATCHDOG_TIMER_NAME="${SERVICE_BASE}-watchdog.timer"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
SERVICE_FILE="${SYSTEMD_USER_DIR}/${SERVICE_NAME}"
WATCHDOG_SERVICE_FILE="${SYSTEMD_USER_DIR}/${WATCHDOG_SERVICE_NAME}"
WATCHDOG_TIMER_FILE="${SYSTEMD_USER_DIR}/${WATCHDOG_TIMER_NAME}"
NETWORK_WATCHDOG="${CODEX_NETWORK_WATCHDOG:-${CODEX_HOME}/scripts/codex-network-watchdog.sh}"
NETWORK_WATCHDOG_SERVICE_NAME="codex-network-watchdog.service"
NETWORK_WATCHDOG_TIMER_NAME="codex-network-watchdog.timer"
NETWORK_WATCHDOG_SERVICE_FILE="${SYSTEMD_USER_DIR}/${NETWORK_WATCHDOG_SERVICE_NAME}"
NETWORK_WATCHDOG_TIMER_FILE="${SYSTEMD_USER_DIR}/${NETWORK_WATCHDOG_TIMER_NAME}"
REPO_FILE="${CODEX_ASSISTANT_REPO_FILE:-${CODEX_HOME}/codex-assistant.repo}"
DEFAULT_REPO="${HOME}/Projects/codex-assistant"
RUN_MODE="${CODEX_TELEGRAM_RUN_MODE:-auto}"
DEFERRED_RESTART_SECONDS="${CODEX_TELEGRAM_DEFERRED_RESTART_SECONDS:-15}"

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
    docker|compose)
      return 0
      ;;
    auto|host|systemd|supervisor|process|legacy)
      return 1
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
  if service_known; then
    systemctl --user stop "${SERVICE_NAME}"
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

stop_docker_if_present() {
  if [ -f "${COMPOSE_FILE}" ] && docker_available; then
    compose down
  fi
}

systemd_available() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

service_known() {
  systemd_available && systemctl --user cat "${SERVICE_NAME}" >/dev/null 2>&1
}

watchdog_known() {
  systemd_available && systemctl --user cat "${WATCHDOG_TIMER_NAME}" >/dev/null 2>&1
}

network_watchdog_known() {
  systemd_available && systemctl --user cat "${NETWORK_WATCHDOG_TIMER_NAME}" >/dev/null 2>&1
}

inside_service_cgroup() {
  [ -r "/proc/$$/cgroup" ] && grep -Fq "/${SERVICE_NAME}" "/proc/$$/cgroup"
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
Environment=CODEX_TELEGRAM_RUN_MODE=systemd

[Install]
WantedBy=default.target
SERVICE
  systemctl --user daemon-reload
  echo "installed ${SERVICE_FILE}"
}

write_watchdog_files() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  write_service_file >/dev/null
  cat >"${WATCHDOG_SERVICE_FILE}" <<SERVICE
[Unit]
Description=Codex Telegram Bot watchdog check

[Service]
Type=oneshot
ExecStart=%h/.codex/scripts/codex-telegram-bot-control.sh watchdog-check
Environment=PYTHONUNBUFFERED=1
Environment=CODEX_TELEGRAM_RUN_MODE=systemd
SERVICE

  cat >"${WATCHDOG_TIMER_FILE}" <<TIMER
[Unit]
Description=Check Codex Telegram Bot every minute

[Timer]
OnStartupSec=30s
OnUnitActiveSec=1min
AccuracySec=10s
Unit=${WATCHDOG_SERVICE_NAME}

[Install]
WantedBy=timers.target
TIMER

  systemctl --user daemon-reload
  echo "installed ${WATCHDOG_SERVICE_FILE}"
  echo "installed ${WATCHDOG_TIMER_FILE}"
}

write_network_watchdog_files() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  if [ ! -x "${NETWORK_WATCHDOG}" ]; then
    echo "network watchdog script is not executable: ${NETWORK_WATCHDOG}" >&2
    return 1
  fi

  mkdir -p "${SYSTEMD_USER_DIR}"
  cat >"${NETWORK_WATCHDOG_SERVICE_FILE}" <<SERVICE
[Unit]
Description=Codex internet connectivity watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=%h/.codex/scripts/codex-network-watchdog.sh watchdog
Environment=PYTHONUNBUFFERED=1
SERVICE

  cat >"${NETWORK_WATCHDOG_TIMER_FILE}" <<TIMER
[Unit]
Description=Check internet connectivity every minute

[Timer]
OnStartupSec=20s
OnUnitActiveSec=1min
AccuracySec=10s
Unit=${NETWORK_WATCHDOG_SERVICE_NAME}

[Install]
WantedBy=timers.target
TIMER

  systemctl --user daemon-reload
  echo "installed ${NETWORK_WATCHDOG_SERVICE_FILE}"
  echo "installed ${NETWORK_WATCHDOG_TIMER_FILE}"
}

run_network_watchdog() {
  if [ ! -x "${NETWORK_WATCHDOG}" ]; then
    return 0
  fi

  if systemd_available && systemctl --user is-active --quiet "${NETWORK_WATCHDOG_TIMER_NAME}"; then
    return 0
  fi

  "${NETWORK_WATCHDOG}" watchdog || true
}

schedule_deferred_systemd_restart() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  local unit_name
  unit_name="${SERVICE_BASE}-deferred-restart-$(date +%s)-$$.service"

  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --user \
      --unit="${unit_name}" \
      --description="Deferred Codex Telegram Bot restart" \
      --on-active="${DEFERRED_RESTART_SECONDS}s" \
      /bin/bash -lc "systemctl --user restart '${SERVICE_NAME}'"
  else
    nohup /bin/bash -lc \
      "sleep '${DEFERRED_RESTART_SECONDS}'; systemctl --user restart '${SERVICE_NAME}' --no-block" \
      >>"${LOG_FILE}" 2>&1 &
  fi

  echo "scheduled deferred restart for ${SERVICE_NAME} in ${DEFERRED_RESTART_SECONDS}s"
}

systemd_start() {
  write_service_file
  if is_running; then
    legacy_stop
  fi
  stop_docker_if_present
  systemctl --user start "${SERVICE_NAME}"
  systemctl --user --no-pager --full status "${SERVICE_NAME}"
}

supervisor_start() {
  if is_running; then
    echo "codex telegram bot daemon is already running: $(cat "${PID_FILE}")"
    return 0
  fi

  stop_docker_if_present
  nohup setsid "${SUPERVISOR}" >>"${LOG_FILE}" 2>&1 &
  echo "$!" >"${PID_FILE}"
  sleep 1

  if is_running; then
    echo "codex telegram bot daemon started: $(cat "${PID_FILE}")"
  else
    echo "codex telegram bot daemon failed to start; see ${LOG_FILE}" >&2
    return 1
  fi
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
  case "${RUN_MODE}" in
    process|legacy)
      stop_docker_if_present
      legacy_start
      ;;
    systemd)
      systemd_start
      ;;
    supervisor)
      supervisor_start
      ;;
    auto|host)
      if systemd_available; then
        systemd_start
      else
        supervisor_start
      fi
      ;;
  esac
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
  stop_docker_if_present
}

restart() {
  if docker_mode; then
    docker_restart
    return
  fi
  if [ "${CODEX_TELEGRAM_DEFER_SELF_RESTART:-1}" != "0" ] && service_known && inside_service_cgroup; then
    schedule_deferred_systemd_restart
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

install_watchdog() {
  write_watchdog_files
}

enable_watchdog() {
  write_watchdog_files
  systemctl --user enable --now "${WATCHDOG_TIMER_NAME}"
  echo "enabled ${WATCHDOG_TIMER_NAME}"
}

enable_network_watchdog() {
  write_network_watchdog_files
  systemctl --user enable --now "${NETWORK_WATCHDOG_TIMER_NAME}"
  echo "enabled ${NETWORK_WATCHDOG_TIMER_NAME}"
}

disable_watchdog() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  systemctl --user disable --now "${WATCHDOG_TIMER_NAME}" 2>/dev/null || true
  systemctl --user stop "${WATCHDOG_SERVICE_NAME}" 2>/dev/null || true
  systemctl --user daemon-reload
  echo "disabled ${WATCHDOG_TIMER_NAME}"
}

disable_network_watchdog() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  systemctl --user disable --now "${NETWORK_WATCHDOG_TIMER_NAME}" 2>/dev/null || true
  systemctl --user stop "${NETWORK_WATCHDOG_SERVICE_NAME}" 2>/dev/null || true
  systemctl --user daemon-reload
  echo "disabled ${NETWORK_WATCHDOG_TIMER_NAME}"
}

network_watchdog_status() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  if network_watchdog_known; then
    systemctl --user --no-pager --full status "${NETWORK_WATCHDOG_TIMER_NAME}"
    systemctl --user --no-pager --full status "${NETWORK_WATCHDOG_SERVICE_NAME}" || true
    systemctl --user list-timers --all "${NETWORK_WATCHDOG_TIMER_NAME}" --no-pager
  else
    echo "codex network watchdog is not installed"
    return 1
  fi
}

network_watchdog_check() {
  if [ ! -x "${NETWORK_WATCHDOG}" ]; then
    echo "network watchdog script is not executable: ${NETWORK_WATCHDOG}" >&2
    return 1
  fi

  "${NETWORK_WATCHDOG}" watchdog
}

network_status() {
  if [ ! -x "${NETWORK_WATCHDOG}" ]; then
    echo "network watchdog script is not executable: ${NETWORK_WATCHDOG}" >&2
    return 1
  fi

  "${NETWORK_WATCHDOG}" status
}

network_reconnect() {
  if [ ! -x "${NETWORK_WATCHDOG}" ]; then
    echo "network watchdog script is not executable: ${NETWORK_WATCHDOG}" >&2
    return 1
  fi

  "${NETWORK_WATCHDOG}" reconnect-now
}

watchdog_check() {
  run_network_watchdog

  if docker_mode; then
    if docker_status >/dev/null 2>&1; then
      echo "codex telegram bot docker service is running"
    else
      echo "$(date -Is) codex telegram bot docker service is not running; starting"
      docker_start
    fi
    return
  fi

  if ! systemd_available; then
    if is_running; then
      echo "codex telegram bot daemon is running: $(cat "${PID_FILE}")"
    else
      echo "$(date -Is) codex telegram bot daemon is not running; starting"
      supervisor_start
    fi
    return
  fi

  if ! service_known; then
    write_service_file
  fi

  if systemctl --user is-active --quiet "${SERVICE_NAME}"; then
    echo "codex telegram bot service is running"
    return
  fi

  echo "$(date -Is) ${SERVICE_NAME} is not active; starting"
  systemctl --user reset-failed "${SERVICE_NAME}" >/dev/null 2>&1 || true
  stop_docker_if_present
  systemctl --user start "${SERVICE_NAME}"
}

watchdog_status() {
  if ! systemd_available; then
    echo "systemd --user is not available in this session" >&2
    return 1
  fi

  if watchdog_known; then
    systemctl --user --no-pager --full status "${WATCHDOG_TIMER_NAME}"
    systemctl --user --no-pager --full status "${WATCHDOG_SERVICE_NAME}" || true
    systemctl --user list-timers --all "${WATCHDOG_TIMER_NAME}" --no-pager
  else
    echo "codex telegram bot watchdog is not installed"
    return 1
  fi
}

enable_service() {
  if docker_mode; then
    docker_start
    echo "Docker restart policy is active: unless-stopped"
    return
  fi
  write_service_file
  systemctl --user enable --now "${SERVICE_NAME}"
  enable_network_watchdog
  enable_watchdog
  echo "enabled and started ${SERVICE_NAME} with bot and network watchdog timers"
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

  systemctl --user disable --now "${WATCHDOG_TIMER_NAME}" 2>/dev/null || true
  systemctl --user stop "${WATCHDOG_SERVICE_NAME}" 2>/dev/null || true
  systemctl --user disable --now "${NETWORK_WATCHDOG_TIMER_NAME}" 2>/dev/null || true
  systemctl --user stop "${NETWORK_WATCHDOG_SERVICE_NAME}" 2>/dev/null || true
  systemctl --user disable --now "${SERVICE_NAME}" 2>/dev/null || true
  systemctl --user daemon-reload
  echo "disabled ${SERVICE_NAME}, ${WATCHDOG_TIMER_NAME}, and ${NETWORK_WATCHDOG_TIMER_NAME}"
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
  watchdog-install)
    install_watchdog
    ;;
  watchdog-enable)
    enable_watchdog
    ;;
  watchdog-disable)
    disable_watchdog
    ;;
  network-watchdog-install)
    write_network_watchdog_files
    ;;
  network-watchdog-enable)
    enable_network_watchdog
    ;;
  network-watchdog-disable)
    disable_network_watchdog
    ;;
  network-watchdog-check)
    network_watchdog_check
    ;;
  network-watchdog-status)
    network_watchdog_status
    ;;
  network-status)
    network_status
    ;;
  network-reconnect)
    network_reconnect
    ;;
  watchdog-check)
    watchdog_check
    ;;
  watchdog-status)
    watchdog_status
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
    echo "Usage: $0 start|stop|restart|status|log [lines]|install|enable|autostart|disable|watchdog-install|watchdog-enable|watchdog-disable|watchdog-check|watchdog-status|network-watchdog-install|network-watchdog-enable|network-watchdog-disable|network-watchdog-check|network-watchdog-status|network-status|network-reconnect|systemd-status|docker-status|docker-logs [lines]" >&2
    exit 2
    ;;
esac
