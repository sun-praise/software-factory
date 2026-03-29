#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

TOKENS_FILE="${TOKENS_FILE:-${REPO_ROOT}/.tokens.local}"

validate_token_file_path() {
  local resolved
  resolved=$(cd "$(dirname "${TOKENS_FILE}")" 2>/dev/null && pwd)/$(basename "${TOKENS_FILE}") || return 1
  if [[ "${resolved}" != "${REPO_ROOT}/"* && "${resolved}" != "${REPO_ROOT}" ]]; then
    printf '[start_worker] ERROR: TOKENS_FILE %s is outside repo root %s; refusing to source\n' \
      "${TOKENS_FILE}" "${REPO_ROOT}" >&2
    return 1
  fi
}

if [[ -f "${TOKENS_FILE}" ]]; then
  validate_token_file_path || exit 1
  token_perms=$(stat -c '%a' "${TOKENS_FILE}" 2>/dev/null || stat -f '%Lp' "${TOKENS_FILE}" 2>/dev/null || echo "")
  if [[ -n "${token_perms}" && "${token_perms: -2}" != "00" ]]; then
    printf '[start_worker] WARNING: %s has permissions %s (should be 0600); leaking tokens possible\n' \
      "${TOKENS_FILE}" "${token_perms}" >&2
  fi
  set -a
  # shellcheck disable=SC1090
  source "${TOKENS_FILE}"
  set +a
  echo "[start_worker] loaded tokens from ${TOKENS_FILE}" >&2
else
  echo "[start_worker] no tokens file at ${TOKENS_FILE}; using current env" >&2
fi

DB_PATH="${DB_PATH:-${REPO_ROOT}/data/software_factory.db}"
WORKSPACE_DIR="${WORKSPACE_DIR:-${REPO_ROOT}}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD="${PYTHON_BIN}"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON_CMD="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON_CMD="python3"
fi

exec "${PYTHON_CMD}" "${REPO_ROOT}/scripts/run_worker.py" --loop --workspace-dir "${WORKSPACE_DIR}"
