#!/usr/bin/env bash

log() {
  local level=$1
  shift
  printf '%s [%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$level" "$*" >&2
}

info() { log "INFO" "$@"; }
warn() { log "WARN" "$@"; }
error() { log "ERROR" "$@"; }

fail() {
  error "$@"
  exit 1
}

require_cmd() {
  local cmd
  for cmd in "$@"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      fail "Required command '${cmd}' is not installed or not on PATH"
    fi
  done
}
