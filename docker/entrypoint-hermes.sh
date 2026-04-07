#!/bin/bash
# Entrypoint for the Hermes sandbox worker.
#
# Reads instance config from /tmp/hermes/instance-config.json (uploaded at
# sandbox creation by OpenShellExecutor) and starts the WebSocket worker
# that connects back to the Logos gateway.
#
# Environment variables set by OpenShell:
#   HTTP_PROXY / HTTPS_PROXY  — point to the sandbox proxy at 10.200.0.1:3128
#   SSL_CERT_FILE             — ephemeral CA for TLS termination (if set)
#
# The worker uses inference.local for LLM calls (routed via OpenShell's
# privacy router to the configured inference provider).

set -euo pipefail

CONFIG_FILE="/tmp/hermes/instance-config.json"

# Default inference endpoint (OpenShell's privacy router)
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://inference.local/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-unused}"

# Trust OpenShell's ephemeral TLS CA for inference.local
if [ -f /etc/openshell-tls/ca.crt ]; then
    export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/openshell-tls/ca.crt}"
    export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-/etc/openshell-tls/ca.crt}"
fi

# Read config values if config file exists
if [ -f "$CONFIG_FILE" ]; then
    echo "Reading config from $CONFIG_FILE"
    # Export any model override
    MODEL=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('model',''))" 2>/dev/null || true)
    if [ -n "$MODEL" ]; then
        export HERMES_MODEL="$MODEL"
    fi
else
    echo "Warning: $CONFIG_FILE not found, using defaults"
fi

echo "Starting sandbox worker..."
echo "  OPENAI_BASE_URL=$OPENAI_BASE_URL"
echo "  HERMES_MODEL=${HERMES_MODEL:-not set}"

exec python3 /app/sandbox_worker.py
