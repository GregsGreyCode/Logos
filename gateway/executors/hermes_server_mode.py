"""
LOG-44 Phase 1 — Hermes-as-server mode helpers.

Opt-in setup for running the upstream `hermes gateway run` HTTP server
inside each OpenShell sandbox, so the dispatch path can be host-initiated
HTTP (to localhost:8642 inside the sandbox, reached via `openshell
sandbox exec curl`) instead of per-task `sandbox_worker.py` subprocess.

Gated by the env var ``LOGOS_HERMES_SERVER_MODE=1``. When unset, none of
this code runs and Plan A-prime (per-task exec via sandbox_worker.py)
remains in force.

Intended integration site in ``OpenShellExecutor.spawn``::

    if os.getenv("LOGOS_HERMES_SERVER_MODE") == "1":
        from .hermes_server_mode import enable_hermes_server_mode
        enable_hermes_server_mode(sandbox_name, config)

That's the whole patch to `openshell.py` — everything else lives here.

See ``docs/architecture/hermes-as-server-prototype.md`` for the full
design + empirical validation of this path on 2026-04-17.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Constants — keep in sync with prototypes/log44-phase1/dispatch_v2.py.
# These are the defaults; per-agent overrides happen via config.yaml.
HERMES_HOME_IN_SANDBOX = "/tmp/hermes-srv-home"
HERMES_BIND_HOST = "127.0.0.1"
HERMES_BIND_PORT = 8642
HEALTH_TIMEOUT_S = 30
DEFAULT_EXEC_TIMEOUT_S = 30


@dataclass(frozen=True)
class HermesServerSetup:
    """Record of what we installed into a sandbox for Hermes-server mode.

    Captured so dispatch_task_v2 (and any future cleanup) can pull the
    API key + endpoint without re-deriving them.
    """

    sandbox_name: str
    api_key: str
    base_url: str        # e.g. http://127.0.0.1:8642
    hermes_home: str     # e.g. /tmp/hermes-srv-home

    def bearer_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


def _run_sb_exec(
    sandbox_name: str,
    script: str,
    timeout: float = DEFAULT_EXEC_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    """Run a multi-line shell script inside the sandbox via `openshell sandbox exec`.

    Works around two quirks of the exec transport:
    - Args containing literal newlines are rejected (gRPC-level validation)
      → script is base64-encoded on the host and decoded on the other side.
    - Exec gates on stdin EOF → we connect stdin to /dev/null.
    """
    b64 = base64.b64encode(script.encode()).decode()
    cmd = [
        "openshell", "sandbox", "exec", "--no-tty",
        "--name", sandbox_name,
        "--", "sh", "-c", f"echo {b64} | base64 -d | sh",
    ]
    logger.debug("hermes_server_mode: sb-exec on %s: script_len=%d", sandbox_name, len(script))
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )


def _resolve_model(config: Any) -> str:
    """Pick the model name to put in hermes config.yaml.

    Logos's per-agent config uses `config.model_name` (InstanceConfig);
    fall back to env var then a sensible default. This is the model
    Hermes will request through inference.local.
    """
    candidates = [
        getattr(config, "model_name", None),
        getattr(config, "model", None),
        os.getenv("LOGOS_HERMES_DEFAULT_MODEL"),
        "gpt-oss-20b",
    ]
    for c in candidates:
        if c:
            return str(c)
    return "gpt-oss-20b"


def _resolve_system_prompt(config: Any) -> Optional[str]:
    """Extract an optional system prompt / soul text to seed Hermes with.

    Phase 1: best-effort; if no soul text, return None and Hermes uses
    its stock system prompt. Phase 2 wires in per-agent souls properly.
    """
    for attr in ("system_prompt", "soul_text", "soul"):
        v = getattr(config, attr, None)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _build_config_yaml(model: str, system_prompt: Optional[str]) -> str:
    """Generate a minimal hermes config.yaml.

    Schema reference: knowledge-repos/hermes-agent/cli-config.yaml.example.
    Validated empirically against hermes 0.7.0 (see prototype doc).
    """
    lines = [
        "model:",
        f"  default: {model}",
        "  provider: custom",
        "  base_url: https://inference.local/v1",
        "api_server:",
        "  enabled: true",
        f"  host: {HERMES_BIND_HOST}",
        f"  port: {HERMES_BIND_PORT}",
    ]
    if system_prompt:
        # Embed as a literal block scalar so multi-line souls survive
        lines.append("system_prompt: |")
        for sp_line in system_prompt.splitlines():
            lines.append(f"  {sp_line}")
    return "\n".join(lines) + "\n"


def _build_env_file(api_key: str) -> str:
    """Generate .env for hermes.

    OPENAI_API_KEY is a placeholder (the real auth is handled by the
    OpenShell L7 proxy when requests go through inference.local).
    """
    return (
        f"API_SERVER_KEY={api_key}\n"
        f"OPENAI_API_KEY=lm-studio\n"
        f"OPENAI_BASE_URL=https://inference.local/v1\n"
    )


def deploy_hermes_config(
    sandbox_name: str,
    config: Any,
    api_key: str,
) -> None:
    """Write hermes config.yaml + .env into the sandbox's HERMES_HOME."""
    model = _resolve_model(config)
    system_prompt = _resolve_system_prompt(config)
    config_yaml = _build_config_yaml(model, system_prompt)
    env_file = _build_env_file(api_key)

    # Use shlex.quote to keep any special chars in the model name safe
    # when they land on the sh -c command line inside the sandbox.
    script = f"""
set -e
mkdir -p {shlex.quote(HERMES_HOME_IN_SANDBOX)}/memories
mkdir -p {shlex.quote(HERMES_HOME_IN_SANDBOX)}/sessions
mkdir -p {shlex.quote(HERMES_HOME_IN_SANDBOX)}/logs
cat > {shlex.quote(HERMES_HOME_IN_SANDBOX)}/config.yaml <<'___CFG___'
{config_yaml}___CFG___
cat > {shlex.quote(HERMES_HOME_IN_SANDBOX)}/.env <<'___ENV___'
{env_file}___ENV___
chmod 600 {shlex.quote(HERMES_HOME_IN_SANDBOX)}/.env
echo "[hermes-server] config deployed ({len(config_yaml)}b yaml, {len(env_file)}b env)"
"""
    r = _run_sb_exec(sandbox_name, script)
    if r.returncode != 0:
        raise RuntimeError(
            f"deploy_hermes_config({sandbox_name}) failed rc={r.returncode}: "
            f"{r.stderr.strip()[-500:]}"
        )
    logger.info("hermes_server_mode: config deployed to %s (model=%s)", sandbox_name, model)


