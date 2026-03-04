#!/usr/bin/env bash
set -Eeuo pipefail

error_handler() {
  local exit_code="$?"
  local line_no="$1"
  echo "[scratch-house] failed at line ${line_no} (exit=${exit_code})" >&2
  exit "${exit_code}"
}

trap 'error_handler $LINENO' ERR

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

if [[ "${DEBUG:-0}" == "1" ]]; then
  set -x
fi

if [[ -f "${ROOT_DIR}/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.venv/bin/activate"
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8787}"
LINK_API_TOKEN="${LINK_API_TOKEN:-}"
LINK_TTL_SECONDS="${LINK_TTL_SECONDS:-120}"
REPORTS_DIR="${REPORTS_DIR:-${ROOT_DIR}/reports}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

mkdir -p "${REPORTS_DIR}"

echo "[scratch-house] starting server"
echo "  ws      : ${HOST}:${PORT}"
echo "  link api: ${API_HOST}:${API_PORT}"
echo "  reports : ${REPORTS_DIR}"
echo "  root    : ${ROOT_DIR}"
echo "  python  : $(command -v python || true)"

args=(
  --host "${HOST}"
  --port "${PORT}"
  --api-host "${API_HOST}"
  --api-port "${API_PORT}"
  --link-api-token "${LINK_API_TOKEN}"
  --link-ttl-seconds "${LINK_TTL_SECONDS}"
  --reports-dir "${REPORTS_DIR}"
  --log-level "${LOG_LEVEL}"
)

if command -v scratch-house-server >/dev/null 2>&1; then
  echo "[scratch-house] cmd: scratch-house-server ${args[*]}"
  exec scratch-house-server "${args[@]}"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[scratch-house] python interpreter not found." >&2
  echo "Install first: python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e ." >&2
  exit 1
fi

echo "[scratch-house] scratch-house-server entrypoint not found; falling back to python module."
echo "[scratch-house] cmd: ${PYTHON_BIN} -m scratch_house.server ${args[*]}"
exec "${PYTHON_BIN}" -m scratch_house.server "${args[@]}"
