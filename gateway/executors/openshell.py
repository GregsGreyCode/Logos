"""
OpenShellExecutor — runs Hermes agent instances as OpenShell sandboxes.

OpenShell (https://github.com/NVIDIA/OpenShell) provides policy-governed,
sandboxed container environments backed by K3s inside a single Docker
container.  No separate Kubernetes cluster is required.

Integration model
─────────────────
1.  A custom sandbox image (``logos-hermes-sandbox``) extends the OpenShell
    base image with the Hermes gateway.  The Hermes HTTP server starts
    automatically on port 8080 inside the sandbox.

2.  ``OpenShellExecutor.spawn()`` creates a named OpenShell sandbox and
    establishes an SSH port-forward so the Logos gateway can reach the
    Hermes HTTP API at ``http://127.0.0.1:{local_port}``.

3.  A declarative YAML policy (see ``gateway/policies/openshell_default.yaml``)
    restricts outbound access to the configured model endpoint(s) only.

4.  ``delete_instance()`` terminates the port-forward and destroys the
    sandbox.

Prerequisites
─────────────
- Docker Desktop (or daemon) running.
- ``openshell`` installed: ``curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh``
- The Hermes sandbox image built: ``docker build -f Dockerfile.openshell-sandbox -t logos-hermes-sandbox .``

Runtime mode
────────────
Set ``runtime.mode = "openshell"`` in your Logos config, or choose
"OpenShell" in the setup wizard under "Where to run agents".
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from .base import InstanceConfig, ResourceHeadroom, SpawnedInstance

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
_STATE_FILE = _HERMES_HOME / "openshell_instances.json"
_HEALTH_TIMEOUT = 30   # sandboxes start K3s — give them more time than local procs
_PORT_MIN = 8200
_PORT_MAX = 8299        # separate range from local executor (8081-8199)

# Default sandbox image.  Override via LOGOS_OPENSHELL_IMAGE env var or
# config.  Use --from to pull a community image, e.g. "logos-hermes-sandbox".
_DEFAULT_IMAGE = os.getenv("LOGOS_OPENSHELL_IMAGE", "logos-hermes-sandbox")

# Path to the default egress policy applied to every sandbox.
_DEFAULT_POLICY = Path(__file__).parent.parent / "policies" / "openshell_default.yaml"


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


def _allocate_port(instances: List[dict]) -> int:
    """Find a free local port in the OpenShell executor's reserved range."""
    import socket as _socket
    used = {inst.get("local_port") for inst in instances}
    for port in range(_PORT_MIN, _PORT_MAX + 1):
        if port in used:
            continue
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free ports in range {_PORT_MIN}–{_PORT_MAX}.  Stop some instances first.")


def _start_port_forward(sandbox_name: str, local_port: int, sandbox_port: int = 8080) -> Optional[int]:
    """
    Establish an SSH port-forward so the Logos gateway can reach the Hermes
    HTTP server running inside the sandbox.

    OpenShell sandboxes are reachable via ``openshell sandbox connect``, which
    uses SSH under the hood.  We use the same SSH path with ``-L`` to forward
    ``127.0.0.1:{local_port}`` → ``localhost:{sandbox_port}`` inside the sandbox.

    Returns the PID of the background SSH process, or None on failure.
    """
    try:
        # Ask openshell for the SSH connection string for this sandbox.
        result = _openshell("sandbox", "ssh-config", sandbox_name, check=False)
        if result.returncode != 0:
            # Fallback: derive from ``openshell sandbox inspect`` JSON output
            result = _openshell("sandbox", "inspect", sandbox_name, "--output", "json", check=False)
            info = json.loads(result.stdout or "{}")
            ssh_host = info.get("ssh_host", "")
            ssh_port = info.get("ssh_port", 22)
            ssh_key  = info.get("ssh_key", "")
        else:
            # Parse OpenSSH config-format output (Host / HostName / Port / IdentityFile)
            info = _parse_ssh_config(result.stdout)
            ssh_host = info.get("HostName", "")
            ssh_port = int(info.get("Port", 22))
            ssh_key  = info.get("IdentityFile", "")

        if not ssh_host:
            logger.warning("Could not determine SSH host for sandbox %s; port-forward unavailable", sandbox_name)
            return None

        ssh_cmd = [
            "ssh",
            "-N",                                          # no remote command
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{local_port}:localhost:{sandbox_port}",
            "-p", str(ssh_port),
        ]
        if ssh_key:
            ssh_cmd += ["-i", ssh_key]
        ssh_cmd.append(ssh_host)

        # Start the tunnel as a detached background process.
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Give the tunnel a moment to establish
        time.sleep(1.5)
        if proc.poll() is not None:
            logger.warning("SSH tunnel for %s exited immediately (rc=%d)", sandbox_name, proc.returncode)
            return None
        logger.info("Port-forward established: 127.0.0.1:%d → %s:%d (pid %d)",
                    local_port, sandbox_name, sandbox_port, proc.pid)
        return proc.pid

    except Exception as exc:
        logger.warning("Failed to start port-forward for %s: %s", sandbox_name, exc)
        return None


