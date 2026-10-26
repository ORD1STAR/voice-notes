#!/usr/bin/env bash
# Install Ollama and download the default model used for structuring notes.
set -euo pipefail

MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b}"

if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
ollama pull "$MODEL"
echo "Model $MODEL is ready."
