#!/usr/bin/env bash

# Shared development environment for interactive shells, Codex tasks, and
# local user services. Keep this file source-safe: it must not exit the caller.

export GOPATH="${GOPATH:-${HOME}/go}"
export GOMODCACHE="${GOMODCACHE:-${GOPATH}/pkg/mod}"

case ":${PATH:-}:" in
  *":${GOPATH}/bin:"*)
    ;;
  *)
    export PATH="${GOPATH}/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
    ;;
esac

# User-local CLIs (claude, pipx tools). systemd user services started at boot
# do not get this from the login shell profile.
case ":${PATH}:" in
  *":${HOME}/.local/bin:"*)
    ;;
  *)
    export PATH="${HOME}/.local/bin:${PATH}"
    ;;
esac

codex_dev_env_socket_ok() {
  [ -n "${1:-}" ] && [ -S "$1" ]
}

codex_dev_env_shell_quote() {
  printf "%s" "$1" | sed "s/'/'\\\\''/g; 1s/^/'/; \$s/\$/'/"
}

if ! codex_dev_env_socket_ok "${SSH_AUTH_SOCK:-}"; then
  uid="$(id -u 2>/dev/null || printf '%s' 1000)"
  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${uid}}"
  for candidate in \
    "${runtime_dir}/keyring/ssh" \
    "${runtime_dir}/gcr/ssh" \
    "/run/user/${uid}/keyring/ssh" \
    "/run/user/${uid}/gcr/ssh"; do
    if codex_dev_env_socket_ok "${candidate}"; then
      export SSH_AUTH_SOCK="${candidate}"
      break
    fi
  done
  unset candidate runtime_dir uid
fi

if ! codex_dev_env_socket_ok "${SSH_AUTH_SOCK:-}" && command -v ssh-agent >/dev/null 2>&1; then
  uid="$(id -u 2>/dev/null || printf '%s' 1000)"
  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${uid}}"
  agent_sock="${runtime_dir}/codex-ssh-agent.sock"
  agent_env="${HOME}/.cache/codex-ssh-agent.env"

  if [ -r "${agent_env}" ]; then
    # shellcheck disable=SC1090
    . "${agent_env}" >/dev/null 2>&1 || true
  fi

  if ! codex_dev_env_socket_ok "${SSH_AUTH_SOCK:-}"; then
    mkdir -p "$(dirname "${agent_env}")"
    rm -f "${agent_sock}"
    eval "$(ssh-agent -s -a "${agent_sock}")" >/dev/null
    {
      printf 'export SSH_AUTH_SOCK='
      codex_dev_env_shell_quote "${SSH_AUTH_SOCK}"
      printf '\nexport SSH_AGENT_PID='
      codex_dev_env_shell_quote "${SSH_AGENT_PID:-}"
      printf '\n'
    } >"${agent_env}"
    chmod 600 "${agent_env}"
  fi
  unset agent_env agent_sock runtime_dir uid
fi

unset -f codex_dev_env_socket_ok codex_dev_env_shell_quote
