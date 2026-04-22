"""Worker Registry — read-only view over OpenShell sandbox state.

Post-Phase-2 this module is no longer a dispatcher. Plan A-Prime's
per-task ``openshell sandbox exec`` subprocess model is gone; all
dispatch flows through ``gateway.worker_registry_v2.dispatch_task_v2``
against the in-sandbox ``hermes gateway run`` HTTP server.

What stays:

* Read-only accessors over the executor state file
  (``~/.logos/openshell_instances.json``) so the admin UI, health
  checks, and heal logic can continue to ask "what sandboxes exist,
  are they healthy, which gateway owns them" without knowing about
  the executor internals. Exposed as ``get()`` / ``workers`` /
  ``list_workers()`` / ``list_healthy()`` for back-compat with older
  call sites.

* ``active_task_count`` — bridged to v2's ``_INFLIGHT`` registry so
  the world-view thought bubbles still render.

* Periodic background sync of sandbox ``logs`` and ``sessions``
  directories to the host per-agent dir. Runs every 30 / 60 minutes
  respectively, independent of dispatch. v2's per-dispatch memory
  sync (``gateway.worker_registry_v2.sync_memories_from_sandbox``)
  handles the write-path-hot directory.

``resolve_sandbox_gateway`` is the shared helper used by v2 dispatch
to route a sandbox name to its owning OpenShell gateway — kept at
module level so v2 doesn't depend on this class.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Health entry shim ─────────────────────────────────────────────────────

@dataclass
class _SandboxHealthEntry:
    """Read-only view on a sandbox's state-file entry.

    Preserves the attribute surface that http_api chat dispatch and
    admin_handlers.handle_agents_list expect from a legacy WorkerEntry
    object. ``.healthy`` is True iff the sandbox is in ``phase ==
    "ready"``; transient "provisioning" entries read as unhealthy.
    """
    _state_entry: dict

    @property
    def worker_id(self) -> str:
        return self._state_entry.get("worker_id", "") or self._state_entry.get("sandbox_name", "")

    @property
    def sandbox_name(self) -> str:
        return self._state_entry.get("sandbox_name", "")

    @property
    def healthy(self) -> bool:
        return self._state_entry.get("phase") == "ready"

    @property
    def status(self) -> str:
        return "idle" if self.healthy else (self._state_entry.get("phase") or "unknown")

    @property
    def soul(self) -> str:
        return self._state_entry.get("soul_name") or "general"

    @property
    def toolsets(self) -> list:
        return self._state_entry.get("toolsets") or []

    @property
    def instance_label(self) -> str:
        return self._state_entry.get("name", "")

    @property
    def requester(self) -> str:
        return self._state_entry.get("requester", "")

    @property
    def registered_at(self) -> float:
        """Unix timestamp when the sandbox record was written to the
        state file. Back-compat with the old persistent-worker API —
        http_api uses it as a cache-buster / incarnation tag so the
        frontend can detect sandbox restarts."""
        val = self._state_entry.get("created_at")
        try:
            return float(val) if val else 0.0
        except (TypeError, ValueError):
            return 0.0

    def to_dict(self) -> dict:
        """UI-facing shape consumed by admin_handlers.handle_agents_list."""
        created = self._state_entry.get("created_at") or 0
        return {
            "worker_id": self.worker_id,
            "sandbox_name": self.sandbox_name,
            "soul": self.soul,
            "toolsets": self.toolsets,
            "instance_label": self.instance_label,
            "requester": self.requester,
            "status": self.status,
            "healthy": self.healthy,
            "uptime_s": int(time.time() - created) if created else 0,
            "current_task_id": None,
            "pid": None,
        }


# ── Gateway routing ───────────────────────────────────────────────────────


def resolve_sandbox_gateway(sandbox_name: str) -> Optional[str]:
    """Look up which OpenShell gateway a sandbox lives in.

    Resolution order:
      1. Exact match in the executor state file
         (``~/.logos/openshell_instances.json``). This is the
         source of truth for multi-gateway installs — each
         state-file entry carries its ``openshell_name``.
      2. Match by agent name in ``auth.db.agents`` → the bound
         ``model_route_id`` → ``model_routes.openshell_name``.
         Covers the case where the state file got pruned but
         the agent row still exists.
      3. Default route from ``get_default_gateway_name()``. Only
         a sane answer if there's exactly one route configured;
         otherwise the caller should have gotten a state-file
         hit via the agent name.
      4. ``None`` — caller raises a clear error.

    The key reason this helper exists: ``openshell sandbox exec``
    without ``-g`` uses whatever gateway is CLI-selected
    (``~/.config/openshell/active_gateway``), which is whichever
    one was added most recently. That means a dispatch to a
    sandbox in gateway A silently targets gateway B if B was
    provisioned more recently — and openshell returns
    ``status: NotFound, message: "sandbox not found"``.
    Looking up the target gateway explicitly and passing ``-g``
    makes dispatch routing deterministic regardless of CLI state.

    Called by ``worker_registry_v2.dispatch_task_v2``. Kept at
    module level so v2 doesn't depend on the registry class.
    """
    # 1. State file lookup
    try:
        from gateway.executors.openshell import _load_state
        for inst in _load_state():
            if (inst.get("sandbox_name") == sandbox_name
                    or inst.get("worker_id") == sandbox_name):
                gw = inst.get("openshell_name")
                if gw:
                    return gw
    except Exception as exc:
        logger.warning(
            "resolve_sandbox_gateway: load_state failed: %s", exc,
        )

    # 2. Agent row lookup by inferred agent name.
    # Sandbox names are ``hermes-<sanitized-agent-name>`` (see
    # OpenShellExecutor._sanitize_sandbox_name), so strip the
    # prefix and try the sanitized match against agent rows.
    try:
        from gateway.auth import db as auth_db
        from gateway.executors.openshell import _sanitize_sandbox_name
        prefix = "hermes-"
        if sandbox_name.startswith(prefix):
            for agent in auth_db.list_agents():
                if _sanitize_sandbox_name(f"hermes-{agent.get('name', '')}") == sandbox_name:
                    route_id = agent.get("model_route_id")
                    if route_id:
                        route = auth_db.get_model_route(route_id)
                        if route and route.get("openshell_name"):
                            return route["openshell_name"]
    except Exception as exc:
        logger.warning(
            "resolve_sandbox_gateway: agent/route lookup failed: %s", exc,
        )

    # 3. Default route fallback (single-gateway install)
    try:
        from gateway.openshell_routes import get_default_gateway_name
        default_gw = get_default_gateway_name()
        if default_gw:
            return default_gw
    except Exception as exc:
        logger.warning(
            "resolve_sandbox_gateway: default gateway lookup failed: %s", exc,
        )

    return None


# ── Registry ──────────────────────────────────────────────────────────────


class WorkerRegistry:
    """Read-only view over the executor state file.

    All dispatch runs through ``worker_registry_v2.dispatch_task_v2``
    (see module docstring). This class exposes the query surface
    (``get``, ``workers``, ``list_workers``, ``list_healthy``,
    ``active_task_count``) used by admin UI, heal logic, and chat
    routing to answer "which sandboxes exist and are they healthy".
    """

    def __init__(self) -> None:
        # No per-process dispatch state — v2 owns it all. The class
        # still exists so older callers can bind one instance to the
        # aiohttp app (``app["worker_registry"]``) and query it.
        pass

    # ─── Read accessors ─────────────────────────────────────────────────

    @property
    def workers(self) -> Dict[str, _SandboxHealthEntry]:
        """Return a dict of every state-file sandbox, keyed by worker_id."""
        from gateway.executors.openshell import _load_state
        out: Dict[str, _SandboxHealthEntry] = {}
        try:
            for inst in _load_state():
                key = inst.get("worker_id") or inst.get("sandbox_name") or ""
                if key:
                    out[key] = _SandboxHealthEntry(inst)
        except Exception as exc:
            logger.warning("worker_registry.workers: load_state failed: %s", exc)
        return out

    def get(self, worker_id: str) -> Optional[_SandboxHealthEntry]:
        """Look up a sandbox by worker_id / sandbox_name. Returns None
        if the state file has no matching entry (agent not yet spawned,
        or the entry was pruned)."""
        from gateway.executors.openshell import _load_state
        try:
            for inst in _load_state():
                if (inst.get("worker_id") == worker_id
                        or inst.get("sandbox_name") == worker_id):
                    return _SandboxHealthEntry(inst)
        except Exception as exc:
            logger.warning("worker_registry.get(%r): load_state failed: %s", worker_id, exc)
        return None

    def list_workers(self) -> List[dict]:
        return [e.to_dict() for e in self.workers.values()]

    def list_healthy(self) -> List[_SandboxHealthEntry]:
        return [e for e in self.workers.values() if e.healthy]

    def active_task_count(self, sandbox_name: str) -> int:
        """Return the number of in-flight v2 dispatches against this
        sandbox.

        Bridges to ``worker_registry_v2._INFLIGHT``. Used by
        ``admin_handlers.handle_agents_list`` to render thought-bubble
        indicators on agents currently processing a task.
        """
        try:
            from gateway.worker_registry_v2 import _INFLIGHT as _v2_inflight
        except ImportError:
            return 0
        return sum(
            1 for e in _v2_inflight.values()
            if e.get("sandbox_name") == sandbox_name
        )

    # ─── Periodic sandbox → host sync (logs + sessions) ─────────────────

    def _download_sandbox_dir(
        self,
        sandbox_name: str,
        gateway: str,
        sandbox_path: str,
        host_dir: Path,
    ) -> bool:
        """Download a directory from a sandbox to the host. Returns True on success."""
        host_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "openshell", "-g", gateway,
                "sandbox", "download",
                sandbox_name, sandbox_path,
                str(host_dir),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0

    def _agent_host_dir(self, agent_name: str) -> Path:
        """Return the per-agent host directory: ~/.logos/agents/<name>/"""
        from gateway.executors.openshell import _HERMES_HOME
        return _HERMES_HOME / "agents" / agent_name

    async def _periodic_sync_loop(
        self,
        sync_type: str,
        sandbox_path: str,
        host_subdir: str,
        interval_seconds: int,
    ) -> None:
        """Background loop that syncs a sandbox directory for all agents on a schedule.

        Args:
            sync_type: Label for logging (e.g. "logs", "sessions")
            sandbox_path: Path inside sandbox (hermes-srv-home layout)
            host_subdir: Subdirectory under ~/.logos/agents/<name>/
            interval_seconds: Seconds between sync cycles
        """
        await asyncio.sleep(60)  # initial delay — let gateway fully start
        while True:
            try:
                from gateway.executors.openshell import _load_state
                for inst in _load_state():
                    if inst.get("phase") != "ready":
                        continue
                    sandbox_name = inst.get("sandbox_name", "")
                    agent_name = inst.get("name", "")
                    gw = inst.get("openshell_name", "")
                    if not sandbox_name or not agent_name or not gw:
                        continue
                    host_dir = self._agent_host_dir(agent_name) / host_subdir
                    try:
                        ok = await asyncio.to_thread(
                            self._download_sandbox_dir,
                            sandbox_name, gw, sandbox_path, host_dir,
                        )
                        if ok:
                            n = len([f for f in host_dir.iterdir() if f.is_file()])
                            if n > 0:
                                logger.debug(
                                    "%s sync: %s → %s (%d file(s))",
                                    sync_type, sandbox_name, host_dir, n,
                                )
                    except Exception as exc:
                        logger.debug(
                            "%s sync: failed for %s: %s",
                            sync_type, sandbox_name, exc,
                        )
            except Exception as exc:
                logger.debug("periodic %s sync cycle failed: %s", sync_type, exc)
            await asyncio.sleep(interval_seconds)

    def start_background_sync_tasks(self) -> None:
        """Launch the periodic log and session sync background tasks.

        Called once at gateway startup from start_http_api(). Paths
        target the v2 hermes-srv-home layout written by
        ``gateway.executors.hermes_server_mode.deploy_hermes_config``.
        """
        asyncio.create_task(
            self._periodic_sync_loop(
                sync_type="logs",
                sandbox_path="/tmp/hermes-srv-home/logs/",
                host_subdir="logs",
                interval_seconds=1800,  # 30 minutes
            ),
            name="sandbox-sync-logs",
        )
        asyncio.create_task(
            self._periodic_sync_loop(
                sync_type="sessions",
                sandbox_path="/tmp/hermes-srv-home/sessions/",
                host_subdir="sessions",
                interval_seconds=3600,  # 1 hour
            ),
            name="sandbox-sync-sessions",
        )
        # Agent-created skills (hermes skill_manager tool writes here).
        # 15 min cadence — skill creation is low-frequency, but we want
        # them visible in the Mind modal's Skills tab soon enough that a
        # user who just asked the agent to "save this as a skill" doesn't
        # have to wait an hour. The host mirror at
        # ~/.logos/agents/<name>/skills/ is what deploy_agent_skills
        # re-uploads on respawn so skills survive sandbox death.
        asyncio.create_task(
            self._periodic_sync_loop(
                sync_type="skills",
                sandbox_path="/tmp/hermes-srv-home/skills/",
                host_subdir="skills",
                interval_seconds=900,  # 15 minutes
            ),
            name="sandbox-sync-skills",
        )
        logger.info("worker_registry: started background logs + sessions + skills sync tasks")


_registry_singleton: Optional[WorkerRegistry] = None


def get_registry() -> WorkerRegistry:
    """Return a process-wide ``WorkerRegistry`` singleton.

    Used by modules that need the registry surface without threading
    it through as a parameter (e.g. ``gateway.world_awareness``).
    """
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = WorkerRegistry()
    return _registry_singleton
