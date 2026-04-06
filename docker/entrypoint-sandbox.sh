#!/bin/bash
# Hermes agent sandbox entrypoint.
#
# OpenShell strips Docker ENV vars and doesn't support --env on
# sandbox create.  This script sets up the environment, reads any
# instance config uploaded by OpenShellExecutor, and starts the server.

set -e

# ── Base environment ──────────────────────────────────────────────────
export PATH="/app/venv/bin:$PATH"
export PYTHONPATH="/app"
export LOGOS_HOME="/tmp/logos"
export HERMES_HOME="/tmp/hermes"
export HERMES_PORT="${HERMES_PORT:-8080}"

# Create writable state dirs
mkdir -p /tmp/logos/logs /tmp/hermes/sessions /tmp/hermes/memories

# ── Read instance config (uploaded by OpenShellExecutor) ──────────────
CONFIG_FILE="/tmp/hermes/instance-config.json"
if [ -f "$CONFIG_FILE" ]; then
    # Parse JSON keys as env vars using Python (always available in venv)
    eval "$(python -c "
import json, sys
with open('$CONFIG_FILE') as f:
    cfg = json.load(f)
for k, v in cfg.items():
    # Shell-escape the value
    v = str(v).replace(\"'\", \"'\\\"'\\\"'\")
    print(f\"export {k}='{v}'\")
")"
    echo "[entrypoint] Loaded $(python -c "import json; print(len(json.load(open('$CONFIG_FILE'))))" 2>/dev/null || echo '?') config vars from $CONFIG_FILE"
fi

# ── Start server ──────────────────────────────────────────────────────
exec /app/venv/bin/python -m gateway.run "$@"
