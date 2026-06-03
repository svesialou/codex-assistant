#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
DEV_ENV="${CODEX_DEV_ENV_FILE:-${CODEX_HOME}/scripts/dev-env.sh}"
REPO_FILE="${CODEX_ASSISTANT_REPO_FILE:-${CODEX_HOME}/codex-assistant.repo}"
DEFAULT_REPO="${HOME}/Projects/codex-assistant"

if [ -r "${DEV_ENV}" ]; then
  # shellcheck disable=SC1090
  source "${DEV_ENV}"
fi

if [ -n "${CODEX_ASSISTANT_REPO:-}" ]; then
  REPO_DIR="${CODEX_ASSISTANT_REPO}"
elif [ -f "${REPO_FILE}" ]; then
  REPO_DIR="$(cat "${REPO_FILE}")"
else
  REPO_DIR="${DEFAULT_REPO}"
fi

TOOL_DIR="${CODEX_TELEGRAM_BOT_TOOL_DIR:-${CODEX_HOME}/tools/codex_telegram_bot}"

if [ -d "${TOOL_DIR}/codex_telegram_bot" ]; then
  PYTHONPATH_ROOT="${TOOL_DIR}"
elif [ -d "${REPO_DIR}/codex_telegram_bot" ]; then
  PYTHONPATH_ROOT="${REPO_DIR}"
else
  echo "codex telegram bot sources not found" >&2
  echo "Run scripts/install.sh from the codex-assistant repository." >&2
  exit 1
fi

export PYTHONPATH="${PYTHONPATH_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m codex_telegram_bot "$@"
