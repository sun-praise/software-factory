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

AI_TOKEN_PATTERNS=(
  ANTHROPIC_API_KEY
  ANTHROPIC_AUTH_TOKEN
  ANTHROPIC_BASE_URL
  ANTHROPIC_MODEL
  ANTHROPIC_SMALL_FAST_MODEL
  OPENAI_API_KEY
  OPENAI_BASE_URL
  OPENAI_MODEL
  ZHIPU_API_KEY
  ZHIPU_AUTH_TOKEN
  API_TIMEOUT_MS
  DEEPSEEK_API_KEY
  ENABLE_TOOL_SEARCH
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
)

UNSET_ARGS=()
for var in "${AI_TOKEN_PATTERNS[@]}"; do
  if [[ -n "${!var:-}" ]]; then
    UNSET_ARGS+=("-u" "${var}")
  fi
done

echo "[start_web] stripping AI tokens: ${UNSET_ARGS[*]:-none}" >&2

exec env -i \
  HOME="${HOME:-}" \
  PATH="${PATH:-}" \
  TERM="${TERM:-}" \
  DB_PATH="${DB_PATH}" \
  HOST="${HOST}" \
  PORT="${PORT}" \
  "${UNSET_ARGS[@]}" \
  "${PYTHON_CMD}" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}"
