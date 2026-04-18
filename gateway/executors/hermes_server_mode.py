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
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Constants — keep in sync with prototypes/log44-phase1/dispatch_v2.py.
# These are the defaults; per-agent overrides happen via config.yaml.
HERMES_HOME_IN_SANDBOX = "/tmp/hermes-srv-home"
HERMES_BIND_HOST = "127.0.0.1"
HERMES_BIND_PORT = 8642
HEALTH_TIMEOUT_S = 30
DEFAULT_EXEC_TIMEOUT_S = 30

# LOG-51.2: the cancel monkeypatch file lives next to this module and
# gets uploaded to every hermes-mode sandbox so hermes's /v1/runs SSE
# stream interrupts the agent on client disconnect. Path-in-sandbox is
# fixed so the launch command can reference it without per-sandbox
# state.
_CANCEL_PATCH_SRC = Path(__file__).parent / "hermes_cancel_monkeypatch.py"
CANCEL_PATCH_PATH_IN_SANDBOX = f"{HERMES_HOME_IN_SANDBOX}/hermes_cancel_monkeypatch.py"

# LOG-44.2: per-soul boot hooks. If a soul ships a ``boot.md`` next to
# its ``soul.md`` in ``souls/<name>/``, it gets uploaded to
# ``/tmp/hermes-srv-home/BOOT.md`` on spawn and hermes's built-in
# boot_md hook (gateway/builtin_hooks/boot_md.py in the sandbox image)
# runs the agent with those instructions on every gateway startup. A
# soul without boot.md produces no BOOT.md file, so its agents don't
# auto-run anything on boot.
_SOULS_DIR = Path(__file__).parent.parent.parent / "souls"
BOOT_MD_PATH_IN_SANDBOX = f"{HERMES_HOME_IN_SANDBOX}/BOOT.md"


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


def _build_env_file(
    api_key: str,
    extra_env: Optional[dict] = None,
) -> str:
    """Generate .env for hermes.

    OPENAI_API_KEY is a placeholder (the real auth is handled by the
    OpenShell L7 proxy when requests go through inference.local).

    GATEWAY_ALLOW_ALL_USERS=true is required because hermes's API
    server has a *second* auth layer (user allowlist) on top of the
    bearer token — without it every POST /v1/runs returns 401
    "Invalid API key" even when API_SERVER_KEY matches. Safe inside
    the sandbox because the only network ingress to port 8642 is the
    gateway's own `openshell sandbox exec` transport; there's no
    external attacker to guard against here.

    ``extra_env`` (LOG-44.3) — per-agent env vars appended after the
    baseline. Typically channel-adapter credentials
    (``TELEGRAM_BOT_TOKEN``, ``DISCORD_BOT_TOKEN``, etc) read from
    ``agent_channel_credentials`` by the caller. Hermes's built-in
    ``_apply_env_overrides`` in ``gateway/config.py`` inside the
    sandbox turns these into enabled platform adapters on boot.
    Baseline keys take precedence — extra_env can't clobber
    ``API_SERVER_KEY`` or the ``OPENAI_*`` ones by accident.
    """
    lines = [
        f"API_SERVER_KEY={api_key}",
        "GATEWAY_ALLOW_ALL_USERS=true",
        "OPENAI_API_KEY=lm-studio",
        "OPENAI_BASE_URL=https://inference.local/v1",
    ]
    _baseline_keys = {"API_SERVER_KEY", "GATEWAY_ALLOW_ALL_USERS",
                      "OPENAI_API_KEY", "OPENAI_BASE_URL"}
    if extra_env:
        for k, v in extra_env.items():
            if not k or not isinstance(k, str):
                continue
            if k in _baseline_keys:
                logger.warning(
                    "hermes_server_mode: extra_env tried to clobber "
                    "baseline key %s — ignoring", k,
                )
                continue
            # Newlines in values would split into subsequent env entries
            # and leak secrets or shift semantics; reject defensively.
            sv = str(v)
            if "\n" in sv or "\r" in sv:
                logger.warning(
                    "hermes_server_mode: extra_env value for %s "
                    "contains newline — ignoring", k,
                )
                continue
            lines.append(f"{k}={sv}")
    return "\n".join(lines) + "\n"


