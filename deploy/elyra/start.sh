#!/usr/bin/env bash
# Launch JupyterLab + Elyra locally on http://127.0.0.1:8888 (localhost only).
# Jupyter prints a tokenised URL on startup; set JUPYTER_TOKEN to choose your own.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$REPO/.venv-elyra"
export JUPYTER_DATA_DIR="$VENV/jupyter-data"

exec "$VENV/bin/jupyter" lab \
  --ServerApp.ip=127.0.0.1 \
  --ServerApp.port="${ELYRA_PORT:-8888}" \
  --ServerApp.root_dir="$REPO" \
  --config="$REPO/deploy/elyra/jupyter_server_config.py" \
  --ServerApp.open_browser="${ELYRA_OPEN_BROWSER:-true}" \
  "$@"
