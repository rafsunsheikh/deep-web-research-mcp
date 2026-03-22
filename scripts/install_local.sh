#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
SEARXNG_CONTAINER_NAME="${SEARXNG_CONTAINER_NAME:-deep-research-searxng}"
SEARXNG_PORT="${SEARXNG_PORT:-8888}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() {
  printf '[install] %s\n' "$*"
}

die() {
  printf '[install] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
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

configure_searxng_json() {
  log "Configuring SearXNG JSON support inside container ${SEARXNG_CONTAINER_NAME}"
  docker exec "${SEARXNG_CONTAINER_NAME}" python3 - <<'PY'
from pathlib import Path

settings_path = Path("/etc/searxng/settings.yml")
text = settings_path.read_text()

if "formats:\n    - html\n    - json" not in text:
    old = "  formats:\n    - html\n"
    new = "  formats:\n    - html\n    - json\n"
    if old not in text:
        raise SystemExit("Could not find search.formats block in /etc/searxng/settings.yml")
    text = text.replace(old, new, 1)

if '  bind_address: "127.0.0.1"\n' in text:
    text = text.replace('  bind_address: "127.0.0.1"\n', '  bind_address: "0.0.0.0"\n', 1)

if '  method: "POST"\n' in text:
    text = text.replace('  method: "POST"\n', '  method: "GET"\n', 1)

settings_path.write_text(text)
PY
}

start_or_create_searxng() {
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${SEARXNG_CONTAINER_NAME}"; then
    log "Starting existing SearXNG container ${SEARXNG_CONTAINER_NAME}"
    docker start "${SEARXNG_CONTAINER_NAME}" >/dev/null
  else
    log "Creating SearXNG container ${SEARXNG_CONTAINER_NAME} on port ${SEARXNG_PORT}"
    docker run \
      --name "${SEARXNG_CONTAINER_NAME}" \
      -p "${SEARXNG_PORT}:8080" \
      -d searxng/searxng >/dev/null
  fi

  configure_searxng_json
  docker restart "${SEARXNG_CONTAINER_NAME}" >/dev/null
}

main() {
  require_command "${PYTHON_BIN}"
  require_command docker
  require_command curl

  log "Creating virtual environment in ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"

  log "Installing Python dependencies"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/pip" install -e "${ROOT_DIR}" pytest pytest-asyncio

  log "Pulling SearXNG image if needed"
  docker pull searxng/searxng >/dev/null

  start_or_create_searxng

  log "Waiting for SearXNG JSON endpoint"
  wait_for_json_endpoint || die "SearXNG JSON endpoint did not become ready at http://localhost:${SEARXNG_PORT}/search?q=test&format=json"

  log "Running project test suite"
  "${VENV_DIR}/bin/python" -m pytest -q

  cat <<EOF

[install] Setup complete.
[install] Next steps:
[install]   1. Start the MCP server: ${ROOT_DIR}/scripts/run_server_ready.sh
[install]   2. Or query from the terminal: ${VENV_DIR}/bin/python ${ROOT_DIR}/deep_research_client.py "your question here"

EOF
}

main "$@"
