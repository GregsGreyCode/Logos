"""
HTTP API server for the Hermes gateway.

Provides:
  GET  /          — unified admin dashboard (no auth)
  GET  /health    — health check (no auth)
  GET  /status    — agent execution status JSON (no auth)
  GET  /sessions  — list active sessions (Bearer auth)
  POST /chat      — send a message, SSE stream (no auth — same-origin dashboard)
  GET  /canary/status                     — probe hermes-canary in-cluster health (active: bool)
  GET  /proxy/state                       — proxy → ai-router /admin/state
  POST /proxy/providers/{key}/toggle      — proxy → ai-router /admin/providers/{key}/toggle
"""

import asyncio
import importlib.metadata
import json
import logging
import os
import pathlib
import re
import time
from pathlib import Path
from typing import Any

import yaml

import aiohttp
from aiohttp import web

from gateway.auth import db as auth_db
from gateway.auth.handlers import (
    handle_audit_logs,
    handle_login,
    handle_logout,
    handle_me,
    handle_refresh,
    handle_users_list,
    handle_users_me_patch,
    handle_users_patch,
    handle_users_post,
)
from gateway import admin_handlers
from gateway.auth.middleware import auth_middleware, check_rate_limit, require_csrf, require_permission
from gateway.auth.password import hash_password
from gateway.auth.rbac import can_spawn
from gateway.config import Platform
from gateway.session import SessionSource, build_session_context, build_session_context_prompt, build_agent_system_prompt

logger = logging.getLogger(__name__)

_start_time: float = 0.0
_hermes_home: Path = Path(
    os.environ.get("LOGOS_HOME")
    or os.environ.get("HERMES_HOME")
    or str(Path.home() / ".logos")
)
_AI_ROUTER_BASE = os.environ.get(
    "AI_ROUTER_BASE",
    "http://ai-router.hermes.svc.cluster.local:9001",
)
_CANARY_HEALTH_URL = "http://hermes-canary.hermes.svc.cluster.local/health"
_INSTANCE_NAME = (
    os.environ.get("LOGOS_INSTANCE_NAME")
    or os.environ.get("HERMES_INSTANCE_NAME")
    or "Hermes"
)
_IS_CANARY = (
    os.environ.get("LOGOS_IS_CANARY")
    or os.environ.get("HERMES_IS_CANARY")
    or ""
).lower() in ("1", "true", "yes")

try:
    # Read directly from pyproject.toml — immune to stale installed metadata
    import tomllib as _tomllib
    with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb") as _f:
        _APP_VERSION = _tomllib.load(_f)["project"]["version"]
except Exception:
    try:
        _APP_VERSION = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        _APP_VERSION = "dev"
_BUILD_SHA = os.environ.get("BUILD_SHA", "local")[:7]
_VERSION_LABEL = f"v{_APP_VERSION} · {_BUILD_SHA}{' · canary' if _IS_CANARY else ''}"
_SERVER_START_TS = str(int(__import__("time").time()))  # unique per pod start; used to invalidate setup localStorage
# Generic instance-name sanitiser (used by every executor for naming).
from gateway.executors.base import safe_k8s_name as _safe_k8s_name

# In-memory request queue for instances that couldn't spawn due to resource constraints
_instance_queue: list[dict] = []

# ── Soul Registry — re-exported from gateway.souls ────────────────────────────
from gateway.souls import (  # noqa: E402
    SoulManifest as SoulManifest,
    load_souls as _load_souls,
    get_soul_registry as _get_soul_registry,
    validate_soul_overrides as _validate_soul_overrides,
    compute_effective_toolsets as _compute_effective_toolsets,
)
# _SOUL_REGISTRY alias for the one place that accesses it directly (startup + admin page)
import gateway.souls as _souls_module

_SOULS_DIR = pathlib.Path(__file__).parent.parent / "souls"



# Stable epoch for hue-cycle phase-locking across all browser tabs and the tray icon.
_HUE_EPOCH_MS: int = int(time.time() * 1000)


# When running as a PyInstaller frozen executable, __file__ points inside the
# zip archive and "html/" must be resolved via sys._MEIPASS instead.
import sys as _sys
_html_dir = (
    Path(_sys._MEIPASS) / "gateway" / "html"
    if getattr(_sys, "frozen", False)
    else Path(__file__).parent / "html"
)
_ADMIN_HTML  = (_html_dir / "main_app.html").read_text(encoding="utf-8")
_LOGIN_HTML  = (_html_dir / "login.html").read_text(encoding="utf-8")
_SETUP_HTML  = (_html_dir / "setup.html").read_text(encoding="utf-8")


def _check_auth(request: web.Request) -> bool:
    """Legacy internal-token check — still used by /sessions endpoint."""
    token = (
        os.environ.get("LOGOS_INTERNAL_TOKEN")
        or os.environ.get("HERMES_INTERNAL_TOKEN")
        or ""
    )
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {token}"


async def _prune_orphan_sandboxes(executor) -> None:
    """Delete OpenShell sandboxes whose agent record no longer exists.

    The reverse of ``_resurrect_missing_sandboxes``: when an agent is
    deleted via the UI we destroy its sandbox in the request handler,
    but if that handler raced with state-file pruning (or the gateway
    crashed mid-delete, or the user wiped the auth DB while sandboxes
    were running) the OpenShell side keeps the sandbox alive forever.
    This pass walks every ``hermes-*`` sandbox and removes any that
    don't map to a known agent. Runs once at startup.
    """
    if not executor or type(executor).__name__ != "OpenShellExecutor":
        return
    try:
        agents = auth_db.list_agents()
    except Exception as exc:
        logger.warning("prune-orphans: could not list agents: %s", exc)
        return

    try:
        from gateway.executors.openshell import (
            _list_all_sandbox_names_with_gateway,
            _sanitize_sandbox_name,
            _openshell,
        )
        # Enumerate sandboxes across every provisioned gateway, not just
        # the CLI's default — multi-route routing means orphans can live
        # in any of N gateways and the old single-gateway scan would
        # silently leak them.
        live_with_gw = _list_all_sandbox_names_with_gateway()
    except Exception as exc:
        logger.warning("prune-orphans: could not list openshell sandboxes: %s", exc)
        return

    expected = {
        _sanitize_sandbox_name(f"hermes-{a.get('name', '')}")
        for a in agents
        if a.get("name")
    }
    # Only consider hermes-prefixed sandboxes — leave anything else
    # (e.g. user-managed sandboxes from the openshell CLI) alone.
    orphans = [(n, gw) for (n, gw) in live_with_gw
               if n.startswith("hermes-") and n not in expected]
    if not orphans:
        return

    logger.info("prune-orphans: deleting %d orphan sandbox(es): %s",
                len(orphans),
                ", ".join(f"{n}@{gw}" for n, gw in orphans))

    import asyncio as _asyncio

    async def _delete_one(sandbox_name: str, sandbox_gw: str):
        try:
            await _asyncio.to_thread(
                _openshell, "sandbox", "delete", sandbox_name,
                gateway=sandbox_gw, check=False,
            )
            logger.info("prune-orphans: deleted '%s' from gateway '%s'",
                        sandbox_name, sandbox_gw)
        except Exception as exc:
            logger.warning("prune-orphans: failed for '%s'@'%s': %s",
                           sandbox_name, sandbox_gw, exc)

    for sandbox_name, sandbox_gw in orphans:
        _asyncio.create_task(_delete_one(sandbox_name, sandbox_gw))


async def _resurrect_missing_sandboxes(executor) -> None:
    """One-shot startup pass: spawn a sandbox for any named agent whose
    OpenShell sandbox no longer exists.

    Why this exists: agent records live in auth.db, but the sandboxes that
    serve them are managed by OpenShell out-of-process. The two can drift
    apart in normal operation:
      - admin force-deletes a sandbox via `openshell sandbox delete`
      - the sandbox image is rebuilt and existing sandboxes need to pick
        up new sandbox_worker.py code (the only way is delete + respawn)
      - host crash leaves the auth DB intact but loses the sandbox CRs

    Without this pass, the agent shows up in the UI but its chat hangs
    forever because there's no worker on the other end. The user has no
    obvious way to fix it short of deleting the agent and recreating it.

    Each spawn takes 30-60s (openshell sandbox create blocks on the k8s
    Sandbox CR provisioning), so we run them in background threads and
    return immediately. The Sandboxes dashboard shows phase=provisioning
    while the openshell create is in flight.
    """
    if not executor or type(executor).__name__ != "OpenShellExecutor":
        return
    try:
        agents = auth_db.list_agents()
    except Exception as exc:
        logger.warning("resurrect: could not list agents: %s", exc)
        return
    if not agents:
        return

    # Snapshot the live sandbox names from OpenShell once, instead of
    # calling _sandbox_exists per agent (which spawns one CLI subprocess
    # each — slow if there are many agents). Multi-route note: an agent
    # is considered "alive" as long as a sandbox by its name exists in
    # ANY gateway. Resurrection only fires when no gateway has it. The
    # spawn path picks the right gateway via _resolve_route(), so we
    # don't need to remember which gateway was the original home.
    try:
        from gateway.executors.openshell import (
            _list_all_sandbox_names_with_gateway,
            _sanitize_sandbox_name,
        )
        live_names = {n for (n, _gw) in _list_all_sandbox_names_with_gateway()}
    except Exception as exc:
        logger.warning("resurrect: could not list openshell sandboxes: %s", exc)
        return

    from gateway.executors.base import InstanceConfig
    import json as _json
    import asyncio as _asyncio

    missing = []
    # Track live sandboxes with their gateway for state reconciliation
    live_names_with_gw = {}
    try:
        live_names_with_gw = {n: gw for (n, gw) in _list_all_sandbox_names_with_gateway()}
    except Exception:
        pass  # already captured in live_names above

    for a in agents:
        name = a.get("name") or ""
        if not name:
            continue
        sandbox_name = _sanitize_sandbox_name(f"hermes-{name}")
        if sandbox_name in live_names:
            continue
        missing.append(a)

    # Reconcile state file: ensure every live sandbox has a state entry.
    # After a gateway restart, the state file may be empty even though
    # sandboxes are still running from the previous instance. Without
    # state entries, worker_registry.get() returns None and chat dispatch
    # reports "worker not connected" even though the sandbox is Ready.
    from gateway.executors.openshell import _load_state, _save_state, _state_lock
    with _state_lock():
        current_state = _load_state()
        _known_names = {s.get("sandbox_name") for s in current_state}
        _reconciled = 0
        for a in agents:
            name = a.get("name") or ""
            if not name:
                continue
            sandbox_name = _sanitize_sandbox_name(f"hermes-{name}")
            if sandbox_name not in live_names or sandbox_name in _known_names:
                continue
            # Sandbox exists in OpenShell but not in the state file — create entry
            toolsets_raw = a.get("toolsets") or ""
            try:
                toolsets = _json.loads(toolsets_raw) if toolsets_raw else []
            except Exception:
                toolsets = []
            current_state.append({
                "name": name,
                "sandbox_name": sandbox_name,
                "worker_id": sandbox_name,
                "source": "openshell",
                "soul_name": a.get("soul_slug") or "general",
                "model": a.get("model") or "",
                "openshell_name": live_names_with_gw.get(sandbox_name, ""),
                "model_route_id": a.get("model_route_id") or "",
                "requester": "(reconciled)",
                "toolsets": toolsets if isinstance(toolsets, list) else [],
                "policy": "",
                "sandbox_image": executor.sandbox_image,
                "created_at": __import__("time").time(),
                "phase": "ready",
            })
            _reconciled += 1
        if _reconciled:
            _save_state(current_state)
            logger.info("resurrect: reconciled %d existing sandbox(es) into state file", _reconciled)

    if not missing:
        logger.info("resurrect: all %d agents already have sandboxes", len(agents))
        return

    logger.info("resurrect: %d agent(s) missing sandboxes — spawning in background",
                len(missing))

    async def _spawn_one(agent: dict):
        try:
            toolsets_raw = agent.get("toolsets") or ""
            try:
                toolsets = _json.loads(toolsets_raw) if toolsets_raw else []
            except Exception:
                toolsets = []
            cfg = InstanceConfig(
                name=agent["name"],
                soul_name=agent.get("soul_slug") or "general",
                model=agent.get("model") or "",
                requester="(resurrected)",
                instance_label=agent["name"],
                toolsets=toolsets if isinstance(toolsets, list) else [],
                # CRITICAL: pass the agent's model_route_id so the spawn
                # ends up in the RIGHT openshell gateway. Without this,
                # _resolve_route() falls back to the default route (e.g.
                # logos-openshell / openai/gpt-oss-20b) and every
                # resurrected agent lands on the default model regardless
                # of which route it was bound to. That's how the user's
                # Ani agent (route=qwen) kept respawning into Hermes'
                # gateway and serving Hermes' model.
                model_route_id=agent.get("model_route_id"),
            )
            await _asyncio.to_thread(executor.spawn, cfg)
            logger.info("resurrect: spawned sandbox for agent '%s'", agent["name"])
        except Exception as exc:
            logger.warning("resurrect: failed for agent '%s': %s", agent.get("name"), exc)

    for agent in missing:
        _asyncio.create_task(_spawn_one(agent))


def _ensure_admin_exists() -> None:
    """Seed the first admin account from env vars if the users table is empty."""
    admin_email = (
        os.environ.get("LOGOS_ADMIN_EMAIL")
        or os.environ.get("HERMES_ADMIN_EMAIL")
        or ""
    ).strip()
    admin_pass = (
        os.environ.get("LOGOS_ADMIN_PASSWORD")
        or os.environ.get("HERMES_ADMIN_PASSWORD")
        or ""
    ).strip()
    if not admin_email or not admin_pass:
        return
    try:
        # Skip if any admin user already exists — the admin may have changed their
        # email during setup, so checking by email alone would re-create a duplicate.
        _, total = auth_db.list_users(page=1, limit=1, role="admin")
        if total > 0:
            return
        auth_db.create_user(
            email=admin_email,
            username="admin",
            password_hash=hash_password(admin_pass),
            role="admin",
            display_name=(
                os.environ.get("LOGOS_ADMIN_NAME")
                or os.environ.get("HERMES_ADMIN_NAME")
                or "Admin"
            ),
        )
        logger.info("Seeded admin account: %s", admin_email)
    except Exception as exc:
        logger.warning("Failed to seed admin account: %s", exc)


# ── Unified Services (tool credentials) ──────────────────────────────────

