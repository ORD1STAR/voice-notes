#!/usr/bin/env bash
# Start the server on port 2525 (override with PORT).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${GROQ_API_KEY:-}" ]; then
  echo "GROQ_API_KEY is not set. The page will load, but uploads will be refused." >&2
fi
if [ -z "${VOICE_NOTES_PASSWORD_HASH:-}" ]; then
  echo "VOICE_NOTES_PASSWORD_HASH is not set, so nobody can sign in. Run scripts/hash-password.sh and see README.md." >&2
fi
exec .venv/bin/python -m app