# Canonical mapping from Logos platform identifier → hermes env-var
# name. Matches hermes's own ``_token_env_names`` in its gateway/config.py
# — any mismatch here means the credential lands in ``.env`` but hermes
# doesn't recognise it and the adapter stays disabled.
_PLATFORM_ENV_MAP = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord": "DISCORD_BOT_TOKEN",
    "slack": "SLACK_BOT_TOKEN",
    "mattermost": "MATTERMOST_TOKEN",
    "matrix": "MATRIX_ACCESS_TOKEN",
    "weixin": "WEIXIN_TOKEN",
}


def build_channel_extra_env(
    agent_id: str,
    *,
    apply_presets: bool = True,
    sandbox_name_for_log: Optional[str] = None,
) -> dict:
    """Return the per-agent channel env dict for the LOG-44.3 path.

    Reads ``agent_channel_credentials`` via auth.db, builds a
    ``{ENV_VAR: token}`` dict for every enabled row whose platform has
    a known env-var mapping. Platforms not in ``_PLATFORM_ENV_MAP``
    (or credentials with an empty token) are silently skipped.

    When ``apply_presets=True`` (default), also calls
    ``gateway.policies.apply_preset(agent_id, platform)`` for each
    credential's platform — idempotent per policies, fails best-effort
    with a WARNING so a missing preset file doesn't block the whole
    build. Set False from paths that shouldn't trigger policy writes
    (dry runs, introspection).

    ``sandbox_name_for_log`` is used purely for log formatting so
    spawn/refresh messages name the sandbox they relate to. Falls
    back to the agent_id when unset.
    """
    try:
        from gateway.auth import db as _auth_db
    except ImportError:
        logger.warning(
            "build_channel_extra_env: auth.db not importable — "
            "returning empty env (agent=%s)", agent_id,
        )
        return {}

    extra_env: dict = {}
    label_tag = sandbox_name_for_log or agent_id
    try:
        rows = _auth_db.list_agent_channel_credentials(
            agent_id=agent_id, enabled_only=True,
        )
    except Exception as exc:
        logger.warning(
            "build_channel_extra_env(%s): list_agent_channel_credentials "
            "raised %s — proceeding without channel env", label_tag, exc,
        )
        return {}

    for row in rows:
        plat = (row.get("platform") or "").lower()
        env_name = _PLATFORM_ENV_MAP.get(plat)
        token = row.get("token") or ""
        if not env_name or not token:
            continue
        extra_env[env_name] = token
        if apply_presets:
            try:
                from gateway import policies as _policies
                _policies.apply_preset(agent_id, plat)
                logger.info(
                    "build_channel_extra_env(%s): applied '%s' preset "
                    "(channel cred %s)",
                    label_tag, plat, row.get("label") or "default",
                )
            except Exception as preset_exc:
                logger.warning(
                    "build_channel_extra_env(%s): could not apply '%s' "
                    "preset: %s (agent may be unable to reach %s API)",
                    label_tag, plat, preset_exc, plat,
                )
    if extra_env:
        logger.info(
            "build_channel_extra_env(%s): env keys %s",
            label_tag, sorted(extra_env.keys()),
        )
    return extra_env


def deploy_hermes_config(
    sandbox_name: str,
    config: Any,
    api_key: str,
    extra_env: Optional[dict] = None,
) -> None:
    """Write hermes config.yaml + .env into the sandbox's HERMES_HOME.

    ``extra_env`` (LOG-44.3) — per-agent env vars appended to .env,
    typically platform credentials like ``TELEGRAM_BOT_TOKEN``. Built
    by the caller from ``agent_channel_credentials``.
    """
    model = _resolve_model(config)
    system_prompt = _resolve_system_prompt(config)
    config_yaml = _build_config_yaml(model, system_prompt)
    env_file = _build_env_file(api_key, extra_env=extra_env)

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