def launch_hermes_gateway(sandbox_name: str) -> None:
    """Start `hermes gateway run` in the background inside the sandbox.

    Idempotent: if a process is already running, this is a no-op. The
    gateway logs to /tmp/hermes-gw.log inside the sandbox for
    post-mortem debugging (reachable via `openshell sandbox exec cat`).
    """
    home = shlex.quote(HERMES_HOME_IN_SANDBOX)
    script = f"""
if pgrep -f 'hermes gateway run' > /dev/null 2>&1; then
  echo '[hermes-server] already running (pid='$(pgrep -f 'hermes gateway run' | head -1)')'
  exit 0
fi
rm -f /tmp/hermes-gw.log
HERMES_HOME={home} nohup hermes gateway run -v > /tmp/hermes-gw.log 2>&1 &
sleep 1
pid=$(pgrep -f 'hermes gateway run' | head -1)
if [ -z "$pid" ]; then
  echo '[hermes-server] launch failed — no process after 1s'
  tail -20 /tmp/hermes-gw.log
  exit 1
fi
echo "[hermes-server] launched pid=$pid"
"""
    r = _run_sb_exec(sandbox_name, script)
    if r.returncode != 0:
        raise RuntimeError(
            f"launch_hermes_gateway({sandbox_name}) failed rc={r.returncode}: "
            f"{r.stderr.strip()[-500:]} / stdout: {r.stdout.strip()[-500:]}"
        )
    logger.info("hermes_server_mode: launched on %s — %s", sandbox_name, r.stdout.strip())


def wait_for_hermes_health(
    sandbox_name: str,
    timeout_s: int = HEALTH_TIMEOUT_S,
) -> float:
    """Poll /health inside the sandbox until 200 or timeout.

    Returns the elapsed seconds to ready. Raises TimeoutError on failure.
    In the prototype this was ~0.2s; give it up to 30s to survive cold
    disk / cgroup scheduling on busy hosts.
    """
    probe_script = f"""
python3 - <<'___PROBE___'
import urllib.request, sys
try:
    r = urllib.request.urlopen("http://{HERMES_BIND_HOST}:{HERMES_BIND_PORT}/health", timeout=2)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
___PROBE___
"""
    start = time.monotonic()
    deadline = start + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            r = _run_sb_exec(sandbox_name, probe_script, timeout=10)
            if r.returncode == 0:
                elapsed = time.monotonic() - start
                logger.info(
                    "hermes_server_mode: %s health ready in %.2fs (attempt=%d)",
                    sandbox_name, elapsed, attempt,
                )
                return elapsed
        except subprocess.TimeoutExpired:
            pass  # probe itself timed out; try again until wall deadline
        time.sleep(1)
    elapsed = time.monotonic() - start
    raise TimeoutError(
        f"hermes gateway on {sandbox_name} did not pass /health probe "
        f"within {timeout_s}s ({attempt} attempts, {elapsed:.1f}s elapsed)"
    )


def enable_hermes_server_mode(
    sandbox_name: str,
    config: Any,
    api_key: Optional[str] = None,
) -> HermesServerSetup:
    """One-shot: deploy config, launch gateway, wait for health.

    Returns a :class:`HermesServerSetup` containing the API key + URL
    so the caller can stash it for dispatch_task_v2 to reuse. If
    ``api_key`` is not supplied, a fresh random 32-byte token is minted
    (recommended — never reuse keys across sandboxes).

    Raises:
        RuntimeError: if config deploy or gateway launch fails.
        TimeoutError: if /health doesn't come up in HEALTH_TIMEOUT_S.
    """
    if api_key is None:
        api_key = secrets.token_urlsafe(32)

    logger.info("hermes_server_mode: enabling on %s", sandbox_name)
    deploy_hermes_config(sandbox_name, config, api_key)
    launch_hermes_gateway(sandbox_name)
    wait_for_hermes_health(sandbox_name)

    setup = HermesServerSetup(
        sandbox_name=sandbox_name,
        api_key=api_key,
        base_url=f"http://{HERMES_BIND_HOST}:{HERMES_BIND_PORT}",
        hermes_home=HERMES_HOME_IN_SANDBOX,
    )
    logger.info("hermes_server_mode: %s ready at %s", sandbox_name, setup.base_url)
    return setup


def is_enabled() -> bool:
    """Return True iff LOG-44 hermes-server-mode is active for this run."""
    return os.getenv("LOGOS_HERMES_SERVER_MODE", "") == "1"


__all__ = [
    "HermesServerSetup",
    "deploy_hermes_config",
    "launch_hermes_gateway",
    "wait_for_hermes_health",
    "enable_hermes_server_mode",
    "is_enabled",
    "HERMES_HOME_IN_SANDBOX",
    "HERMES_BIND_HOST",
    "HERMES_BIND_PORT",
]