async def _handle_services_catalogue(request: web.Request) -> web.Response:
    """GET /api/services — unified catalogue of MCP servers + tool integrations."""
    from gateway.services import get_tool_integrations
    mcp_servers = []
    try:
        svc = request.app.get("mcp_service")
        if svc:
            mcp_servers = svc.get_catalogue()
    except Exception:
        pass
    # Read inference settings from config
    inference_cfg = {}
    try:
        import yaml as _yaml
        _hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
        _cfg_path = _hermes_home / "config.yaml"
        if _cfg_path.exists():
            _cfg = _yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}
            _lms = _cfg.get("lmstudio") or {}
            inference_cfg = {
                "n_parallel": _lms.get("n_parallel", 2),
                "server_type": os.environ.get("HERMES_SERVER_TYPE", ""),
                "model": os.environ.get("HERMES_MODEL", ""),
                "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            }
    except Exception:
        pass

    return web.json_response({
        "mcp_servers": mcp_servers,
        "tool_integrations": get_tool_integrations(),
        "inference": inference_cfg,
    })


async def _handle_services_set_key(request: web.Request) -> web.Response:
    """POST /api/services/keys — set a tool credential (admin only)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "error": "env_var and value required"}, status=400)
    from gateway.services import set_credential, get_tool_integrations
    set_credential(env_var, value)
    # Push the new credential to every running sandbox via instance-config
    # so the next chat already has it (sandbox_worker.py applies env from
    # config at startup). Without this, saving e.g. BROWSERLESS_URL only
    # affects the gateway's env — sandboxes wouldn't see the URL until a
    # full destroy+respawn. Best-effort: failures log but don't tank the
    # save, since the credential is already in the DB and the next sandbox
    # spawn (or per-agent toolset toggle) will pick it up too.
    executor = request.app.get("executor")
    pushed = 0
    if executor and hasattr(executor, "refresh_all_instance_configs"):
        try:
            pushed = executor.refresh_all_instance_configs()
        except Exception as exc:
            logger.warning("services_set_key: refresh broadcast failed: %s", exc)
    return web.json_response({
        "ok": True,
        "integrations": get_tool_integrations(),
        "sandboxes_refreshed": pushed,
    })


async def _handle_services_delete_key(request: web.Request) -> web.Response:
    """DELETE /api/services/keys — remove a tool credential (admin only)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    if not env_var:
        return web.json_response({"ok": False, "error": "env_var required"}, status=400)
    from gateway.services import delete_credential, get_tool_integrations
    delete_credential(env_var)
    return web.json_response({"ok": True, "integrations": get_tool_integrations()})


async def _handle_services_inference(request: web.Request) -> web.Response:
    """POST /api/services/inference — save inference server settings (n_parallel, etc.)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    n_parallel = body.get("n_parallel")
    if not isinstance(n_parallel, int) or n_parallel < 1 or n_parallel > 16:
        return web.json_response({"ok": False, "error": "n_parallel must be 1-16"}, status=400)
    # Save to config.yaml
    try:
        import yaml as _yaml
        _hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
        _cfg_path = _hermes_home / "config.yaml"
        _cfg = {}
        if _cfg_path.exists():
            _cfg = _yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}
        _cfg.setdefault("lmstudio", {})["n_parallel"] = n_parallel
        _cfg_path.write_text(_yaml.dump(_cfg, default_flow_style=False, allow_unicode=True))
        logger.info("services: n_parallel set to %d", n_parallel)
        return web.json_response({"ok": True, "n_parallel": n_parallel})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _handle_services_validate_key(request: web.Request) -> web.Response:
    """POST /api/services/validate — test a credential with a real API call."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "message": "env_var and value required"}, status=400)
    from gateway.services import validate_credential
    result = await validate_credential(env_var, value)
    return web.json_response(result)


async def _handle_messaging_catalogue(request: web.Request) -> web.Response:
    """GET /api/services/messaging — list messaging platform integrations."""
    from gateway.services import get_messaging_integrations
    return web.json_response({"messaging": get_messaging_integrations()})


async def _handle_messaging_set_key(request: web.Request) -> web.Response:
    """POST /api/services/messaging/keys — set a messaging token (validate first)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "error": "env_var and value required"}, status=400)

    from gateway.services import MESSAGING_INTEGRATIONS, validate_messaging_credential, set_credential, get_messaging_integrations
    if env_var not in MESSAGING_INTEGRATIONS:
        return web.json_response({"ok": False, "error": f"Unknown platform: {env_var}"}, status=400)

    # Validate before saving
    result = await validate_messaging_credential(env_var, value)
    if not result["ok"]:
        return web.json_response({"ok": False, "error": result["message"]}, status=400)

    set_credential(env_var, value)

    # Trigger platform adapter connect/reconnect
    _ENV_TO_PLATFORM = {
        "TELEGRAM_BOT_TOKEN": Platform.TELEGRAM,
        "DISCORD_BOT_TOKEN": Platform.DISCORD,
        "SLACK_BOT_TOKEN": Platform.SLACK,
        "WHATSAPP_TOKEN": Platform.WHATSAPP,
    }
    platform = _ENV_TO_PLATFORM.get(env_var)
    if platform:
        runner = request.app.get("runner")
        if runner:
            try:
                await runner.connect_platform(platform)
            except Exception as exc:
                logger.warning("Adapter restart for %s failed: %s", env_var, exc)

    return web.json_response({"ok": True, "details": result.get("details", {}), "messaging": get_messaging_integrations()})


async def _handle_messaging_delete_key(request: web.Request) -> web.Response:
    """DELETE /api/services/messaging/keys — remove a messaging token."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    if not env_var:
        return web.json_response({"ok": False, "error": "env_var required"}, status=400)

    from gateway.services import MESSAGING_INTEGRATIONS, delete_credential, get_messaging_integrations
    if env_var not in MESSAGING_INTEGRATIONS:
        return web.json_response({"ok": False, "error": f"Unknown platform: {env_var}"}, status=400)

    delete_credential(env_var)

    # Disconnect platform adapter
    _ENV_TO_PLATFORM = {
        "TELEGRAM_BOT_TOKEN": Platform.TELEGRAM,
        "DISCORD_BOT_TOKEN": Platform.DISCORD,
        "SLACK_BOT_TOKEN": Platform.SLACK,
        "WHATSAPP_TOKEN": Platform.WHATSAPP,
    }
    platform = _ENV_TO_PLATFORM.get(env_var)
    if platform:
        runner = request.app.get("runner")
        if runner:
            try:
                await runner.disconnect_platform(platform)
            except Exception as exc:
                logger.warning("Adapter disconnect for %s failed: %s", env_var, exc)

    return web.json_response({"ok": True, "messaging": get_messaging_integrations()})


async def _handle_messaging_validate(request: web.Request) -> web.Response:
    """POST /api/services/messaging/validate — test a token without saving."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "message": "env_var and value required"}, status=400)

    from gateway.services import validate_messaging_credential
    result = await validate_messaging_credential(env_var, value)
    return web.json_response(result)


async def _handle_sandboxes_list(request: web.Request) -> web.Response:
    """GET /admin/sandboxes — aggregated sandbox status from CLI + workers + state."""
    import shutil
    executor = request.app.get("executor")
    worker_registry = request.app.get("worker_registry")

    cli_available = bool(shutil.which("openshell"))
    sandboxes = []
    policy_summary = {}
    resources = {}

    # 1. Instance records from executor state file
    instance_map = {}
    if executor and hasattr(executor, "list_instances"):
        try:
            for inst in executor.list_instances():
                instance_map[inst.get("sandbox_name", "")] = inst
        except Exception:
            pass

    # 1b. Live sandbox list from the OpenShell CLI — picks up sandboxes
    # that are still provisioning (or were created out of band) and are
    # not yet in the executor state file. Parses the human-readable
    # `openshell sandbox list` table since there is no JSON output flag.
    cli_sandboxes = {}
    if cli_available:
        try:
            import re as _re
            import subprocess as _sp
            _r = _sp.run(["openshell", "sandbox", "list"],
                         capture_output=True, text=True, check=False, timeout=5)
            # Strip ANSI color codes the CLI emits
            ansi = _re.compile(r"\x1b\[[0-9;]*m")
            lines = [ansi.sub("", line) for line in (_r.stdout or "").splitlines() if line.strip()]
            # First line is the header (NAME / NAMESPACE / CREATED / PHASE)
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    # NAME NAMESPACE YYYY-MM-DD HH:MM:SS PHASE
                    name = parts[0]
                    phase = parts[-1]
                    cli_sandboxes[name] = {"name": name, "phase": phase}
        except Exception:
            pass

    # 2. Worker health from registry (every registered worker is a remote
    # OpenShell sandbox — the old in-process "self" worker no longer exists).
    worker_map = {}
    if worker_registry:
        for wid, entry in worker_registry.workers.items():
            worker_map[wid] = entry.to_dict()

    # 3. Merge: instance records + worker health + CLI phase
    seen_sandboxes = set()
    for sandbox_name, inst in instance_map.items():
        worker_id = inst.get("worker_id", sandbox_name)
        worker = worker_map.get(worker_id, {})
        cli = cli_sandboxes.get(sandbox_name, {})
        sandboxes.append({
            "name": inst.get("name", ""),
            "sandbox_name": sandbox_name,
            "worker_id": worker_id,
            "soul": inst.get("soul_name", ""),
            "model": inst.get("model", ""),
            "requester": inst.get("requester", ""),
            "toolsets": inst.get("toolsets", []),
            "policy": inst.get("policy", ""),
            "sandbox_image": inst.get("sandbox_image", ""),
            "created_at": inst.get("created_at", 0),
            "phase": cli.get("phase") or inst.get("phase") or "unknown",
            "worker_status": worker.get("status", "disconnected"),
            "worker_healthy": worker.get("healthy", False),
            "worker_uptime_s": worker.get("uptime_s", 0),
            "current_task_id": worker.get("current_task_id"),
        })
        seen_sandboxes.add(sandbox_name)

    # CLI sandboxes not in instance state (created out of band or
    # in-flight before the state file was written). Cross-reference
    # the worker registry by matching worker_id == sandbox_name so
    # CLI-only sandboxes still pick up live worker health.
    for sandbox_name, cli in cli_sandboxes.items():
        if sandbox_name not in seen_sandboxes:
            worker = worker_map.get(sandbox_name, {})
            # Strip the "hermes-" prefix to derive a friendlier display name
            display_name = sandbox_name[len("hermes-"):] if sandbox_name.startswith("hermes-") else sandbox_name
            sandboxes.append({
                "name": worker.get("instance_label") or display_name,
                "sandbox_name": sandbox_name,
                "worker_id": sandbox_name,
                "soul": worker.get("soul", ""), "model": "", "requester": worker.get("requester", ""),
                "toolsets": worker.get("toolsets", []), "policy": "", "sandbox_image": "",
                "created_at": 0,
                "phase": cli.get("phase") or "unknown",
                "worker_status": worker.get("status", "disconnected"),
                "worker_healthy": worker.get("healthy", False),
                "worker_uptime_s": worker.get("uptime_s", 0),
                "current_task_id": worker.get("current_task_id"),
            })
            seen_sandboxes.add(sandbox_name)

    # Workers not in instance state and not in CLI list (orphaned —
    # e.g. the in-process gateway worker named "hermes")
    for wid, w in worker_map.items():
        if wid not in seen_sandboxes:
            sandboxes.append({
                "name": w.get("instance_label") or wid, "sandbox_name": wid, "worker_id": wid,
                "soul": w.get("soul", ""), "model": "", "requester": w.get("requester", ""),
                "toolsets": w.get("toolsets", []), "policy": "", "sandbox_image": "",
                "created_at": 0,
                "phase": "unknown",
                "worker_status": w.get("status", "idle"),
                "worker_healthy": w.get("healthy", False),
                "worker_uptime_s": w.get("uptime_s", 0),
                "current_task_id": w.get("current_task_id"),
            })

    # 4. Resources
    if executor and hasattr(executor, "get_resources"):
        try:
            resources = executor.get_resources()
        except Exception:
            pass

    # 5. Parse policy YAML
    try:
        _policy_path = pathlib.Path(__file__).parent / "policies" / "openshell_default.yaml"
        if _policy_path.exists():
            import yaml as _yaml
            raw = _yaml.safe_load(_policy_path.read_text(encoding="utf-8")) or {}
            # Build human-readable summary
            net_policies = []
            for key, pol in (raw.get("network_policies") or {}).items():
                endpoints = []
                for ep in (pol.get("endpoints") or []):
                    endpoints.append({
                        "host": ep.get("host", ""), "port": ep.get("port", ""),
                        "protocol": ep.get("protocol", ""), "tls": ep.get("tls", ""),
                        "access": ep.get("access", ""),
                        "allowed_ips": ep.get("allowed_ips", []),
                    })
                net_policies.append({"name": pol.get("name", key), "key": key, "endpoints": endpoints})
            fs = raw.get("filesystem_policy") or {}
            proc = raw.get("process") or {}
            policy_summary = {
                "network": net_policies,
                "filesystem": {
                    "read_only": fs.get("read_only", []),
                    "read_write": fs.get("read_write", []),
                },
                "process": {"user": proc.get("run_as_user", ""), "group": proc.get("run_as_group", "")},
                "binaries": list({b.get("path", "") for pol in (raw.get("network_policies") or {}).values() for b in (pol.get("binaries") or [])}),
            }
    except Exception:
        pass

    return web.json_response({
        "sandboxes": sandboxes,
        "policy": policy_summary,
        "resources": resources,
        "cli_available": cli_available,
    })


async def _handle_sandbox_logs(request: web.Request) -> web.Response:
    """GET /admin/sandboxes/{name}/logs — tail the worker log inside the sandbox.

    The openshell CLI does not have a `sandbox logs` subcommand (only
    create/list/delete/exec/connect/upload/download/ssh-config), so we
    read log files directly via `sandbox exec`.

    Architecture note (Plan A-prime / commit c36c1af): there is NO
    persistent worker process in the current runtime. Each chat
    dispatch spawns a fresh ``python3 /app/sandbox_worker.py``
    subprocess via ``openshell sandbox exec``. The earlier
    implementation looked for ``/tmp/worker.log`` written by an
    ``entrypoint.sh`` that no longer exists, so the Logs button
    silently returned "(no worker log yet)" no matter how busy the
    sandbox was.

    Current source-of-truth log file: ``/tmp/worker.jsonl`` — the
    structured JSON sink that ``sandbox_worker.py`` opens via
    ``logging.FileHandler`` (see docker/sandbox_worker.py:134). It
    accumulates one JSON record per log line across every dispatch,
    so tailing it gives a chronological feed of recent worker
    activity. We tail the last 200 lines."""
    name = request.match_info["name"]
    try:
        from gateway.executors.openshell import _openshell, _load_state
        from gateway.openshell_routes import get_default_gateway_name
        # Look up which gateway this sandbox lives inside. Without
        # gateway scoping, the exec call only checks the CLI default
        # gateway and returns "sandbox not found" for any sandbox
        # that lives in a non-default route.
        fallback_gw = get_default_gateway_name()
        target_gw = fallback_gw
        for inst in _load_state():
            if inst.get("sandbox_name") == name or inst.get("name") == name:
                target_gw = inst.get("openshell_name") or fallback_gw
                break
        if not target_gw:
            return web.json_response(
                {"logs": "", "stderr": "no model routes configured — run /setup first"}
            )
        # Try /tmp/worker.jsonl first (current Plan A-prime source).
        # Fall back to /tmp/worker.log for any legacy/custom image that
        # still uses the old layout. Final fallback is a clear message
        # — better than the silent "(no worker log yet)" the old code
        # produced for every healthy sandbox.
        result = _openshell(
            "sandbox", "exec", "-n", name, "--no-tty", "--",
            "sh", "-c",
            "if [ -s /tmp/worker.jsonl ]; then tail -n 200 /tmp/worker.jsonl; "
            "elif [ -s /tmp/worker.log ]; then tail -n 200 /tmp/worker.log; "
            "else echo '(no dispatches yet — log file is created on first chat to this sandbox)'; fi",
            gateway=target_gw, check=False, timeout=15,
        )
        return web.json_response({"logs": result.stdout or "", "stderr": result.stderr or ""})
    except FileNotFoundError:
        return web.json_response({"logs": "", "stderr": "openshell CLI not available"})
    except Exception as exc:
        return web.json_response({"logs": "", "stderr": str(exc)})


async def _handle_sandbox_restart(request: web.Request) -> web.Response:
    """POST /admin/sandboxes/{name}/restart — destroy and re-spawn.

    Resolution rules (the messy bit):
      * The {name} param is canonically the AGENT name (e.g. "Hermes"),
        which is what the existing dashboard restart button passes via
        sb.name. The chat M dropdown's switchAgentRoute() also passes
        the agent name. We tolerate the sandbox name (e.g.
        "hermes-hermes") as well, in case any caller passes that —
        match it back to the agent record so the rest of the flow has
        the canonical id.
      * The agent record in auth.db is the source of truth for
        model_route_id. The state file's copy of model_route_id is a
        cache that drifts whenever an agent gets rebound (M dropdown
        switch, /admin/agents PATCH, etc.). Reading from the state
        file here would silently respawn into the OLD gateway.
      * Some agents may be missing from the state file entirely
        (stale prune from list_instances() hitting a transient
        openshell CLI error). Don't 404 in that case — fall back to
        building the InstanceConfig from the agent record alone, and
        let the executor's spawn() repopulate state.
    """
    raw_name = request.match_info["name"]
    executor = request.app.get("executor")
    if not executor or not hasattr(executor, "delete_instance"):
        return web.json_response({"ok": False, "error": "No OpenShell executor"}, status=400)

    # ── 1. Resolve the agent record from auth.db ──────────────────
    # Try by name first (the canonical case). If that misses, the
    # caller probably passed the sandbox name (hermes-<sanitized>) —
    # scan agents whose sanitized sandbox name matches.
    from gateway.executors.openshell import _load_state, _sanitize_sandbox_name
    agent = auth_db.get_agent_by_name(raw_name)
    if not agent:
        for a in auth_db.list_agents():
            if _sanitize_sandbox_name(f"hermes-{a.get('name', '')}") == raw_name:
                agent = a
                break
    if not agent:
        return web.json_response(
            {"ok": False, "error": f"No agent matching '{raw_name}' in auth.db"},
            status=404,
        )
    agent_name = agent["name"]

    # ── 2. Find the existing state-file entry (if any) so we know
    # which gateway the sandbox CURRENTLY lives inside. The agent's
    # model_route_id may have just been changed via PATCH /admin/agents,
    # in which case the auth.db value is the NEW gateway and the state
    # file value is the OLD gateway — we want the old one for the delete,
    # and the new one for the spawn.
    instances = _load_state()
    sandbox_name_canonical = _sanitize_sandbox_name(f"hermes-{agent_name}")
    inst = next(
        (i for i in instances
         if i.get("name") == agent_name or i.get("sandbox_name") == sandbox_name_canonical),
        None,
    )

    # ── 3. Delete the existing sandbox (best-effort, never fatal) ──
    # delete_instance() reads the gateway from the state file too;
    # if state is missing it falls back to the user's default model
    # route (get_default_gateway_name), which covers the common single-
    # gateway case. For the multi-gateway case the state file MUST be
    # intact for the delete to find the right gateway — we fix state-
    # file drift in a follow-up; for now the delete tolerates a miss
    # because the spawn step always proceeds anyway.
    #
    # CRITICAL: delete_instance() shells out to `openshell sandbox
    # delete` which is a synchronous subprocess call. If we ran it
    # directly inside this async handler the event loop would be
    # blocked for the duration (~5-10s typical). asyncio.to_thread
    # offloads it so other gateway requests keep flowing. If the
    # state file is missing, delete_instance falls back to the user's
    # default model route for gateway resolution (or skips cleanly if
    # no routes exist yet).
    try:
        await asyncio.to_thread(executor.delete_instance, agent_name)
    except Exception as exc:
        logger.warning("Sandbox restart — delete failed for '%s': %s", agent_name, exc)

    # ── 4. Re-spawn with the agent's CURRENT model_route_id ────────
    # auth.db is the source of truth — the state file's
    # model_route_id is a stale cache. Pulling from auth.db here is
    # what makes the chat M dropdown's "switch model" actually take
    # effect: PATCH agent → POST restart reads the new value here →
    # spawn lands in the new gateway.
    #
    # As of the split-spawn refactor (commit "split spawn into
    # create+upload+exec") executor.spawn() actually returns when
    # the sandbox is provisioned and the worker entrypoint has been
    # detached — typically 10-20s instead of "blocks forever holding
    # the entrypoint SSH session". So awaiting it here gives a
    # bounded latency on the restart endpoint instead of a phantom
    # hang per the pre-refactor behaviour.
    try:
        from gateway.executors.base import InstanceConfig
        config = InstanceConfig(
            name=agent_name,
            soul_name=agent.get("soul_slug") or (inst or {}).get("soul_name") or "general",
            model=agent.get("model") or (inst or {}).get("model") or "",
            requester=(inst or {}).get("requester", ""),
            instance_label=agent_name,
            toolsets=(inst or {}).get("toolsets") or [],
            policy=(inst or {}).get("policy", ""),
            # SOURCE OF TRUTH: auth.db.agents.model_route_id, not the
            # state file's stale cache. This is the load-bearing line
            # for the chat M dropdown's switch-model flow.
            model_route_id=agent.get("model_route_id"),
        )
        spawned = await asyncio.to_thread(executor.spawn, config)
    except Exception as exc:
        logger.exception("Sandbox restart spawn failed for '%s'", agent_name)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    # ── 5. Sandbox is Ready ────────────────────────────────────────
    # Plan A-prime (TASKS.md #24): there's no persistent worker to
    # register — each chat dispatch spawns a fresh ``openshell sandbox
    # exec`` subprocess on-demand. By the time executor.spawn()
    # returns, the sandbox CR is in the Ready phase (we flipped the
    # state file record inside spawn). That's the only health signal
    # we need at restart time; the first chat message will exercise
    # the actual dispatch path.
    worker_registry = request.app.get("worker_registry")
    sandbox_name = spawned.name and _sanitize_sandbox_name(f"hermes-{spawned.name}")
    if worker_registry and sandbox_name:
        entry = worker_registry.get(sandbox_name)
        if not entry or not entry.healthy:
            logger.warning(
                "Sandbox restart: state file entry for '%s' is not "
                "in phase=ready after spawn",
                sandbox_name,
            )
            return web.json_response(
                {
                    "ok": False,
                    "sandbox": spawned.name,
                    "error": (
                        f"Sandbox '{sandbox_name}' was provisioned but its "
                        f"state file entry is not marked ready. Check "
                        f"`logos debug tail` and `openshell sandbox list` "
                        f"for the current phase."
                    ),
                },
                status=504,
            )

    return web.json_response({"ok": True, "sandbox": spawned.name, "worker_registered": True})


# ── Model routes ──────────────────────────────────────────────────────────
# REST CRUD for the model_routes table + the underlying OpenShell
# gateways. Each route is one OpenShell sub-gateway pinned to a single
# (provider, model) pair. See gateway/openshell_routes.py for the
# subprocess wrappers; this layer just exposes them over HTTP and
# handles the JSON shapes the dashboard / curl will use.
#
# All write endpoints offload the openshell CLI calls to a thread via
# asyncio.to_thread because cold-provisioning a new gateway can take
# 30-60 seconds and the aiohttp event loop must not block. The
# response only returns after the operation completes — there's no
# 202-Accepted-then-poll pattern (yet); the UI in commit 5 will show
# a spinner while the request is in flight.

async def _handle_model_routes_list(request: web.Request) -> web.Response:
    """GET /api/admin/model-routes — list every model_routes row.

    Each row carries its current status (provisioning/ready/error/stopped)
    plus a `bound_agents` count so the UI can show how many agents would
    break if the route were destroyed. Read-only — does not call the
    openshell CLI."""
    routes = auth_db.list_model_routes()
    for r in routes:
        r["bound_agents"] = auth_db.count_agents_using_route(r["id"])
    return web.json_response({"routes": routes})


async def _handle_model_routes_post(request: web.Request) -> web.Response:
    """POST /api/admin/model-routes — provision (or reuse) a route.

    Body: ``{provider: str, model: str, set_as_default?: bool}``

    Returns AS SOON as the model_routes row exists in the DB (status
    ``provisioning`` for cold provision, status ``ready`` for the
    fast paths). The slow openshell gateway start runs in a background
    asyncio task and updates the row to ``ready`` (or ``error``) when
    done. The admin UI's 5-second polling refresh on the model-routes
    table picks up the status transition live, so the user sees the
    new row appear immediately and watch it go from provisioning →
    ready without staring at a wedged modal.

    Two resolution paths (in order of speed):

      1. Existing row for (provider, model) → reuse it. Re-pin the
         inference route in a background task so OpenShell state
         matches the DB. Returns immediately.
      2. Cold provision → insert the row sync, return now, finish
         the gateway start in a background task.
    """
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    set_as_default = bool(body.get("set_as_default", False))

    if not provider:
        return web.json_response({"ok": False, "error": "provider is required"}, status=400)
    if not model:
        return web.json_response({"ok": False, "error": "model is required"}, status=400)

    from gateway import openshell_routes as _osr
    import asyncio as _asyncio

    # Path 1: existing row → reuse + re-pin in background
    existing = auth_db.get_model_route_by_provider_model(provider, model)
    if existing:
        async def _re_pin_bg():
            try:
                await _asyncio.to_thread(
                    _osr.provision_or_reuse_route, provider, model, set_as_default,
                )
            except Exception as exc:
                logger.warning(
                    "model-routes re-pin failed for %s/%s: %s",
                    provider, model, exc,
                )
        _asyncio.create_task(_re_pin_bg())
        return web.json_response({"ok": True, "route": existing})

    # Path 2: cold provision — insert row sync, finish in background
    try:
        route = await _asyncio.to_thread(
            _osr.create_route_provisioning_row, provider, model,
        )
    except Exception as exc:
        logger.warning(
            "model-routes row insert failed for %s/%s: %s",
            provider, model, exc,
        )
        return web.json_response(
            {"ok": False, "error": str(exc)}, status=500,
        )

    async def _finish_bg():
        try:
            await _asyncio.to_thread(
                _osr.finish_provisioning, route["id"], set_as_default,
            )
            logger.info(
                "model-routes background provision finished for route %s (%s/%s)",
                route["id"], provider, model,
            )
        except Exception as exc:
            logger.warning(
                "model-routes background provision failed for route %s: %s",
                route["id"], exc,
            )
            # finish_provisioning already updates the row to status='error'
            # with detail before raising, so the admin UI poll will show it.

    _asyncio.create_task(_finish_bg())
    return web.json_response({"ok": True, "route": route})


async def _handle_model_routes_restart(request: web.Request) -> web.Response:
    """POST /api/admin/model-routes/{id}/restart — stop+start the
    underlying OpenShell gateway.

    Useful when the gateway has wedged or after the host reboots without
    an autostart unit. Sandboxes bound to the route will lose their
    WebSocket connection and need to respawn — the worker_registry
    handles that automatically when the sandbox reconnects."""
    route_id = request.match_info["id"]
    if not auth_db.get_model_route(route_id):
        return web.json_response({"ok": False, "error": "route not found"}, status=404)
    try:
        from gateway import openshell_routes as _osr
        import asyncio as _asyncio
        route = await _asyncio.to_thread(_osr.restart_route, route_id)
        return web.json_response({"ok": True, "route": route})
    except Exception as exc:
        logger.warning("model-routes restart failed for %s: %s", route_id, exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _handle_model_routes_set_default(request: web.Request) -> web.Response:
    """POST /api/admin/model-routes/{id}/set-default — promote a route
    to is_default=1, clearing the flag on every other row in one
    transaction. Pure DB op — no openshell CLI involvement."""
    route_id = request.match_info["id"]
    route = auth_db.set_default_model_route(route_id)
    if not route:
        return web.json_response({"ok": False, "error": "route not found"}, status=404)
    return web.json_response({"ok": True, "route": route})


async def _handle_model_routes_refresh(request: web.Request) -> web.Response:
    """POST /api/admin/model-routes/{id}/refresh — re-query OpenShell
    for the route's current status and update the row's status field.

    Used by the admin UI's "refresh" button after a manual openshell
    intervention or when a route is stuck in 'provisioning'. Cheap —
    just one `openshell gateway info` call per request, run in a
    thread so it doesn't block."""
    route_id = request.match_info["id"]
    if not auth_db.get_model_route(route_id):
        return web.json_response({"ok": False, "error": "route not found"}, status=404)
    try:
        from gateway import openshell_routes as _osr
        import asyncio as _asyncio
        route = await _asyncio.to_thread(_osr.refresh_status, route_id)
        return web.json_response({"ok": True, "route": route})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _handle_model_routes_delete(request: web.Request) -> web.Response:
    """DELETE /api/admin/model-routes/{id} — destroy the route + its
    underlying OpenShell gateway.

    Refuses with 409 if:
      * any agents are still bound to the route (caller must re-bind
        or delete those agents first to avoid orphaning their sandboxes)
      * this is the last remaining route (deleting it would leave Logos
        with no way to route inference)

    On success the openshell gateway is destroyed best-effort and the
    DB row is removed. If the openshell CLI call fails (gateway already
    gone, network blip, etc.) the DB row is still dropped so the UI
    doesn't show a phantom route — the underlying state will be cleaned
    up on the next host reboot."""
    route_id = request.match_info["id"]
    try:
        from gateway import openshell_routes as _osr
        import asyncio as _asyncio
        ok = await _asyncio.to_thread(_osr.destroy_route, route_id)
        if not ok:
            return web.json_response({"ok": False, "error": "route not found"}, status=404)
        return web.json_response({"ok": True})
    except RuntimeError as exc:
        # Soft-error path — last-route guard or agents-still-bound.
        # Surface as 409 Conflict so the UI can render the message
        # inline without treating it as a server crash.
        return web.json_response({"ok": False, "error": str(exc)}, status=409)
    except Exception as exc:
        logger.warning("model-routes delete failed for %s: %s", route_id, exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _handle_platforms_list(request: web.Request) -> web.Response:
    """GET /admin/platforms — connection state + routing rules per platform.

    Returns one entry per *configured* platform (enabled or not), with:
      - name, enabled, connected, has_token
      - routing: list of {id, scope, scope_id, agent_id, agent_name}
    """
    runner = request.app.get("runner")
    if not runner:
        return web.json_response({"platforms": []})
    from gateway.auth import db as _auth_db

    # Build agent_id → name lookup once
    agents_by_id = {a["id"]: a.get("name", a["id"]) for a in _auth_db.list_agents()}

    out = []
    for platform, pconfig in runner.config.platforms.items():
        connected = platform in runner.adapters
        rules = []
        try:
            for r in _auth_db.list_platform_routing(platform=platform.value):
                rules.append({
                    "id":         r["id"],
                    "platform":   r["platform"],
                    "scope":      r["scope"],
                    "scope_id":   r["scope_id"],
                    "agent_id":   r["agent_id"],
                    "agent_name": agents_by_id.get(r["agent_id"], r["agent_id"]),
                    "created_at": r["created_at"],
                })
        except Exception:
            logger.exception("list_platform_routing failed for %s", platform.value)
        out.append({
            "name":      platform.value,
            "enabled":   bool(getattr(pconfig, "enabled", False)),
            "connected": connected,
            "routing":   rules,
        })
    return web.json_response({"platforms": out, "agents": [
        {"id": a["id"], "name": a.get("name", a["id"])} for a in _auth_db.list_agents()
    ]})


async def _handle_platforms_routing_upsert(request: web.Request) -> web.Response:
    """POST /admin/platforms/routing — create or update a routing rule.

    Body: {platform, scope, scope_id, agent_id}
    """
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    platform = (body.get("platform") or "").strip()
    scope    = (body.get("scope")    or "").strip()
    scope_id = (body.get("scope_id") or "").strip()
    agent_id = (body.get("agent_id") or "").strip()
    if not platform or not scope or not agent_id:
        return web.json_response(
            {"ok": False, "error": "platform, scope, agent_id required"}, status=400,
        )
    if scope not in ("global", "chat", "user"):
        return web.json_response(
            {"ok": False, "error": "scope must be global|chat|user"}, status=400,
        )
    from gateway.auth import db as _auth_db
    if not _auth_db.get_agent(agent_id):
        return web.json_response({"ok": False, "error": "unknown agent_id"}, status=404)
    try:
        row = _auth_db.upsert_platform_routing(
            platform=platform, scope=scope, scope_id=scope_id, agent_id=agent_id,
        )
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    _auth_db.write_audit_log(
        user.get("sub", ""), "platform_routing_upsert",
        metadata={"platform": platform, "scope": scope, "agent_id": agent_id},
        ip_address=request.remote,
    )
    return web.json_response({"ok": True, "routing": row})


async def _handle_platforms_routing_delete(request: web.Request) -> web.Response:
    """DELETE /admin/platforms/routing/{id} — remove a routing rule."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    rid = request.match_info["id"]
    from gateway.auth import db as _auth_db
    _auth_db.delete_platform_routing(rid)
    _auth_db.write_audit_log(
        user.get("sub", ""), "platform_routing_delete",
        metadata={"id": rid}, ip_address=request.remote,
    )
    return web.json_response({"ok": True})


async def _handle_setup_page(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if is_setup_completed():
        raise web.HTTPFound("/")
    html = _SETUP_HTML.replace("__VERSION_LABEL__", _VERSION_LABEL).replace("__SETUP_TS__", _SERVER_START_TS)
    return web.Response(text=html, content_type="text/html")


async def _handle_setup_status(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    return web.json_response({"completed": is_setup_completed()})


async def _handle_setup_reset(request: web.Request) -> web.Response:
    from gateway.auth.db import reset_setup_completed, write_audit_log
    user_id = request["current_user"]["sub"]
    reset_setup_completed()
    write_audit_log(user_id, "setup_reset", ip_address=request.remote)
    return web.json_response({"ok": True})


async def _handle_setup_wipe(request: web.Request) -> web.Response:
    """POST /api/setup/wipe — destructive factory reset.

    True first-install simulation: wipes everything user-scoped so the
    next visit goes through the full /setup wizard including the admin
    account-creation step.

    Wipes:
      - Every agent (DB row + on-disk ~/.logos/agents/<name>/ via
        delete_agent's filesystem hook + OpenShell sandbox via the
        executor's destroy path)
      - Every model route (OpenShell gateway destroyed when reachable;
        DB row removed unconditionally)
      - Dispatch ledger, agent_runs, evolution_proposals, audit log
      - All users (caller is logging themselves out — the wizard's
        admin-create step will mint the next admin account)
      - Auth session cookie (force a clean re-auth flow)
      - The setup_completed flag (so /setup re-prompts on next load)

    Deliberately KEEPS:
      - Souls / skills / themes (platform fixtures, not user data)
      - Action policies / routing policies (the wizard re-seeds basics)

    Body must contain {"confirm": "WIPE"} — the literal string. The
    UI's type-to-confirm modal sends exactly that, so a stray POST
    can't accidentally nuke a deployment.
    """
    from gateway.auth.db import (
        list_agents, delete_agent, list_model_routes, write_audit_log,
        reset_setup_completed, _conn,
    )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if (body.get("confirm") or "") != "WIPE":
        return web.json_response(
            {"error": "confirmation_missing", "detail": "Body must contain {\"confirm\": \"WIPE\"}."},
            status=400,
        )

    user_id = request["current_user"]["sub"]
    counts = {"agents": 0, "model_routes": 0, "users": 0}

    # Delete all agents — delete_agent() already wipes ~/.logos/agents/<name>/
    # (sessions, memories, logs) and cleans dispatches/agent_runs/proposals
    # for that agent. Also tries to destroy the OpenShell sandbox via the
    # executor when one exists.
    executor = request.app.get("executor")
    for a in list_agents():
        try:
            if executor:
                try:
                    executor.delete_instance(a["name"])
                except Exception as exc:
                    logger.warning("wipe: executor delete failed for %s: %s", a["name"], exc)
            delete_agent(a["id"])
            counts["agents"] += 1
        except Exception as exc:
            logger.warning("wipe: failed to delete agent %s (%s): %s", a.get("name"), a["id"], exc)

    # Destroy each model route's underlying OpenShell gateway, then nuke
    # the DB row. We bypass openshell_routes.destroy_route's "must keep
    # one route" guard because the whole point of factory reset is to
    # land on zero routes.
    try:
        from gateway import openshell_routes as _osr
        for r in list_model_routes():
            try:
                gw_name = r.get("openshell_name")
                if gw_name:
                    try:
                        _osr._run_openshell("gateway", "destroy", "--force",
                                            gateway=gw_name, check=False, timeout=120)
                    except Exception as exc:
                        logger.warning("wipe: openshell destroy %s failed: %s", gw_name, exc)
                with _conn() as conn:
                    conn.execute("DELETE FROM model_routes WHERE id = ?", (r["id"],))
                counts["model_routes"] += 1
            except Exception as exc:
                logger.warning("wipe: failed to remove route %s: %s", r.get("id"), exc)
    except Exception as exc:
        logger.warning("wipe: openshell route cleanup failed: %s", exc)

    # Audit log BEFORE we truncate it + wipe the user (FK on user_id).
    # Logged at warn level since this is a high-impact action.
    write_audit_log(user_id, "setup_wipe", metadata=counts, ip_address=request.remote)

    # Truncate orphan tables that aren't tied to a specific agent.
    with _conn() as conn:
        for tbl in ("dispatches", "agent_runs", "evolution_proposals", "audit_log"):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass  # missing table is fine

        # Wipe ALL users — this is true factory reset, not just data.
        # The next /setup visit will mint a fresh admin account from
        # the wizard's user-creation step. Caller's session cookie is
        # cleared on the response below so the redirect lands on /setup
        # without an auth loop.
        try:
            cur = conn.execute("DELETE FROM users")
            counts["users"] = cur.rowcount
        except Exception as exc:
            logger.warning("wipe: user delete failed: %s", exc)

    reset_setup_completed()
    logger.warning("setup wipe by user_id=%s: deleted %d agent(s), %d model route(s), %d user(s)",
                   user_id, counts["agents"], counts["model_routes"], counts["users"])
    resp = web.json_response({"ok": True, "deleted": counts})
    # Strip the session cookies so the browser can't keep using a token
    # whose user no longer exists — clear_auth_cookies handles all three
    # (access_token / refresh_token / csrf_token) with the right paths.
    from gateway.auth.tokens import clear_auth_cookies
    clear_auth_cookies(resp)
    return resp


async def _handle_index(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if not is_setup_completed():
        raise web.HTTPFound("/login")
    inject = f'<script>window.__LOGOS__={{isCanary:{str(_IS_CANARY).lower()},runtimeMode:"openshell",version:"{_VERSION_LABEL}"}};window._hueEpochMs={_HUE_EPOCH_MS};</script>'
    html = _ADMIN_HTML.replace("</head>", inject + "</head>", 1)
    return web.Response(text=html, content_type="text/html")


async def _handle_login_page(request: web.Request) -> web.Response:
    html = _LOGIN_HTML.replace("__VERSION_LABEL__", _VERSION_LABEL)
    return web.Response(text=html, content_type="text/html")


async def _handle_log_tail(request: web.Request) -> web.Response:
    """Return the last N lines of the gateway log file.

    GET /api/logs?n=200&file=gateway   (file: gateway|errors)
    Requires view_audit_logs permission (admin/operator).
    """
    n = min(int(request.query.get("n", 200)), 2000)
    fname = request.query.get("file", "gateway")
    if fname not in ("gateway", "errors"):
        return web.json_response({"error": "invalid file"}, status=400)
    log_path = _hermes_home / "logs" / f"{fname}.log"
    try:
        if not log_path.exists():
            return web.json_response({"lines": [], "path": str(log_path), "exists": False})
        # Read last N lines efficiently without loading the whole file
        lines: list[str] = []
        with open(log_path, "rb") as fh:
            # Seek backwards in chunks to find the last N newlines
            chunk = 1024 * 32
            fh.seek(0, 2)  # end
            size = fh.tell()
            buf = b""
            pos = size
            while len(lines) < n + 1 and pos > 0:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
                lines = buf.split(b"\n")
        lines = [l.decode("utf-8", errors="replace") for l in lines[-n:] if l]
        return web.json_response({"lines": lines, "path": str(log_path), "exists": True, "total_bytes": log_path.stat().st_size})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _handle_status(request: web.Request) -> web.Response:
    runner: Any = request.app["runner"]
    uptime = int(time.time() - _start_time)
    now = time.time()

    active = []
    for session_key, s in list(runner._session_status.items()):
        tool_started = s.get("tool_started_at") or now
        session_started = s.get("session_started_at") or now

        # Pull live token counts from the running agent if available
        agent = runner._running_agents.get(session_key)
        prompt_tokens = 0
        completion_tokens = 0
        api_calls = 0
        if agent is not None:
            prompt_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
            completion_tokens = getattr(agent, "session_completion_tokens", 0) or 0
            api_calls = getattr(agent, "session_api_calls", 0) or 0

        active.append({
            "session_key": session_key,
            "platform": s.get("platform", "unknown"),
            "current_tool": s.get("current_tool", "unknown"),
            "elapsed_s": int(now - tool_started),
            "tool_started_at": tool_started,
            "tool_count": s.get("tool_count", 0),
            "error_count": s.get("error_count", 0),
            "recent_tools": s.get("recent_tools", []),
            "stuck": s.get("stuck", False),
            "session_started_at": session_started,
            "elapsed_session_s": int(now - session_started),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "api_calls": api_calls,
        })

    # Recent completed sessions (ring buffer, newest last)
    recent = list(getattr(runner, "_recent_sessions", []))

    cpu_percent = None
    mem_mb = None
    try:
        import psutil as _psutil
        _proc = _psutil.Process()
        cpu_percent = round(_proc.cpu_percent(interval=None), 1)
        mem_mb = int(_proc.memory_info().rss / 1024 / 1024)
    except Exception:
        pass

    current_model = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or ""

    return web.json_response({
        "status": "ok",
        "uptime_s": uptime,
        "instance_name": _INSTANCE_NAME,
        "active_sessions": active,
        "recent_sessions": recent,
        "current_model": current_model,
        "cpu_percent": cpu_percent,
        "mem_mb": mem_mb,
    })


async def _handle_model_patch(request: web.Request) -> web.Response:
    """PATCH /api/model — change the active model at runtime and persist to config.yaml.

    Also resolves the correct provider: if the model belongs to a cloud
    provider, activate that provider.  If it belongs to a local machine,
    switch provider to the local endpoint so requests don't get routed
    to the wrong backend (e.g. sending a qwen model to Anthropic).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    new_model = (body.get("model") or "").strip()
    if not new_model:
        return web.json_response({"error": "model required"}, status=400)
    _hermes_home = pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".logos"))
    _config_path = _hermes_home / "config.yaml"
    try:
        import yaml as _yaml
        _cfg: dict = {}
        if _config_path.exists():
            with open(_config_path, encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
        os.environ["HERMES_MODEL"] = new_model
        # Write model to both locations for compatibility
        _cfg["HERMES_MODEL"] = new_model
        if "model" not in _cfg or not isinstance(_cfg.get("model"), dict):
            _cfg["model"] = {}
        _cfg["model"]["default"] = new_model

        # Resolve the correct provider for this model
        resolved_provider = None
        # Check if model matches any cloud provider's active_model
        try:
            from gateway.auth import db as _adb
            cloud_provs = _adb.list_cloud_providers()
            for cp in cloud_provs:
                if cp.get("active_model") == new_model:
                    resolved_provider = cp.get("provider")  # e.g. "anthropic", "openrouter"
                    # Also update endpoint env vars for this provider
                    from gateway.setup_handlers import _FRONTIER_PROVIDERS
                    frov = _FRONTIER_PROVIDERS.get(resolved_provider, {})
                    _base_url = cp.get("base_url") or frov.get("base_url", "")
                    _api_key = cp.get("api_key") or ""
                    os.environ["OPENAI_API_KEY"] = _api_key
                    os.environ["OPENAI_BASE_URL"] = _base_url
                    if frov.get("server_type"):
                        os.environ["HERMES_SERVER_TYPE"] = frov["server_type"]
                        _cfg["HERMES_SERVER_TYPE"] = frov["server_type"]
                    break
        except Exception:
            pass

        if not resolved_provider:
            # Model is local — use local machine endpoint
            try:
                from gateway.auth import db as _adb
                machines = _adb.list_machines()
                for m in machines:
                    if m.get("enabled") and m.get("endpoint_url"):
                        # Use the first enabled local machine
                        _base_url = m["endpoint_url"].rstrip("/")
                        os.environ["OPENAI_BASE_URL"] = _base_url
                        os.environ["OPENAI_API_KEY"] = m.get("api_key") or "not-needed"
                        os.environ["HERMES_SERVER_TYPE"] = "lmstudio"
                        _cfg["OPENAI_BASE_URL"] = _base_url
                        _cfg["HERMES_SERVER_TYPE"] = "lmstudio"
                        resolved_provider = "openai"
                        break
            except Exception:
                pass

        # Update config.yaml provider
        if resolved_provider:
            _cfg.setdefault("model", {})
            if isinstance(_cfg["model"], str):
                _cfg["model"] = {"default": _cfg["model"]}
            _cfg["model"]["provider"] = resolved_provider

        with open(_config_path, "w", encoding="utf-8") as _f:
            _yaml.dump(_cfg, _f, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "model": new_model})


async def _handle_toolsets(request: web.Request) -> web.Response:
    """Return available toolsets and per-tool availability for the current install."""
    try:
        from core.model_tools import check_tool_availability
        from tools.registry import registry
        available_ts, unavailable_info = check_tool_availability(quiet=True)
        ts_meta = registry.get_available_toolsets()
        # Enrich with description from core/toolsets.py TOOLSET_REGISTRY
        try:
            from core.toolsets import TOOLSET_REGISTRY
            for name, meta in ts_meta.items():
                reg_entry = TOOLSET_REGISTRY.get(name, {})
                meta["description"] = reg_entry.get("description", "")
                meta["tools"] = reg_entry.get("tools", meta.get("tools", []))
        except Exception:
            pass
        # Include which toolsets are currently enabled in config
        try:
            from logos_cli.config import load_config
            cfg = load_config()
            enabled = cfg.get("toolsets", ["hermes-cli"])
            # Resolve the enabled toolset(s) to individual toolset names
            from core.toolsets import resolve_toolset
            enabled_tools = set()
            for ts_name in (enabled if isinstance(enabled, list) else [enabled]):
                try:
                    enabled_tools.update(resolve_toolset(ts_name))
                except Exception:
                    pass
        except Exception:
            enabled = ["hermes-cli"]
            enabled_tools = set()
        return web.json_response({
            "available": sorted(available_ts),
            "toolsets": ts_meta,
            "unavailable": unavailable_info,
            "enabled_toolsets": enabled if isinstance(enabled, list) else [enabled],
            "enabled_tools": sorted(enabled_tools),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@require_csrf
async def _handle_toolsets_toggle(request: web.Request) -> web.Response:
    """POST /api/toolsets/toggle — enable or disable a toolset in the active config.

    Body: { "toolset": "knowledge", "enabled": true }

    Updates the config.yaml toolsets list. The change takes effect on the next
    agent session (existing sessions keep their current toolset).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    toolset_name = (body.get("toolset") or "").strip()
    enabled = body.get("enabled", True)

    if not toolset_name:
        return web.json_response({"error": "toolset is required"}, status=400)

    import yaml as _yaml
    _home = pathlib.Path(
        os.environ.get("LOGOS_HOME")
        or os.environ.get("HERMES_HOME")
        or str(pathlib.Path.home() / ".logos")
    )
    _config_path = _home / "config.yaml"

    try:
        _cfg: dict = {}
        if _config_path.exists():
            with open(_config_path, encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}

        current = _cfg.get("toolsets", ["hermes-cli"])
        if not isinstance(current, list):
            current = [current]

        if enabled and toolset_name not in current:
            current.append(toolset_name)
        elif not enabled and toolset_name in current:
            current.remove(toolset_name)

        _cfg["toolsets"] = current

        with open(_config_path, "w", encoding="utf-8") as _f:
            _yaml.dump(_cfg, _f, default_flow_style=False, sort_keys=False)

    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response({"ok": True, "toolsets": current})


async def _handle_canary_status(request: web.Request) -> web.Response:
    """Check if the canary pod is alive by probing its in-cluster health endpoint."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _CANARY_HEALTH_URL,
                timeout=aiohttp.ClientTimeout(total=2),
            ) as r:
                return web.json_response({"active": r.status < 400})
    except Exception:
        return web.json_response({"active": False})


async def _handle_proxy_state(request: web.Request) -> web.Response:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_AI_ROUTER_BASE}/admin/state",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                from gateway import admin_handlers
                routes = data.get("routes", {})
                data["route_model_classes"] = {
                    alias: admin_handlers.ALIAS_TO_CLASS.get(alias, "general")
                    for alias in routes
                }
                return web.json_response(data)
    except Exception as e:
        return web.json_response({
            "providers": {},
            "routes": {},
            "route_model_classes": {},
            "grafana_url": "http://192.168.1.253:3200",
            "_error": str(e),
        })


@require_permission("manage_platform")
@require_csrf
async def _handle_proxy_toggle(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_AI_ROUTER_BASE}/admin/providers/{key}/toggle",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                return web.json_response(data)
    except Exception as e:
        raise web.HTTPBadGateway(reason=str(e))


async def _handle_proxy_models_live(request: web.Request) -> web.Response:
    """GET /proxy/models-live — proxy to ai-router /admin/models-live."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_AI_ROUTER_BASE}/admin/models-live",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                return web.json_response(await r.json())
    except Exception as e:
        return web.json_response({"providers": {}, "_error": str(e)})


@require_permission("manage_machines")
@require_csrf
async def _handle_proxy_benchmark(request: web.Request) -> web.Response:
    """POST /proxy/benchmark — proxy to ai-router /admin/benchmark."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_AI_ROUTER_BASE}/admin/benchmark",
                json=body,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                return web.json_response(await r.json())
    except Exception as e:
        raise web.HTTPBadGateway(reason=str(e))


async def _handle_routing_claims(request: web.Request) -> web.Response:
    """GET /internal/routing/claims — full machine→user claim map for the MCP routing tool."""
    claims = auth_db.list_all_claims()
    machines = auth_db.list_machines()
    users = auth_db.list_users(limit=500)
    return web.json_response({
        "claims": claims,
        "machines": machines,
        "users": [{"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                   "email": u["email"], "policy_id": u.get("policy_id")} for u in users],
    })


async def _handle_routing_apply(request: web.Request) -> web.Response:
    """POST /internal/routing/apply — Hermes MCP tool applies a suggested profile.
    Body: {"user_id": str, "policy_name": str, "description": str, "rules": [...], "fallback": str}
    """
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid_json")

    user_id = body.get("user_id")
    policy_name = body.get("policy_name")
    rules = body.get("rules", [])
    description = body.get("description", "Auto-configured by Hermes")
    fallback = body.get("fallback", "any_available")

    if not user_id or not policy_name:
        raise web.HTTPBadRequest(reason="user_id and policy_name required")

    user = auth_db.get_user_by_id(user_id)
    if not user:
        raise web.HTTPNotFound(reason="user_not_found")

    # Create or reuse policy with this name
    existing = next((p for p in auth_db.list_policies() if p["name"] == policy_name), None)
    if existing:
        policy = auth_db.update_policy(existing["id"], description=description, fallback=fallback)
        pid = existing["id"]
    else:
        policy = auth_db.create_policy(policy_name, description=description, fallback=fallback)
        pid = policy["id"]

    auth_db.set_policy_rules(pid, rules)
    auth_db.assign_user_policy(user_id, pid)

    return web.json_response({"ok": True, "policy": auth_db.get_policy(pid),
                              "rules": auth_db.get_policy_rules(pid)})


async def _handle_souls_get(request: web.Request) -> web.Response:
    registry = _get_soul_registry()
    return web.json_response({"souls": [s.to_dict() for s in registry.values()]})


async def _handle_soul_detail(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    registry = _get_soul_registry()
    soul = registry.get(slug)
    if not soul:
        raise web.HTTPNotFound(reason=f"soul not found: {slug}")
    return web.json_response(soul.to_dict(include_soul_md=True))


async def _handle_instances_get(request: web.Request) -> web.Response:
    executor = request.app["executor"]
    loop = asyncio.get_event_loop()
    try:
        res = await loop.run_in_executor(None, executor.get_resources)
    except Exception as e:
        res = {"_error": str(e)}
    try:
        inst = await loop.run_in_executor(None, executor.list_instances)
    except Exception as e:
        inst = []
        if "_error" not in res:
            res = {"_error": str(e)}
    caller = request.get("current_user") or {}
    caller_role = caller.get("role", "viewer")
    caller_name = (caller.get("display_name") or caller.get("username") or "").lower()
    # Non-admins only see instances spawned for themselves
    if caller_role not in ("admin", "operator"):
        inst = [i for i in inst if i.get("requester", "").lower() == caller_name]
    return web.json_response({
        "instances": inst,
        "resources": res,
        "queue": _instance_queue,
    })


@require_csrf
async def _handle_instances_post(request: web.Request) -> web.Response:
    caller = request.get("current_user") or {}
    caller_role = caller.get("role", "viewer")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    requester = (body.get("requester") or "").strip()
    if not requester:
        return web.json_response({"error": "requester is required"}, status=400)

    soul_slug = (body.get("soul_slug") or "general").strip()
    tool_overrides = body.get("tool_overrides") or {}
    model_alias = (body.get("model_alias") or "balanced").strip()
    machine_id_override = body.get("machine_id") or None
    instance_label = (body.get("instance_label") or "").strip()

    # Validate instance_label: lowercase alphanumeric + hyphens, max 32 chars
    if instance_label:
        import re as _re
        sanitised = _re.sub(r"[^a-z0-9-]", "", instance_label.lower())
        if sanitised != instance_label or len(instance_label) > 32:
            return web.json_response(
                {"error": "invalid_label",
                 "message": "Instance label must be lowercase alphanumeric with hyphens, max 32 chars"},
                status=400,
            )

    # Validate soul and overrides before checking resources
    registry = _get_soul_registry()
    soul = registry.get(soul_slug)
    if not soul:
        return web.json_response(
            {"error": "soul_not_found", "soul_slug": soul_slug},
            status=400,
        )

    # RBAC: check if caller can spawn this soul
    if not can_spawn(caller_role, soul.to_dict()):
        return web.json_response(
            {"error": "forbidden", "message": "You don't have permission to spawn this soul"},
            status=403,
        )

    from gateway.auth.rbac import has_permission as _has_perm

    # RBAC: machine routing override requires override_routing permission
    if machine_id_override and not _has_perm(caller_role, "override_routing"):
        return web.json_response(
            {"error": "forbidden", "message": "Machine routing override requires operator or admin role"},
            status=403,
        )

    # RBAC: toolset overrides require override_toolsets permission
    if tool_overrides and not _has_perm(caller_role, "override_toolsets"):
        return web.json_response(
            {"error": "forbidden", "message": "Toolset overrides require operator or admin role"},
            status=403,
        )

    try:
        _validate_soul_overrides(soul, tool_overrides)
    except ValueError as exc:
        code, _, detail = str(exc).partition(":")
        messages = {
            "cannot_remove_enforced": f"toolset '{detail}' is enforced by soul '{soul_slug}' and cannot be removed",
            "toolset_not_available": f"toolset '{detail}' is forbidden by soul '{soul_slug}'",
            "toolset_not_in_soul": f"toolset '{detail}' is not in the optional list for soul '{soul_slug}'",
        }
        return web.json_response(
            {"error": code, "message": messages.get(code, str(exc)), "toolset": detail},
            status=400,
        )

    # Resolve routing — must happen before spawn so we can pin the machine
    caller_id = caller.get("sub")
    try:
        route = await admin_handlers.resolve_route(
            user_id=caller_id,
            model_alias=model_alias,
            machine_id_override=machine_id_override,
        )
    except admin_handlers.RoutingError as exc:
        return web.json_response(
            {"error": "routing_failed", "message": str(exc), "profile": exc.profile_name},
            status=503,
        )

    resolved_machine   = route["machine"]
    resolved_endpoint  = resolved_machine["endpoint_url"] if resolved_machine else None
    resolved_machine_name = resolved_machine["name"] if resolved_machine else None
    resolved_machine_id   = resolved_machine["id"]   if resolved_machine else None
    logger.info(
        "routing resolved: user=%s model=%s layer=%s machine=%s",
        caller_id, model_alias, route["layer"],
        resolved_machine_name or "none",
    )

    loop = asyncio.get_event_loop()
    executor = request.app["executor"]

    # Check resources via executor
    try:
        headroom = await loop.run_in_executor(None, executor.get_headroom)
        can_spawn_now = headroom.can_spawn
        headroom_reason = headroom.reason
    except Exception as e:
        can_spawn_now = False
        headroom_reason = f"executor unavailable: {e}"

    if not can_spawn_now:
        _instance_queue.append({"requester": requester, "soul_slug": soul_slug, "instance_label": instance_label or soul_slug, "reason": headroom_reason, "requested_at": time.time()})
        logger.info("Instance request queued for %s: %s", requester, headroom_reason)
        return web.json_response({"status": "queued", "requester": requester, "reason": headroom_reason})

    # Per-user instance limit
    max_instances = 5
    try:
        existing = await loop.run_in_executor(None, executor.list_instances)
        user_instances = [i for i in existing if (i.get("requester") or "").lower() == requester.lower()]
        if len(user_instances) >= max_instances:
            return web.json_response({
                "error": "instance_limit",
                "message": f"You already have {len(user_instances)} instances (limit: {max_instances}). Delete one before spawning another.",
            }, status=400)
    except Exception:
        pass  # don't block spawn if list_instances fails

    try:
        from gateway.executors.base import InstanceConfig as _IC
        # Default label to soul slug so each soul gets a distinct instance
        effective_label = instance_label or soul_slug
        _ic = _IC(
            name=_safe_k8s_name(requester, effective_label),
            soul_name=soul_slug,
            model=model_alias,
            requester=requester,
            instance_label=effective_label,
            tool_overrides=tool_overrides or {},
            machine_endpoint=resolved_endpoint,
            machine_name=resolved_machine_name,
            machine_id=resolved_machine_id,
        )
        spawned = await loop.run_in_executor(None, executor.spawn, _ic)
        is_exists = spawned.url == "" and not spawned.healthy
        result = {
            "status": "exists" if is_exists else "created",
            "name": spawned.name,
            "instance_name": spawned.soul_name,
            "instance_label": effective_label,
            "soul": {"slug": spawned.soul_name, "name": spawned.soul_name},
        }
        if is_exists:
            result["message"] = f"An instance named '{effective_label}' already exists for {requester}. Choose a different name or delete the existing one."
    except Exception as e:
        logger.exception("Failed to spawn instance for %s", requester)
        return web.json_response({"error": "spawn_failed", "message": str(e)}, status=500)

    # Log routing decision
    auth_db.log_routing_decision(
        user_id=caller_id,
        model_alias=model_alias,
        model_class=route["model_class"],
        machine_id=resolved_machine_id,
        machine_name=resolved_machine_name,
        layer=route["layer"],
        instance_name=f"Hermes for {requester}",
    )

    # Audit: who spawned what
    auth_db.write_audit_log(
        caller.get("sub"), "spawn_instance",
        target_type="instance", target_id=requester,
        metadata={
            "soul_slug": soul_slug,
            "requester": requester,
            "model_alias": model_alias,
            "machine": resolved_machine_name,
            "routing_layer": route["layer"],
        },
        ip_address=request.remote,
    )

    # Try to resolve NodePort / URL (may take a moment to assign)
    await asyncio.sleep(1)
    try:
        instances = await loop.run_in_executor(None, executor.list_instances)
        dep_name = _ic.name
        match = next((i for i in instances if i["name"] == dep_name), {})
        result["node_port"] = match.get("node_port")
        result["instance_name"] = match.get("instance_name", f"Hermes for {requester}")
    except Exception:
        pass

    return web.json_response(result)


@require_permission("delete_instance")
@require_csrf
async def _handle_instances_delete(request: web.Request) -> web.Response:
    name   = request.match_info["name"]
    caller = request.get("current_user") or {}
    if name == "hermes":
        raise web.HTTPForbidden(reason="Cannot delete the primary hermes deployment")
    loop = asyncio.get_event_loop()
    executor = request.app["executor"]
    try:
        await loop.run_in_executor(None, executor.delete_instance, name)
    except Exception as e:
        raise web.HTTPInternalServerError(reason=str(e))
    auth_db.write_audit_log(
        caller.get("sub"), "delete_instance",
        target_type="instance", target_id=name,
        ip_address=request.remote,
    )
    return web.json_response({"status": "deleted", "name": name})


# ── Instance management API (memory, knowledge, config) ─────────────────────

def _instance_home(name: str) -> Path:
    """Resolve the HERMES_HOME directory for a named instance.

    The primary gateway agent ('hermes') uses _hermes_home directly.
    Spawned instances live under _hermes_home/instances/{name}/.
    """
    if name == "hermes":
        return _hermes_home
    return _hermes_home / "instances" / name


@require_permission("view_instances")
async def _handle_instance_memory_get(request: web.Request) -> web.Response:
    """GET /instances/{name}/memory — read all memory files for an instance."""
    name = request.match_info["name"]
    home = _instance_home(name)
    memories_dir = home / "memories"
    shared_dir = _hermes_home / "shared"

    def _read_safe(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            return ""

    return web.json_response({
        "instance": name,
        "memory": _read_safe(memories_dir / "MEMORY.md"),
        "user_profile": _read_safe(shared_dir / "USER.md"),
        "bug_notes": _read_safe(home / "bug_notes.md"),
    })


@require_permission("view_instances")
@require_csrf
async def _handle_instance_memory_put(request: web.Request) -> web.Response:
    """PUT /instances/{name}/memory — update a memory target for an instance."""
    name = request.match_info["name"]
    home = _instance_home(name)
    memories_dir = home / "memories"
    shared_dir = _hermes_home / "shared"

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    target = body.get("target", "")
    content = body.get("content", "")

    if target == "memory":
        memories_dir.mkdir(parents=True, exist_ok=True)
        (memories_dir / "MEMORY.md").write_text(content, encoding="utf-8")
    elif target == "user_profile":
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / "USER.md").write_text(content, encoding="utf-8")
    elif target == "bug_notes":
        home.mkdir(parents=True, exist_ok=True)
        (home / "bug_notes.md").write_text(content, encoding="utf-8")
    else:
        return web.json_response(
            {"error": "invalid_target", "message": "Target must be: memory, user_profile, bug_notes"},
            status=400,
        )

    return web.json_response({"status": "updated", "target": target, "instance": name})


@require_permission("view_instances")
async def _handle_instance_knowledge_get(request: web.Request) -> web.Response:
    """GET /instances/{name}/knowledge — list sources and stats."""
    name = request.match_info["name"]
    home = _instance_home(name)

    from tools.knowledge_store import KnowledgeStore
    store = KnowledgeStore(knowledge_dir=home / "knowledge")
    sources = store.list_sources()
    stats = store.stats()

    return web.json_response({
        "instance": name,
        "sources": sources.get("sources", []),
        "stats": stats,
    })


@require_permission("view_instances")
@require_csrf
async def _handle_instance_knowledge_ingest(request: web.Request) -> web.Response:
    """POST /instances/{name}/knowledge/ingest — ingest a document."""
    name = request.match_info["name"]
    home = _instance_home(name)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    source_name = (body.get("source_name") or "").strip()
    content = body.get("content", "")

    if not source_name:
        return web.json_response({"error": "source_name is required"}, status=400)
    if not content:
        return web.json_response({"error": "content is required"}, status=400)

    from tools.knowledge_store import KnowledgeStore
    try:
        from logos_cli.config import load_config
        cfg = load_config().get("knowledge", {})
    except Exception:
        cfg = {}

    store = KnowledgeStore(
        knowledge_dir=home / "knowledge",
        embedding_model=cfg.get("embedding_model", "nomic-embed-text"),
        embedding_endpoint=cfg.get("embedding_endpoint"),
        chunk_size=cfg.get("chunk_size", 512),
        chunk_overlap=cfg.get("chunk_overlap", 64),
        max_chunks=cfg.get("max_chunks", 10_000),
    )
    result = store.ingest(content, source_name=source_name, source_type="upload")

    status_code = 200 if result.get("success") else 400
    return web.json_response(result, status=status_code)


@require_permission("view_instances")
@require_csrf
async def _handle_instance_knowledge_delete(request: web.Request) -> web.Response:
    """DELETE /instances/{name}/knowledge/{source} — remove a knowledge source."""
    name = request.match_info["name"]
    source = request.match_info["source"]
    home = _instance_home(name)

    from tools.knowledge_store import KnowledgeStore
    store = KnowledgeStore(knowledge_dir=home / "knowledge")
    result = store.remove_source(source)

    status_code = 200 if result.get("success") else 404
    return web.json_response(result, status=status_code)


@require_permission("view_instances")
async def _handle_instance_knowledge_search(request: web.Request) -> web.Response:
    """GET /instances/{name}/knowledge/search?q=... — semantic search preview."""
    name = request.match_info["name"]
    query = request.query.get("q", "").strip()
    home = _instance_home(name)

    if not query:
        return web.json_response({"error": "query parameter 'q' is required"}, status=400)

    from tools.knowledge_store import KnowledgeStore
    try:
        from logos_cli.config import load_config
        cfg = load_config().get("knowledge", {})
    except Exception:
        cfg = {}

    store = KnowledgeStore(
        knowledge_dir=home / "knowledge",
        embedding_model=cfg.get("embedding_model", "nomic-embed-text"),
        embedding_endpoint=cfg.get("embedding_endpoint"),
    )
    result = store.search(query, top_k=int(request.query.get("top_k", "5")))
    return web.json_response(result)


@require_permission("view_instances")
@require_csrf
async def _handle_instance_fork(request: web.Request) -> web.Response:
    """POST /instances/{name}/fork — copy memory and/or knowledge to another instance.

    Body: { "target_instance": "hermes-greg-coder", "copy_memory": true, "copy_knowledge": true }
    """
    source_name = request.match_info["name"]
    source_home = _instance_home(source_name)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    target_name = (body.get("target_instance") or "").strip()
    copy_memory = body.get("copy_memory", True)
    copy_knowledge = body.get("copy_knowledge", True)

    if not target_name:
        return web.json_response({"error": "target_instance is required"}, status=400)
    if target_name == source_name:
        return web.json_response({"error": "Cannot fork an instance onto itself"}, status=400)

    target_home = _instance_home(target_name)
    copied = []

    import shutil

    # Copy MEMORY.md
    if copy_memory:
        src_mem = source_home / "memories" / "MEMORY.md"
        if src_mem.exists():
            tgt_mem_dir = target_home / "memories"
            tgt_mem_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_mem), str(tgt_mem_dir / "MEMORY.md"))
            copied.append("MEMORY.md")

    # Copy knowledge base
    if copy_knowledge:
        src_knowledge = source_home / "knowledge"
        if src_knowledge.exists() and any(src_knowledge.iterdir()):
            tgt_knowledge = target_home / "knowledge"
            if tgt_knowledge.exists():
                shutil.rmtree(str(tgt_knowledge))
            shutil.copytree(str(src_knowledge), str(tgt_knowledge))
            copied.append("knowledge/")

    return web.json_response({
        "status": "forked",
        "source": source_name,
        "target": target_name,
        "copied": copied,
    })


def _spawn_templates_path() -> Path:
    return _hermes_home / "spawn_templates.json"


def _read_spawn_templates() -> list:
    p = _spawn_templates_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _write_spawn_templates(templates: list) -> None:
    p = _spawn_templates_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(templates, indent=2))


@require_permission("view_instances")
async def _handle_spawn_templates_get(request: web.Request) -> web.Response:
    return web.json_response(_read_spawn_templates())


@require_permission("view_instances")
@require_csrf
async def _handle_spawn_templates_put(request: web.Request) -> web.Response:
    """Replace the full list (client sends the already-deduped, ordered list)."""
    body = await request.json()
    if not isinstance(body, list):
        raise web.HTTPBadRequest(reason="Expected a JSON array")
    _write_spawn_templates(body[:12])
    return web.json_response({"status": "ok"})


@require_permission("view_instances")
@require_csrf
async def _handle_spawn_templates_delete(request: web.Request) -> web.Response:
    tpl_id = request.match_info["id"]
    templates = [t for t in _read_spawn_templates() if str(t.get("id")) != tpl_id]
    _write_spawn_templates(templates)
    return web.json_response({"status": "ok"})


async def _handle_hue(request: web.Request) -> web.Response:
    """Return the server hue epoch so the tray icon can phase-lock its cycle."""
    return web.json_response({"epoch_ms": _HUE_EPOCH_MS, "rate": 6})


async def _handle_favicon(request: web.Request) -> web.Response:
    """Serve logos.ico as /favicon.ico — public route so Edge --app shows the
    correct icon in the title bar and Windows taskbar without requiring auth."""
    import sys as _sys2
    import pathlib as _pl2
    candidates = []
    if getattr(_sys2, "frozen", False):
        candidates.append(_pl2.Path(_sys2._MEIPASS) / "launcher" / "logos.ico")
    candidates.append(_pl2.Path(__file__).parent.parent / "launcher" / "logos.ico")
    for p in candidates:
        if p.exists():
            data = p.read_bytes()
            import hashlib
            etag = '"' + hashlib.md5(data).hexdigest()[:16] + '"'
            if request.headers.get("If-None-Match") == etag:
                return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "public, max-age=86400"})
            return web.Response(
                body=data,
                content_type="image/x-icon",
                headers={"Cache-Control": "public, max-age=86400", "ETag": etag},
            )
    raise web.HTTPNotFound()


