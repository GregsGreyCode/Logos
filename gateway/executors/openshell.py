"""
OpenShellExecutor — runs Hermes agent instances as OpenShell sandboxes.

Integration model (reverse-connection)
──────────────────────────────────────
1.  A sandbox image (``hermes-sandbox``) contains a lightweight WebSocket
    worker (``sandbox_worker.py``) and its dependencies (aiohttp, Python 3.12).

2.  ``spawn()`` creates a named OpenShell sandbox with:
    - An uploaded instance config (``/tmp/hermes/instance-config.json``)
    - A network policy allowing access to the Logos gateway and inference.local
    - The entrypoint ``/app/entrypoint.sh`` which starts the worker

3.  The worker connects OUT to the Logos gateway at ``ws://host.openshell.internal:{port}/ws/worker``
    through OpenShell's HTTP CONNECT proxy.  The proxy enforces the network policy.

4.  Once connected, the worker registers with the ``WorkerRegistry`` and receives
    chat tasks dispatched by the gateway.  Responses stream back over the same
    WebSocket.

5.  ``delete_instance()`` destroys the sandbox — the WebSocket drops and the
    worker is automatically unregistered.

Prerequisites
─────────────
- Docker running.
- ``openshell`` CLI installed.
- The sandbox image built and imported (see docker/Dockerfile.hermes-sandbox).
- UFW allows port 8091 from Docker networks (172.16.0.0/12, 10.0.0.0/8).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from .base import InstanceConfig, ResourceHeadroom, SpawnedInstance

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(
    os.getenv("LOGOS_HOME") or os.getenv("HERMES_HOME") or str(Path.home() / ".logos")
)
_STATE_FILE = _HERMES_HOME / "openshell_instances.json"

# Default sandbox image source. Two modes:
#   1. A pre-built image tag (e.g. "ghcr.io/myorg/hermes-sandbox:1.0") — used
#      when the image is published to a registry the cluster can pull from.
#   2. An absolute path to a Dockerfile — OpenShell builds and imports it
#      into the gateway on demand. Used for dev/local and one-off setups.
#
# We default to mode #2 by computing the path to docker/Dockerfile.hermes-sandbox
# in the repo (../../docker/ relative to this file). Override either with the
# LOGOS_OPENSHELL_IMAGE env var.
_REPO_ROOT = Path(__file__).parent.parent.parent
_BUNDLED_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile.hermes-sandbox"
_DEFAULT_IMAGE = os.getenv(
    "LOGOS_OPENSHELL_IMAGE",
    str(_BUNDLED_DOCKERFILE) if _BUNDLED_DOCKERFILE.exists() else "hermes-sandbox:local",
)

# Path to the default egress policy applied to every sandbox.
_DEFAULT_POLICY = Path(__file__).parent.parent / "policies" / "openshell_default.yaml"

# Gateway port — must match what Logos is listening on
_GATEWAY_PORT = int(
    os.getenv("LOGOS_PORT") or os.getenv("HERMES_PORT") or "8091"
)

# How long to wait for the worker to register after sandbox creation
_WORKER_REGISTER_TIMEOUT = 60


# ── State persistence ──────────────────────────────────────────────────────

def _load_state() -> List[dict]:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_state(instances: List[dict]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(instances, indent=2), encoding="utf-8")


# ── OpenShell CLI helpers ──────────────────────────────────────────────────

def _openshell(*args: str, gateway: Optional[str] = None,
               check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run ``openshell <args>`` and return the CompletedProcess.

    When ``gateway`` is provided, the CLI is invoked with ``-g <gateway>``
    so the command operates on a specific OpenShell gateway. Required for
    multi-route routing — without scoping, ``openshell sandbox create``
    targets whatever gateway is currently selected by ``openshell gateway
    select``, which is global state we can't rely on.
    """
    exe = shutil.which("openshell")
    if not exe:
        raise FileNotFoundError(
            "openshell CLI not found on PATH.  "
            "Install it: curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh"
        )
    cmd = [exe]
    if gateway:
        cmd.extend(["-g", gateway])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
    )


def _sanitize_sandbox_name(name: str) -> str:
    """
    Coerce ``name`` into a valid RFC 1123 subdomain so the underlying
    Kubernetes Sandbox CR accepts it. Lowercase, replace any character
    that isn't [a-z0-9.-] with '-', collapse runs of '-', strip leading
    and trailing non-alphanumerics, and truncate to 63 characters.
    """
    import re
    s = name.lower()
    s = re.sub(r"[^a-z0-9.-]+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-.")
    if not s:
        s = "agent"
    if not s[0].isalnum():
        s = "a" + s
    if len(s) > 63:
        s = s[:63].rstrip("-.")
    return s