def deploy_cancel_monkeypatch(sandbox_name: str) -> None:
    """Upload the LOG-51.2 cancel monkeypatch into the sandbox.

    The launcher is a standalone Python script (shipped in this repo
    alongside ``hermes_server_mode.py``) that rebinds
    ``APIServerAdapter._handle_runs`` + ``_handle_run_events`` on the
    upstream hermes module before delegating to ``/usr/local/bin/hermes``
    via ``runpy``. Delivered by upload (not by forking hermes-agent) so
    the sandbox image stays swappable — see LOG-45's runtime-contract
    notes and LOG-51's temporary-patch framing.

    Idempotent: overwrites any existing copy each spawn so patch updates
    ship with the next sandbox refresh without extra plumbing.
    """
    if not _CANCEL_PATCH_SRC.exists():
        raise RuntimeError(
            f"deploy_cancel_monkeypatch: source file missing at "
            f"{_CANCEL_PATCH_SRC}. Logos deploy is broken — bail out "
            f"rather than launch hermes without the cancel patch."
        )
    patch_src = _CANCEL_PATCH_SRC.read_text(encoding="utf-8")
    # Base64-encode so shell doesn't need to care about the script body
    # (newlines, quotes, heredoc delimiters, whatever). Same mechanism
    # _run_sb_exec uses for its own script arg.
    b64_patch = base64.b64encode(patch_src.encode()).decode()
    patch_path = shlex.quote(CANCEL_PATCH_PATH_IN_SANDBOX)
    script = f"""
set -e
mkdir -p {shlex.quote(HERMES_HOME_IN_SANDBOX)}
echo {b64_patch} | base64 -d > {patch_path}
chmod 644 {patch_path}
echo "[hermes-server] cancel monkeypatch deployed ({len(patch_src)}b) to {CANCEL_PATCH_PATH_IN_SANDBOX}"
"""
    r = _run_sb_exec(sandbox_name, script)
    if r.returncode != 0:
        raise RuntimeError(
            f"deploy_cancel_monkeypatch({sandbox_name}) failed rc={r.returncode}: "
            f"{r.stderr.strip()[-500:]}"
        )
    logger.info(
        "hermes_server_mode: cancel monkeypatch deployed to %s (%d bytes)",
        sandbox_name, len(patch_src),
    )


def deploy_boot_md(sandbox_name: str, soul_name: str) -> None:
    """Upload the soul's ``boot.md`` as ``BOOT.md`` in the sandbox.

    Hermes's built-in ``boot_md`` hook (in the sandbox image at
    ``gateway/builtin_hooks/boot_md.py``) is always registered and
    runs the agent with ``BOOT.md`` contents on every ``gateway:startup``
    event. Per-soul boot behavior is therefore just a matter of
    putting the right file at the right path before hermes launches.

    Looks for ``souls/<soul_name>/boot.md`` on the host. If present,
    uploads it verbatim as ``/tmp/hermes-srv-home/BOOT.md``. If absent
    (most souls), *explicitly removes* any prior ``BOOT.md`` so an
    earlier spawn's stale boot hook can't fire for a soul that has
    since been edited to drop its boot behavior. Idempotent on respawn.
    """
    source = _SOULS_DIR / soul_name / "boot.md"
    target = shlex.quote(BOOT_MD_PATH_IN_SANDBOX)

    if source.is_file():
        try:
            boot_content = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"deploy_boot_md({sandbox_name}): cannot read "
                f"{source}: {exc}"
            ) from exc
        b64_boot = base64.b64encode(boot_content.encode("utf-8")).decode()
        script = f"""
set -e
mkdir -p {shlex.quote(HERMES_HOME_IN_SANDBOX)}
echo {b64_boot} | base64 -d > {target}
chmod 644 {target}
echo "[hermes-server] BOOT.md deployed ({len(boot_content)}b, soul={soul_name})"
"""
        action = f"deployed from souls/{soul_name}/boot.md ({len(boot_content)} bytes)"
    else:
        # No boot.md — nuke any previous one so a changed-my-mind soul
        # edit actually takes effect on next respawn.
        script = f"""
rm -f {target}
echo "[hermes-server] BOOT.md cleared (soul={soul_name} has no boot.md)"
"""
        action = f"cleared (soul {soul_name} has no boot.md)"

    r = _run_sb_exec(sandbox_name, script)
    if r.returncode != 0:
        raise RuntimeError(
            f"deploy_boot_md({sandbox_name}) failed rc={r.returncode}: "
            f"{r.stderr.strip()[-500:]}"
        )
    logger.info("hermes_server_mode: BOOT.md %s for %s", action, sandbox_name)