async def _handle_logo(request: web.Request) -> web.Response:
    """Serve the chat logo image from the baked-in app directory."""
    import pathlib
    logo = pathlib.Path("/app/chat_logo.png")
    if not logo.exists():
        raise web.HTTPNotFound()
    data = logo.read_bytes()
    return web.Response(
        body=data,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _handle_health(request: web.Request) -> web.Response:
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    uptime = int(time.time() - _start_time)
    from gateway.auth.db import is_setup_completed as _isc
    return web.json_response({
        "status": "ok",
        "product": "logos",
        "setup_completed": _isc(),
        "sessions": len(sessions),
        "uptime_s": uptime,
        "platform_stats": getattr(runner, "_platform_stats", {}),
    })


async def _handle_health_ready(request: web.Request) -> web.Response:
    """Deep readiness check: verifies auth DB and soul registry are operational.

    Returns 200 when ready, 503 when not. Used as the k8s readiness probe so
    traffic is only sent to pods that have fully initialised their subsystems.
    """
    checks: dict[str, str] = {}
    ok = True

    # Auth DB — a simple list_users call exercises the connection
    try:
        auth_db.list_users(limit=1)
        checks["auth_db"] = "ok"
    except Exception as exc:
        checks["auth_db"] = f"fail: {exc}"
        ok = False

    # Soul registry — must have loaded at least one soul
    souls = _souls_module._SOUL_REGISTRY
    if souls:
        checks["souls"] = f"ok ({len(souls)} loaded)"
    else:
        checks["souls"] = "empty"
        ok = False

    status = 200 if ok else 503
    return web.json_response(
        {"status": "ready" if ok else "not_ready", "checks": checks},
        status=status,
    )


_MODEL_CATALOG_PATH = Path(__file__).parent / "model_catalog.yaml"
_model_catalog_cache: list | None = None


async def _handle_model_catalog(request: web.Request) -> web.Response:
    """GET /api/model-catalog — return the Ollama model catalog.

    Loads from gateway/model_catalog.yaml on first call (cached).
    Falls back to an empty list if the file is missing.
    """
    global _model_catalog_cache
    if _model_catalog_cache is None:
        try:
            import yaml
            _model_catalog_cache = yaml.safe_load(
                _MODEL_CATALOG_PATH.read_text(encoding="utf-8")
            ) or []
        except Exception as exc:
            logger.warning("Failed to load model catalog: %s", exc)
            _model_catalog_cache = []
    return web.json_response(_model_catalog_cache)


async def _handle_sessions(request: web.Request) -> web.Response:
    if not _check_auth(request):
        raise web.HTTPUnauthorized()
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    return web.json_response([s.to_dict() for s in sessions])


async def _handle_api_platform_sessions(request: web.Request) -> web.Response:
    """GET /api/platform-sessions?platform=telegram — list server-side sessions by platform."""
    current_user = request.get("current_user") or {}
    if current_user.get("role", "viewer") not in ("admin", "operator"):
        raise web.HTTPForbidden()
    platform_filter = request.rel_url.query.get("platform")
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    if platform_filter:
        sessions = [s for s in sessions if s.platform and s.platform.value == platform_filter]
    else:
        sessions = [s for s in sessions if s.platform and s.platform.value not in ("local",)]
    return web.json_response([s.to_dict() for s in sessions])


async def _handle_api_session_messages(request: web.Request) -> web.Response:
    """GET /api/platform-sessions/{session_id}/messages — load transcript for a session."""
    current_user = request.get("current_user") or {}
    if current_user.get("role", "viewer") not in ("admin", "operator"):
        raise web.HTTPForbidden()
    session_id = request.match_info["session_id"]
    runner: Any = request.app["runner"]
    messages = runner.session_store.load_transcript(session_id)
    filtered = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return web.json_response(filtered)


async def _handle_transcribe(request: web.Request) -> web.Response:
    """POST /chat/transcribe — accept a webm/wav/ogg audio blob, return transcript."""
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "audio":
            return web.json_response({"error": "missing audio field"}, status=400)
        audio_bytes = await field.read(decode=True)
    except Exception as e:
        return web.json_response({"error": f"read failed: {e}"}, status=400)

    if not audio_bytes:
        return web.json_response({"error": "empty audio"}, status=400)

    # Write to a temp file so transcribe_audio can read it
    import tempfile
    suffix = ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        from tools.transcription_tools import transcribe_audio
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, transcribe_audio, tmp_path),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            result = {"success": False, "error": "transcription timed out (30s)"}
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    if not result.get("success"):
        return web.json_response({"error": result.get("error", "transcription failed")}, status=500)

    return web.json_response({"transcript": result.get("transcript", "")})


