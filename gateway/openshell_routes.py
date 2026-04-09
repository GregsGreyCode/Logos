"""
OpenShell route management — provision and tear down per-(provider, model)
inference gateways.

OpenShell's privacy router enforces a *single* forced model per gateway via
``openshell inference set --provider <p> --model <m>``. The model field in
each request body is ignored and overwritten with the configured model. That
means you can't have multiple Logos agents on different models sharing one
OpenShell gateway — they'd all hit the same forced model.

The fix is to run multiple OpenShell gateways on the same host, one per
(provider, model) pair. Each gateway:

  * has its own unique name (``logos-os-<sanitized-model>``)
  * has its own unique host port (``9091``, ``9092``, ...; the original
    primordial gateway is on ``9090`` as ``logos-openshell``)
  * has its own k3s cluster (full Docker container)
  * is pinned to one provider+model via ``openshell inference set``
  * resolves ``inference.local`` (inside its sandboxes) to that one model

Logos's ``OpenShellExecutor.spawn`` reads the agent's ``model_route_id``,
looks up the row in ``model_routes``, and passes ``-g <openshell_name>``
to the spawn subprocess so the sandbox lands inside the right gateway.

This module wraps the ``openshell`` CLI and the ``model_routes`` table.
HTTP endpoints (gateway/http_api.py) and the executor
(gateway/executors/openshell.py) call into here rather than touching the
CLI directly.

Trade-off — known and accepted: each gateway is a full k3s+Docker container
(~200-500 MB RAM idle). 5 routes ≈ 1-2.5 GB RAM overhead even when nothing
is running. A future optimisation could ``openshell gateway stop`` routes
with zero bound sandboxes after N idle minutes; that's deferred until the
basic flow is working.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from typing import Optional

from gateway.auth import db as auth_db

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────────

# The original gateway provisioned by `openshell gateway start`. Logos
# adopts this on first /setup run rather than spinning up a parallel one.
PRIMORDIAL_NAME = "logos-openshell"
PRIMORDIAL_PORT = 9090

# Auto-allocation pool for new gateways. Routes 2..N pick the next free
# port in this range. Cap is intentional — running >9 OpenShell containers
# on a homelab host will exhaust RAM. Raise if needed.
_PORT_ALLOC_START = 9091
_PORT_ALLOC_END = 9099


# ── Helpers ─────────────────────────────────────────────────────────────────

def _openshell_exe() -> Optional[str]:
    """Return the path to the openshell binary, or None if not on PATH."""
    return shutil.which("openshell")


def _sanitize_route_name(model: str) -> str:
    """Convert a model identifier to a valid OpenShell gateway name.

    OpenShell gateway names must be valid Kubernetes resource names (RFC
    1123 subdomains): lowercase alphanumerics and dashes only, starting
    and ending with alphanumeric, max 63 characters.
    """
    s = model.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "unknown"
    return f"logos-os-{s}"[:63].rstrip("-")


def _next_free_port() -> int:
    """Pick the next unused port in the OpenShell allocation range."""
    used = {r["openshell_port"] for r in auth_db.list_model_routes()}
    used.add(PRIMORDIAL_PORT)  # always reserved even if not yet registered
    for p in range(_PORT_ALLOC_START, _PORT_ALLOC_END + 1):
        if p not in used:
            return p
    raise RuntimeError(
        f"No free port in OpenShell allocation range "
        f"{_PORT_ALLOC_START}-{_PORT_ALLOC_END}. "
        f"Destroy an unused route or raise the cap."
    )


def _run_openshell(
    *args: str,
    gateway: Optional[str] = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Invoke the openshell CLI with optional --gateway scoping.

    Raises ``RuntimeError`` if the binary isn't on PATH (rather than the
    less-informative ``FileNotFoundError`` from subprocess). Sets a hard
    timeout because ``gateway start`` can hang on a misconfigured host.
    """
    exe = _openshell_exe()
    if not exe:
        raise RuntimeError(
            "openshell CLI not found on PATH. Install it from "
            "https://github.com/NVIDIA/OpenShell"
        )
    cmd = [exe]
    if gateway:
        cmd.extend(["-g", gateway])
    cmd.extend(args)
    logger.debug("running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _stderr_or_stdout(exc: subprocess.CalledProcessError) -> str:
    """Pick the most informative output for an error log line."""
    return ((exc.stderr or exc.stdout) or str(exc)).strip()[:500]


# ── Status query ────────────────────────────────────────────────────────────

def gateway_is_alive(openshell_name: str) -> bool:
    """Return True if `openshell gateway info -g <name>` exits cleanly."""
    try:
        result = _run_openshell("gateway", "info", gateway=openshell_name,
                                check=False, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def refresh_status(route_id: str) -> Optional[dict]:
    """Re-check OpenShell for the route's gateway status and update the row.

    Used by:
      * /admin/model-routes list refresh (commit 4)
      * background reconciliation if a route is stuck in 'provisioning'

    Status transitions written here:
      * ``provisioning`` → ``ready``  (gateway info succeeded)
      * ``ready``        → ``error``  (gateway info failed)
      * ``error``        → ``ready``  (gateway came back up)
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        return None
    if gateway_is_alive(route["openshell_name"]):
        new_status = "ready"
        new_detail = None
    else:
        new_status = "error"
        new_detail = "gateway info reported the underlying gateway is unreachable"
    if new_status != route["status"] or new_detail != route.get("status_detail"):
        auth_db.update_model_route(
            route_id, status=new_status, status_detail=new_detail,
        )
    return auth_db.get_model_route(route_id)


# ── Route lifecycle ────────────────────────────────────────────────────────

def adopt_primordial(provider: str, model: str) -> dict:
    """Register the existing primordial gateway as a model route.

    Used by /setup on first run when the user already has the
    ``logos-openshell`` gateway running (provisioned out-of-band by
    ``openshell gateway start``). Pins its inference route to the chosen
    model and writes the row. Marked ``is_primordial=True`` so it can't
    be deleted from the admin UI even when no agents are bound to it.
    """
    if not gateway_is_alive(PRIMORDIAL_NAME):
        raise RuntimeError(
            f"primordial gateway '{PRIMORDIAL_NAME}' is not running. "
            f"Start it with `openshell gateway start --name {PRIMORDIAL_NAME}` first."
        )

    # Ensure the provider record exists on the gateway. Idempotent: if it
    # already exists, openshell exits non-zero with a "name in use" error
    # we tolerate.
    try:
        _run_openshell(
            "provider", "create",
            "--name", provider,
            "--type", "openai",
            "--credential", "OPENAI_API_KEY",
            gateway=PRIMORDIAL_NAME,
        )
    except subprocess.CalledProcessError as exc:
        msg = _stderr_or_stdout(exc).lower()
        if "exists" not in msg and "in use" not in msg and "duplicate" not in msg:
            raise RuntimeError(
                f"failed to create provider '{provider}' on primordial gateway: "
                f"{_stderr_or_stdout(exc)}"
            )

    # Pin the inference route to the chosen model. This is the actual
    # behaviour change OpenShell needs — without this, requests through
    # inference.local keep going to whatever model was last set.
    try:
        _run_openshell(
            "inference", "set",
            "--provider", provider,
            "--model", model,
            "--no-verify",
            gateway=PRIMORDIAL_NAME,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"failed to pin inference route on primordial gateway: "
            f"{_stderr_or_stdout(exc)}"
        )

    # Write the DB row. The UNIQUE(provider, model) constraint catches
    # double-adopts; we let the caller handle the resulting IntegrityError.
    return auth_db.create_model_route(
        provider=provider,
        model=model,
        openshell_name=PRIMORDIAL_NAME,
        openshell_port=PRIMORDIAL_PORT,
        status="ready",
        is_default=True,
        is_primordial=True,
    )


def create_route_provisioning_row(provider: str, model: str) -> dict:
    """Step 1 of cold provision — insert the model_routes row in
    'provisioning' status and return it.

    This is the FAST half of provisioning (just a DB insert + a name and
    port pick). Splitting it out from the slow gateway-start lets the
    HTTP handler respond immediately so the admin UI can show the new
    row in the table while the actual openshell calls finish in a
    background task. The user no longer has to stare at a wedged
    "provisioning…" modal for 60s.
    """
    name = _sanitize_route_name(model)
    port = _next_free_port()
    try:
        return auth_db.create_model_route(
            provider=provider,
            model=model,
            openshell_name=name,
            openshell_port=port,
            status="provisioning",
        )
    except Exception as exc:
        raise RuntimeError(
            f"could not register route for {provider}/{model}: {exc}"
        )


def finish_provisioning(route_id: str, set_as_default: bool = False) -> dict:
    """Steps 2-5 of cold provision — actually start the openshell gateway,
    register the provider, pin the inference route, and mark the row
    'ready'.

    Assumes ``create_route_provisioning_row`` has already inserted the
    row in 'provisioning' status. On any failure the row is updated to
    'error' with status_detail populated, the underlying gateway is
    best-effort destroyed, and the exception is re-raised.

    Designed to be called from a background task (via
    ``asyncio.to_thread``) so the HTTP request that triggered the
    provision can return long before this finishes.
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        raise RuntimeError(f"finish_provisioning: route {route_id} not found")

    name = route["openshell_name"]
    port = route["openshell_port"]
    provider = route["provider"]
    model = route["model"]

    def _fail(detail: str, cleanup_gateway: bool = False) -> None:
        if cleanup_gateway:
            try:
                _run_openshell("gateway", "destroy", "--force",
                                gateway=name, check=False, timeout=60)
            except Exception as cleanup_err:
                logger.warning(
                    "openshell gateway destroy cleanup failed for %s: %s",
                    name, cleanup_err,
                )
        auth_db.update_model_route(route_id, status="error", status_detail=detail[:500])

    # Step 2: Start the gateway (slow, ~30-60s on cold start)
    try:
        _run_openshell(
            "gateway", "start",
            "--name", name,
            "--port", str(port),
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        _fail(f"timeout waiting for `openshell gateway start --name {name}`",
              cleanup_gateway=True)
        raise RuntimeError(f"gateway start timed out for route {route_id}")
    except subprocess.CalledProcessError as exc:
        _fail(_stderr_or_stdout(exc), cleanup_gateway=True)
        raise RuntimeError(
            f"failed to start OpenShell gateway '{name}': {_stderr_or_stdout(exc)}"
        )

    # Step 3: Register the provider on the new gateway.
    #
    # CRITICAL: must pass BOTH the credential (OPENAI_API_KEY) AND the
    # config (OPENAI_BASE_URL) so the new gateway knows where to send
    # inference requests. Earlier code only passed --credential, leaving
    # the new gateway with no LM Studio host URL — workers connected,
    # made their first chat-completions call, got a connection error,
    # and crashed within ~16 seconds of registering. The user hit this
    # on the very first cold-provision of an alternate route.
    #
    # Resolution order for the LM Studio URL:
    #   1. Look up the user's "Local Node" (or first enabled) machine
    #      record in auth.db — that's where /setup writes the URL the
    #      user picked during the wizard.
    #   2. Fall back to OPENAI_BASE_URL env var if the gateway is being
    #      provisioned outside the normal /setup flow (e.g. CLI tests).
    #   3. Fall back to "http://host.docker.internal:1234/v1" — the
    #      default LM Studio URL on a vanilla Docker setup.
    base_url = ""
    api_key = "unused"
    try:
        machines = auth_db.list_machines() if hasattr(auth_db, "list_machines") else []
        for m in machines:
            if m.get("enabled") and m.get("endpoint_url"):
                base_url = m["endpoint_url"]
                if m.get("api_key"):
                    api_key = m["api_key"]
                break
    except Exception as exc:
        logger.warning("could not read machines from auth.db: %s", exc)
    if not base_url:
        import os as _os
        base_url = _os.environ.get("OPENAI_BASE_URL") or "http://host.docker.internal:1234/v1"

    try:
        _run_openshell(
            "provider", "create",
            "--name", provider,
            "--type", "openai",
            "--credential", f"OPENAI_API_KEY={api_key}",
            "--config", f"OPENAI_BASE_URL={base_url}",
            gateway=name,
        )
    except subprocess.CalledProcessError as exc:
        msg = _stderr_or_stdout(exc).lower()
        if "exists" not in msg and "in use" not in msg and "duplicate" not in msg:
            _fail(_stderr_or_stdout(exc), cleanup_gateway=True)
            raise RuntimeError(
                f"failed to create provider on '{name}': {_stderr_or_stdout(exc)}"
            )

    # Step 4: Pin the inference route
    try:
        _run_openshell(
            "inference", "set",
            "--provider", provider,
            "--model", model,
            "--no-verify",
            gateway=name,
        )
    except subprocess.CalledProcessError as exc:
        _fail(_stderr_or_stdout(exc), cleanup_gateway=True)
        raise RuntimeError(
            f"failed to set inference on '{name}': {_stderr_or_stdout(exc)}"
        )

    # Step 5: Mark ready and optionally promote
    auth_db.update_model_route(route_id, status="ready", status_detail=None)
    if set_as_default:
        auth_db.set_default_model_route(route_id)
    return auth_db.get_model_route(route_id)


def provision_new_route(provider: str, model: str,
                         set_as_default: bool = False) -> dict:
    """Spin up a brand-new OpenShell gateway for (provider, model).

    Synchronous full provision — does both halves (create row + finish).
    Used by ``provision_or_reuse_route`` which is in turn called from
    ``/setup`` (an interactive wizard that already shows progress).
    The HTTP admin handler uses ``create_route_provisioning_row`` +
    ``finish_provisioning`` directly so it can return as soon as the
    row exists and finish the slow part in a background task.
    """
    route = create_route_provisioning_row(provider, model)
    return finish_provisioning(route["id"], set_as_default=set_as_default)


def provision_or_reuse_route(provider: str, model: str,
                              set_as_default: bool = False) -> dict:
    """Get an existing route for (provider, model) or provision a new one.

    Resolution order:
      1. If a model_routes row already exists for (provider, model), reuse
         it. Re-pin the underlying gateway's inference route as a side
         effect (cheap, idempotent) so the actual OpenShell state matches
         the DB.
      2. If NO routes exist at all, try to adopt the primordial
         ``logos-openshell`` gateway as the first route.
      3. Otherwise, provision a fresh OpenShell gateway alongside the
         existing ones (slow path — see provision_new_route).

    Used by /setup (commit 3) and the /admin/model-routes POST handler
    (commit 4). Caller is expected to be in a non-blocking context — this
    function is sync and can take >60s on the cold-provision path.
    """
    # 1. Existing match
    existing = auth_db.get_model_route_by_provider_model(provider, model)
    if existing:
        # Re-pin the inference route in case OpenShell state drifted (e.g.
        # the gateway was restarted manually). Best-effort: errors are
        # surfaced via status_detail but don't prevent reuse.
        try:
            _run_openshell(
                "inference", "set",
                "--provider", provider,
                "--model", model,
                "--no-verify",
                gateway=existing["openshell_name"],
            )
            auth_db.update_model_route(existing["id"], status="ready",
                                        status_detail=None)
        except subprocess.CalledProcessError as exc:
            auth_db.update_model_route(existing["id"], status="error",
                                        status_detail=_stderr_or_stdout(exc))
            logger.warning(
                "re-pinning route %s failed: %s",
                existing["id"], _stderr_or_stdout(exc),
            )
        if set_as_default:
            auth_db.set_default_model_route(existing["id"])
        return auth_db.get_model_route(existing["id"])

    # 2. No routes exist — adopt the primordial if it's alive
    if not auth_db.list_model_routes() and gateway_is_alive(PRIMORDIAL_NAME):
        return adopt_primordial(provider, model)

    # 3. Fresh provision
    return provision_new_route(provider, model, set_as_default=set_as_default)


def restart_route(route_id: str) -> dict:
    """Stop and re-start the OpenShell gateway underlying a route.

    Useful when the gateway has wedged or after the host reboots without
    an autostart unit. Sandboxes will lose their connection during the
    restart and will need to respawn.
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        raise RuntimeError(f"route {route_id} not found")

    auth_db.update_model_route(route_id, status="provisioning",
                                status_detail="restart in progress")

    name = route["openshell_name"]
    port = route["openshell_port"]

    try:
        _run_openshell("gateway", "stop", gateway=name, check=False, timeout=60)
        _run_openshell(
            "gateway", "start",
            "--name", name,
            "--port", str(port),
            timeout=300,
        )
        _run_openshell(
            "inference", "set",
            "--provider", route["provider"],
            "--model", route["model"],
            "--no-verify",
            gateway=name,
        )
        auth_db.update_model_route(route_id, status="ready", status_detail=None)
        return auth_db.get_model_route(route_id)
    except subprocess.CalledProcessError as exc:
        auth_db.update_model_route(route_id, status="error",
                                    status_detail=_stderr_or_stdout(exc))
        raise RuntimeError(f"restart failed for route {route_id}: {_stderr_or_stdout(exc)}")
    except subprocess.TimeoutExpired:
        auth_db.update_model_route(route_id, status="error",
                                    status_detail="restart timeout")
        raise RuntimeError(f"restart timed out for route {route_id}")


def destroy_route(route_id: str) -> bool:
    """Tear down an OpenShell gateway and remove its route record.

    Refuses if the route is primordial or if any agents are still bound
    to it (the caller is expected to either delete the agents first or
    re-bind them to a different route via update_agent).
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        return False
    if route["is_primordial"]:
        raise RuntimeError(
            f"cannot destroy primordial route {route_id} "
            f"({route['openshell_name']}) — it's the original gateway"
        )
    bound = auth_db.count_agents_using_route(route_id)
    if bound > 0:
        raise RuntimeError(
            f"cannot destroy route {route_id}: {bound} agent(s) still bound. "
            f"Re-bind or delete those agents first."
        )

    # Best-effort destroy of the underlying gateway. Even if openshell
    # complains (gateway already gone, etc.), we still want to drop the
    # DB row so the UI doesn't show a phantom route.
    try:
        _run_openshell("gateway", "destroy", "--force",
                        gateway=route["openshell_name"], check=False,
                        timeout=120)
    except Exception as exc:
        logger.warning(
            "openshell gateway destroy failed for %s: %s — dropping DB row anyway",
            route["openshell_name"], exc,
        )

    return auth_db.delete_model_route(route_id)
