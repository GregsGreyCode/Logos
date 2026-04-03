"""
DockerSandboxExecutor — runs agent instances as Docker containers.

Reduced-isolation fallback for platforms where OpenShell is not available
(currently Windows) but Docker Desktop is present.  Each instance is a
detached ``docker run`` container using the logos-hermes-sandbox image.

Compared to OpenShellExecutor:
  - No egress policy enforcement (OpenShell's declarative YAML policies
    are not applied — the container has unrestricted outbound networking
    unless you layer Docker network restrictions externally).
  - No SSH tunnel; the container port is published directly to localhost.

Compared to LocalProcessExecutor:
  - Agents run inside a container with their own filesystem and PID namespace.
  - Host filesystem is not accessible unless explicitly mounted.
  - The container is removed on exit (--rm).

This is a real sandbox — it provides OS-level isolation via Docker — but
it is weaker than the full OpenShell mode because there is no network
policy layer.  The setup UI labels this "Container sandbox" to distinguish
it from the full "OpenShell sandbox".

Requirements:
  - Docker CLI on PATH (``docker`` or ``docker.exe``)
  - The sandbox image built (``logos-hermes-sandbox`` or the Docker-only variant)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator, List, Optional

from .base import InstanceConfig, ResourceHeadroom, SpawnedInstance

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
_STATE_FILE = _HERMES_HOME / "docker_instances.json"
_LOCK_FILE = _HERMES_HOME / "docker_instances.lock"
_HEALTH_TIMEOUT = 30  # containers need time to start the Python process
_DEFAULT_IMAGE = os.getenv("LOGOS_DOCKER_SANDBOX_IMAGE", "logos-hermes-sandbox")


# ── File locking (cross-platform) ───────────────────────────────────────

@contextlib.contextmanager
def _file_lock() -> Generator[None, None, None]:
    """Acquire an exclusive file lock around state read-modify-write cycles.

    Uses fcntl.flock on Unix and msvcrt.locking on Windows.  The lock file
    is separate from the state file so a crash mid-write cannot corrupt both.
    """
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(_LOCK_FILE, "w")
    try:
        if sys.platform == "win32":
            import msvcrt
            # LOCK_EX — retry briefly if another process holds the lock
            deadline = time.monotonic() + 10
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ── State persistence ────────────────────────────────────────────────────

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


# ── Docker helpers ───────────────────────────────────────────────────────

def _docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run ``docker <args>`` and return the CompletedProcess."""
    exe = shutil.which("docker")
    if not exe:
        raise RuntimeError("docker CLI not found on PATH")
    cmd = [exe, *args]
    kwargs: dict = {"timeout": 120}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.run(cmd, check=check, **kwargs)


def _container_running(container_name: str) -> bool:
    """Return True if a Docker container with this name is running."""
    try:
        result = _docker("inspect", "-f", "{{.State.Running}}", container_name, check=False)
        return result.returncode == 0 and "true" in (result.stdout or "").lower()
    except Exception:
        return False


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


def _allocate_port(instances: List[dict], port_min: int = 8200, port_max: int = 8299) -> int:
    """Find a free port in the Docker sandbox range."""
    import socket as _socket
    used = {inst.get("port") for inst in instances}
    for port in range(port_min, port_max + 1):
        if port in used:
            continue
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free ports in range {port_min}–{port_max}")


# ── Executor ─────────────────────────────────────────────────────────────

