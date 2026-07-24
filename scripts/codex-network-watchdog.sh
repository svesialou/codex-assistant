#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
STATE_DIR="${CODEX_NETWORK_WATCHDOG_STATE_DIR:-${CODEX_HOME}/state/network-watchdog}"
LOG_FILE="${CODEX_NETWORK_WATCHDOG_LOG_FILE:-${CODEX_HOME}/log/network-watchdog.log}"
LOCK_DIR="${STATE_DIR}/lock"
LAST_RECONNECT_FILE="${STATE_DIR}/last-reconnect"

CHECK_URLS="${CODEX_NETWORK_CHECK_URLS:-https://api.telegram.org http://connectivity-check.ubuntu.com/}"
DNS_HOSTS="${CODEX_NETWORK_DNS_HOSTS:-api.telegram.org api.openai.com}"
CONNECT_TIMEOUT="${CODEX_NETWORK_CHECK_CONNECT_TIMEOUT:-3}"
MAX_TIME="${CODEX_NETWORK_CHECK_MAX_TIME:-7}"
RECONNECT_ENABLED="${CODEX_NETWORK_RECONNECT_ENABLED:-1}"
RECONNECT_COOLDOWN_SECONDS="${CODEX_NETWORK_RECONNECT_COOLDOWN_SECONDS:-300}"
POST_RECONNECT_WAIT_SECONDS="${CODEX_NETWORK_POST_RECONNECT_WAIT_SECONDS:-12}"
RECONNECT_TYPES="${CODEX_NETWORK_RECONNECT_TYPES:-vpn:tun:802-3-ethernet:802-11-wireless}"
RECONNECT_CONNECTIONS="${CODEX_NETWORK_RECONNECT_CONNECTIONS:-}"
RECONNECT_DEVICES="${CODEX_NETWORK_RECONNECT_DEVICES:-}"

mkdir -p "${STATE_DIR}" "$(dirname "${LOG_FILE}")"

log() {
  local line
  line="$(date -Is) $*"
  echo "${line}"
  printf '%s\n' "${line}" >>"${LOG_FILE}"
}

usage() {
  cat <<'USAGE'
Usage:
  codex-network-watchdog.sh watchdog
  codex-network-watchdog.sh status
  codex-network-watchdog.sh reconnect-now

Environment:
  CODEX_NETWORK_CHECK_URLS                 Space-separated HTTP/HTTPS URLs to probe.
  CODEX_NETWORK_DNS_HOSTS                  Space-separated DNS hostnames for fallback checks.
  CODEX_NETWORK_RECONNECT_ENABLED          Set to 0 to check only.
  CODEX_NETWORK_RECONNECT_COOLDOWN_SECONDS Default: 300.
  CODEX_NETWORK_RECONNECT_TYPES            Colon-separated NetworkManager types to reconnect.
  CODEX_NETWORK_RECONNECT_CONNECTIONS      Optional space-separated connection names to prefer.
  CODEX_NETWORK_RECONNECT_DEVICES          Optional space-separated device names to prefer.
USAGE
}

acquire_lock() {
  if mkdir "${LOCK_DIR}" 2>/dev/null; then
    trap 'rmdir "${LOCK_DIR}" >/dev/null 2>&1 || true' EXIT
    return 0
  fi

  log "network watchdog is already running; skipping"
  exit 0
}

http_probe_ok() {
  local url="$1"
  local code

  if ! command -v curl >/dev/null 2>&1; then
    return 1
  fi

  code="$(
    curl -4 -L -sS -o /dev/null \
      --connect-timeout "${CONNECT_TIMEOUT}" \
      --max-time "${MAX_TIME}" \
      -w '%{http_code}' \
      "${url}" 2>/dev/null || true
  )"

  case "${code}" in
    2*|3*|4*)
      log "http probe ok: ${url} code=${code}"
      return 0
      ;;
  esac

  log "http probe failed: ${url} code=${code:-000}"
  return 1
}

dns_probe_ok() {
  local host="$1"

  if getent ahostsv4 "${host}" >/dev/null 2>&1; then
    log "dns probe ok: ${host}"
    return 0
  fi

  log "dns probe failed: ${host}"
  return 1
}

connectivity_ok() {
  local url host

  for url in ${CHECK_URLS}; do
    if http_probe_ok "${url}"; then
      return 0
    fi
  done

  if command -v curl >/dev/null 2>&1; then
    return 1
  fi

  for host in ${DNS_HOSTS}; do
    if dns_probe_ok "${host}"; then
      return 0
    fi
  done

  return 1
}

cooldown_active() {
  local now last elapsed

  [ -f "${LAST_RECONNECT_FILE}" ] || return 1

  now="$(date +%s)"
  last="$(cat "${LAST_RECONNECT_FILE}" 2>/dev/null || echo 0)"
  case "${last}" in
    ''|*[!0-9]*)
      return 1
      ;;
  esac

  elapsed=$((now - last))
  if [ "${elapsed}" -lt "${RECONNECT_COOLDOWN_SECONDS}" ]; then
    log "reconnect cooldown active: ${elapsed}s elapsed, ${RECONNECT_COOLDOWN_SECONDS}s required"
    return 0
  fi

  return 1
}

