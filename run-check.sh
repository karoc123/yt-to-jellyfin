#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

UV_BIN="${HOME}/.local/bin/uv"
if ! command -v uv &>/dev/null; then
  if [ -x "$UV_BIN" ]; then
    export PATH="${HOME}/.local/bin:$PATH"
  fi
fi

echo "=== 1. Checking dependencies with uv ==="
uv sync --locked

echo "=== 2. Ruff linting ==="
uv run ruff check .

echo "=== 3. Ruff formatting check ==="
uv run ruff format --check .

echo "=== 4. Running pytest suite ==="
uv run pytest -q

echo " All local checks passed successfully!"