# ── Action policy handlers ─────────────────────────────────────────────────

async def _handle_action_policies_list(request: web.Request) -> web.Response:
    rows = auth_db.list_action_policies()
    return web.json_response({"action_policies": rows})


async def _handle_action_policies_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    name = body.get("name", "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="name required")
    try:
        row = auth_db.create_action_policy(
            name=name,
            description=body.get("description", ""),
            network_policy=body.get("network_policy", "internet_enabled"),
            network_allowlist=body.get("network_allowlist", "[]")
                if isinstance(body.get("network_allowlist"), str)
                else json.dumps(body.get("network_allowlist", [])),
            filesystem_policy=body.get("filesystem_policy", "workspace_only"),
            exec_policy=body.get("exec_policy", "restricted"),
            write_policy=body.get("write_policy", "auto_apply"),
            provider_policy=body.get("provider_policy", "any"),
            secret_policy=body.get("secret_policy", "tool_only"),
        )
    except Exception as e:
        raise web.HTTPConflict(reason=str(e))
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "create_action_policy",
        target_type="action_policy", target_id=row["id"],
    )
    return web.json_response({"action_policy": row}, status=201)


async def _handle_action_policies_get(request: web.Request) -> web.Response:
    row = auth_db.get_action_policy(request.match_info["id"])
    if not row:
        raise web.HTTPNotFound(reason="Action policy not found")
    return web.json_response({"action_policy": row})


async def _handle_action_policies_patch(request: web.Request) -> web.Response:
    policy_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    # Serialise allowlist if passed as list
    if "network_allowlist" in body and isinstance(body["network_allowlist"], list):
        body["network_allowlist"] = json.dumps(body["network_allowlist"])
    row = auth_db.update_action_policy(policy_id, **body)
    if not row:
        raise web.HTTPNotFound(reason="Action policy not found")
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "update_action_policy",
        target_type="action_policy", target_id=policy_id,
    )
    return web.json_response({"action_policy": row})


async def _handle_action_policies_delete(request: web.Request) -> web.Response:
    policy_id = request.match_info["id"]
    deleted = auth_db.delete_action_policy(policy_id)
    if not deleted:
        raise web.HTTPNotFound(reason="Action policy not found")
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "delete_action_policy",
        target_type="action_policy", target_id=policy_id,
    )
    return web.json_response({"deleted": True})


async def _handle_user_action_policy_patch(request: web.Request) -> web.Response:
    user_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    policy_id = body.get("action_policy_id")  # None to clear
    auth_db.assign_user_action_policy(user_id, policy_id)
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "assign_action_policy",
        target_type="user", target_id=user_id,
        metadata={"action_policy_id": policy_id},
    )
    return web.json_response({"user_id": user_id, "action_policy_id": policy_id})


# ── Approval request handlers ──────────────────────────────────────────────

async def _handle_approvals_list(request: web.Request) -> web.Response:
    current_user = request.get("current_user") or {}
    role = current_user.get("role", "viewer")
    user_id = current_user.get("sub")
    # Non-admin/operator users can only see their own session's approvals
    session_id = request.rel_url.query.get("session_id")
    status_filter = request.rel_url.query.get("status")
    if role not in ("admin", "operator") and not session_id:
        # Safety: require session_id for non-privileged users
        raise web.HTTPForbidden(reason="session_id required for non-admin users")
    page = int(request.rel_url.query.get("page", 1))
    rows, total = auth_db.list_approval_requests(
        session_id=session_id, status=status_filter, page=page
    )
    return web.json_response({"approvals": rows, "total": total, "page": page})


