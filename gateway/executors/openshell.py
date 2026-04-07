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

_HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
_STATE_FILE = _HERMES_HOME / "openshell_instances.json"

# Default sandbox image.  Override via LOGOS_OPENSHELL_IMAGE env var.
_DEFAULT_IMAGE = os.getenv("LOGOS_OPENSHELL_IMAGE", "hermes-sandbox:local")

# Path to the default egress policy applied to every sandbox.
_DEFAULT_POLICY = Path(__file__).parent.parent / "policies" / "openshell_default.yaml"

# Gateway port — must match what Logos is listening on
_GATEWAY_PORT = int(os.getenv("HERMES_PORT", "8091"))

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

def _openshell(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run ``openshell <args>`` and return the CompletedProcess."""
    exe = shutil.which("openshell")
    if not exe:
        raise FileNotFoundError(
            "openshell CLI not found on PATH.  "
            "Install it: curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh"
        )
    return subprocess.run(
        [exe, *args],
        capture_output=capture,
        text=True,
        check=check,
    )


def _sandbox_exists(name: str) -> bool:
    """Return True if an OpenShell sandbox with this name is still running."""
    try:
        result = _openshell("sandbox", "list", "--output", "json", check=False)
        sandboxes = json.loads(result.stdout or "[]")
        return any(s.get("name") == name for s in sandboxes)
    except Exception:
        return False


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
        instances = _load_state()

        # Prune entries whose sandbox has already been deleted
        instances = [i for i in instances if _sandbox_exists(i.get("sandbox_name", ""))]

        sandbox_name = f"hermes-{config.name}"
        worker_id = sandbox_name  # worker registers with this ID

        logger.info("Creating OpenShell sandbox '%s' from image '%s'", sandbox_name, self.sandbox_image)

        # Write instance config to a temp file for upload
        instance_config = {
            "worker_id": worker_id,
            "instance_name": config.name,
            "gateway_url": f"http://host.openshell.internal:{_GATEWAY_PORT}",
            "soul": config.soul_name or "general",
            "toolsets": config.toolsets or [],
            "model": config.model or "",
        }

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

            result = _openshell(*create_args, check=True)
            logger.debug("openshell sandbox create stdout: %s", result.stdout.strip())

        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to create OpenShell sandbox '{sandbox_name}': {exc.stderr}"
            ) from exc
        finally:
            if config_tmpfile:
                try:
                    os.unlink(config_tmpfile.name)
                except OSError:
                    pass

        record = {
            "name": config.name,
            "sandbox_name": sandbox_name,
            "worker_id": worker_id,
            "source": "openshell",
            "soul_name": config.soul_name,
            "model": config.model,
            "requester": config.requester,
            "toolsets": config.toolsets or [],
            "policy": config.policy or "",
            "sandbox_image": self.sandbox_image,
            "created_at": time.time(),
        }
        instances.append(record)
        _save_state(instances)

        return SpawnedInstance(
            name=config.name,
            url="",  # no direct URL — routed through gateway via worker_id
            port=0,
            source="openshell",
            soul_name=config.soul_name,
            model=config.model,
            requester=config.requester,
            healthy=False,  # will become healthy when worker registers
        )

    def list_instances(self) -> List[dict]:
        instances = _load_state()
        alive = []
        changed = False
        for inst in instances:
            if _sandbox_exists(inst.get("sandbox_name", "")):
                alive.append(inst)
            else:
                changed = True
        if changed:
            _save_state(alive)
        return alive

    def delete_instance(self, name: str) -> None:
        instances = _load_state()
        remaining = []
        for inst in instances:
            if inst.get("name") == name:
                sandbox_name = inst.get("sandbox_name", f"hermes-{name}")
                try:
                    _openshell("sandbox", "delete", sandbox_name, check=False)
                    logger.info("Deleted OpenShell sandbox '%s'", sandbox_name)
                except Exception as exc:
                    logger.warning("Error deleting sandbox '%s': %s", sandbox_name, exc)
            else:
                remaining.append(inst)
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
