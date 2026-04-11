#!/bin/bash
# Entrypoint for the Logos sandbox container.
#
# Plan A architecture (TASKS.md #24): the sandbox container stays alive
# with `sleep infinity` so the host Logos gateway can reach in via
# `openshell sandbox exec --no-tty --name <sandbox> -- python3
# /app/sandbox_worker.py` to launch the worker on-demand. The worker is
# NO LONGER auto-started at container boot — the host drives its
# lifecycle.
#
# Why: the previous reverse-connection model (worker auto-launches,
# connects OUT via WebSocket/CONNECT-tunnel to the gateway's /ws/worker)
# broke when OpenShell's L7 proxy tightened post-upgrade behaviour on
# HTTP CONNECT tunnels. The host-drives-sandbox pattern uses OpenShell's
# blessed gRPC/mTLS exec transport, which was empirically rock-solid
# throughout the 2026-04-11 debugging session.
#
# Env vars the worker needs (OPENAI_BASE_URL, HERMES_MODEL, etc.) are
# injected by the host's OpenShellExecutor into the `openshell sandbox
# exec` invocation at launch time — not set here. Config is uploaded
# to /tmp/hermes/instance-config.json at sandbox creation and read by
# the worker via load_config().
#
# Graceful shutdown: `openshell sandbox stop` sends SIGTERM to PID 1.
# `exec sleep infinity` makes sleep PID 1 directly, which handles
# SIGTERM cleanly without a bash signal-forwarding layer.

set -euo pipefail

echo "[entrypoint] Logos sandbox ready (Plan A — worker launched on-demand via openshell sandbox exec)" >&2
echo "[entrypoint] Config path:   /tmp/hermes/instance-config.json" >&2
echo "[entrypoint] Worker script: /app/sandbox_worker.py" >&2

exec sleep infinity
