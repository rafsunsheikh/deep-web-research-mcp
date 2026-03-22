#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
SEARXNG_CONTAINER_NAME="${SEARXNG_CONTAINER_NAME:-deep-research-searxng}"
SEARXNG_PORT="${SEARXNG_PORT:-8888}"

log() {
  printf '[ready] %s\n' "$*" >&2
}

die() {
  printf '[ready] ERROR: %s\n' "$*" >&2
  exit 1
}

require_path() {
  [[ -e "$1" ]] || die "Missing required path: $1"
}

ensure_container_running() {
  if ! docker ps --format '{{.Names}}' | grep -Fxq "${SEARXNG_CONTAINER_NAME}"; then
    if docker ps -a --format '{{.Names}}' | grep -Fxq "${SEARXNG_CONTAINER_NAME}"; then
      log "Starting existing SearXNG container ${SEARXNG_CONTAINER_NAME}"
      docker start "${SEARXNG_CONTAINER_NAME}" >/dev/null
    else
      die "SearXNG container ${SEARXNG_CONTAINER_NAME} does not exist. Run scripts/install_local.sh first."
    fi
  fi
}

wait_for_json_endpoint() {
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://localhost:${SEARXNG_PORT}/search?q=test&format=json" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

main() {
  command -v docker >/dev/null 2>&1 || die "docker is required"
  command -v curl >/dev/null 2>&1 || die "curl is required"

  require_path "${VENV_DIR}/bin/python"
  require_path "${ROOT_DIR}/server.py"

  ensure_container_running

  log "Checking SearXNG JSON endpoint on http://localhost:${SEARXNG_PORT}"
  wait_for_json_endpoint || die "SearXNG is not ready or JSON output is not enabled."

  log "Starting MCP server on stdio transport"
  exec "${VENV_DIR}/bin/python" "${ROOT_DIR}/server.py"
}

main "$@"