async def _handle_approvals_get(request: web.Request) -> web.Response:
    row = auth_db.get_approval_request(request.match_info["id"])
    if not row:
        raise web.HTTPNotFound(reason="Approval request not found")
    return web.json_response({"approval": row})


async def _handle_approvals_approve(request: web.Request) -> web.Response:
    approval_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = body.get("note", "")
    decided_by = (request.get("current_user") or {}).get("sub")
    updated = auth_db.resolve_approval_request(approval_id, "approved", decided_by, note)
    if not updated:
        row = auth_db.get_approval_request(approval_id)
        if not row:
            raise web.HTTPNotFound(reason="Approval request not found")
        raise web.HTTPConflict(reason=f"Request is already {row['status']}")
    auth_db.write_audit_log(
        decided_by, "approve_tool_request",
        target_type="approval_request", target_id=approval_id,
        metadata={"note": note},
    )

    # ── MCP access grant hook ──────────────────────────────────────────────
    # If this approval was for an MCP server access request, grant the session
    # access and inject the server's tools so they appear on the next agent turn.
    try:
        from gateway.auth.policy import ACTION_MCP_ACCESS
        if updated.get("action_type") == ACTION_MCP_ACCESS:
            import json as _json
            meta = _json.loads(updated.get("tool_args") or "{}")
            _srv_name = meta.get("server_name")
            _sess_id  = updated.get("session_id")
            _mcp_svc  = request.app.get("mcp_service")
            if _srv_name and _sess_id and _mcp_svc:
                from gateway.mcp_access import grant_access as _grant
                from tools.mcp_tool import inject_mcp_server_for_session as _inject
                _grant(_sess_id, _srv_name)
                _url = _mcp_svc.get_server_url(_srv_name, "local")
                await asyncio.get_event_loop().run_in_executor(
                    None, _inject, _srv_name, _url
                )
                logger.info("mcp approval hook: granted session=%s server=%s", _sess_id, _srv_name)
    except Exception as _mcp_hook_err:
        logger.warning("mcp approval hook error: %s", _mcp_hook_err)

    return web.json_response({"approved": True, "approval_id": approval_id})


async def _handle_approvals_reject(request: web.Request) -> web.Response:
    approval_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = body.get("note", "")
    decided_by = (request.get("current_user") or {}).get("sub")
    updated = auth_db.resolve_approval_request(approval_id, "rejected", decided_by, note)
    if not updated:
        row = auth_db.get_approval_request(approval_id)
        if not row:
            raise web.HTTPNotFound(reason="Approval request not found")
        raise web.HTTPConflict(reason=f"Request is already {row['status']}")
    auth_db.write_audit_log(
        decided_by, "reject_tool_request",
        target_type="approval_request", target_id=approval_id,
        metadata={"note": note},
    )
    return web.json_response({"rejected": True, "approval_id": approval_id})


# ── Workflow handlers ──────────────────────────────────────────────────────

async def _handle_world_state(request: web.Request) -> web.Response:
    """GET /world/state — snapshot of all named agents for world awareness.

    Consumed by the ``get_agent_world`` tool inside sandboxes so an
    agent can check who else is around, their soul, appearance, and
    whether they are currently busy. Also used by the dashboard to
    render the world roster without hitting the admin ``/agents``
    endpoint (which returns much richer, caller-restricted data).

    Query params:
      ?agent_id=<id>  — mark this agent as "self" in the response
                        (``is_self: true`` on the matching row).
    """
    from gateway.world_awareness import build_world_snapshot
    self_id = request.query.get("agent_id")
    snapshot = build_world_snapshot(self_agent_id=self_id, include_self=True)
    return web.json_response(snapshot)


async def _handle_workflows_list(request: web.Request) -> web.Response:
    rows = auth_db.list_workflow_definitions()
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflows": [_WD.from_row(r).to_dict() for r in rows]})


async def _handle_workflows_post(request: web.Request) -> web.Response:
    caller = request.get("current_user") or {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    # Validate steps
    steps_raw = body.get("steps", [])
    if not isinstance(steps_raw, list):
        return web.json_response({"error": "steps must be an array"}, status=400)
    try:
        from workflows.model import StepDefinition as _SD
        _ = [_SD.from_dict(s) for s in steps_raw]
    except Exception as exc:
        return web.json_response({"error": f"invalid step definition: {exc}"}, status=400)

    import json as _json
    row = auth_db.create_workflow_definition(
        name=name,
        steps_json=_json.dumps(steps_raw),
        description=body.get("description", ""),
        version=body.get("version", "1.0"),
        tags=_json.dumps(body.get("tags", [])),
        created_by=caller.get("sub"),
    )
    auth_db.write_audit_log(
        caller.get("sub"), "create_workflow",
        target_type="workflow", target_id=row["id"],
        metadata={"name": name},
    )
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()}, status=201)


async def _handle_workflows_get(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    row = auth_db.get_workflow_definition(wf_id)
    if not row:
        raise web.HTTPNotFound(reason="Workflow not found")
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()})


async def _handle_workflows_patch(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    import json as _json
    kwargs = {}
    if "name" in body:
        kwargs["name"] = body["name"]
    if "description" in body:
        kwargs["description"] = body["description"]
    if "version" in body:
        kwargs["version"] = body["version"]
    if "tags" in body:
        kwargs["tags"] = _json.dumps(body["tags"])
    if "steps" in body:
        try:
            from workflows.model import StepDefinition as _SD
            _ = [_SD.from_dict(s) for s in body["steps"]]
            kwargs["steps_json"] = _json.dumps(body["steps"])
        except Exception as exc:
            return web.json_response({"error": f"invalid step definition: {exc}"}, status=400)
    row = auth_db.update_workflow_definition(wf_id, **kwargs)
    if not row:
        raise web.HTTPNotFound(reason="Workflow not found")
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()})