def _sandbox_exists(name: str, gateway: Optional[str] = None) -> bool:
    """Return True if an OpenShell sandbox with this name is still running
    inside the given gateway.

    With multi-route routing, each sandbox lives inside exactly one gateway
    and the same sandbox name in two different gateways is not the same
    sandbox. Callers MUST scope the lookup to the gateway they care about
    — passing ``gateway=None`` falls back to the CLI's currently-selected
    gateway, which is brittle.
    """
    try:
        # `openshell sandbox list --names` prints one sandbox name per line.
        result = _openshell("sandbox", "list", "--names",
                            gateway=gateway, check=False)
        names = {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}
        return name in names
    except Exception:
        return False


def _list_sandbox_names(gateway: Optional[str] = None) -> List[str]:
    """Return all live OpenShell sandbox names in the given gateway,
    or [] on failure. ``gateway=None`` uses the CLI default gateway."""
    try:
        result = _openshell("sandbox", "list", "--names",
                            gateway=gateway, check=False)
        return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    except Exception:
        return []


def _list_all_sandbox_names_with_gateway() -> List[tuple[str, str]]:
    """Enumerate every live sandbox across every known OpenShell gateway.

    Returns ``[(sandbox_name, gateway_name), ...]``. Iterates over the
    model_routes table so each provisioned gateway is queried exactly
    once. Falls back to the primordial gateway when the routes table is
    empty (the bootstrap path before /setup populates routes).

    Used by orphan-prune and missing-sandbox-resurrect passes that need
    a global view of "what sandboxes exist anywhere" before they can
    decide which ones to clean up or recreate.
    """
    from gateway.openshell_routes import PRIMORDIAL_NAME
    from gateway.auth import db as auth_db

    gateways_to_query: set[str] = set()
    try:
        for r in auth_db.list_model_routes():
            name = r.get("openshell_name")
            if name:
                gateways_to_query.add(name)
    except Exception as exc:
        logger.warning("could not enumerate model_routes: %s", exc)
    if not gateways_to_query:
        gateways_to_query.add(PRIMORDIAL_NAME)

    out: List[tuple[str, str]] = []
    for gw in gateways_to_query:
        for name in _list_sandbox_names(gateway=gw):
            out.append((name, gw))
    return out


# ── Route resolution ───────────────────────────────────────────────────────

def _resolve_route(config: "InstanceConfig") -> tuple[str, str]:
    """Resolve (openshell_gateway_name, effective_model) for a spawn.

    Returns the OpenShell gateway the sandbox should land inside and the
    model name baked into ``/tmp/hermes/instance-config.json`` for the
    sandbox worker. Resolution order:

      1. ``config.model_route_id`` is set → look up the model_routes row,
         use its (openshell_name, model). The route's model takes priority
         over ``config.model`` because the OpenShell gateway is forcing
         that exact model anyway — sending a different value in the
         worker's chat-completions call would be ignored by OpenShell's
         privacy router.

      2. ``model_routes.is_default = 1`` row exists → use it. This is the
         path agents created via /admin/agents take when the user picks
         "Auto (use default)" instead of an explicit route.

      3. Fall through to the legacy path: primordial ``logos-openshell``
         gateway with the env-resolved model. This handles the case where
         model_routes is empty (the user hasn't run /setup yet under the
         new architecture, or DB was just migrated and routes haven't
         been populated).
    """
    from gateway.openshell_routes import PRIMORDIAL_NAME
    from gateway.auth import db as auth_db

    # 1. Explicit binding
    if getattr(config, "model_route_id", None):
        route = auth_db.get_model_route(config.model_route_id)
        if route:
            return route["openshell_name"], route["model"]
        logger.warning(
            "spawn: agent has model_route_id=%r but row not found — falling back",
            config.model_route_id,
        )

    # 2. Default route
    try:
        default = auth_db.get_default_model_route()
        if default:
            return default["openshell_name"], default["model"]
    except Exception as exc:
        logger.warning("spawn: get_default_model_route failed: %s", exc)

    # 3. Bootstrap fallback — primordial gateway with env/config-resolved model
    resolved_model = (config.model or "").strip()
    if not resolved_model:
        resolved_model = (
            os.environ.get("LOGOS_MODEL")
            or os.environ.get("HERMES_MODEL")
            or os.environ.get("LLM_MODEL")
            or ""
        ).strip()
    if resolved_model and not getattr(config, "model_route_id", None):
        logger.info(
            "spawn(%s): no model_routes binding — using primordial gateway with model=%r",
            config.name, resolved_model,
        )
    return PRIMORDIAL_NAME, resolved_model


