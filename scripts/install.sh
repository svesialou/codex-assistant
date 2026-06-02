#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
TOOL_DIR="${CODEX_HOME}/tools/codex_telegram_bot"
SCRIPTS_DIR="${CODEX_HOME}/scripts"
SECRETS_DIR="${CODEX_HOME}/secrets"
ENV_FILE="${SECRETS_DIR}/telegram.env"
COMPOSE_ENV_FILE="${REPO_DIR}/.env"

mkdir -p "${TOOL_DIR}" "${SCRIPTS_DIR}" "${SECRETS_DIR}" \
  "${CODEX_HOME}/telegram-bot" "${CODEX_HOME}/project-index" \
  "${HOME}/Projects" "${HOME}/MyProjects"

rm -rf "${TOOL_DIR}/codex_telegram_bot" "${TOOL_DIR}/tests"
cp -a "${REPO_DIR}/codex_telegram_bot" "${TOOL_DIR}/"
cp -a "${REPO_DIR}/tests" "${TOOL_DIR}/"
cp "${REPO_DIR}/README.md" "${TOOL_DIR}/README.md"
find "${TOOL_DIR}" -type d -name __pycache__ -prune -exec rm -rf {} +

cp "${REPO_DIR}/scripts/codex-telegram-bot.sh" "${SCRIPTS_DIR}/codex-telegram-bot.sh"
cp "${REPO_DIR}/scripts/codex-project-index.sh" "${SCRIPTS_DIR}/codex-project-index.sh"
cp "${REPO_DIR}/scripts/codex-telegram-bot-control.sh" "${SCRIPTS_DIR}/codex-telegram-bot-control.sh"
chmod +x \
  "${SCRIPTS_DIR}/codex-telegram-bot.sh" \
  "${SCRIPTS_DIR}/codex-project-index.sh" \
  "${SCRIPTS_DIR}/codex-telegram-bot-control.sh"

printf '%s\n' "${REPO_DIR}" >"${CODEX_HOME}/codex-assistant.repo"

if [ ! -f "${ENV_FILE}" ]; then
  cat >"${ENV_FILE}" <<'ENV'
# Fill these values before starting the bot.
CODEX_TELEGRAM_BOT_TOKEN=
CODEX_TELEGRAM_CHAT_ID=

# Optional hardening:
# CODEX_TELEGRAM_ALLOWED_CHAT_IDS=
# CODEX_TELEGRAM_ALLOWED_USER_IDS=
ENV
  chmod 600 "${ENV_FILE}"
  echo "created ${ENV_FILE}; fill Telegram token and chat id before starting"
fi

if [ ! -f "${COMPOSE_ENV_FILE}" ]; then
  cat >"${COMPOSE_ENV_FILE}" <<ENV
CODEX_HOST_HOME=${HOME}
HOST_UID=$(id -u)
HOST_GID=$(id -g)
CODEX_CLI_VERSION=0.135.0
ENV
  echo "created ${COMPOSE_ENV_FILE}"
fi

echo "installed codex assistant into ${CODEX_HOME}"
echo "run: ${SCRIPTS_DIR}/codex-telegram-bot-control.sh restart"