def redeploy_hermes_env(
    sandbox_name: str,
    api_key: str,
    extra_env: Optional[dict] = None,
) -> None:
    """Rewrite just the ``.env`` file inside the sandbox (LOG-44.3.4).

    Used by ``OpenShellExecutor.refresh_channel_credentials`` to apply
    a credential change without a full respawn. The baseline
    ``API_SERVER_KEY`` MUST be the value originally minted at spawn —
    reuse it from ``hermes_server_setup`` on the state record or
    dispatch_v2 will start 401ing. ``config.yaml`` is intentionally
    left alone (model + system_prompt don't depend on credentials).
    """
    env_file = _build_env_file(api_key, extra_env=extra_env)
    script = f"""
set -e
mkdir -p {shlex.quote(HERMES_HOME_IN_SANDBOX)}
cat > {shlex.quote(HERMES_HOME_IN_SANDBOX)}/.env <<'___ENV___'
{env_file}___ENV___
chmod 600 {shlex.quote(HERMES_HOME_IN_SANDBOX)}/.env
echo "[hermes-server] .env redeployed ({len(env_file)}b, {len(extra_env or {})} extra keys)"
"""
    r = _run_sb_exec(sandbox_name, script)
    if r.returncode != 0:
        raise RuntimeError(
            f"redeploy_hermes_env({sandbox_name}) failed rc={r.returncode}: "
            f"{r.stderr.strip()[-500:]}"
        )
    logger.info(
        "hermes_server_mode: .env refreshed on %s (%d extra keys)",
        sandbox_name, len(extra_env or {}),
    )


