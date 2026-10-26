#!/usr/bin/env bash
# Create a virtual environment and install the dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg was not found. Install it first, for example: sudo apt install ffmpeg" >&2
  exit 1
fi
echo "Done. Set GROQ_API_KEY and run ./scripts/run.sh"
