#!/usr/bin/env bash
# Start the server on port 2525 (override with PORT).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${GROQ_API_KEY:-}" ]; then
  echo "GROQ_API_KEY is not set. The page will load, but uploads will be refused." >&2
fi
exec .venv/bin/python -m app
