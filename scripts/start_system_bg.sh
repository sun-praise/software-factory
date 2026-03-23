#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

load_env_file() {
  if [[ "${LOAD_ENV_FILE:-1}" != "1" ]]; then
    return
  fi

  if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env"
    set +a
  fi
}

load_env_file

RUNTIME_DIR="${RUNTIME_DIR:-${REPO_ROOT}/.runtime/local}"
PID_DIR="${RUNTIME_DIR}/pids"
LOG_DIR="${RUNTIME_DIR}/logs"
WEB_PID_FILE="${PID_DIR}/web.pid"
WORKER_PID_FILE="${PID_DIR}/worker.pid"
WEB_LOG_FILE="${LOG_DIR}/web.log"
WORKER_LOG_FILE="${LOG_DIR}/worker.log"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"
DB_PATH="${DB_PATH:-${REPO_ROOT}/data/software_factory.db}"
WORKSPACE_DIR="${WORKSPACE_DIR:-${REPO_ROOT}}"
WORKER_INTERVAL_SECONDS="${WORKER_INTERVAL_SECONDS:-2}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD="${PYTHON_BIN}"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON_CMD="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON_CMD="python3"
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") [start|stop|restart|status|logs]

Commands:
  start    Start web and worker in background
  stop     Stop web and worker
  restart  Restart both processes
  status   Show process status and runtime paths
  logs     Print log file paths

Environment overrides:
  LOAD_ENV_FILE=0            Skip sourcing ${REPO_ROOT}/.env
  PYTHON_BIN=/path/python    Python interpreter to use
  HOST=127.0.0.1             Web bind host
  PORT=8001                  Web bind port
  DB_PATH=/path/app.db       Shared SQLite path for web and worker
  WORKSPACE_DIR=/path/repo   Worker runtime root
  WORKER_INTERVAL_SECONDS=2  Worker polling interval
  RUNTIME_DIR=/path/runtime  Directory for pid/log files
EOF
}

ensure_dirs() {
  mkdir -p "${PID_DIR}" "${LOG_DIR}" "$(dirname "${DB_PATH}")"
}

assert_python() {
  if ! command -v "${PYTHON_CMD}" >/dev/null 2>&1; then
    printf 'python not found: %s\n' "${PYTHON_CMD}" >&2
    exit 1
  fi
}

start_detached() {
  local log_file="$1"
  shift

  if command -v setsid >/dev/null 2>&1; then
    nohup setsid "$@" >"${log_file}" 2>&1 < /dev/null &
  else
    nohup "$@" >"${log_file}" 2>&1 < /dev/null &
  fi
  echo $!
}

read_pid() {
  local pid_file="$1"
  if [[ -f "${pid_file}" ]]; then
    tr -d '[:space:]' <"${pid_file}"
  fi
}

pid_is_running() {
  local pid="$1"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null
}

clear_stale_pid() {
  local pid_file="$1"
  local pid
  pid=$(read_pid "${pid_file}")
  if [[ -n "${pid}" ]] && ! pid_is_running "${pid}"; then
    rm -f "${pid_file}"
  fi
}

start_web() {
  clear_stale_pid "${WEB_PID_FILE}"
  local pid
  pid=$(read_pid "${WEB_PID_FILE}")
  if pid_is_running "${pid}"; then
    printf 'web already running: pid=%s\n' "${pid}"
    return
  fi

  (
    cd "${REPO_ROOT}"
    start_detached "${WEB_LOG_FILE}" env \
      DB_PATH="${DB_PATH}" \
      HOST="${HOST}" \
      PORT="${PORT}" \
      "${PYTHON_CMD}" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}" \
      >"${WEB_PID_FILE}"
  )

  sleep 1
  pid=$(read_pid "${WEB_PID_FILE}")
  if ! pid_is_running "${pid}"; then
    printf 'failed to start web; inspect %s\n' "${WEB_LOG_FILE}" >&2
    tail -n 40 "${WEB_LOG_FILE}" >&2 || true
    exit 1
  fi
  printf 'web started: pid=%s log=%s\n' "${pid}" "${WEB_LOG_FILE}"
}