async def _handle_workflows_delete(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    deleted = auth_db.delete_workflow_definition(wf_id)
    if not deleted:
        raise web.HTTPNotFound(reason="Workflow not found")
    auth_db.write_audit_log(
        caller.get("sub"), "delete_workflow",
        target_type="workflow", target_id=wf_id,
    )
    return web.json_response({"deleted": True})


async def _handle_workflow_trigger(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    caller_id = caller.get("sub")
    try:
        body = await request.json()
    except Exception:
        body = {}
    inputs = body.get("inputs") or {}

    # Resolve caller's action policy for the run.
    _action_policy = None
    if caller_id and caller_id.startswith("usr_"):
        try:
            from gateway.auth.policy import ActionPolicy as _AP
            _pr = auth_db.get_user_action_policy_row(caller_id)
            _action_policy = _AP.from_row(_pr) if _pr else None
        except Exception:
            pass

    engine = request.app.get("workflow_engine")
    if not engine:
        return web.json_response({"error": "workflow engine not available"}, status=503)
    try:
        run_id = await engine.start_run(
            workflow_id=wf_id,
            triggered_by=caller_id,
            inputs=inputs,
            action_policy=_action_policy,
            auth_user_id=caller_id,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except Exception as exc:
        logger.exception("Failed to start workflow run")
        return web.json_response({"error": str(exc)}, status=500)

    auth_db.write_audit_log(
        caller_id, "trigger_workflow",
        target_type="workflow_run", target_id=run_id,
        metadata={"workflow_id": wf_id, "inputs": inputs},
    )
    return web.json_response({"run_id": run_id, "workflow_id": wf_id}, status=202)


async def _handle_workflow_runs_list(request: web.Request) -> web.Response:
    wf_id  = request.rel_url.query.get("workflow_id")
    status = request.rel_url.query.get("status")
    limit  = min(int(request.rel_url.query.get("limit", 50)), 200)
    offset = int(request.rel_url.query.get("offset", 0))
    runs, total = auth_db.list_workflow_runs(workflow_id=wf_id, status=status,
                                              limit=limit, offset=offset)
    return web.json_response({"runs": runs, "total": total})


async def _handle_workflow_run_get(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    run = auth_db.get_workflow_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="Workflow run not found")
    steps = auth_db.get_workflow_step_runs(run_id)
    return web.json_response({"run": run, "steps": steps})


async def _handle_workflow_run_cancel(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    run = auth_db.get_workflow_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="Workflow run not found")
    if run["status"] in ("success", "failed", "cancelled"):
        return web.json_response({"error": "run already terminal"}, status=409)
    engine = request.app.get("workflow_engine")
    if engine:
        await engine.cancel_run(run_id)
    else:
        auth_db.update_workflow_run(run_id, status="cancelled",
                                    finished_at=int(time.time() * 1000))
    auth_db.write_audit_log(
        caller.get("sub"), "cancel_workflow_run",
        target_type="workflow_run", target_id=run_id,
    )
    return web.json_response({"cancelled": True, "run_id": run_id})


async def _handle_workflow_approval_decide(request: web.Request) -> web.Response:
    """Approve or reject a workflow approval step via its approval_request id."""
    approval_id = request.match_info["id"]
    decision    = request.match_info["decision"]   # 'approve' | 'reject'
    if decision not in ("approve", "reject"):
        return web.json_response({"error": "decision must be 'approve' or 'reject'"}, status=400)
    caller = request.get("current_user") or {}
    decided_by = caller.get("sub")

    engine = request.app.get("workflow_engine")
    if engine:
        await engine.resume_approval(
            approval_id=approval_id,
            approved=(decision == "approve"),
            decided_by=decided_by,
        )
    else:
        # Engine not running (e.g. tests) — just update the DB record.
        status = "approved" if decision == "approve" else "rejected"
        auth_db.resolve_approval_request(approval_id, status=status, decided_by=decided_by)
    return web.json_response({"decided": True, "decision": decision, "approval_id": approval_id})


async def _handle_tools_configure(request: web.Request) -> web.Response:
    """POST /tools/configure — save a tool API key and auto-apply presets.

    Body: {"credentials": {"FIRECRAWL_API_KEY": "fc-..."}}

    For each credential key:
    1. Saves to ~/.hermes/.env via save_env_value()
    2. Sets in os.environ for the running gateway process
    3. Auto-applies the corresponding network preset to agents that
       have the matching toolset enabled
    """
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    credentials = body.get("credentials")
    if not credentials or not isinstance(credentials, dict):
        return web.json_response({"error": "credentials dict required"}, status=400)

    saved = []
    errors = []

    for env_key, env_value in credentials.items():
        if not env_key or not isinstance(env_key, str) or not isinstance(env_value, str):
            errors.append({"key": env_key, "error": "invalid key or value"})
            continue
        env_value = env_value.strip()
        if not env_value:
            errors.append({"key": env_key, "error": "value cannot be empty"})
            continue

        # 1. Save to ~/.hermes/.env
        try:
            from logos_cli.config import save_env_value
            save_env_value(env_key, env_value)
        except Exception as exc:
            errors.append({"key": env_key, "error": f"failed to save: {exc}"})
            continue

        # 2. Set in os.environ for the running process
        os.environ[env_key] = env_value

        # 3. Auto-apply matching presets
        applied_presets = []
        try:
            from gateway import policies as gp
            applied_presets = gp.auto_apply_presets_for_env(env_key)
        except Exception as exc:
            logger.warning("tools/configure: auto_apply failed for %s: %s", env_key, exc)

        saved.append({
            "key": env_key,
            "presets_applied": applied_presets,
        })

    return web.json_response({
        "saved": saved,
        "errors": errors,
    })


async def _reconcile_sandbox_state() -> None:
    """One-shot startup reconciliation between state file + cluster reality.

    Walks ~/.logos/openshell_instances.json, dedupes per sandbox_name
    (favouring ready > provisioning > error), then for each survivor
    polls `openshell sandbox list` on the relevant gateway and updates
    phase based on what k3s actually shows. Entries whose pods are gone
    are dropped so the UI doesn't show ghost agents.

    Without this the gateway boots with whatever the state file last
    contained, which may include stale "provisioning" entries from a
    crash mid-spawn — and `worker_registry.get()` returns the first
    match, so the UI flags running agents as "not ready" until the user
    sends a message that re-runs the dispatch path.
    """
    import asyncio as _asyncio
    from gateway.executors.openshell import _load_state, _save_state, _openshell, _state_lock

    instances = _load_state()
    if not instances:
        return

    # Dedupe per sandbox_name. ready always wins; among same phase the
    # most recently created entry wins.
    rank = {"ready": 3, "provisioning": 2, "error": 1}
    by_name: dict[str, dict] = {}
    for inst in instances:
        name = inst.get("sandbox_name", "")
        if not name:
            continue
        existing = by_name.get(name)
        if not existing:
            by_name[name] = inst
            continue
        new_r = rank.get(inst.get("phase", ""), 0)
        old_r = rank.get(existing.get("phase", ""), 0)
        if new_r > old_r or (new_r == old_r and inst.get("created_at", 0) > existing.get("created_at", 0)):
            by_name[name] = inst

    # Group by gateway so we make one CLI call per cluster.
    by_gateway: dict[str, list[dict]] = {}
    for inst in by_name.values():
        gw = inst.get("openshell_name") or ""
        by_gateway.setdefault(gw, []).append(inst)

    updated: list[dict] = []
    for gw, group in by_gateway.items():
        if not gw:
            updated.extend(group)
            continue
        try:
            res = await _asyncio.to_thread(
                _openshell, "sandbox", "list",
                gateway=gw, check=False, timeout=10,
            )
            stdout = (res.stdout or "") if res.returncode == 0 else ""
        except Exception as exc:
            logger.debug("startup reconcile: list failed for %s: %s — keeping cached entries", gw, exc)
            updated.extend(group)
            continue
        for inst in group:
            sandbox_name = inst.get("sandbox_name", "")
            if not sandbox_name:
                continue
            # Exact-match the first whitespace token instead of substring —
            # substring would false-positive when one sandbox name is a
            # prefix of another (e.g. "hermes-adam" matches a line for
            # "hermes-adam-v2" too, so both would share that single line's
            # phase).
            line = next(
                (l for l in stdout.splitlines()
                 if l.split() and l.split()[0] == sandbox_name),
                None,
            )
            if not line:
                # Pod is gone in k3s — drop the stale state entry.
                logger.info("startup reconcile: dropping stale entry for %s (pod missing in %s)", sandbox_name, gw)
                continue
            # Map k3s phase words → our three-state vocabulary. Terminal
            # failures should show as "error" so the UI renders them
            # distinctly instead of "still spinning up any second now"
            # (which is what "provisioning" implies).
            _upper = line.upper()
            if "READY" in _upper:
                new_phase = "ready"
            elif ("FAILED" in _upper or "TERMINATED" in _upper
                  or "ERROR" in _upper or "EVICTED" in _upper):
                new_phase = "error"
            else:
                new_phase = "provisioning"
            if inst.get("phase") != new_phase:
                logger.info(
                    "startup reconcile: %s phase %s -> %s (gateway=%s)",
                    sandbox_name, inst.get("phase"), new_phase, gw,
                )
                inst["phase"] = new_phase
            updated.append(inst)

    # Hold the state lock only for the write — spawn() acquires the same
    # lock for its state-file mutations, so without this a spawn that
    # lands during reconcile's polling loop would get overwritten when
    # we _save_state below.
    try:
        with _state_lock():
            _save_state(updated)
    except Exception as exc:
        logger.warning("startup reconcile: _save_state failed (kept in-memory only): %s", exc)
    logger.info("startup reconcile: %d sandbox entries kept", len(updated))


async def _handle_chat(request: web.Request) -> web.StreamResponse:
    # /chat is intentionally unauthenticated (same-origin dashboard, LAN-only NodePort).
    # Rate limiting prevents runaway agent spawning from a single IP.
    ip = request.remote or "unknown"
    if not check_rate_limit(ip, max_requests=30, window=60):
        raise web.HTTPTooManyRequests(
            text='{"error":"rate_limited"}',
            content_type="application/json",
        )

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    message = body.get("message", "")
    session_id = body.get("session_id", "http-default")
    agent_id = body.get("agent_id")
    # Transient mode: Compare tab uses this so probe traffic doesn't
    # pollute the agent's identity. Affects two visible behaviours:
    #   1. dispatch ledger origin = 'compare' (so Activity → Events
    #      can filter the noise out)
    #   2. session JSON / memory writes — should also be skipped, but
    #      that requires sandbox_worker.py changes that ship with M10.
    #      Tracked here so the wiring is one half-done piece, not two.
    is_transient = bool(body.get("transient", False))

    # ── Gateway command: /setup tools ──────────────────────────────────
    # Intercept before any session/dispatch logic. Returns tool readiness
    # as a special SSE message the frontend renders as an interactive card.
    _msg_stripped = (message or "").strip().lower()
    if _msg_stripped in ("/setup tools", "/setup", "/tools setup"):
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)
        readiness = []
        if agent_id:
            try:
                from gateway import policies as gp
                readiness = gp.get_tool_readiness(agent_id)
            except Exception:
                pass
        # Also include available presets for context
        from gateway import policies as gp
        preset_map = {}
        for tool_name, info in gp.TOOL_PRESET_MAP.items():
            for env_key in info.get("env", []):
                preset_map[env_key] = {
                    "tool": tool_name,
                    "preset": info.get("presets", [None])[0],
                    "setup_url": info.get("setup_url", ""),
                    "description": info.get("description", ""),
                }
        await resp.write(("data: " + json.dumps({
            "type": "setup_tools",
            "readiness": readiness,
            "preset_map": preset_map,
        }) + "\n\n").encode())
        await resp.write(("data: " + json.dumps({"type": "done", "elapsed_s": 0}) + "\n\n").encode())
        return resp

    # M6 correlation IDs: stamp every log record emitted during this request
    # with identifiers that can be grepped from the unified log afterwards.
    # Generate a fresh task_id per dispatch so a single chat turn (worker
    # dispatch → inference → streaming reply → tool calls) is queryable as
    # one unit via `logos debug tail --filter task_id=<id>`. See MISSING.md M6.
    import uuid as _uuid
    _task_id = _uuid.uuid4().hex[:12]
    try:
        from gateway.run import set_log_context
        _current_user = request.get("current_user") or {}
        set_log_context(
            session_id=session_id,
            task_id=_task_id,
            user_id=_current_user.get("sub") or _current_user.get("id"),
            chat_id=body.get("chat_id"),
            # worker_id gets set later once the sandbox_name is resolved —
            # at this point `agent_id` is still the raw DB id, not the
            # sanitised sandbox name the worker registry keys on.
        )
    except Exception as _ctx_exc:
        logger.debug("set_log_context skipped: %s", _ctx_exc)

    # OpenShell-only routing: every chat must target a named agent that has
    # its own sandbox worker. No in-process fallback.
    if not agent_id:
        raise web.HTTPBadRequest(
            reason="agent_id is required — chat must target a named sandboxed agent",
        )
    try:
        _agent_config = auth_db.get_agent(agent_id)
    except Exception:
        _agent_config = None
    if not _agent_config:
        raise web.HTTPNotFound(reason=f"agent {agent_id} not found")

    # On-demand LM Studio model load — only for agents whose model
    # actually runs on LM Studio (or another local OpenAI-compat server).
    # Cloud-routed models (anthropic/openai/openrouter) live on their
    # provider's API; pinging LM Studio with their model IDs wastes a
    # round-trip and produces confusing 400s in LM Studio's log.
    #
    # Derive "is cloud" from the agent's bound model_route rather than
    # matching magic prefixes on the model name. When someone adds a new
    # provider (mistral, deepseek, etc.) it just works — no code change.
    _agent_model_for_load = (_agent_config.get("model") or "").strip()
    _LOCAL_PROVIDERS = {"lmstudio", "ollama"}
    _is_cloud_model = False
    _route_id = _agent_config.get("model_route_id")
    if _route_id:
        try:
            _route = auth_db.get_model_route(_route_id)
            _route_provider = ((_route or {}).get("provider") or "").lower()
            if _route_provider and _route_provider not in _LOCAL_PROVIDERS:
                _is_cloud_model = True
        except Exception:
            pass  # fall through to heuristic
    # Fallback heuristic for agents without a route binding (shouldn't
    # happen normally; kept as a belt-and-braces guard).
    if not _is_cloud_model and _agent_model_for_load:
        _ml = _agent_model_for_load.lower()
        if (_ml.startswith(("claude-", "gpt-", "o1", "o3", "o4", "gemini-"))
            or ("/" in _ml and _ml.split("/", 1)[0] in {"anthropic", "openai", "google"})):
            _is_cloud_model = True
    if _agent_model_for_load and not _is_cloud_model:
        try:
            _machines = auth_db.list_machines()
            for _m in _machines:
                if _m.get("enabled") and _m.get("endpoint_url"):
                    from gateway.lmstudio_loader import ensure_loaded as _ensure
                    await _ensure(
                        _m["endpoint_url"],
                        _agent_model_for_load,
                        _m.get("api_key"),
                    )
                    break
        except Exception as _ld_exc:
            logger.warning("lmstudio ensure_loaded skipped: %s", _ld_exc)

    # --- Process file attachments ---
    raw_attachments = body.get("attachments") or []
    # Legacy single-image support
    legacy_image = body.get("image")
    if legacy_image and not raw_attachments:
        raw_attachments = [{"data": legacy_image, "name": "image.png", "type": "image/png"}]

    media_urls: list[str] = []
    media_types: list[str] = []
    if raw_attachments:
        import base64
        from gateway.platforms.base import (
            cache_image_from_bytes,
            cache_audio_from_bytes,
            cache_document_from_bytes,
        )
        _ATTACH_MAX = 5
        _ATTACH_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
        for att in raw_attachments[:_ATTACH_MAX]:
            data_url = att.get("data", "")
            att_name = att.get("name", "file")
            att_type = att.get("type", "application/octet-stream")
            # Decode base64 data URL: "data:<mime>;base64,<payload>"
            if ";base64," not in data_url:
                continue
            payload = data_url.split(";base64,", 1)[1]
            try:
                raw_bytes = base64.b64decode(payload)
            except Exception:
                continue
            if len(raw_bytes) > _ATTACH_MAX_SIZE:
                continue
            # Cache to disk using existing helpers
            ext = os.path.splitext(att_name)[1] or ".bin"
            if att_type.startswith("image/"):
                cached = cache_image_from_bytes(raw_bytes, ext)
            elif att_type.startswith("audio/"):
                cached = cache_audio_from_bytes(raw_bytes, ext)
            else:
                cached = cache_document_from_bytes(raw_bytes, att_name)
            media_urls.append(cached)
            media_types.append(att_type)

    # Use authenticated identity; fall back to body fields for backwards-compat
    auth_user = request.get("current_user") or {}
    user_name = (
        auth_db.get_user_by_id(auth_user.get("sub", ""))or {}
    ).get("display_name") or auth_user.get("email") or body.get("user_name", "User")
    user_id = auth_user.get("sub") or body.get("user_id", "http-user")

    if not message:
        raise web.HTTPBadRequest(reason="message is required")

    runner: Any = request.app["runner"]

    # Resolve the authenticated user's action policy (if any).
    # Applies only to auth-db users (usr_... IDs); platform/anonymous users get DEFAULT_POLICY.
    _action_policy = None
    _auth_user_id = None
    _real_user_id = auth_user.get("sub", "")
    if _real_user_id and _real_user_id.startswith("usr_"):
        _auth_user_id = _real_user_id
        try:
            from gateway.auth.policy import ActionPolicy as _AP, merge_policies as _merge
            _policy_row = auth_db.get_user_action_policy_row(_real_user_id)
            _action_policy = _AP.from_row(_policy_row) if _policy_row else None
            # Session-level tightening: caller may request a stricter policy for this request only.
            # Requires manage_action_policies permission (admins/operators creating sandboxed sessions).
            _session_policy_id = body.get("action_policy_id")
            from gateway.auth.rbac import has_permission as _has_perm
            if _session_policy_id and _has_perm(auth_user.get("role", "viewer"), "manage_action_policies"):
                _sess_row = auth_db.get_action_policy(_session_policy_id)
                if _sess_row:
                    _action_policy = _merge(_action_policy, _AP.from_row(_sess_row))
        except Exception as _pe:
            logger.warning("Failed to resolve action policy for %s: %s", _real_user_id, _pe)

    # ── Resolve the target sandbox worker first ──
    # We need the worker's `registered_at` timestamp BEFORE building the
    # session source so the transcript can be scoped to a single sandbox
    # incarnation. When the sandbox dies and respawns (agent deleted +
    # recreated, crash/restart, k3s reschedule, etc.) the new worker gets
    # a fresh `registered_at`, which yields a brand-new session_id and
    # therefore an empty transcript — the agent wakes up with no memory
    # of the prior incarnation, exactly as the user expects.
    #
    # CRITICAL: target_worker is derived FROM THE AGENT RECORD, never from
    # the request body. Earlier this was `body.get("worker_id") or
    # <derived>` which let a stale frontend cache override agent-id-based
    # routing — causing chats targeted at agent A to dispatch to agent B's
    # worker (the "Hermes responds as Ani" bug). agent_id is the only
    # identifier we trust; everything else is auth.db-derived.
    from gateway.executors.openshell import _sanitize_sandbox_name
    target_worker = _sanitize_sandbox_name(
        f"hermes-{_agent_config.get('name', '')}"
    )
    worker_registry = request.app.get("worker_registry")
    worker_entry = worker_registry.get(target_worker) if worker_registry else None
    # Previously we appended a per-sandbox "-w<registered_at>" incarnation
    # tag to the session_id to force amnesia across sandbox recreation.
    # That broke mid-conversation history: the first dispatch (no sandbox
    # yet) used key "chat_xyz" and the second (sandbox now spawned) used
    # "chat_xyz-w12345" — different keys, so Claude's second reply had no
    # memory of the first. Keep the tag empty; persistence of history
    # across sandbox respawns is the correct default for a user-visible
    # chat UI, and agent-identity confusion is handled by the separate
    # `target_worker` routing above (derived from agent_id, not session).
    incarnation_tag = ""

    # Diagnostic: log every dispatch decision so future "wrong agent
    # responded" bugs can be traced from the gateway log alone instead
    # of guessing from the symptom in the chat UI. Includes the body's
    # worker_id (now ignored for routing) so we can detect when the
    # frontend is sending a stale value.
    logger.info(
        "chat dispatch: agent_id=%r name=%r → target_worker=%r "
        "(body.worker_id=%r ignored, worker_connected=%s)",
        agent_id,
        _agent_config.get("name"),
        target_worker,
        body.get("worker_id"),
        bool(worker_entry and worker_entry.healthy),
    )


    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id=f"{session_id}{incarnation_tag}",
        chat_type="dm",
        user_id=user_id,
        user_name=user_name,
    )

    session_entry = runner.session_store.get_or_create_session(source)
    session_key = session_entry.session_key
    history = runner.session_store.load_transcript(session_entry.session_id)
    # Client-provided history fallback — used when the session is brand new
    # (load_transcript returns []) and the client has prior turns it wants
    # the agent to remember. This is how the compare tab's "→ Continue"
    # button preserves context when promoting a compare pane into a real
    # chat: compare sessions are sent with ``transient: true`` so the
    # session_store never wrote them, which meant the new chat's first
    # message went to an amnesiac agent ("I don't have context about what
    # you're referring to"). We only accept client history when the server
    # has none — once the backend has a real transcript for this session,
    # that's the source of truth and we ignore anything the client sends.
    if not history:
        _client_history = body.get("history") or []
        if isinstance(_client_history, list) and _client_history:
            # Defensive normalisation: keep only {role, content} with
            # string content, drop anything else. Prevents a malformed
            # payload from poisoning the prompt builder.
            _clean = []
            for m in _client_history:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = m.get("content", "")
                if role in ("user", "assistant", "system") and isinstance(content, str):
                    _clean.append({"role": role, "content": content})
            if _clean:
                history = _clean
                logger.info(
                    "chat dispatch: seeded %d history message(s) from client "
                    "for new session_id=%s",
                    len(_clean), session_entry.session_id,
                )
    context = build_session_context(source, runner.config, session_entry)
    # Compose the FULL system prompt: identity ("You are Jay.") + soul +
    # description + session context. The sandbox worker forwards this to
    # inference verbatim, so any missing piece (e.g. the name) means the
    # model has no way to know it. build_agent_system_prompt handles all
    # four pieces and falls back gracefully when fields are absent.
    context_prompt = build_agent_system_prompt(
        _agent_config, build_session_context_prompt(context),
    )

    # Diagnostic: log the first line of the constructed context_prompt
    # so we can verify "You are <agent name>." actually matches the
    # dispatch target. If the dispatch is to hermes-hermes but the
    # prompt's first line says "You are Ani.", we have a prompt-builder
    # bug. If the prompt says "You are Hermes." but the model still
    # responds as Ani, the bug is the model's adherence (or qwen3.5-9b
    # training-data bias toward common AI character names), not routing.
    _first_line = (context_prompt or "").splitlines()[0] if context_prompt else "<empty>"
    logger.info("chat dispatch prompt[0]: %r (length=%d)", _first_line, len(context_prompt or ""))

    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
    )
    await resp.prepare(request)

    async def send_event(data: dict) -> None:
        try:
            await resp.write(f"data: {json.dumps(data)}\n\n".encode())
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass  # Client disconnected mid-stream — nothing we can do

    async def heartbeat_loop() -> None:
        """Send SSE comments every 20s to keep the connection alive through proxies."""
        while True:
            await asyncio.sleep(20)
            try:
                await resp.write(b": heartbeat\n\n")
            except Exception:
                break

    await send_event({"type": "start"})

    # Enrich message with attachment analysis (vision, transcription, doc context)
    if media_urls:
        message = await runner._enrich_message_with_attachments(
            message, media_urls, media_types,
        )

    heartbeat = asyncio.ensure_future(heartbeat_loop())

    # Queue for structured tool_start/tool_end events from the agent thread
    http_sse_queue = asyncio.Queue()

    async def drain_tool_events():
        """Forward tool events from the agent thread to the SSE stream."""
        while True:
            try:
                evt = await asyncio.wait_for(http_sse_queue.get(), timeout=0.5)
                await send_event(evt)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                # Drain remaining events before exiting
                while not http_sse_queue.empty():
                    try:
                        await send_event(http_sse_queue.get_nowait())
                    except Exception:
                        break
                return

    drain_task = asyncio.ensure_future(drain_tool_events())

    result = {}
    # Capture the agent's configured model so we can include it in the
    # per-message stats `done` event below. The worker doesn't currently
    # echo the model back in its task_result, so without this the Stats
    # dropdown's "model" row stays blank. Resolution priority: explicit
    # per-agent setting → gateway-wide active model env var → empty
    # string. Defined here (before the try/except) so it's always bound,
    # even on the sandbox-unavailable error path.
    _agent_model = (
        _agent_config.get("model")
        or os.environ.get("LOGOS_MODEL")
        or os.environ.get("HERMES_MODEL")
        or ""
    )
    t_agent_start = time.time()
    try:
        # Worker + incarnation tag were resolved above, before the session
        # was built. Re-check liveness here in case the worker dropped off
        # between session creation and dispatch. If the sandbox is gone
        # (host reboot, admin deleted, gateway crash), kick off a reactive
        # respawn via sandbox_heal.ensure_sandbox_alive — the UI streams
        # the "provisioning…" events through send_event so the existing
        # overlay renders live, and the spawn call blocks this dispatch
        # for 10-30s before we proceed to the real task. This replaces
        # the old bail-out that told users to "check Admin → Sandboxes."
        if not (worker_entry and worker_entry.healthy):
            from gateway.sandbox_heal import ensure_sandbox_alive
            _heal_ok, worker_entry = await ensure_sandbox_alive(
                worker_registry=worker_registry,
                executor=request.app.get("executor"),
                worker_id=target_worker,
                agent_record=_agent_config,
                on_event=send_event,
            )
            if not _heal_ok:
                # Heal failed — fall back to the old behaviour: clear error,
                # don't silently run in-process (would bypass sandbox policy).
                await send_event({
                    "type": "error",
                    "error_type": "sandbox_unavailable",
                    "error_title": "Agent sandbox could not be started",
                    "error_action": (
                        "Auto-respawn failed. Check gateway logs for the "
                        "spawn error, or visit Admin → Sandboxes to investigate."
                    ),
                    "content": f"worker '{target_worker}' auto-respawn failed",
                    "error_class": "SandboxUnavailable",
                })
                result = {"final_response": ""}
            else:
                # Heal succeeded — fall through to the else branch below by
                # re-evaluating the same condition on the freshly-healed entry.
                pass
        if (worker_entry and worker_entry.healthy):
            # Dispatch to the connected OpenShell sandbox worker.  The worker
            # manages its own inference config (via inference.local or its
            # uploaded instance-config.json) — we only send conversation
            # context.
            import uuid as _uuid
            task_payload = {
                "type": "run_conversation",
                "task_id": str(_uuid.uuid4()),
                "session_id": session_entry.session_id,
                "session_key": session_key,
                "message": message,
                "history": history,
                "context_prompt": context_prompt,
                "toolsets": worker_entry.toolsets or ["hermes-cli"],
                "max_iterations": int(os.environ.get("LOGOS_MAX_ITERATIONS",
                                                     os.environ.get("HERMES_MAX_ITERATIONS", "90"))),
            }
            # Stream callback — forward worker events to client SSE
            async def _on_worker_stream(event):
                etype = event.get("type")
                if etype in ("tool_start", "tool_end"):
                    await send_event(event)
                elif etype == "tool_progress":
                    await send_event({
                        "type": "tool_progress",
                        "tool": event.get("tool", ""),
                        "preview": event.get("preview", ""),
                    })
                elif etype == "token":
                    await send_event({
                        "type": "token",
                        "content": event.get("content", ""),
                    })
                elif etype == "thinking":
                    await send_event({
                        "type": "thinking",
                        "content": event.get("content", ""),
                    })
                elif etype == "memory_write":
                    await send_event({
                        "type": "memory_write",
                        "preview": event.get("preview", ""),
                    })

            # ── Dispatch ledger: record start ──
            _dispatch_id = None
            try:
                _dispatch_id = auth_db.create_dispatch(
                    task_id=task_payload["task_id"],
                    agent_id=agent_id or "",
                    sandbox_name=target_worker,
                    model=_agent_model,
                    origin=("compare" if is_transient else "user_chat"),
                    origin_detail=json.dumps({
                        "user_id": (_real_user_id or ""),
                        "chat_id": body.get("chat_id", ""),
                        "transient": is_transient,
                    }),
                    session_id=session_entry.session_id if session_entry else "",
                    user_id=_real_user_id or "",
                )
            except Exception as _dsp_exc:
                logger.debug("dispatch ledger create skipped: %s", _dsp_exc)

            # ── Budget gate ─────────────────────────────────────────────
            # Refuse this dispatch if the agent's daily_budget_usd has
            # already been exceeded by today's cost_log rows. For agents
            # with fallback_route_id set, we could auto-swap models here;
            # for now we refuse with a clear error and let the user
            # decide. The UI renders the refusal as an error bubble with
            # a "switch to fallback" button.
            _budget = _agent_config.get("daily_budget_usd")
            if _budget is not None and _budget > 0:
                try:
                    _fallback = _agent_config.get("fallback_route_id") or None
                    _rollup = auth_db.cost_rollup(
                        agent_id=_agent_config.get("id"),
                        since_ts=int(time.time() * 1000) - 86_400_000,  # last 24h
                        until_ts=int(time.time() * 1000),
                    )
                    _spent = float(_rollup.get("total_usd") or 0)
                    if _spent >= float(_budget):
                        # Optional auto-swap to fallback route: rebind +
                        # carry on with the new model. Only kicks in if
                        # the agent has a fallback configured. Swap is
                        # persistent (via update_agent) so the agent stays
                        # on the fallback until the budget rolls over or
                        # the user re-selects the cloud route manually.
                        _swapped = False
                        if _fallback:
                            try:
                                _fallback_route = auth_db.get_model_route(_fallback)
                                if (_fallback_route and
                                        _fallback_route.get("provider") in ("lmstudio", "ollama") and
                                        _fallback_route.get("status") == "ready"):
                                    auth_db.update_agent(
                                        _agent_config["id"],
                                        model_route_id=_fallback,
                                        model=_fallback_route.get("model") or _agent_model_for_load,
                                    )
                                    # Push the new model into the running
                                    # sandbox's instance-config.json so THIS
                                    # dispatch uses the fallback, not the
                                    # next one. Without the refresh the
                                    # sandbox_worker reads the OLD model
                                    # from instance-config on startup and
                                    # the very request that triggered the
                                    # swap still goes to the expensive
                                    # cloud endpoint, contradicting the
                                    # "Auto-switched" message the user sees.
                                    try:
                                        _executor = request.app.get("executor")
                                        if _executor and hasattr(_executor, "refresh_instance_config"):
                                            await asyncio.to_thread(
                                                _executor.refresh_instance_config,
                                                _agent_config.get("name", ""),
                                            )
                                    except Exception as _rfx:
                                        logger.warning(
                                            "budget auto-swap: refresh_instance_config failed: %s", _rfx,
                                        )
                                    _swapped = True
                                    logger.info(
                                        "budget: agent %s hit $%.2f/$%.2f cap — auto-swapped to fallback route %s (%s)",
                                        _agent_config.get("name"), _spent, _budget,
                                        _fallback, _fallback_route.get("model"),
                                    )
                                    # Surface a system message in the stream so the
                                    # user sees what happened instead of a silent swap.
                                    await send_event({
                                        "type": "message", "role": "system",
                                        "content": (
                                            f"⚠️ {_agent_config.get('name')} hit the daily "
                                            f"budget (${_spent:.2f}/${_budget:.2f}). "
                                            f"Auto-switched to local model "
                                            f"{_fallback_route.get('model')}. "
                                            "Raise the budget or wait for the 24h window to reset."
                                        ),
                                    })
                            except Exception as _bsw_exc:
                                logger.warning("budget auto-swap failed: %s", _bsw_exc)
                        if not _swapped:
                            await send_event({
                                "type": "error",
                                "error_title": "Daily budget exceeded",
                                "error_type": "budget",
                                "error_action": (
                                    f"{_agent_config.get('name')} has spent "
                                    f"${_spent:.2f} of ${_budget:.2f} today. "
                                    "Raise the budget, configure a local fallback route, "
                                    "or wait for the 24h window to roll over."
                                ),
                                "content": f"budget_exceeded spent=${_spent:.2f} cap=${_budget:.2f}",
                            })
                            await send_event({"type": "message", "content": ""})
                            return resp
                except Exception as _bgate_exc:
                    logger.warning("budget gate skipped: %s", _bgate_exc)

            worker_result = await worker_registry.dispatch_task(
                target_worker, task_payload, timeout=600,
                on_stream_event=_on_worker_stream,
            )
            result = {
                "final_response": worker_result.get("final_response", ""),
                "api_calls": worker_result.get("api_calls", 0),
                "tools_used": worker_result.get("tools_used", []),
            }

            # ── Dispatch ledger: record completion ──
            if _dispatch_id:
                try:
                    _dsp_status = worker_result.get("status", "ok")
                    _dsp_elapsed = round(time.time() - t_agent_start, 2)
                    auth_db.complete_dispatch(
                        _dispatch_id,
                        status=_dsp_status,
                        elapsed_s=_dsp_elapsed,
                        prompt_tokens=worker_result.get("prompt_tokens"),
                        completion_tokens=worker_result.get("completion_tokens"),
                        error=worker_result.get("error"),
                    )
                except Exception as _dsp_exc:
                    logger.debug("dispatch ledger complete skipped: %s", _dsp_exc)
            # Worker sets status="error" when its inference call raised
            # (e.g. LM Studio returned HTTP 500 mid-eviction during JIT
            # auto-evict, OpenShell privacy router blew up, model crashed).
            # Without this branch the gateway used to silently emit an
            # empty `message` event and the user saw a blank assistant
            # bubble with no indication anything went wrong. Surface it
            # as an error event the frontend can render distinctly.
            if worker_result.get("status") == "error":
                _werr = worker_result.get("error") or "Worker reported an error"
                _werr_lower = str(_werr).lower()
                _err_type = "worker_error"
                _err_title = "Inference failed"
                _err_action = ""
                if "500" in _werr_lower or "internal server error" in _werr_lower:
                    _err_title = "Inference server error"
                    _err_action = (
                        "LM Studio returned 500. If you have multiple agents loaded, "
                        "the JIT auto-evict setting may have unloaded this model "
                        "mid-request. Disable 'JIT models auto-evict' in LM Studio "
                        "developer settings."
                    )
                elif "401" in _werr_lower or "unauthorized" in _werr_lower:
                    _err_title = "Inference auth failed"
                    _err_action = "Check your LM Studio API key in Settings → Inference."
                elif "404" in _werr_lower or "model not found" in _werr_lower:
                    _err_title = "Model not loaded"
                    _err_action = "Load the model in LM Studio first, or wait for ensure_loaded."
                await send_event({
                    "type": "error",
                    "error_type": _err_type,
                    "error_title": _err_title,
                    "error_action": _err_action,
                    "content": str(_werr)[:500],
                    "error_class": "WorkerError",
                })
                # Still emit a message event with empty content so the
                # streaming placeholder gets finalized cleanly downstream.
                await send_event({"type": "message", "content": ""})
            else:
                final = result.get("final_response", "")
                # Guard: an empty user-facing reply is never acceptable.
                # Common cause: the model wraps its whole reply in <think>
                # tags and the stripper eats everything. Surface a
                # diagnostic placeholder so the user sees *something* and
                # the silent-failure mode shows up in dispatch logs instead
                # of looking like a UI bug.
                if not (final or "").strip():
                    logger.warning(
                        "chat: worker returned empty final_response (session=%s, agent=%s) — "
                        "likely thinking-tag stripping; surfacing fallback",
                        session_entry.session_id, agent_id,
                    )
                    final = "⚠️ The agent finished without producing a reply. (Likely a reasoning-model formatting issue — try rephrasing or starting a new chat.)"
                await send_event({"type": "message", "content": final})

            # ── Persist this turn so the next message in the same sandbox
            # incarnation sees the full transcript. Without this the agent
            # would re-enter every turn with empty `history` and behave
            # like an amnesic ("It looks like we're at the start of this
            # thread"). The session_id is sandbox-incarnation-aware (see
            # the worker resolution block above), so a sandbox respawn
            # automatically resets memory by switching to a fresh row.
            try:
                runner.session_store.append_to_transcript(
                    session_entry.session_id,
                    {"role": "user", "content": message},
                )
                if final:
                    runner.session_store.append_to_transcript(
                        session_entry.session_id,
                        {"role": "assistant", "content": final},
                    )
            except Exception as _persist_err:
                logger.warning(
                    "Failed to persist chat turn for session %s: %s",
                    session_entry.session_id, _persist_err,
                )
    except Exception as exc:
        logger.exception("Error running agent for HTTP /chat")
        err_str = str(exc)
        err_lower = err_str.lower()
        # Classify the error so the frontend can render appropriate UI
        error_type = "unknown"
        error_title = "Something went wrong"
        error_action = ""
        # Worker dropped mid-dispatch (rejected by worker_registry's
        # finally cleanup when the WebSocket dies). Surface as a
        # specific "agent connection lost" message rather than the
        # generic "Something went wrong" the frontend used to show.
        if isinstance(exc, ConnectionError) or "disconnected before" in err_lower:
            error_type = "sandbox_disconnected"
            error_title = "Agent connection lost mid-reply"
            error_action = (
                "The sandbox worker disconnected while processing your message. "
                "Wait a few seconds for it to reconnect, then try sending again."
            )
        elif (isinstance(exc, TimeoutError) or "did not return task_result within" in err_lower
              or "timed out waiting for the next stdout line" in err_lower
              or "timed out" in err_lower and "task" in err_lower):
            # Dispatch-level timeout: worker_registry's hard cap elapsed
            # before the sandbox emitted its task_result. Usually means
            # the model is just slow (big prompt + reasoning), or the
            # underlying provider is hanging. Distinct from "network" —
            # the sandbox IS running, it just hasn't finished. Show a
            # dedicated message so the user knows the difference.
            error_type = "dispatch_timeout"
            error_title = "Agent reply timed out"
            error_action = (
                "The model didn't finish within the gateway's dispatch window. "
                "Try again — if it keeps timing out, the model may be stuck or "
                "the prompt may be too long; consider switching models or "
                "trimming the conversation."
            )
        elif "credit balance" in err_lower or "insufficient" in err_lower and "credit" in err_lower or "billing" in err_lower or "402" in err_str:
            error_type = "billing"
            error_title = "Out of credits"
            error_action = "Top up your account with the model provider, then try again."
        elif "401" in err_str or "invalid api key" in err_lower or "unauthorized" in err_lower or "authentication" in err_lower:
            error_type = "auth"
            error_title = "Authentication failed"
            error_action = "Check your API key in Settings > Inference."
        elif "429" in err_str or "rate limit" in err_lower or "too many requests" in err_lower:
            error_type = "rate_limit"
            error_title = "Rate limited"
            error_action = "Wait a moment and try again. The provider is throttling requests."
        elif "404" in err_str or "model not found" in err_lower or "not a valid model" in err_lower:
            error_type = "model_error"
            error_title = "Model not found"
            error_action = "The model may have been renamed or removed. Check Settings > Inference."
        elif "context" in err_lower and ("length" in err_lower or "too long" in err_lower or "exceeded" in err_lower):
            error_type = "context_overflow"
            error_title = "Conversation too long"
            error_action = "Start a new topic or switch to a model with a larger context window."
        elif "connection" in err_lower or "refused" in err_lower or "unreachable" in err_lower or "timeout" in err_lower:
            error_type = "network"
            error_title = "Cannot reach model server"
            error_action = "Check that your inference server is running and reachable."
        elif "model has crashed" in err_lower or "exit code" in err_lower:
            error_type = "model_crash"
            error_title = "Model crashed"
            error_action = "The model process crashed. Check your inference server logs."
        await send_event({
            "type": "error",
            "error_type": error_type,
            "error_title": error_title,
            "error_action": error_action,
            "content": err_str,
            "error_class": type(exc).__name__,
        })
    finally:
        heartbeat.cancel()
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

    # Worker-side fields take priority when present (worker has the
    # authoritative usage data from the LLM response). Fall back to
    # the agent-config model captured at dispatch time so the stats
    # dropdown always shows *something* under "model" even before the
    # worker is taught to echo usage stats back.
    await send_event({
        "type":            "done",
        "elapsed_s":       round(time.time() - t_agent_start, 1),
        "prompt_tokens":   result.get("last_prompt_tokens") or result.get("prompt_tokens") or 0,
        "api_calls":       result.get("api_calls", 0),
        "tools_used":      result.get("tools_used", 0),
        "tools_available": len(result.get("tools", [])),
        "model":           result.get("model") or _agent_model or "",
        "tool_detail":     result.get("tool_detail", []),
    })
    return resp