# ── Executor ──────────────────────────────────────────────────────────────

class OpenShellExecutor:
    """
    Manages Hermes agent instances as OpenShell sandboxes.

    Each sandbox runs a WebSocket worker that connects back to the Logos
    gateway.  No SSH tunnels or port forwarding needed.
    """

    def __init__(
        self,
        sandbox_image: str = _DEFAULT_IMAGE,
        policy_file: Optional[str] = None,
    ):
        self.sandbox_image = sandbox_image
        self.policy_file = policy_file or (str(_DEFAULT_POLICY) if _DEFAULT_POLICY.exists() else None)

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        from gateway.openshell_routes import PRIMORDIAL_NAME

        instances = _load_state()

        # Prune entries whose sandbox has already been deleted. Each entry
        # carries its own openshell_name now (older entries default to the
        # primordial gateway since they predate multi-route routing).
        instances = [
            i for i in instances
            if _sandbox_exists(
                i.get("sandbox_name", ""),
                gateway=i.get("openshell_name") or PRIMORDIAL_NAME,
            )
        ]

        # OpenShell sandboxes are backed by Kubernetes Sandbox CRs, so the
        # sandbox name must be a valid RFC 1123 subdomain: lowercase
        # [a-z0-9.-], must start/end with alphanumeric, max 63 chars.
        sandbox_name = _sanitize_sandbox_name(f"hermes-{config.name}")
        worker_id = sandbox_name  # worker registers with this ID

        # Resolve which OpenShell gateway this sandbox should land inside,
        # and what model the worker should request from inference.local.
        # See _resolve_route() for the lookup order.
        openshell_gw, resolved_model = _resolve_route(config)

        logger.info(
            "Creating OpenShell sandbox '%s' in gateway '%s' (model=%s) from image '%s'",
            sandbox_name, openshell_gw, resolved_model or "<none>", self.sandbox_image,
        )

        # Write instance config to a temp file for upload
        instance_config = {
            "worker_id": worker_id,
            "instance_name": config.name,
            "gateway_url": f"http://host.openshell.internal:{_GATEWAY_PORT}",
            "soul": config.soul_name or "general",
            "toolsets": config.toolsets or [],
            "model": resolved_model,
        }

        # Persist the state record up front so the dashboard can show
        # this sandbox as "provisioning" while openshell create is still
        # running. We remove it again on failure below. The new
        # openshell_name + model_route_id fields let list_instances() and
        # delete_instance() know which gateway to query without re-reading
        # the agent record.
        record = {
            "name": config.name,
            "sandbox_name": sandbox_name,
            "worker_id": worker_id,
            "source": "openshell",
            "soul_name": config.soul_name,
            "model": resolved_model,
            "openshell_name": openshell_gw,
            "model_route_id": getattr(config, "model_route_id", None),
            "requester": config.requester,
            "toolsets": config.toolsets or [],
            "policy": config.policy or "",
            "sandbox_image": self.sandbox_image,
            "created_at": time.time(),
            "phase": "provisioning",
        }
        instances.append(record)
        _save_state(instances)

        config_tmpfile = None
        try:
            config_tmpfile = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="hermes-config-", delete=False,
            )
            json.dump(instance_config, config_tmpfile)
            config_tmpfile.close()

            # Build sandbox create command
            create_args = [
                "sandbox", "create",
                "--name", sandbox_name,
                "--from", self.sandbox_image,
                "--no-auto-providers",
                "--upload", f"{config_tmpfile.name}:/tmp/hermes/instance-config.json",
            ]

            # Upload soul file if it exists
            if config.soul_name and config.soul_name != "default":
                soul_dir = _HERMES_HOME / "souls"
                soul_file = soul_dir / f"{config.soul_name}.md"
                if soul_file.exists():
                    create_args += ["--upload", f"{soul_file}:/tmp/hermes/SOUL.md"]

            # Apply network policy
            if self.policy_file and Path(self.policy_file).exists():
                create_args += ["--policy", self.policy_file]

            # Trailing command: start the worker
            create_args += ["--", "/app/entrypoint.sh"]

            result = _openshell(*create_args, gateway=openshell_gw, check=True)
            logger.debug("openshell sandbox create stdout: %s", result.stdout.strip())

            # Update phase on success
            record["phase"] = "ready"
            _save_state(instances)

        except subprocess.CalledProcessError as exc:
            # Roll back the state record on failure
            instances = [i for i in instances if i.get("sandbox_name") != sandbox_name]
            _save_state(instances)
            raise RuntimeError(
                f"Failed to create OpenShell sandbox '{sandbox_name}' in gateway "
                f"'{openshell_gw}': {exc.stderr}"
            ) from exc
        finally:
            if config_tmpfile:
                try:
                    os.unlink(config_tmpfile.name)
                except OSError:
                    pass

        return SpawnedInstance(
            name=config.name,
            url="",  # no direct URL — routed through gateway via worker_id
            port=0,
            source="openshell",
            soul_name=config.soul_name,
            model=resolved_model,
            requester=config.requester,
            healthy=False,  # will become healthy when worker registers
        )

    def list_instances(self) -> List[dict]:
        from gateway.openshell_routes import PRIMORDIAL_NAME

        instances = _load_state()
        alive = []
        changed = False
        for inst in instances:
            gw = inst.get("openshell_name") or PRIMORDIAL_NAME
            if _sandbox_exists(inst.get("sandbox_name", ""), gateway=gw):
                alive.append(inst)
            else:
                changed = True
        if changed:
            _save_state(alive)
        return alive

    def delete_instance(self, name: str) -> None:
        # Resolve the sandbox name authoritatively from the agent name
        # — do NOT rely on the local state file for the SANDBOX NAME. The
        # state file goes out of sync whenever spawn() races with
        # list_instances() (the latter prunes entries whose CR isn't
        # visible yet), which left orphan OpenShell sandboxes for deleted
        # agents. Always issue the delete; clean up any matching state
        # entries afterwards.
        #
        # However, we DO need the state file (or, failing that, the
        # primordial fallback) to find which gateway the sandbox lives
        # inside — `openshell sandbox delete <name>` without `-g` only
        # checks the CLI's currently-selected gateway and silently
        # succeeds if the sandbox isn't there. Best-effort lookup: scan
        # the state file for an entry matching by name OR sandbox_name,
        # use its openshell_name; otherwise default to the primordial.
        from gateway.openshell_routes import PRIMORDIAL_NAME

        sandbox_name = _sanitize_sandbox_name(f"hermes-{name}")
        target_gw = PRIMORDIAL_NAME
        for inst in _load_state():
            if inst.get("name") == name or inst.get("sandbox_name") == sandbox_name:
                target_gw = inst.get("openshell_name") or PRIMORDIAL_NAME
                break

        try:
            _openshell("sandbox", "delete", sandbox_name,
                        gateway=target_gw, check=False)
            logger.info(
                "Deleted OpenShell sandbox '%s' from gateway '%s'",
                sandbox_name, target_gw,
            )
        except FileNotFoundError:
            logger.warning("Cannot delete sandbox '%s' — openshell CLI not on PATH", sandbox_name)
        except Exception as exc:
            logger.warning("Error deleting sandbox '%s': %s", sandbox_name, exc)

        # Drop any matching state record so the dashboard stops showing it.
        instances = _load_state()
        remaining = [
            i for i in instances
            if i.get("name") != name and i.get("sandbox_name") != sandbox_name
        ]
        if len(remaining) != len(instances):
            _save_state(remaining)

    def get_headroom(self) -> ResourceHeadroom:
        """Estimate available resources for spawning more sandboxes."""
        try:
            import psutil
            cpu_free = psutil.cpu_count(logical=True) * (1 - psutil.cpu_percent(interval=0.1) / 100)
            mem_free_gb = psutil.virtual_memory().available / 1024**3
            can_spawn = cpu_free >= 1.0 and mem_free_gb >= 1.0
            return ResourceHeadroom(
                available_cpu=cpu_free,
                available_mem_gb=mem_free_gb,
                can_spawn=can_spawn,
                reason="" if can_spawn else "Low host resources",
            )
        except Exception:
            return ResourceHeadroom(can_spawn=True)

    def get_resources(self) -> dict:
        headroom = self.get_headroom()
        return {
            "free_cpu": round(headroom.available_cpu, 2),
            "free_mem": int(headroom.available_mem_gb * 1024**3),
            "can_spawn": headroom.can_spawn,
            "reason": headroom.reason,
            "executor": "openshell",
        }
