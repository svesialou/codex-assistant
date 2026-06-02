#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
REPO_FILE="${CODEX_ASSISTANT_REPO_FILE:-${CODEX_HOME}/codex-assistant.repo}"
DEFAULT_REPO="${HOME}/Projects/codex-assistant"

if [ -n "${CODEX_ASSISTANT_REPO:-}" ]; then
  REPO_DIR="${CODEX_ASSISTANT_REPO}"
elif [ -f "${REPO_FILE}" ]; then
  REPO_DIR="$(cat "${REPO_FILE}")"
else
  REPO_DIR="${DEFAULT_REPO}"
fi

TOOL_DIR="${CODEX_TELEGRAM_BOT_TOOL_DIR:-${CODEX_HOME}/tools/codex_telegram_bot}"
ENV_FILE="${CODEX_TELEGRAM_ENV_FILE:-${CODEX_HOME}/secrets/telegram.env}"

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

ROOTS="${CODEX_TELEGRAM_PROJECT_DIRS:-${HOME}/Projects:${HOME}/MyProjects}"
OUTPUT="${CODEX_TELEGRAM_INDEX_DIR:-${CODEX_HOME}/project-index}"
WORKSPACE_ROOT="${CODEX_TELEGRAM_WORKSPACE_ROOT:-${HOME}}"

if [ -d "${TOOL_DIR}/codex_telegram_bot" ]; then
  PYTHONPATH_ROOT="${TOOL_DIR}"
elif [ -d "${REPO_DIR}/codex_telegram_bot" ]; then
  PYTHONPATH_ROOT="${REPO_DIR}"
else
  echo "codex telegram bot sources not found" >&2
  echo "Run scripts/install.sh from the codex-assistant repository." >&2
  exit 1
fi

PYTHONPATH="${PYTHONPATH_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m codex_telegram_bot.project_index \
    --roots "${ROOTS}" \
    --output "${OUTPUT}" \
    --workspace-root "${WORKSPACE_ROOT}"
