#!/usr/bin/env bash

set -euo pipefail

OPENCODE_BIN_DIR="${OPENCODE_INSTALL_DIR:-$HOME/.opencode/bin}"
OPENCODE_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}"
INSTALL_URL="${OPENCODE_INSTALL_URL:-https://opencode.ai/install}"
MAX_ATTEMPTS="${OPENCODE_INSTALL_ATTEMPTS:-3}"
DEFAULT_OPENCODE_BIN_DIR="$HOME/.opencode/bin"
FALLBACK_OPENCODE_BIN_DIR="${RUNNER_TOOL_CACHE:-$HOME/.cache}/opencode/bin"

mkdir -p "$OPENCODE_BIN_DIR"
mkdir -p "$OPENCODE_CACHE_DIR"

export OPENCODE_INSTALL_DIR="$OPENCODE_BIN_DIR"
export XDG_CACHE_HOME="$OPENCODE_CACHE_DIR"
export PATH="$OPENCODE_BIN_DIR:$PATH"

if command -v opencode >/dev/null 2>&1; then
  echo "OpenCode already installed at: $(command -v opencode)"
  opencode --version || true
  exit 0
fi

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  echo "Installing OpenCode (attempt ${attempt}/${MAX_ATTEMPTS})"

  if curl \
    --fail \
    --silent \
    --show-error \
    --location \
    --retry 5 \
    --retry-all-errors \
    --retry-delay 2 \
    "$INSTALL_URL" | bash; then
    break
  fi

  if [ "$attempt" -eq "$MAX_ATTEMPTS" ]; then
    echo "OpenCode installation failed after ${MAX_ATTEMPTS} attempts" >&2
    exit 1
  fi

  sleep $((attempt * 5))
  attempt=$((attempt + 1))
done

if ! command -v opencode >/dev/null 2>&1; then
  for candidate in \
    "$OPENCODE_BIN_DIR/opencode" \
    "$DEFAULT_OPENCODE_BIN_DIR/opencode" \
    "$FALLBACK_OPENCODE_BIN_DIR/opencode"
  do
    if [ -x "$candidate" ]; then
      export PATH="$(dirname "$candidate"):$PATH"
      break
    fi
  done
fi

if ! command -v opencode >/dev/null 2>&1; then
  echo "OpenCode install script finished, but 'opencode' is still unavailable" >&2
  exit 1
fi

echo "OpenCode installed at: $(command -v opencode)"
opencode --version || true
