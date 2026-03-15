#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  printf 'DEEPSEEK_API_KEY is required\n' >&2
  exit 1
fi

export DB_PATH="${DB_PATH:-${REPO_ROOT}/data/software_factory.db}"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.deepseek.com/anthropic}"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-${DEEPSEEK_API_KEY}}"
export API_TIMEOUT_MS="${API_TIMEOUT_MS:-600000}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-deepseek-chat}"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-deepseek-chat}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"

WORKSPACE_DIR="${WORKSPACE_DIR:-${REPO_ROOT}}"

exec python3 "${REPO_ROOT}/scripts/run_worker.py" --loop --workspace-dir "${WORKSPACE_DIR}"