# ── Agent Runs handlers ──────────────────────────────────────────────────────

async def _handle_runs_list(request: web.Request) -> web.Response:
    user = request.get("current_user") or {}
    role = user.get("role", "viewer")
    uid = user.get("sub", "")
    # Operators/admins see all runs; users see only their own
    from gateway.auth.rbac import has_permission
    see_all = has_permission(role, "manage_users")
    params = request.rel_url.query
    status_f = params.get("status") or None
    session_f = params.get("session_id") or None
    limit = min(int(params.get("limit", 50)), 200)
    offset = int(params.get("offset", 0))
    runs, total = auth_db.list_agent_runs(
        user_id=None if see_all else uid,
        status=status_f,
        session_id=session_f,
        limit=limit,
        offset=offset,
    )
    # Parse JSON fields and resolve user_id → username
    user_ids = {r["user_id"] for r in runs if r.get("user_id")}
    user_map = {}
    for uid in user_ids:
        u = auth_db.get_user_by_id(uid)
        if u:
            user_map[uid] = u.get("username") or u.get("email") or uid
    for r in runs:
        for field in ("tool_sequence", "tool_detail", "approval_ids"):
            try:
                r[field] = json.loads(r[field] or "[]")
            except Exception:
                r[field] = []
        if r.get("user_id"):
            r["username"] = user_map.get(r["user_id"], r["user_id"])
    return web.json_response({"runs": runs, "total": total})


async def _handle_run_get(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    run = auth_db.get_agent_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="run_not_found")
    for field in ("tool_sequence", "tool_detail", "approval_ids"):
        try:
            run[field] = json.loads(run[field] or "[]")
        except Exception:
            run[field] = []
    return web.json_response({"run": run})


async def _handle_run_clone(request: web.Request) -> web.Response:
    """Clone a run — return a prefilled payload the UI can use to start a new chat."""
    run_id = request.match_info["id"]
    run = auth_db.get_agent_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="run_not_found")
    tool_seq = []
    try:
        tool_seq = json.loads(run.get("tool_sequence") or "[]")
    except Exception:
        pass
    destructive_tools = {"write_file", "patch", "terminal", "execute_code", "delete_file"}
    had_destructive = bool(set(tool_seq) & destructive_tools)
    return web.json_response({
        "clone": {
            "user_message": run.get("user_message", ""),
            "session_id": run.get("session_id", ""),
            "model": run.get("model", ""),
            "original_run_id": run_id,
            "had_destructive_tools": had_destructive,
            "warning": (
                "This run used destructive tools. Review carefully before running."
                if had_destructive else None
            ),
        }
    })