def _parse_ssh_config(text: str) -> dict:
    """Parse a single-host OpenSSH config block into a dict."""
    result: dict = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1]
    return result


def _health_check(port: int, timeout: int = _HEALTH_TIMEOUT) -> bool:
    """Poll http://127.0.0.1:{port}/health until ready or timeout."""
    import urllib.request
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _kill_pid(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        import signal as _signal
        os.kill(pid, _signal.SIGTERM)
    except Exception:
        pass


# ── Executor ──────────────────────────────────────────────────────────────

class OpenShellExecutor:
    """
    Manages Hermes agent instances as OpenShell sandboxes.

    Each instance is a named OpenShell sandbox running the Hermes HTTP
    server on port 8080.  An SSH port-forward maps a local port
    (8200–8299) to the sandbox's Hermes endpoint so the Logos gateway
    can proxy chat traffic to it.

    Advantages over local/k8s executors
    ─────────────────────────────────────
    - Works on Windows, macOS, and Linux with only Docker installed.
    - Built-in egress policy enforcement (no rogue tool calls phoning home).
    - GPU passthrough available (``--gpu`` flag) for local inference.
    - Single-command install; no separate Kubernetes cluster needed.
    """

    def __init__(
        self,
        sandbox_image: str = _DEFAULT_IMAGE,
        policy_file: Optional[str] = None,
    ):
        self.sandbox_image = sandbox_image
        self.policy_file = policy_file or (str(_DEFAULT_POLICY) if _DEFAULT_POLICY.exists() else None)

    # ── Protocol methods ──────────────────────────────────────────────

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        instances = _load_state()

        # Prune entries whose sandbox has already been deleted
        instances = [i for i in instances if _sandbox_exists(i["name"])]

        local_port = config.port if config.port else _allocate_port(instances)
        sandbox_name = f"hermes-{config.name}"
        url = f"http://127.0.0.1:{local_port}"

        logger.info("Creating OpenShell sandbox '%s' from image '%s'", sandbox_name, self.sandbox_image)

        # Build environment variables to inject into the sandbox
        env_args: list[str] = []
        env_vars = {
            "HERMES_INSTANCE_NAME": config.name,
            "HERMES_PORT": "8080",
        }
        if config.soul_name and config.soul_name != "default":
            env_vars["HERMES_SOUL"] = config.soul_name
        if config.toolsets:
            env_vars["HERMES_TOOLSETS"] = ",".join(config.toolsets)
        if config.policy:
            env_vars["HERMES_POLICY_LEVEL"] = config.policy
        for k, v in env_vars.items():
            env_args += ["--env", f"{k}={v}"]

        # Create the sandbox.  ``--detach`` (or equivalent) keeps it running
        # in the background.  ``--name`` gives it a stable identifier.
        create_args = [
            "sandbox", "create",
            "--name", sandbox_name,
            "--from", self.sandbox_image,
            "--detach",
            *env_args,
        ]
        try:
            result = _openshell(*create_args, check=True)
            logger.debug("openshell sandbox create: %s", result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to create OpenShell sandbox '{sandbox_name}': {exc.stderr}"
            ) from exc

        # Apply the default egress policy (allow only the configured model endpoint)
        if self.policy_file and Path(self.policy_file).exists():
            try:
                _openshell("policy", "set", sandbox_name,
                           "--policy", self.policy_file, "--wait", check=False)
                logger.info("Applied egress policy to sandbox '%s'", sandbox_name)
            except Exception as exc:
                logger.warning("Could not apply policy to sandbox '%s': %s", sandbox_name, exc)

        # Establish SSH port-forward so the Logos gateway can reach Hermes
        tunnel_pid = _start_port_forward(sandbox_name, local_port)

        record: dict = {
            "name": config.name,
            "sandbox_name": sandbox_name,
            "url": url,
            "local_port": local_port,
            "tunnel_pid": tunnel_pid,
            "source": "openshell",
            "soul_name": config.soul_name,
            "model": config.model,
            "requester": config.requester,
            "toolsets": config.toolsets or [],
            "policy": config.policy or "",
            "sandbox_image": self.sandbox_image,
        }
        instances.append(record)
        _save_state(instances)

        healthy = _health_check(local_port)
        if not healthy:
            logger.warning(
                "Sandbox '%s' did not become healthy within %ds.  "
                "Check: openshell logs %s",
                sandbox_name, _HEALTH_TIMEOUT, sandbox_name,
            )

        return SpawnedInstance(
            name=config.name,
            url=url,
            port=local_port,
            source="openshell",
            soul_name=config.soul_name,
            model=config.model,
            requester=config.requester,
            healthy=healthy,
        )

    def list_instances(self) -> List[dict]:
        instances = _load_state()
        alive = []
        changed = False
        for inst in instances:
            if _sandbox_exists(inst["sandbox_name"]):
                inst["healthy"] = _health_check(inst["local_port"], timeout=2)
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
                # Kill the SSH tunnel
                _kill_pid(inst.get("tunnel_pid"))
                # Destroy the sandbox
                try:
                    _openshell("sandbox", "delete", sandbox_name, check=False)
                    logger.info("Deleted OpenShell sandbox '%s'", sandbox_name)
                except Exception as exc:
                    logger.warning("Error deleting sandbox '%s': %s", sandbox_name, exc)
            else:
                remaining.append(inst)
        _save_state(remaining)

    def get_headroom(self) -> ResourceHeadroom:
        """
        OpenShell runs inside Docker; we measure Docker's available resources.
        Falls back to host psutil if the Docker stats API is unavailable.
        """
        try:
            import urllib.request
            with urllib.request.urlopen("http://localhost:2375/info", timeout=2) as r:
                info = json.loads(r.read())
            total_cpus = info.get("NCPU", 1)
            mem_total  = info.get("MemTotal", 0)
            # Rough estimate: count running hermes sandboxes
            running = len(self.list_instances())
            cpu_free = max(0.0, float(total_cpus) - running * 0.5)
            mem_free_gb = max(0.0, mem_total / 1024**3 - running * 0.5)
            can_spawn = cpu_free >= 0.5 and mem_free_gb >= 0.5
            return ResourceHeadroom(
                available_cpu=cpu_free,
                available_mem_gb=mem_free_gb,
                can_spawn=can_spawn,
                reason="" if can_spawn else f"Low resources: {cpu_free:.1f} CPU, {mem_free_gb:.1f} GB free",
            )
        except Exception:
            pass
        # Fallback: use host psutil
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
            "free_cpu":  round(headroom.available_cpu, 2),
            "free_mem":  int(headroom.available_mem_gb * 1024**3),
            "can_spawn": headroom.can_spawn,
            "reason":    headroom.reason,
            "executor":  "openshell",
        }
