#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
REPO_FILE="${CODEX_ASSISTANT_REPO_FILE:-${CODEX_HOME}/codex-assistant.repo}"
DEFAULT_REPO="${HOME}/Projects/codex-assistant"
TOOL_DIR="${CODEX_TELEGRAM_BOT_TOOL_DIR:-${CODEX_HOME}/tools/codex_telegram_bot}"
QUIET=0

if [ "${1:-}" = "--quiet" ]; then
  QUIET=1
fi

if [ -n "${CODEX_ASSISTANT_REPO:-}" ]; then
  REPO_DIR="${CODEX_ASSISTANT_REPO}"
elif [ -f "${REPO_FILE}" ]; then
  REPO_DIR="$(cat "${REPO_FILE}")"
else
  REPO_DIR="${DEFAULT_REPO}"
fi

if [ ! -d "${REPO_DIR}/codex_telegram_bot" ]; then
  echo "source repo is missing codex_telegram_bot: ${REPO_DIR}" >&2
  exit 1
fi

mkdir -p "${TOOL_DIR}"
STAGING_DIR="$(mktemp -d "${TOOL_DIR}.sync.XXXXXX")"
cleanup() {
  rm -rf "${STAGING_DIR}"
}
trap cleanup EXIT

cp -a "${REPO_DIR}/codex_telegram_bot" "${STAGING_DIR}/"
cp -a "${REPO_DIR}/tests" "${STAGING_DIR}/"
cp "${REPO_DIR}/README.md" "${STAGING_DIR}/README.md"
cp "${REPO_DIR}/pyproject.toml" "${STAGING_DIR}/pyproject.toml"
find "${STAGING_DIR}" -type d -name __pycache__ -prune -exec rm -rf {} +

rm -rf "${TOOL_DIR}/codex_telegram_bot" "${TOOL_DIR}/tests"
cp -a "${STAGING_DIR}/codex_telegram_bot" "${TOOL_DIR}/"
cp -a "${STAGING_DIR}/tests" "${TOOL_DIR}/"
cp "${STAGING_DIR}/README.md" "${TOOL_DIR}/README.md"
cp "${STAGING_DIR}/pyproject.toml" "${TOOL_DIR}/pyproject.toml"

if [ "${QUIET}" != "1" ]; then
  echo "synced codex telegram bot runtime"
  echo "source: ${REPO_DIR}"
  echo "installed: ${TOOL_DIR}"
fi