start_worker() {
  clear_stale_pid "${WORKER_PID_FILE}"
  local pid
  pid=$(read_pid "${WORKER_PID_FILE}")
  if pid_is_running "${pid}"; then
    printf 'worker already running: pid=%s\n' "${pid}"
    return
  fi

  (
    cd "${REPO_ROOT}"
    start_detached "${WORKER_LOG_FILE}" env \
      DB_PATH="${DB_PATH}" \
      "${PYTHON_CMD}" scripts/run_worker.py \
      --loop \
      --interval-seconds "${WORKER_INTERVAL_SECONDS}" \
      --workspace-dir "${WORKSPACE_DIR}" \
      >"${WORKER_PID_FILE}"
  )

  sleep 1
  pid=$(read_pid "${WORKER_PID_FILE}")
  if ! pid_is_running "${pid}"; then
    printf 'failed to start worker; inspect %s\n' "${WORKER_LOG_FILE}" >&2
    tail -n 40 "${WORKER_LOG_FILE}" >&2 || true
    exit 1
  fi
  printf 'worker started: pid=%s log=%s\n' "${pid}" "${WORKER_LOG_FILE}"
}

stop_process() {
  local name="$1"
  local pid_file="$2"
  local pid
  pid=$(read_pid "${pid_file}")
  if ! pid_is_running "${pid}"; then
    rm -f "${pid_file}"
    printf '%s not running\n' "${name}"
    return
  fi

  kill "${pid}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! pid_is_running "${pid}"; then
      rm -f "${pid_file}"
      printf '%s stopped: pid=%s\n' "${name}" "${pid}"
      return
    fi
    sleep 0.5
  done

  kill -9 "${pid}" 2>/dev/null || true
  rm -f "${pid_file}"
  printf '%s killed: pid=%s\n' "${name}" "${pid}"
}

print_status() {
  clear_stale_pid "${WEB_PID_FILE}"
  clear_stale_pid "${WORKER_PID_FILE}"

  local web_pid worker_pid
  web_pid=$(read_pid "${WEB_PID_FILE}")
  worker_pid=$(read_pid "${WORKER_PID_FILE}")

  printf 'repo=%s\n' "${REPO_ROOT}"
  printf 'python=%s\n' "${PYTHON_CMD}"
  printf 'db_path=%s\n' "${DB_PATH}"
  printf 'workspace_dir=%s\n' "${WORKSPACE_DIR}"
  printf 'runtime_dir=%s\n' "${RUNTIME_DIR}"
  printf 'web_url=http://%s:%s\n' "${HOST}" "${PORT}"
  if pid_is_running "${web_pid}"; then
    printf 'web=running pid=%s log=%s\n' "${web_pid}" "${WEB_LOG_FILE}"
  else
    printf 'web=stopped log=%s\n' "${WEB_LOG_FILE}"
  fi
  if pid_is_running "${worker_pid}"; then
    printf 'worker=running pid=%s log=%s\n' "${worker_pid}" "${WORKER_LOG_FILE}"
  else
    printf 'worker=stopped log=%s\n' "${WORKER_LOG_FILE}"
  fi
}

print_logs() {
  printf 'web_log=%s\n' "${WEB_LOG_FILE}"
  printf 'worker_log=%s\n' "${WORKER_LOG_FILE}"
}

start_all() {
  ensure_dirs
  assert_python
  (
    cd "${REPO_ROOT}"
    env DB_PATH="${DB_PATH}" "${PYTHON_CMD}" scripts/init_db.py >/dev/null
  )
  start_web
  start_worker
  print_status
}

main() {
  local command="${1:-start}"
  case "${command}" in
    start)
      start_all
      ;;
    stop)
      stop_process "worker" "${WORKER_PID_FILE}"
      stop_process "web" "${WEB_PID_FILE}"
      ;;
    restart)
      stop_process "worker" "${WORKER_PID_FILE}"
      stop_process "web" "${WEB_PID_FILE}"
      start_all
      ;;
    status)
      print_status
      ;;
    logs)
      print_logs
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