class DockerSandboxExecutor:
    """
    Manages agent instances as Docker containers (no OpenShell).

    Provides container-level isolation: separate filesystem, PID namespace,
    bounded mounts, explicit env passing.  Does NOT provide OpenShell's
    egress policy enforcement layer.
    """

    def __init__(self, sandbox_image: str = _DEFAULT_IMAGE):
        self.sandbox_image = sandbox_image

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        with _file_lock():
            instances = _load_state()
            # Prune entries whose container has been removed
            instances = [i for i in instances if _container_running(i.get("container_name", ""))]

            local_port = config.port if config.port else _allocate_port(instances)
            container_name = f"hermes-{config.name}"
            url = f"http://127.0.0.1:{local_port}"

            logger.info("Creating Docker sandbox '%s' from image '%s'", container_name, self.sandbox_image)

            # Per-instance persistent storage on the host
            instance_home = _HERMES_HOME / "instances" / config.name
            (instance_home / "memories").mkdir(parents=True, exist_ok=True)
            shared_home = _HERMES_HOME / "shared"
            shared_home.mkdir(parents=True, exist_ok=True)

            # Build environment variables
            env_args: list[str] = []
            env_vars = {
                "HERMES_INSTANCE_NAME": config.name,
                "HERMES_PORT": "8080",
                "HERMES_SHARED_HOME": "/hermes-shared",
            }
            if config.soul_name and config.soul_name != "default":
                env_vars["HERMES_SOUL"] = config.soul_name
            if config.toolsets:
                env_vars["HERMES_TOOLSETS"] = ",".join(config.toolsets)
            if config.policy:
                env_vars["HERMES_POLICY_LEVEL"] = config.policy
            # Pass the gateway URL so the agent can reach back for MCP, approvals, etc.
            env_vars["HERMES_GATEWAY_URL"] = f"http://host.docker.internal:8080"
            for k, v in env_vars.items():
                env_args += ["-e", f"{k}={v}"]

            # docker run with reduced-privilege hardening
            run_args = [
                "run", "-d",
                "--name", container_name,
                "--rm",                                 # auto-remove on exit
                "-p", f"127.0.0.1:{local_port}:8080",  # publish only to localhost
                "--cap-drop=ALL",                       # drop all Linux capabilities
                "--security-opt=no-new-privileges",     # prevent privilege escalation
                "-v", f"{instance_home}:/home/hermes/.hermes",  # per-instance storage
                "-v", f"{shared_home}:/hermes-shared:ro",       # shared user profile (read-only)
                *env_args,
                self.sandbox_image,
            ]
            try:
                result = _docker(*run_args, check=True)
                container_id = (result.stdout or "").strip()[:12]
                logger.info("Docker sandbox '%s' started (id=%s)", container_name, container_id)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"Failed to create Docker sandbox '{container_name}': {exc.stderr}"
                ) from exc

            record: dict = {
                "name": config.name,
                "container_name": container_name,
                "container_id": container_id,
                "url": url,
                "port": local_port,
                "source": "docker",
                "soul_name": config.soul_name,
                "model": config.model,
                "requester": config.requester,
                "instance_label": config.instance_label,
                "toolsets": config.toolsets or [],
                "policy": config.policy or "",
                "sandbox_image": self.sandbox_image,
            }
            instances.append(record)
            _save_state(instances)

        healthy = _health_check(local_port)
        if not healthy:
            logger.warning(
                "Docker sandbox '%s' did not become healthy within %ds. "
                "Check: docker logs %s",
                container_name, _HEALTH_TIMEOUT, container_name,
            )

        return SpawnedInstance(
            name=config.name,
            url=url,
            port=local_port,
            source="docker",
            soul_name=config.soul_name,
            model=config.model,
            requester=config.requester,
            healthy=healthy,
        )

    def list_instances(self) -> List[dict]:
        with _file_lock():
            instances = _load_state()
            alive = []
            changed = False
            for inst in instances:
                cn = inst.get("container_name", "")
                if _container_running(cn):
                    inst["healthy"] = _health_check(inst["port"], timeout=3)
                    alive.append(inst)
                else:
                    changed = True
            if changed:
                _save_state(alive)
        return alive

    def delete_instance(self, name: str) -> None:
        with _file_lock():
            instances = _load_state()
            remaining = []
            for inst in instances:
                if inst.get("name") == name:
                    cn = inst.get("container_name", "")
                    if cn:
                        try:
                            _docker("stop", cn, check=False)
                            logger.info("Stopped Docker sandbox '%s'", cn)
                        except Exception as exc:
                            logger.warning("Could not stop Docker sandbox '%s': %s", cn, exc)
                else:
                    remaining.append(inst)
            _save_state(remaining)

    def get_headroom(self) -> ResourceHeadroom:
        # Docker manages its own resource allocation; always allow spawn
        # unless Docker daemon is unreachable.
        try:
            _docker("info", check=True)
            return ResourceHeadroom(can_spawn=True, reason="")
        except Exception:
            return ResourceHeadroom(can_spawn=False, reason="Docker daemon not reachable")

    def get_resources(self) -> dict:
        headroom = self.get_headroom()
        return {
            "free_cpu": 0,
            "free_mem": 0,
            "can_spawn": headroom.can_spawn,
            "reason": headroom.reason,
            "executor": "docker",
        }
