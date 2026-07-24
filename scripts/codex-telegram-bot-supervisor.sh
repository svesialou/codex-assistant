#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
ENV_FILE="${CODEX_TELEGRAM_ENV_FILE:-${CODEX_HOME}/secrets/telegram.env}"

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

STATE_DIR="${CODEX_TELEGRAM_STATE_DIR:-${CODEX_HOME}/telegram-bot}"
LOG_DIR="${STATE_DIR}/logs"
RUNNER="${CODEX_HOME}/scripts/codex-telegram-bot.sh"
RESTART_SECONDS="${CODEX_TELEGRAM_RESTART_SECONDS:-5}"

mkdir -p "${LOG_DIR}"

child_pid=""

stop() {
  if [ -n "${child_pid}" ]; then
    kill "${child_pid}" >/dev/null 2>&1 || true
    wait "${child_pid}" >/dev/null 2>&1 || true
  fi
  echo "$(date -Is) codex telegram bot supervisor stopped"
  exit 0
}

trap stop INT TERM

echo "$(date -Is) codex telegram bot supervisor started"

while true; do
  "${RUNNER}" &
  child_pid="$!"

  set +e
  wait "${child_pid}"
  exit_code="$?"
  set -e
  child_pid=""

  if [ "${exit_code}" -eq 0 ]; then
    echo "$(date -Is) codex telegram bot exited normally; supervisor stopping"
    exit 0
  fi

  echo "$(date -Is) codex telegram bot exited with code ${exit_code}; restarting in ${RESTART_SECONDS}s"
  sleep "${RESTART_SECONDS}"
done
