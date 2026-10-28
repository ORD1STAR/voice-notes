#!/usr/bin/env bash
# Asks for the owner password (nothing is shown) and prints its salted hash for VOICE_NOTES_PASSWORD_HASH.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m app.auth
