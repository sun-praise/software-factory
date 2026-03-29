#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

DB_PATH="${DB_PATH:-${REPO_ROOT}/data/software_factory.db}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD="${PYTHON_BIN}"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON_CMD="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON_CMD="python3"
fi

TOKEN_VARS_FILE="${REPO_ROOT}/config/ai_token_env_vars.txt"
mapfile -t AI_TOKEN_ENV_VARS < <(grep -v '^[[:space:]]*$' "${TOKEN_VARS_FILE}" | sed 's/#.*//')

UNSET_ARGS=()
for var in "${AI_TOKEN_ENV_VARS[@]}"; do
  if [[ -n "${!var:-}" ]]; then
    UNSET_ARGS+=("-u" "${var}")
  fi
done

echo "[start_web] stripping AI tokens: ${UNSET_ARGS[*]:-none}" >&2

exec env \
  "${UNSET_ARGS[@]}" \
  DB_PATH="${DB_PATH}" \
  HOST="${HOST}" \
  PORT="${PORT}" \
  "${PYTHON_CMD}" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}"