def restart_hermes_in_sandbox(sandbox_name: str) -> None:
    """pkill + relaunch hermes inside the sandbox (LOG-44.3.4).

    Used for credential hot-refresh: keeps the sandbox pod + workspace
    intact but bounces the hermes process so it re-reads ``.env``.
    Matches the launch command in ``launch_hermes_gateway`` so the
    monkeypatch + logging stay consistent.
    """
    home = shlex.quote(HERMES_HOME_IN_SANDBOX)
    patch = shlex.quote(CANCEL_PATCH_PATH_IN_SANDBOX)
    script = f"""
pkill -f 'hermes_cancel_monkeypatch\\.py gateway run|hermes gateway run' || true
# Brief wait so the socket is released before we relaunch.
sleep 1
rm -f /tmp/hermes-gw.log
HERMES_HOME={home}
export HERMES_HOME
if [ -f "$HERMES_HOME/.env" ]; then
  set -a
  . "$HERMES_HOME/.env"
  set +a
fi
if [ ! -f {patch} ]; then
  echo '[hermes-server] ERROR: cancel monkeypatch missing at {CANCEL_PATCH_PATH_IN_SANDBOX}' >&2
  exit 2
fi
nohup python3 {patch} gateway run -v > /tmp/hermes-gw.log 2>&1 &
sleep 1
pid=$(pgrep -f 'hermes_cancel_monkeypatch\\.py gateway run' | head -1)
if [ -z "$pid" ]; then
  echo '[hermes-server] restart failed — no process after 1s'
  tail -20 /tmp/hermes-gw.log
  exit 1
fi
echo "[hermes-server] restarted pid=$pid (credential refresh)"
"""
    r = _run_sb_exec(sandbox_name, script, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(
            f"restart_hermes_in_sandbox({sandbox_name}) failed rc={r.returncode}: "
            f"{r.stderr.strip()[-500:]} / stdout: {r.stdout.strip()[-500:]}"
        )
    logger.info(
        "hermes_server_mode: restarted hermes on %s — %s",
        sandbox_name, r.stdout.strip(),
    )


def launch_hermes_gateway(sandbox_name: str) -> None:
    """Start `hermes gateway run` in the background inside the sandbox.

    Idempotent: if a process is already running, this is a no-op. The
    gateway logs to /tmp/hermes-gw.log inside the sandbox for
    post-mortem debugging (reachable via `openshell sandbox exec cat`).

    LOG-51.2: launches via ``python3 <monkeypatch.py> gateway run -v``
    rather than ``hermes gateway run -v`` so the SSE-disconnect-
    interrupt patch is applied before hermes starts serving.
    ``deploy_cancel_monkeypatch`` must have run first (orchestrated by
    ``enable_hermes_server_mode``).
    """
    home = shlex.quote(HERMES_HOME_IN_SANDBOX)
    patch = shlex.quote(CANCEL_PATCH_PATH_IN_SANDBOX)
    # Match on a pgrep pattern that covers BOTH launch styles — the
    # direct ``hermes gateway run`` and our new
    # ``python3 …monkeypatch.py gateway run`` — so the idempotency
    # check survives a mid-spawn switch between them (e.g. during
    # rollout, if a sandbox was launched on the old command and we
    # re-spawn on the new one we don't want to double-launch).
    script = f"""
if pgrep -f 'hermes_cancel_monkeypatch\\.py gateway run|hermes gateway run' > /dev/null 2>&1; then
  echo '[hermes-server] already running (pid='$(pgrep -f 'hermes_cancel_monkeypatch\\.py gateway run|hermes gateway run' | head -1)')'
  exit 0
fi
rm -f /tmp/hermes-gw.log
HERMES_HOME={home}
export HERMES_HOME
if [ -f "$HERMES_HOME/.env" ]; then
  set -a
  . "$HERMES_HOME/.env"
  set +a
fi
if [ ! -f {patch} ]; then
  echo '[hermes-server] ERROR: cancel monkeypatch missing at {CANCEL_PATCH_PATH_IN_SANDBOX}' >&2
  exit 2
fi
nohup python3 {patch} gateway run -v > /tmp/hermes-gw.log 2>&1 &
sleep 1
pid=$(pgrep -f 'hermes_cancel_monkeypatch\\.py gateway run' | head -1)
if [ -z "$pid" ]; then
  echo '[hermes-server] launch failed — no process after 1s'
  tail -20 /tmp/hermes-gw.log
  exit 1
fi
echo "[hermes-server] launched pid=$pid (cancel monkeypatch applied)"
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
    extra_env: Optional[dict] = None,
) -> HermesServerSetup:
    """One-shot: deploy config, launch gateway, wait for health.

    Returns a :class:`HermesServerSetup` containing the API key + URL
    so the caller can stash it for dispatch_task_v2 to reuse. If
    ``api_key`` is not supplied, a fresh random 32-byte token is minted
    (recommended — never reuse keys across sandboxes).

    ``extra_env`` (LOG-44.3) — per-agent env vars passed into the
    sandbox's ``.env``. Typically channel-adapter credentials
    (``TELEGRAM_BOT_TOKEN``, ``DISCORD_BOT_TOKEN``, etc). Hermes's
    ``_apply_env_overrides`` inside the sandbox picks them up on
    startup and enables the matching platform adapters. Caller
    (``OpenShellExecutor.spawn``) builds the dict from
    ``agent_channel_credentials`` so ``hermes_server_mode`` stays
    decoupled from auth.db.

    Raises:
        RuntimeError: if config deploy or gateway launch fails.
        TimeoutError: if /health doesn't come up in HEALTH_TIMEOUT_S.
    """
    if api_key is None:
        api_key = secrets.token_urlsafe(32)

    logger.info(
        "hermes_server_mode: enabling on %s (extra_env keys=%s)",
        sandbox_name,
        sorted((extra_env or {}).keys()),
    )
    deploy_hermes_config(sandbox_name, config, api_key, extra_env=extra_env)
    deploy_cancel_monkeypatch(sandbox_name)
    # LOG-44.2: per-soul boot hook. No-op (explicit clear) for souls
    # without a boot.md; otherwise uploads the file for the built-in
    # boot_md hook inside hermes to pick up on next gateway:startup.
    deploy_boot_md(sandbox_name, getattr(config, "soul_name", "default"))
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
    "build_channel_extra_env",
    "deploy_hermes_config",
    "deploy_cancel_monkeypatch",
    "deploy_boot_md",
    "redeploy_hermes_env",
    "restart_hermes_in_sandbox",
    "launch_hermes_gateway",
    "wait_for_hermes_health",
    "enable_hermes_server_mode",
    "is_enabled",
    "HERMES_HOME_IN_SANDBOX",
    "HERMES_BIND_HOST",
    "HERMES_BIND_PORT",
    "CANCEL_PATCH_PATH_IN_SANDBOX",
    "BOOT_MD_PATH_IN_SANDBOX",
]