async def start_http_api(runner: Any, port: int = 8091) -> None:
    """Start the aiohttp server. Call as an asyncio task."""
    global _start_time
    _start_time = time.time()

    # Initialise auth DB alongside existing Logos state
    global _hermes_home
    hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
    _hermes_home = hermes_home
    auth_db.init_db(hermes_home)

    # Ensure a stable JWT secret exists for local installs.
    # K8s sets LOGOS_JWT_SECRET (or legacy HERMES_JWT_SECRET) via a k8s Secret;
    # local desktop/CLI installs never set it.  Generate once, persist to
    # ~/.logos/.jwt_secret so tokens survive gateway restarts without forcing
    # re-login every time.  Also treat the known template placeholder as unset —
    # k8s/02-secret.yaml ships with REPLACE_WITH_JWT_SECRET so a fresh cluster
    # with un-edited secrets gets a real random value rather than the publicly
    # known placeholder.
    _KNOWN_JWT_PLACEHOLDERS = {"", "REPLACE_WITH_JWT_SECRET", "replace_with_jwt_secret"}
    _current_jwt = (
        os.environ.get("LOGOS_JWT_SECRET")
        or os.environ.get("HERMES_JWT_SECRET")
        or ""
    )
    if _current_jwt in _KNOWN_JWT_PLACEHOLDERS:
        import secrets as _secrets
        _jwt_secret_path = hermes_home / ".jwt_secret"
        if _jwt_secret_path.exists():
            _persisted = _jwt_secret_path.read_text().strip()
            os.environ["LOGOS_JWT_SECRET"] = _persisted
            os.environ["HERMES_JWT_SECRET"] = _persisted
        else:
            _jwt_secret_path.parent.mkdir(parents=True, exist_ok=True)
            _new_secret = _secrets.token_hex(32)
            _jwt_secret_path.write_text(_new_secret)
            _jwt_secret_path.chmod(0o600)
            os.environ["LOGOS_JWT_SECRET"] = _new_secret
            os.environ["HERMES_JWT_SECRET"] = _new_secret
            logger.info("Generated new JWT secret at %s", _jwt_secret_path)

    # ── Host preflight: fs.inotify.max_user_instances ──────────────
    # Every OpenShell gateway runs a full k3s cluster inside its
    # container, and k3s's kubelet/containerd/coredns/CNI chain
    # consumes ~50-80 inotify instances per cluster. The kernel
    # default (``512`` on most distros) caps you at ~7 gateways
    # before provisioning starts failing with "k3s healthcheck loop"
    # which surfaces in the UI as the generic "gateway info reported
    # the underlying gateway is unreachable."
    #
    # We can't bump the sysctl from inside the gateway process (needs
    # root), but a warning at boot + the fix inline saves an onboarding
    # trap where someone adds a 4th-8th agent months later and has no
    # idea why the route won't come up.
    try:
        _mui_path = pathlib.Path("/proc/sys/fs/inotify/max_user_instances")
        _muw_path = pathlib.Path("/proc/sys/fs/inotify/max_user_watches")
        _mui = int(_mui_path.read_text().strip()) if _mui_path.exists() else 0
        _muw = int(_muw_path.read_text().strip()) if _muw_path.exists() else 0
        # Rough rule of thumb: one openshell k3s cluster ~= 70 instances.
        # Warn below 2048 (which gives ~28 clusters headroom, enough for
        # any realistic homelab multi-agent setup).
        _MUI_SAFE_THRESHOLD = 2048
        _MUW_SAFE_THRESHOLD = 524288
        _route_budget = max(1, _mui // 70) if _mui else 0
        if _mui and _mui < _MUI_SAFE_THRESHOLD:
            logger.warning(
                "preflight: fs.inotify.max_user_instances=%d — you can reliably "
                "run ~%d OpenShell routes on this host before provisioning "
                "starts failing with 'gateway info reported unreachable'. "
                "Raise with:  sudo sysctl -w fs.inotify.max_user_instances=8192  "
                "(and persist: echo 'fs.inotify.max_user_instances=8192' | sudo "
                "tee -a /etc/sysctl.d/99-openshell.conf && sudo sysctl --system)",
                _mui, _route_budget,
            )
        elif _mui:
            logger.info(
                "preflight: fs.inotify.max_user_instances=%d (~%d routes headroom) ✓",
                _mui, _route_budget,
            )
        if _muw and _muw < _MUW_SAFE_THRESHOLD:
            logger.warning(
                "preflight: fs.inotify.max_user_watches=%d — bump to 1048576 "
                "to avoid containerd stalls on large sandbox filesystems.",
                _muw,
            )
    except Exception as _pf_exc:
        logger.debug("preflight inotify check skipped: %s", _pf_exc)

    # LOGOS_WIPE_ON_START: wipe setup state so /setup always runs fresh (setup-test deployments)
    _wipe_flag = (
        os.environ.get("LOGOS_WIPE_ON_START")
        or os.environ.get("HERMES_WIPE_ON_START")
        or ""
    ).lower()
    if _wipe_flag in ("1", "true", "yes"):
        try:
            auth_db.reset_setup_completed()
            for _m in auth_db.list_machines():
                auth_db.delete_machine(_m["id"])
            for _p in auth_db.list_policies():
                auth_db.delete_policy(_p["id"])
            logger.info("LOGOS_WIPE_ON_START: wiped setup state, machines, and policies")
        except Exception as _wipe_err:
            logger.warning("LOGOS_WIPE_ON_START: partial failure: %s", _wipe_err)
    # Env-var admin seeding (takes priority over generic seed)
    _ensure_admin_exists()
    # Generic seed: machines → profiles → admin user (all no-ops on existing data)
    from gateway import seed as _seed
    _seed.run_seed()

    # Zombie-dispatch sweep: any dispatch row still at status='running'
    # when the process starts was orphaned by a crash or restart of the
    # previous gateway instance — no worker is alive to complete it, so
    # leaving it 'running' contaminates the Activity → Events view as
    # though a task is in-flight. Mark all such rows as 'interrupted'
    # with a short explanatory error. One-shot; subsequent dispatches
    # follow the normal running → ok/error lifecycle.
    try:
        _swept = auth_db.sweep_orphaned_dispatches()
        if _swept:
            logger.info("startup: swept %d orphaned dispatch row(s) (gateway restarted mid-flight)", _swept)
    except Exception as _sweep_err:
        logger.warning("startup: dispatch sweep failed: %s", _sweep_err)

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-CSRF-Token",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app["runner"] = runner

    # Executor — OpenShell is the only supported sandbox runtime.
    from gateway.executors import build_executor
    app["executor"] = build_executor()
    logger.info("Instance executor: %s", type(app["executor"]).__name__)

    # Resurrect any agent records whose sandbox no longer exists in OpenShell,
    # and prune any orphan hermes-* sandboxes whose agent record was deleted
    # while the gateway was down. Both run in the background — spawns are
    # slow (30-60s each) but the dashboard shows phase=provisioning while
    # they're in flight, and deletes are fire-and-forget.
    asyncio.create_task(_resurrect_missing_sandboxes(app["executor"]))
    asyncio.create_task(_prune_orphan_sandboxes(app["executor"]))

    # Worker registry lives on the runner (see GatewayRunner.__init__). We
    # just expose it on the aiohttp app so existing request handlers that
    # read request.app.get("worker_registry") keep working. Having the
    # runner own the registry lets dispatch_platform_message route inbound
    # platform messages through the same code path as the HTTP /chat
    # handler without either side instantiating its own copy.
    worker_registry = runner.worker_registry
    app["worker_registry"] = worker_registry

    # Start periodic sandbox state sync (logs every 30min, sessions every 1hr).
    # Memory sync is per-dispatch (change-detected), not periodic.
    if worker_registry:
        worker_registry.start_background_sync_tasks()

    # ── Pre-fetch cloud-model pricing from OpenRouter ──────────────────────
    # So the first dispatch doesn't stall waiting on a cold catalogue
    # fetch. Best-effort — if OpenRouter is unreachable at boot the
    # cost tracker silently records rows with pricing_known=False and
    # the UI surfaces "N requests with unknown pricing".
    try:
        from gateway import pricing as _pricing
        import asyncio as _asyncio_local
        await _asyncio_local.to_thread(_pricing.ensure_loaded)
    except Exception as exc:
        logger.debug("startup pricing fetch skipped: %s", exc)

    # ── Reconcile state file with reality on startup ───────────────────────
    # The state file at ~/.logos/openshell_instances.json persists across
    # restarts but can drift from the actual cluster (sandbox deleted via
    # CLI, gateway crashed mid-spawn leaving a stale "provisioning" entry,
    # etc.). Without reconciliation the UI shows agents as "not ready"
    # after a gateway restart even though their pods are alive — the user
    # has to send a message to "wake them up" via the dispatch path.
    # This reconciler:
    #   1. dedupes per sandbox_name (ready > provisioning > error)
    #   2. for each entry, polls `openshell sandbox list` on the relevant
    #      gateway and overwrites phase based on the actual k8s state
    #      (Ready -> "ready", anything else -> drop)
    # Cheap (one CLI call per gateway, ~1-2s total) and only runs once.
    try:
        await _reconcile_sandbox_state()
    except Exception as exc:
        logger.warning("startup state reconciliation failed: %s", exc)

    # ── Centralized MCP gateway service ────────────────────────────────────
    # Boots all configured MCP servers once and exposes them over HTTP so
    # agents in any executor mode (local, OpenShell, k8s) can connect via URL.
    try:
        from gateway.mcp_service import MCPGatewayService, load_mcp_gateway_config
        import gateway as _gw_module
        _mcp_cfg = load_mcp_gateway_config()
        _mcp_svc = MCPGatewayService(_mcp_cfg)
        app["mcp_service"] = _mcp_svc
        # Expose via module-level ref so mcp_access_tool.py can reach it
        _gw_module._mcp_service_ref = _mcp_svc
        import os as _os
        _os.environ["HERMES_GATEWAY_MCP"] = "1"

        # Register the in-process 'logos' capabilities server. This has to
        # happen BEFORE `_mcp_svc.start()` so the logos server is visible
        # in the very first catalogue snapshot; the start() coroutine just
        # boots external stdio subprocesses and doesn't touch our injected
        # entries. The runner is passed directly from start_http_api so
        # tools can reach runner.adapters / runner.session_store / etc.
        try:
            from gateway.mcp_logos import register_logos_server
            _mcp_svc._logos_server = register_logos_server(runner, _mcp_svc)
            logger.info("MCP gateway: in-process 'logos' server registered")
        except Exception as _logos_err:
            logger.warning("MCP gateway: failed to register logos server: %s", _logos_err)

        asyncio.ensure_future(_mcp_svc.start())
        logger.info("MCP gateway service initialised (%d server(s) configured)",
                    len(_mcp_cfg.get("mcp_servers") or {}))

        # Rewire docker-deployed MCP servers on startup. Their config
        # lives in the mcp_servers DB table (not config.yaml) so the
        # startup load above doesn't see them, which means after a
        # gateway restart the DB row shows status=running but the
        # gateway's proxy has no idea the server exists. Re-register
        # each one into mcp_service._servers_cfg so /mcp/<name> works
        # and auto-granted toolsets resolve to real tools.
        async def _rewire_docker_mcp_servers():
            try:
                from gateway.auth import db as _mcp_db
                from gateway.mcp_management import _auto_wire_server
            except Exception as _imp_err:
                logger.warning("MCP rewire: import failed: %s", _imp_err)
                return
            try:
                _rows = _mcp_db.list_mcp_servers() or []
            except Exception as _list_err:
                logger.warning("MCP rewire: list_mcp_servers failed: %s", _list_err)
                return
            _rewired = 0
            for _row in _rows:
                if _row.get("deploy_mode") != "docker":
                    continue
                if _row.get("status") != "running" or not _row.get("url"):
                    continue
                try:
                    await _auto_wire_server(
                        app, _row["name"], _row["url"], _row.get("token") or "",
                    )
                    _rewired += 1
                except Exception as _wire_err:
                    logger.warning(
                        "MCP rewire: auto_wire_server(%s) failed: %s",
                        _row.get("name"), _wire_err,
                    )
            if _rewired:
                logger.info("MCP rewire: re-registered %d docker server(s)", _rewired)

                # Grants are in-memory, so a gateway restart wipes them
                # and every sandbox's first MCP call 403s. Re-grant for
                # every running sandbox before the config refresh so
                # the sandbox's freshly-uploaded MCP client has a
                # valid session when it connects through the proxy.
                try:
                    from gateway.executors.openshell import (
                        _load_state as _load_sb_state,
                        _grant_auto_mcp_access,
                    )
                    _granted_total = 0
                    for _inst in _load_sb_state() or []:
                        _wid = _inst.get("worker_id") or _inst.get("sandbox_name")
                        if _wid:
                            _granted_total += _grant_auto_mcp_access(_wid)
                    if _granted_total:
                        logger.info(
                            "MCP rewire: granted %d session/server pair(s)",
                            _granted_total,
                        )
                except Exception as _grant_err:
                    logger.warning("MCP rewire: grant loop failed: %s", _grant_err)

                # Broadcast config refresh so existing sandboxes pick up
                # the ``mcp-<name>`` entries that _auto_granted_mcp_toolsets
                # now yields for these rows.
                _executor = app.get("executor")
                if _executor and hasattr(_executor, "refresh_all_instance_configs"):
                    try:
                        pushed = await asyncio.to_thread(
                            _executor.refresh_all_instance_configs
                        )
                        logger.info(
                            "MCP rewire: refreshed %d sandbox instance-config(s)",
                            pushed,
                        )
                    except Exception as _ref_err:
                        logger.warning("MCP rewire: refresh failed: %s", _ref_err)

        asyncio.ensure_future(_rewire_docker_mcp_servers())
    except Exception as _mcp_err:
        logger.warning("MCP gateway service failed to initialise: %s", _mcp_err)
        app["mcp_service"] = None

    # ── Inject tool credentials from DB into os.environ ────────────────
    try:
        from gateway.services import inject_credentials, autodetect_local_services
        _n_creds = inject_credentials()
        if _n_creds:
            logger.info("Injected %d tool credential(s) from DB", _n_creds)
        # Local-first auto-detect: if browserless / firecrawl / other
        # selfhosted services are running and unconfigured, save them.
        # Probes the gateway's network context for any reachable form
        # but persists the canonical *.internal URL the sandbox uses.
        _n_auto = autodetect_local_services()
        if _n_auto:
            logger.info("Autodetected %d local self-hosted service(s): %s",
                        len(_n_auto), _n_auto)
    except Exception as _cred_err:
        logger.debug("Could not inject credentials: %s", _cred_err)

    # Workflow engine — lazily imported to avoid circular deps at module load.
    try:
        from workflows.engine import WorkflowEngine as _WFEngine
        app["workflow_engine"] = _WFEngine(runner)
        logger.info("Workflow engine initialised")
    except Exception as _wf_err:
        logger.warning("Workflow engine failed to initialise: %s", _wf_err)
        app["workflow_engine"] = None

    _load_souls()

    # ── Public routes ──────────────────────────────────────────────────────
    app.router.add_get("/health",        _handle_health)
    app.router.add_get("/healthz",       _handle_health)       # K8s liveness probe alias
    app.router.add_get("/health/ready",  _handle_health_ready)
    app.router.add_get("/favicon.ico",   _handle_favicon)      # public — Edge --app needs this before auth
    app.router.add_get("/api/hue",       _handle_hue)          # public — tray icon phase-lock
    app.router.add_get("/chat_logo.png", _handle_logo)
    app.router.add_get("/login",         _handle_login_page)
    app.router.add_get("/api/model-catalog", _handle_model_catalog)

    # ── Auth routes (no cookie required) ───────────────────────────────────
    app.router.add_post("/auth/login",   handle_login)
    app.router.add_post("/auth/logout",  handle_logout)
    app.router.add_post("/auth/refresh", handle_refresh)

    # ── Authenticated routes ───────────────────────────────────────────────
    from gateway import setup_handlers as _sh
    # ── MCP gateway routes ─────────────────────────────────────────────────
    from gateway import mcp_handlers as _mch
    app.router.add_get("/api/mcp/catalogue",                    _mch.handle_catalogue)
    app.router.add_get("/api/mcp/status",                       _mch.handle_mcp_status)
    app.router.add_post("/api/mcp/grants/{session_id}/{server}", _mch.handle_grant)
    app.router.add_delete("/api/mcp/grants/{session_id}/{server}", _mch.handle_revoke)
    # StreamableHTTP proxy — catch-all for /mcp/{name} and /mcp/{name}/...
    app.router.add_route("*", r"/mcp/{server_name}",           _mch.handle_mcp_proxy)
    app.router.add_route("*", r"/mcp/{server_name}/{tail:.*}", _mch.handle_mcp_proxy)

    # ── Tools tab — MCP server management ────────────────────────────
    from gateway import mcp_management as _mcm
    _mcm.register_routes(app)

    # ── Unified Services (tool credentials + MCP catalogue) ────────────
    app.router.add_get("/api/services",       _handle_services_catalogue)
    app.router.add_post("/api/services/keys", _handle_services_set_key)
    app.router.add_delete("/api/services/keys", _handle_services_delete_key)
    app.router.add_post("/api/services/validate", _handle_services_validate_key)
    app.router.add_post("/api/services/inference", _handle_services_inference)

    # ── Messaging platform integrations (Channels tab) ────────────────
    app.router.add_get("/api/services/messaging",          _handle_messaging_catalogue)
    app.router.add_post("/api/services/messaging/keys",    _handle_messaging_set_key)
    app.router.add_delete("/api/services/messaging/keys",  _handle_messaging_delete_key)
    app.router.add_post("/api/services/messaging/validate", _handle_messaging_validate)

    app.router.add_get("/setup",              _handle_setup_page)
    app.router.add_get("/api/setup/probe",    _sh.handle_setup_probe)
    app.router.add_get("/api/setup/scan",     _sh.handle_setup_scan)
    app.router.add_get("/api/setup/status",   _handle_setup_status)
    app.router.add_post("/api/setup/pull",    _sh.handle_setup_pull)
    app.router.add_post("/api/setup/compare", _sh.handle_setup_compare)
    app.router.add_post("/api/setup/compare/cancel-server", _sh.handle_setup_compare_cancel_server)
    app.router.add_post("/api/setup/test",    _sh.handle_setup_test)
    app.router.add_get("/api/setup/model-catalog",       _sh.handle_model_catalog)
    app.router.add_post("/api/setup/validate-provider", _sh.handle_validate_provider)
    app.router.add_post("/api/setup/complete",    _sh.handle_setup_complete)
    app.router.add_post("/api/setup/prewarm",     _sh.handle_setup_prewarm)
    app.router.add_get("/api/setup/progress",     _sh.handle_setup_progress)
    app.router.add_get("/api/setup/discover",       _sh.handle_setup_discover)
    app.router.add_post("/api/setup/set-remote",   _sh.handle_setup_set_remote)
    app.router.add_get("/api/setup/env-probe",       _sh.handle_setup_env_probe)
    app.router.add_post("/api/setup/reset",
        require_csrf(require_permission("manage_platform")(_handle_setup_reset)))
    app.router.add_post("/api/setup/wipe",
        require_csrf(require_permission("manage_platform")(_handle_setup_wipe)))

    app.router.add_get("/",              _handle_index)
    # Per-tab URL paths. Each one serves the same SPA (main_app.html);
    # Alpine reads window.location.pathname on init and pushes state on
    # tab changes so refresh keeps the user on the current page.
    for _tab_path in (
        "/agents",
        "/chats",
        "/compare",
        # New IA (post-reorg)
        "/config",
        "/config/inference",
        "/config/tools",
        "/config/messaging",
        "/config/workflows",
        "/activity",
        "/activity/events",
        "/activity/dashboards",
        "/activity/approvals",
        "/activity/proposals",
        "/admin",
        "/admin/users",
        "/admin/security",
        "/admin/audit",
        # Legacy aliases — kept so existing bookmarks redirect via SPA's
        # _applyUrlToState soft-redirect logic instead of 404'ing.
        "/settings",
        "/settings/inference",
        "/settings/routing",
        "/settings/tools",
        "/settings/channels",
        "/settings/proposals",
        "/admin/workflows",
        "/admin/runs",
        "/admin/sandboxes",
        "/admin/model-routes",
        "/admin/platforms",
        "/admin/approvals",
    ):
        app.router.add_get(_tab_path, _handle_index)
    app.router.add_get("/auth/me",       handle_me)
    app.router.add_get("/users/me",      handle_me)
    app.router.add_patch("/users/me",    handle_users_me_patch)
    app.router.add_get(
        "/users",
        require_permission("manage_users")(handle_users_list),
    )
    app.router.add_post(
        "/users",
        require_permission("manage_users")(require_csrf(handle_users_post)),
    )
    app.router.add_patch(
        "/users/{id}",
        require_permission("manage_users")(require_csrf(handle_users_patch)),
    )
    app.router.add_delete(
        "/users/{id}",
        require_permission("manage_users")(require_csrf(admin_handlers.handle_users_delete)),
    )
    app.router.add_post(
        "/users/{id}/reset",
        require_permission("manage_users")(require_csrf(admin_handlers.handle_users_reset)),
    )
    app.router.add_get(
        "/audit-logs",
        require_permission("view_audit_logs")(handle_audit_logs),
    )
    app.router.add_get(
        "/api/logs",
        require_permission("view_audit_logs")(_handle_log_tail),
    )
    app.router.add_get("/souls",         _handle_souls_get)
    app.router.add_get("/souls/{slug}",  _handle_soul_detail)
    app.router.add_get("/api/world/state",   _handle_world_state)
    app.router.add_get(
        "/instances",
        require_permission("view_instances")(_handle_instances_get),
    )
    app.router.add_post("/instances",    _handle_instances_post)
    app.router.add_delete("/instances/{name}", _handle_instances_delete)
    # Instance management (memory, knowledge, config)
    app.router.add_get("/instances/{name}/memory",              _handle_instance_memory_get)
    app.router.add_put("/instances/{name}/memory",              _handle_instance_memory_put)
    app.router.add_get("/instances/{name}/knowledge",           _handle_instance_knowledge_get)
    app.router.add_post("/instances/{name}/knowledge/ingest",   _handle_instance_knowledge_ingest)
    app.router.add_delete("/instances/{name}/knowledge/{source}", _handle_instance_knowledge_delete)
    app.router.add_get("/instances/{name}/knowledge/search",    _handle_instance_knowledge_search)
    app.router.add_post("/instances/{name}/fork",               _handle_instance_fork)
    # Worker REST (no WebSocket — Plan A-prime TASKS.md #24: each chat
    # dispatch spawns a fresh ``openshell sandbox exec`` subprocess
    # per task via WorkerRegistry.dispatch_task. No persistent worker
    # processes exist, so ``/api/workers`` now lists state-file sandbox
    # entries with a health shim. The old /ws/worker route was removed
    # in the Plan A refactor because the reverse-connection WebSocket
    # pattern was unsupported by OpenShell's L7 proxy after an upstream
    # change.
    app.router.add_get("/api/workers", lambda r: web.json_response(
        {"workers": r.app["worker_registry"].list_workers()}
    ))
    app.router.add_get("/spawn-templates",         _handle_spawn_templates_get)
    app.router.add_put("/spawn-templates",         _handle_spawn_templates_put)
    app.router.add_delete("/spawn-templates/{id}", _handle_spawn_templates_delete)
    app.router.add_get("/status",        _handle_status)
    app.router.add_get("/toolsets",      _handle_toolsets)
    app.router.add_post("/api/toolsets/toggle", _handle_toolsets_toggle)
    app.router.add_get("/sessions",      _handle_sessions)
    app.router.add_get("/api/platform-sessions", _handle_api_platform_sessions)
    app.router.add_get("/api/platform-sessions/{session_id}/messages", _handle_api_session_messages)
    app.router.add_post("/chat",               _handle_chat)
    app.router.add_post("/chat/transcribe",    require_csrf(_handle_transcribe))
    app.router.add_post("/tools/configure",    require_csrf(_handle_tools_configure))
    app.router.add_route("OPTIONS", "/chat",   _handle_index)
    app.router.add_get("/canary/status", _handle_canary_status)
    app.router.add_get("/proxy/state",        _handle_proxy_state)
    app.router.add_post("/proxy/providers/{key}/toggle", _handle_proxy_toggle)
    app.router.add_get("/proxy/models-live",  _handle_proxy_models_live)
    app.router.add_post("/proxy/benchmark",   _handle_proxy_benchmark)
    app.router.add_get("/internal/routing/claims",  _handle_routing_claims)
    app.router.add_post("/internal/routing/apply",  require_csrf(_handle_routing_apply))
    app.router.add_patch("/api/model", require_csrf(_handle_model_patch))

    # ── Admin routes ───────────────────────────────────────────────────────
    _mm  = require_permission("manage_machines")
    _mp  = require_permission("claim_machine")
    _mpr = require_permission("manage_profiles")
    _mu  = require_permission("manage_users")
    _ap  = require_permission("assign_profile")
    _vrd = require_permission("view_routing_debug")

    app.router.add_get("/admin/spawn-stats",   _mm(admin_handlers.handle_spawn_stats))
    app.router.add_get("/admin/costs",         _mm(admin_handlers.handle_costs))
    app.router.add_get("/admin/recommended-models", _mm(admin_handlers.handle_recommended_models))
    app.router.add_post("/admin/machines/{id}/download",
                         _mm(require_csrf(admin_handlers.handle_machine_download_start)))
    app.router.add_get("/admin/machines/{id}/download/{job_id}",
                         _mm(admin_handlers.handle_machine_download_status))
    app.router.add_get("/admin/pricing/status",    _mm(admin_handlers.handle_pricing_status))
    app.router.add_post("/admin/pricing/refresh",  _mm(require_csrf(admin_handlers.handle_pricing_refresh)))
    app.router.add_get("/admin/model-classes", _mm(admin_handlers.handle_model_classes))
    app.router.add_get("/admin/machines",      _mm(admin_handlers.handle_machines_list))
    app.router.add_post("/admin/machines",     _mm(require_csrf(admin_handlers.handle_machines_post)))
    app.router.add_patch("/admin/machines/{id}", _mm(require_csrf(admin_handlers.handle_machines_patch)))
    app.router.add_delete("/admin/machines/{id}", _mm(require_csrf(admin_handlers.handle_machines_delete)))
    app.router.add_post("/admin/machines/reorder", _mm(require_csrf(admin_handlers.handle_machines_reorder)))
    app.router.add_get("/admin/machines/{id}/claims",  _mm(admin_handlers.handle_machine_claims_get))
    app.router.add_put("/machines/{id}/claim",         _mp(require_csrf(admin_handlers.handle_machine_claim_put)))
    app.router.add_delete("/machines/{id}/claim",      _mp(require_csrf(admin_handlers.handle_machine_claim_delete)))
    app.router.add_put("/admin/machines/{id}/capabilities", _mm(require_csrf(admin_handlers.handle_machine_capabilities_put)))
    app.router.add_get("/admin/machines/{id}/health", _mm(admin_handlers.handle_machine_health))
    # Cloud providers
    app.router.add_get("/admin/cloud-providers",              _mm(admin_handlers.handle_cloud_providers_list))
    app.router.add_get("/admin/cloud-provider-models",        _mm(admin_handlers.handle_cloud_provider_models))
    app.router.add_post("/admin/cloud-providers",             _mm(require_csrf(admin_handlers.handle_cloud_providers_post)))
    app.router.add_patch("/admin/cloud-providers/{id}",       _mm(require_csrf(admin_handlers.handle_cloud_providers_patch)))
    app.router.add_delete("/admin/cloud-providers/{id}",      _mm(require_csrf(admin_handlers.handle_cloud_providers_delete)))
    app.router.add_post("/admin/cloud-providers/{id}/activate", _mm(require_csrf(admin_handlers.handle_cloud_providers_activate)))
    app.router.add_post("/admin/cloud-providers/{id}/test",     _mm(require_csrf(admin_handlers.handle_cloud_providers_test)))
    # Named agents
    # ── Sandbox dashboard ──────────────────────────────────────────
    # NOTE: served under /api/admin/* to avoid colliding with the SPA
    # tab paths registered at /admin/sandboxes (those serve main_app.html
    # so deep links / refreshes land on the right tab). The router uses
    # first-match-wins resolution, so any same-path API would be shadowed.
    _vr = require_permission("view_runs")
    app.router.add_get("/api/admin/sandboxes",                    _vr(_handle_sandboxes_list))
    app.router.add_get("/api/admin/sandboxes/{name}/logs",        _vr(_handle_sandbox_logs))
    app.router.add_post("/api/admin/sandboxes/{name}/restart",    _mm(require_csrf(_handle_sandbox_restart)))

    # ── Model routes (multi-OpenShell-gateway routing) ────────────
    # REST CRUD for the model_routes table + the underlying OpenShell
    # sub-gateways. Same /api/admin/* prefix as the sandbox endpoints
    # so the SPA tab path /admin/model-routes (commit 5) doesn't get
    # shadowed when the UI lands. read = manage_machines (consistent
    # with the rest of admin infra); writes are CSRF-protected.
    app.router.add_get(
        "/api/admin/model-routes",
        _mm(_handle_model_routes_list),
    )
    app.router.add_post(
        "/api/admin/model-routes",
        _mm(require_csrf(_handle_model_routes_post)),
    )
    app.router.add_post(
        "/api/admin/model-routes/{id}/restart",
        _mm(require_csrf(_handle_model_routes_restart)),
    )
    app.router.add_post(
        "/api/admin/model-routes/{id}/set-default",
        _mm(require_csrf(_handle_model_routes_set_default)),
    )
    app.router.add_post(
        "/api/admin/model-routes/{id}/refresh",
        _mm(require_csrf(_handle_model_routes_refresh)),
    )
    app.router.add_delete(
        "/api/admin/model-routes/{id}",
        _mm(require_csrf(_handle_model_routes_delete)),
    )

    # ── Platforms (connection state + routing rules) ───────────────
    # Same /api/admin/* prefix, same SPA-tab-collision reason as above.
    app.router.add_get("/api/admin/platforms",                            _mm(_handle_platforms_list))
    app.router.add_post("/api/admin/platforms/routing",                   _mm(require_csrf(_handle_platforms_routing_upsert)))
    app.router.add_delete("/api/admin/platforms/routing/{id}",            _mm(require_csrf(_handle_platforms_routing_delete)))

    app.router.add_get("/admin/dispatches",           _mm(admin_handlers.handle_dispatches_list))
    app.router.add_get("/admin/agents",              _mm(admin_handlers.handle_agents_list))
    app.router.add_post("/admin/agents",             _mm(require_csrf(admin_handlers.handle_agents_post)))
    app.router.add_patch("/admin/agents/{id}",       _mm(require_csrf(admin_handlers.handle_agents_patch)))
    app.router.add_delete("/admin/agents/{id}",      _mm(require_csrf(admin_handlers.handle_agents_delete)))
    # Per-agent Tools editor (MISSING.md M10 scope item 5 — the T pill dropdown).
    # GET bundles both application-layer (agents.toolsets) and infrastructure-
    # layer (agents.applied_presets) state; POST variants toggle one item at a
    # time. Toolset toggles are DB-only; preset toggles also push the merged
    # effective policy to the running sandbox via `openshell policy set`.
    app.router.add_get("/admin/agents/{id}/tools",
                       _mm(admin_handlers.handle_agent_tools_get))
    # Agent runtime logs — tails ~/.logos/agents/<name>/logs/agent.log.
    # Read-only, JSONL-ish response; paginated by ``?tail=N`` (default 200,
    # max 2000). Admin/operator only; regular users get 403. Used by the
    # "📋 Logs" modal on the Agents tab + compare pane so a user can see
    # what their agent was actually doing without shelling into the host.
    app.router.add_get("/admin/agents/{id}/logs",
                       _mm(admin_handlers.handle_agent_logs_get))
    app.router.add_post("/admin/agents/{id}/tools/toolsets/toggle",
                        _mm(require_csrf(admin_handlers.handle_agent_toolsets_toggle)))
    app.router.add_post("/admin/agents/{id}/tools/presets/toggle",
                        _mm(require_csrf(admin_handlers.handle_agent_presets_toggle)))
    # Capabilities — user-facing collapse of toolsets + presets + creds.
    # GET returns the catalogue annotated with per-agent state; POST
    # toggles a capability (atomic apply/remove of all bundled toolsets
    # and presets). Same auth as the legacy tools endpoints.
    app.router.add_get("/admin/agents/{id}/capabilities",
                       _mm(admin_handlers.handle_agent_capabilities_get))
    app.router.add_post("/admin/agents/{id}/capabilities/toggle",
                        _mm(require_csrf(admin_handlers.handle_agent_capabilities_toggle)))
    # Layer 1 URL consent — per-agent website blocklist that the local
    # browser tool checks before every navigation.
    app.router.add_put("/admin/agents/{id}/website-blocklist",
                       _mm(require_csrf(admin_handlers.handle_agent_website_blocklist_put)))
    app.router.add_get("/admin/policies",      _mpr(admin_handlers.handle_policies_list))
    app.router.add_post("/admin/policies",     _mpr(require_csrf(admin_handlers.handle_policies_post)))
    app.router.add_patch("/admin/policies/{id}", _mpr(require_csrf(admin_handlers.handle_policies_patch)))
    app.router.add_delete("/admin/policies/{id}", _mpr(require_csrf(admin_handlers.handle_policies_delete)))
    app.router.add_put("/admin/policies/{id}/rules", _mpr(require_csrf(admin_handlers.handle_policy_rules_put)))
    app.router.add_patch("/admin/users/{id}/policy", _ap(require_csrf(admin_handlers.handle_user_policy_patch)))

    # ── Action policies (behaviour enforcement) ────────────────────────────
    _map = require_permission("manage_action_policies")
    _aap = require_permission("assign_action_policy")
    _vap = require_permission("view_approvals")
    _dap = require_permission("decide_approvals")

    app.router.add_get("/action-policies",         _map(_handle_action_policies_list))
    app.router.add_post("/action-policies",        _map(require_csrf(_handle_action_policies_post)))
    app.router.add_get("/action-policies/{id}",    _map(_handle_action_policies_get))
    app.router.add_patch("/action-policies/{id}",  _map(require_csrf(_handle_action_policies_patch)))
    app.router.add_delete("/action-policies/{id}", _map(require_csrf(_handle_action_policies_delete)))
    app.router.add_patch("/users/{id}/action-policy", _aap(require_csrf(_handle_user_action_policy_patch)))

    # ── Approval requests ──────────────────────────────────────────────────
    app.router.add_get("/approvals",              _vap(_handle_approvals_list))
    app.router.add_get("/approvals/{id}",         _vap(_handle_approvals_get))
    app.router.add_post("/approvals/{id}/approve", _dap(require_csrf(_handle_approvals_approve)))
    app.router.add_post("/approvals/{id}/reject",  _dap(require_csrf(_handle_approvals_reject)))

    # ── Workflow execution layer ────────────────────────────────────────────
    _mwf = require_permission("manage_workflows")
    _twf = require_permission("trigger_workflow")
    _vwf = require_permission("view_workflows")
    _dwf = require_permission("decide_workflow_approvals")

    app.router.add_get("/workflows",               _vwf(_handle_workflows_list))
    app.router.add_post("/workflows",              _mwf(require_csrf(_handle_workflows_post)))
    app.router.add_get("/workflows/{id}",          _vwf(_handle_workflows_get))
    app.router.add_patch("/workflows/{id}",        _mwf(require_csrf(_handle_workflows_patch)))
    app.router.add_delete("/workflows/{id}",       _mwf(require_csrf(_handle_workflows_delete)))
    app.router.add_post("/workflows/{id}/trigger", _twf(require_csrf(_handle_workflow_trigger)))
    app.router.add_get("/workflow-runs",           _vwf(_handle_workflow_runs_list))
    app.router.add_get("/workflow-runs/{id}",      _vwf(_handle_workflow_run_get))
    app.router.add_post("/workflow-runs/{id}/cancel", _twf(require_csrf(_handle_workflow_run_cancel)))
    app.router.add_post("/workflow-runs/approvals/{id}/{decision}", _dwf(require_csrf(_handle_workflow_approval_decide)))

    # ── Agent run records ───────────────────────────────────────────────────
    _vrun = require_permission("view_runs")
    app.router.add_get("/runs",            _vrun(_handle_runs_list))
    app.router.add_get("/runs/{id}",       _vrun(_handle_run_get))
    app.router.add_get("/runs/{id}/clone", _vrun(_handle_run_clone))

    # ── Evolution ───────────────────────────────────────────────────────────
    from gateway import evolution_handlers as _eh
    _vev  = require_permission("view_evolution")
    _mev  = require_permission("manage_evolution")
    _dev  = require_permission("decide_evolution")
    app.router.add_get("/evolution/proposals",           _vev(_eh.handle_list_proposals))
    app.router.add_get("/evolution/proposals/{id}",      _vev(_eh.handle_get_proposal))
    app.router.add_post("/evolution/proposals",          _mev(require_csrf(_eh.handle_create_proposal)))
    app.router.add_post("/evolution/proposals/{id}/decide", _dev(require_csrf(_eh.handle_decide_proposal)))
    app.router.add_post("/evolution/proposals/{id}/answer", _mev(require_csrf(_eh.handle_answer_question)))
    app.router.add_post("/evolution/proposals/{id}/consult", _dev(require_csrf(_eh.handle_consult_frontier)))
    app.router.add_post("/evolution/proposals/{id}/apply",   _dev(require_csrf(_eh.handle_apply_proposal)))
    app.router.add_get("/evolution/settings",            _vev(_eh.handle_get_settings))
    app.router.add_patch("/evolution/settings",          _mev(require_csrf(_eh.handle_update_settings)))

    app.router.add_get("/admin/routing/resolve",  _vrd(admin_handlers.handle_routing_resolve))
    app.router.add_get("/admin/routing/log",      require_permission("view_audit_logs")(admin_handlers.handle_routing_log))
    app.router.add_post("/admin/setup",           _mm(require_csrf(admin_handlers.handle_setup_wizard)))
    app.router.add_get("/routing/preview",        admin_handlers.handle_routing_preview)

    # ── Update status/trigger (launcher file-based IPC) ─────────────────
    import json as _json
    import pathlib as _up_pathlib

    _HERMES_HOME_UPD = _up_pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(_up_pathlib.Path.home() / ".logos"))
    _UPDATE_STATUS_FILE  = _HERMES_HOME_UPD / "update_status.json"
    _UPDATE_TRIGGER_FILE = _HERMES_HOME_UPD / "update_trigger.json"

    async def _handle_update_status(request: web.Request) -> web.Response:
        try:
            if _UPDATE_STATUS_FILE.exists():
                data = _json.loads(_UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
                return web.json_response(data)
        except Exception:
            pass
        return web.json_response({"available": "", "downloading": False, "ready": False, "ready_path": None})

    async def _handle_update_trigger(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            action = body.get("action")
            if action not in ("download", "install"):
                return web.json_response({"error": "invalid action"}, status=400)
            _UPDATE_TRIGGER_FILE.write_text(_json.dumps({"action": action}), encoding="utf-8")
            return web.json_response({"ok": True})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    _mm_upd = require_permission("manage_machines")
    app.router.add_get("/update-status",   _mm_upd(_handle_update_status))
    app.router.add_post("/update-trigger", _mm_upd(require_csrf(_handle_update_trigger)))

    # Serve static assets (logo, etc.)
    import pathlib as _pathlib
    import sys as _sys
    if getattr(_sys, "frozen", False):
        # PyInstaller bundle: __file__ doesn't resolve relative to source tree
        _static_dir = _pathlib.Path(_sys._MEIPASS) / "assets"
    else:
        _static_dir = _pathlib.Path(__file__).parent.parent / "assets"
    if _static_dir.exists():
        app.router.add_static("/static", str(_static_dir), show_index=False)
    # Agent World JS modules
    _world_dir = _pathlib.Path(__file__).parent / "world"
    if _world_dir.exists():
        app.router.add_static("/world", str(_world_dir), show_index=False)

    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP API listening on port %d", port)

    async def _queue_retry_loop():
        """Retry queued instance requests when resources free up."""
        _executor = app["executor"]
        while True:
            await asyncio.sleep(60)
            if not _instance_queue:
                continue
            try:
                loop = asyncio.get_event_loop()
                headroom = await loop.run_in_executor(None, _executor.get_headroom)
                if headroom.can_spawn:
                    req = _instance_queue.pop(0)
                    logger.info("Retrying queued instance for %s", req["requester"])
                    from gateway.executors.base import InstanceConfig as _IC
                    _qlabel = req.get("instance_label") or req.get("soul_slug", "general")
                    await loop.run_in_executor(
                        None, _executor.spawn,
                        _IC(
                            name=_safe_k8s_name(req["requester"], _qlabel),
                            soul_name=req.get("soul_slug", "general"),
                            requester=req["requester"],
                            instance_label=_qlabel,
                        ),
                    )
            except Exception as e:
                logger.warning("Queue retry failed: %s", e)

    asyncio.create_task(_queue_retry_loop())

    # ── Workspace TTL cleanup ───────────────────────────────────────────────
    # Run once at startup to remove any workspaces left over from a previous
    # pod lifecycle, then schedule periodic sweeps.
    _ws_cleanup_interval_hours = float(
        os.environ.get("LOGOS_WORKSPACE_CLEANUP_INTERVAL_HOURS")
        or os.environ.get("HERMES_WORKSPACE_CLEANUP_INTERVAL_HOURS")
        or "1"
    )

    async def _workspace_cleanup_loop():
        """Delete ephemeral workspace directories whose TTL has expired."""
        # Startup sweep — workspaces from crashed/restarted pods accumulate
        try:
            from gateway import workspace as _ws_mod
            loop = asyncio.get_event_loop()
            removed = await loop.run_in_executor(None, _ws_mod.cleanup_expired)
            if removed:
                logger.info("Startup workspace cleanup: removed %d expired workspaces", removed)
            else:
                logger.debug("Startup workspace cleanup: no expired workspaces found")
        except Exception as _wse:
            logger.warning("Startup workspace cleanup failed: %s", _wse)

        # Periodic sweeps
        while True:
            await asyncio.sleep(_ws_cleanup_interval_hours * 3600)
            try:
                from gateway import workspace as _ws_mod
                loop = asyncio.get_event_loop()
                removed = await loop.run_in_executor(None, _ws_mod.cleanup_expired)
                if removed:
                    logger.info(
                        "Periodic workspace cleanup: removed %d expired workspaces", removed
                    )
            except Exception as _wse:
                logger.warning("Periodic workspace cleanup error: %s", _wse)

    asyncio.create_task(_workspace_cleanup_loop())
