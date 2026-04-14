"""Reactive sandbox heal — respawn a missing sandbox when a chat dispatch
lands on it, rather than bouncing the user back with "sandbox isn't
connected, check Admin → Sandboxes."

Design principles
─────────────────
- **Reactive, not proactive.** We never poll the sandbox fleet. A missing
  sandbox is only noticed the moment a real dispatch tries to talk to
  it. If the user doesn't chat with an agent, its sandbox stays gone.
- **Agent row is the lifecycle.** As long as ``agents.name = X`` exists,
  a chat to X will revive the sandbox. To make an agent permanently
  gone, delete the agent row — deleting only the sandbox is a reset,
  not a goodbye.
- **Single source of spawn logic.** We delegate to
  ``OpenShellExecutor.spawn()`` exactly like the startup
  ``resurrect_missing_sandboxes`` pass does. Same ``InstanceConfig``
  shape, same ``model_route_id`` honouring, so an auto-respawned
  sandbox lands in the right gateway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


async def _ensure_worker_script_present(executor, entry, worker_id: str) -> None:
    """Probe ``/tmp/sandbox_worker.py`` inside the sandbox; upload if missing.

    Cheap guard against the "sandbox adopted by startup reconcile but
    never had its worker script uploaded" failure mode. Costs a single
    ``test -f`` exec call (~100-200ms) and only re-uploads when the
    script is actually absent — so the steady-state overhead is just
    the probe.

    Silently returns on any error (the caller logs at WARNING level).
    """
    if executor is None:
        return
    # The state-file entry carries ``openshell_name`` — the gateway the
    # sandbox lives inside. Without it we'd hit the default gateway and
    # either 404 or operate on the wrong sandbox.
    gateway = getattr(entry, "openshell_name", None) or (
        entry.to_dict().get("openshell_name") if hasattr(entry, "to_dict") else None
    )
    sandbox_name = worker_id  # one-to-one mapping
    _REPO_ROOT = __import__("pathlib").Path(
        "/home/greg/homelab-infra/projects/logos"
    )
    try:
        from gateway.executors.openshell import _REPO_ROOT as _rr
        _REPO_ROOT = _rr
    except Exception:
        pass
    worker_script = _REPO_ROOT / "docker" / "sandbox_worker.py"
    if not worker_script.exists():
        logger.warning("sandbox_heal: host worker script missing at %s", worker_script)
        return

    import asyncio as _asyncio
    import subprocess as _subprocess

    def _probe_and_upload() -> None:
        cmd_probe = ["openshell"]
        if gateway:
            cmd_probe += ["-g", gateway]
        cmd_probe += ["sandbox", "exec", "--no-tty", "--name", sandbox_name,
                      "--", "test", "-f", "/tmp/sandbox_worker.py"]
        try:
            r = _subprocess.run(cmd_probe, capture_output=True, timeout=10)
        except Exception as exc:
            logger.debug("sandbox_heal: worker-script probe failed: %s", exc)
            return
        if r.returncode == 0:
            return  # present, nothing to do
        logger.info(
            "sandbox_heal: /tmp/sandbox_worker.py missing in %s — uploading",
            sandbox_name,
        )
        cmd_upload = ["openshell"]
        if gateway:
            cmd_upload += ["-g", gateway]
        cmd_upload += ["sandbox", "upload", sandbox_name,
                       str(worker_script), "/tmp/"]
        try:
            _subprocess.run(cmd_upload, capture_output=True, timeout=30, check=True)
        except _subprocess.CalledProcessError as exc:
            logger.warning(
                "sandbox_heal: worker-script upload FAILED for %s: %s",
                sandbox_name, (exc.stderr or exc.stdout or b"").decode(errors="replace")[:200],
            )

    await _asyncio.to_thread(_probe_and_upload)


async def _ensure_instance_config_present(
    executor, entry, worker_id: str, agent_record: dict,
) -> None:
    """Probe ``/tmp/hermes/instance-config.json`` inside the sandbox; if
    missing, call ``executor.refresh_instance_config(agent_name)`` to
    re-upload it. Covers the failure mode where a reconciled sandbox
    starts with no config and the worker defaults to an empty model.

    Agent name comes from the passed-in agent_record; we use
    ``refresh_instance_config`` rather than reconstructing the config
    ourselves so every upload path uses the same source-of-truth logic
    (DB credentials, website_blocklist, allowed_hosts derivation, etc.).
    """
    if executor is None:
        return
    if not hasattr(executor, "refresh_instance_config"):
        return
    gateway = getattr(entry, "openshell_name", None) or (
        entry.to_dict().get("openshell_name") if hasattr(entry, "to_dict") else None
    )
    sandbox_name = worker_id
    agent_name = (agent_record or {}).get("name") or ""
    if not agent_name:
        return

    import asyncio as _asyncio
    import subprocess as _subprocess

    def _probe_and_refresh() -> None:
        cmd_probe = ["openshell"]
        if gateway:
            cmd_probe += ["-g", gateway]
        cmd_probe += ["sandbox", "exec", "--no-tty", "--name", sandbox_name,
                      "--", "test", "-f", "/tmp/hermes/instance-config.json"]
        try:
            r = _subprocess.run(cmd_probe, capture_output=True, timeout=10)
        except Exception as exc:
            logger.debug("sandbox_heal: instance-config probe failed: %s", exc)
            return
        if r.returncode == 0:
            return
        logger.info(
            "sandbox_heal: /tmp/hermes/instance-config.json missing in %s — calling refresh_instance_config",
            sandbox_name,
        )
        try:
            ok = executor.refresh_instance_config(agent_name)
        except Exception as exc:
            logger.warning(
                "sandbox_heal: refresh_instance_config(%s) raised: %s",
                agent_name, exc,
            )
            return
        if not ok:
            logger.warning(
                "sandbox_heal: refresh_instance_config(%s) returned False",
                agent_name,
            )

    await _asyncio.to_thread(_probe_and_refresh)


async def ensure_sandbox_alive(
    *,
    worker_registry: Any,
    executor: Any,
    worker_id: str,
    agent_record: dict,
    on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> tuple[bool, Optional[Any]]:
    """Ensure the sandbox backing ``worker_id`` is alive; spawn if absent.

    Args:
        worker_registry: The gateway's ``WorkerRegistry`` instance.
        executor: The sandbox executor (``OpenShellExecutor``). If None or
            non-OpenShell, this helper is a no-op (returns current state).
        worker_id: Sandbox name, e.g. ``hermes-adam``.
        agent_record: Agent row from ``auth_db`` — supplies the soul,
            model, toolsets, and ``model_route_id`` needed to build an
            ``InstanceConfig`` for a fresh spawn.
        on_event: Optional async callback invoked with SSE-shaped dict
            events (``{"type": "provisioning", ...}``). Lets the web chat
            handler pass through to its existing send_event pipe so the
            UI's "Sandbox is provisioning…" overlay renders live.

    Returns:
        (healthy, worker_entry) — ``healthy`` is True if the sandbox is
        now alive and ready to dispatch; ``worker_entry`` is the
        registry entry (or None if heal failed).
    """
    entry = worker_registry.get(worker_id) if worker_registry else None
    if entry and getattr(entry, "healthy", False):
        # Sandbox record says healthy — but two critical uploads might
        # be missing if the sandbox was adopted via ``startup reconcile``
        # (which only marks phase=ready, never uploads) instead of a
        # fresh ``executor.spawn()``:
        #   1. ``/tmp/sandbox_worker.py``  → dispatch exits rc=2
        #      ("python3: can't open file").
        #   2. ``/tmp/hermes/instance-config.json`` → worker runs but
        #      gets model="" → sandbox responds with the fallback
        #      "[sandbox worker ?] Connected! No model configured…"
        # Verify both; re-upload/refresh on miss. Both probes are cheap
        # (~200ms each) and only trigger uploads when something's actually
        # missing — so the steady-state cost per dispatch is just the probes.
        try:
            await _ensure_worker_script_present(executor, entry, worker_id)
        except Exception as exc:
            logger.warning(
                "sandbox_heal: worker-script verify failed for %s (continuing): %s",
                worker_id, exc,
            )
        try:
            await _ensure_instance_config_present(
                executor, entry, worker_id, agent_record,
            )
        except Exception as exc:
            logger.warning(
                "sandbox_heal: instance-config verify failed for %s (continuing): %s",
                worker_id, exc,
            )
        return True, entry

    if not executor or type(executor).__name__ != "OpenShellExecutor":
        # Non-OpenShell executors manage their own lifecycle; we only
        # heal when we know how.
        return False, entry

    try:
        from gateway.executors.base import InstanceConfig
    except Exception as exc:  # pragma: no cover — import error is dev-only
        logger.warning("sandbox_heal: InstanceConfig import failed: %s", exc)
        return False, entry

    agent_name = agent_record.get("name") or ""
    if not agent_name:
        logger.warning("sandbox_heal: no agent name on record; cannot heal %s", worker_id)
        return False, entry

    try:
        toolsets_raw = agent_record.get("toolsets") or ""
        toolsets = json.loads(toolsets_raw) if toolsets_raw else []
        if not isinstance(toolsets, list):
            toolsets = []
    except Exception:
        toolsets = []

    cfg = InstanceConfig(
        name=agent_name,
        soul_name=agent_record.get("soul_slug") or "general",
        model=agent_record.get("model") or "",
        requester="(auto-respawn)",
        instance_label=agent_name,
        toolsets=toolsets,
        # Without model_route_id the executor falls back to the default
        # route and the agent silently respawns onto the wrong model.
        # Same bug class as the resurrect pass's comment.
        model_route_id=agent_record.get("model_route_id"),
    )

    if on_event is not None:
        try:
            await on_event({
                "type": "provisioning",
                "agent": agent_name,
                "worker_id": worker_id,
                "expected_seconds": 25,
                "message": f"Waking {agent_name}…",
            })
        except Exception:
            pass

    t0 = time.time()
    spawn_exc: Optional[Exception] = None
    try:
        await asyncio.to_thread(executor.spawn, cfg)
    except Exception as exc:
        spawn_exc = exc
        # Phantom-sandbox recovery: a previous half-failed spawn can leave
        # OpenShell's k3s holding a record that ``sandbox create`` rejects
        # with "already exists" but ``sandbox list`` doesn't report. The
        # whole resurrect / auto-respawn loop wedges against this. One
        # retry after a forced delete clears it.
        if "already exists" in str(exc).lower():
            logger.info(
                "sandbox_heal: phantom sandbox detected for %s; deleting + retrying spawn",
                worker_id,
            )
            try:
                await asyncio.to_thread(executor.delete_instance, agent_name)
            except Exception as _del_exc:
                logger.debug("sandbox_heal: delete during phantom recovery: %s", _del_exc)
            try:
                await asyncio.to_thread(executor.spawn, cfg)
                spawn_exc = None
            except Exception as _retry_exc:
                spawn_exc = _retry_exc

    if spawn_exc is not None:
        elapsed = time.time() - t0
        logger.warning(
            "sandbox_heal: auto-respawn FAILED agent=%s worker=%s elapsed=%.1fs err=%s",
            agent_name, worker_id, elapsed, spawn_exc,
        )
        if on_event is not None:
            try:
                await on_event({
                    "type": "provisioning_failed",
                    "agent": agent_name,
                    "worker_id": worker_id,
                    "error": str(spawn_exc)[:300],
                })
            except Exception:
                pass
        return False, worker_registry.get(worker_id) if worker_registry else None

    elapsed = time.time() - t0
    entry = worker_registry.get(worker_id) if worker_registry else None
    healthy = bool(entry and getattr(entry, "healthy", False))
    logger.info(
        "sandbox_heal: auto-respawn %s agent=%s worker=%s elapsed=%.1fs",
        "OK" if healthy else "SPAWNED-BUT-UNHEALTHY",
        agent_name, worker_id, elapsed,
    )
    if on_event is not None:
        try:
            await on_event({
                "type": "provisioning_done",
                "agent": agent_name,
                "worker_id": worker_id,
                "elapsed_seconds": round(elapsed, 1),
                "healthy": healthy,
            })
        except Exception:
            pass
    return healthy, entry