mark_reconnect_attempt() {
  date +%s >"${LAST_RECONNECT_FILE}"
}

type_allowed() {
  local type="$1"
  case ":${RECONNECT_TYPES}:" in
    *":${type}:"*)
      return 0
      ;;
  esac
  return 1
}

add_candidate() {
  local name="$1"
  local existing

  [ -n "${name}" ] || return 0

  for existing in "${RECONNECT_CANDIDATES[@]:-}"; do
    if [ "${existing}" = "${name}" ]; then
      return 0
    fi
  done

  RECONNECT_CANDIDATES+=("${name}")
}

default_route_devices() {
  ip route show default 2>/dev/null | awk '
    {
      for (i = 1; i <= NF; i++) {
        if ($i == "dev" && (i + 1) <= NF) {
          print $(i + 1)
        }
      }
    }
  ' | sort -u
}

collect_reconnect_candidates() {
  local line name type device target dev
  RECONNECT_CANDIDATES=()
  ACTIVE_CONNECTIONS=()

  if ! command -v nmcli >/dev/null 2>&1; then
    return 1
  fi

  while IFS= read -r line; do
    [ -n "${line}" ] && ACTIVE_CONNECTIONS+=("${line}")
  done < <(nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null || true)

  for target in ${RECONNECT_CONNECTIONS}; do
    add_candidate "${target}"
  done

  for dev in ${RECONNECT_DEVICES}; do
    for line in "${ACTIVE_CONNECTIONS[@]:-}"; do
      IFS=: read -r name type device _ <<<"${line}"
      if [ "${device}" = "${dev}" ] && type_allowed "${type}"; then
        add_candidate "${name}"
      fi
    done
  done

  while IFS= read -r dev; do
    for line in "${ACTIVE_CONNECTIONS[@]:-}"; do
      IFS=: read -r name type device _ <<<"${line}"
      if [ "${device}" = "${dev}" ] && type_allowed "${type}"; then
        add_candidate "${name}"
      fi
    done
  done < <(default_route_devices)

  for line in "${ACTIVE_CONNECTIONS[@]:-}"; do
    IFS=: read -r name type device _ <<<"${line}"
    if type_allowed "${type}"; then
      add_candidate "${name}"
    fi
  done
}

reconnect_connection() {
  local name="$1"

  log "reconnecting NetworkManager connection: ${name}"
  nmcli connection down "${name}" >/dev/null 2>&1 || true
  sleep 2

  if nmcli connection up "${name}" >/dev/null 2>&1; then
    log "connection is up: ${name}"
    return 0
  fi

  log "failed to bring connection up: ${name}"
  return 1
}

run_reconnect() {
  local name had_candidate=0

  if [ "${RECONNECT_ENABLED}" = "0" ]; then
    log "connectivity is down; reconnect disabled"
    return 1
  fi

  if ! command -v nmcli >/dev/null 2>&1; then
    log "connectivity is down; nmcli is not available"
    return 1
  fi

  collect_reconnect_candidates || {
    log "connectivity is down; failed to inspect active NetworkManager connections"
    return 1
  }

  if [ "${#RECONNECT_CANDIDATES[@]}" -eq 0 ]; then
    log "connectivity is down; no active NetworkManager connections matched reconnect policy"
    return 1
  fi

  mark_reconnect_attempt

  for name in "${RECONNECT_CANDIDATES[@]}"; do
    had_candidate=1
    reconnect_connection "${name}" || true
  done

  if [ "${had_candidate}" = "1" ]; then
    log "waiting ${POST_RECONNECT_WAIT_SECONDS}s after reconnect"
    sleep "${POST_RECONNECT_WAIT_SECONDS}"
  fi

  return 0
}

watchdog() {
  acquire_lock

  if connectivity_ok; then
    log "internet connectivity is ok"
    return 0
  fi

  log "internet connectivity check failed"

  if cooldown_active; then
    return 0
  fi

  if run_reconnect; then
    if connectivity_ok; then
      log "internet connectivity recovered"
    else
      log "internet connectivity is still down after reconnect attempt"
    fi
  else
    log "internet connectivity remains down; reconnect was not attempted"
  fi
}

status() {
  if connectivity_ok; then
    log "internet connectivity is ok"
    return 0
  fi

  log "internet connectivity is down"
  return 1
}

reconnect_now() {
  acquire_lock
  run_reconnect || true
  status
}

case "${1:-watchdog}" in
  watchdog|check)
    watchdog
    ;;
  status)
    status
    ;;
  reconnect-now)
    reconnect_now
    ;;
  --help|-h|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
