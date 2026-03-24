"""
LocalProcessExecutor — runs agent instances as supervised local processes.

Each instance is a separate Python process running `gateway.run` on a
dedicated port allocated from the configured port pool.

State is persisted in ~/.hermes/instances.json so instances survive
gateway restarts.

Phase 3 implementation: port allocation, spawn, health check, list, delete.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from .base import InstanceConfig, ResourceHeadroom, SpawnedInstance

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
_INSTANCES_FILE = _HERMES_HOME / "instances.json"
_HEALTH_TIMEOUT = 15  # seconds to wait for instance health check


def _load_instances() -> List[dict]:
    try:
        if _INSTANCES_FILE.exists():
            return json.loads(_INSTANCES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_instances(instances: List[dict]) -> None:
    _INSTANCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _INSTANCES_FILE.write_text(json.dumps(instances, indent=2), encoding="utf-8")


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        # OSError covers WinError 6 (invalid handle) on Windows when the
        # process no longer exists and its handle has been released.
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
        time.sleep(0.5)
    return False


class LocalProcessExecutor:
    """
    Manages agent instances as supervised local processes.

    Suitable for Windows desktop and Linux/macOS workstations that do not
    have (or do not want) a Kubernetes cluster.
    """

    def __init__(self, port_range: tuple[int, int] = (8081, 8199)):
        self.port_min, self.port_max = port_range

    def _allocate_port(self, instances: List[dict]) -> int:
        used = {inst.get("port") for inst in instances}
        for port in range(self.port_min, self.port_max + 1):
            if port not in used:
                return port
        raise RuntimeError(
            f"No free ports in range {self.port_min}–{self.port_max}. "
            f"Stop some instances first."
        )

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        instances = _load_instances()

        # Prune dead instances before allocating
        instances = [i for i in instances if _is_alive(i.get("pid", -1))]

        port = config.port if config.port else self._allocate_port(instances)
        url = f"http://127.0.0.1:{port}"

        env = {**os.environ, "HERMES_INSTANCE_NAME": config.name, "HERMES_PORT": str(port)}
        if config.soul_name and config.soul_name != "default":
            env["HERMES_SOUL"] = config.soul_name

        # When running as a frozen executable (Logos.exe), sys.executable is the
        # launcher itself.  Pass --agent-mode so the launcher skips its UI and
        # runs only the gateway on the port supplied via HERMES_PORT.
        # In development (plain Python), use the normal -m gateway.run invocation.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--agent-mode"]
        else:
            cmd = [sys.executable, "-m", "gateway.run"]
        log_path = _HERMES_HOME / "logs" / f"instance_{config.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # On Windows use CREATE_DETACHED_PROCESS so the child survives if the
        # parent exits; on Unix start_new_session=True creates a new session leader.
        _popen_kwargs: dict = {"env": env, "stdout": None, "stderr": None}
        if sys.platform == "win32":
            # CREATE_DETACHED_PROCESS only defined on Windows (0x00000008)
            _popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_DETACHED_PROCESS", 0x00000008)
        else:
            _popen_kwargs["start_new_session"] = True

        with open(log_path, "a") as log_fh:
            _popen_kwargs["stdout"] = log_fh
            _popen_kwargs["stderr"] = log_fh
            proc = subprocess.Popen(cmd, **_popen_kwargs)

        record = {
            "name": config.name,
            "url": url,
            "port": port,
            "pid": proc.pid,
            "source": "local",
            "soul_name": config.soul_name,
            "model": config.model,
            "requester": config.requester,
        }
        instances.append(record)
        _save_instances(instances)

        healthy = _health_check(port)
        if not healthy:
            logger.warning("Instance %s did not become healthy within %ds", config.name, _HEALTH_TIMEOUT)

        return SpawnedInstance(
            name=config.name,
            url=url,
            port=port,
            pid=proc.pid,
            source="local",
            soul_name=config.soul_name,
            model=config.model,
            requester=config.requester,
            healthy=healthy,
        )

    def list_instances(self) -> List[dict]:
        instances = _load_instances()
        alive = []
        changed = False
        for inst in instances:
            pid = inst.get("pid", -1)
            if _is_alive(pid):
                inst["healthy"] = _health_check(inst["port"], timeout=2)
                # Per-process resource stats via psutil (best-effort)
                try:
                    import psutil as _ps
                    proc = _ps.Process(pid)
                    inst["cpu_percent"] = round(proc.cpu_percent(interval=None), 1)
                    inst["mem_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
                except Exception:
                    pass
                alive.append(inst)
            else:
                changed = True  # prune dead entry
        if changed:
            _save_instances(alive)
        return alive

    def delete_instance(self, name: str) -> None:
        instances = _load_instances()
        remaining = []
        for inst in instances:
            if inst.get("name") == name:
                pid = inst.get("pid")
                if pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                        # Give it a moment to exit cleanly
                        for _ in range(20):
                            if not _is_alive(pid):
                                break
                            time.sleep(0.1)
                        else:
                            # SIGKILL doesn't exist on Windows; fall back to
                            # TerminateProcess via os.kill with a no-op signal
                            # (signal 0) just to confirm alive, then force-kill.
                            if hasattr(signal, "SIGKILL"):
                                os.kill(pid, signal.SIGKILL)
                            else:
                                # Windows: forcefully terminate via subprocess
                                try:
                                    subprocess.run(
                                        ["taskkill", "/F", "/PID", str(pid)],
                                        capture_output=True,
                                    )
                                except Exception:
                                    pass
                    except (ProcessLookupError, OSError):
                        pass
            else:
                remaining.append(inst)
        _save_instances(remaining)

    def get_headroom(self) -> ResourceHeadroom:
        try:
            import psutil
            cpu_free = psutil.cpu_count(logical=True) * (1 - psutil.cpu_percent(interval=0.1) / 100)
            mem = psutil.virtual_memory()
            mem_free_gb = mem.available / (1024 ** 3)
            # Require at least 1 CPU core and 1 GB RAM free
            can_spawn = cpu_free >= 1.0 and mem_free_gb >= 1.0
            reason = "" if can_spawn else (
                f"Low resources: {cpu_free:.1f} CPU cores, {mem_free_gb:.1f} GB RAM free"
            )
            return ResourceHeadroom(
                available_cpu=cpu_free,
                available_mem_gb=mem_free_gb,
                can_spawn=can_spawn,
                reason=reason,
            )
        except ImportError:
            # psutil not available — allow spawn, log warning
            logger.debug("psutil not installed; skipping resource headroom check")
            return ResourceHeadroom(can_spawn=True)
        except Exception as exc:
            logger.warning("get_headroom failed: %s", exc)
            return ResourceHeadroom(can_spawn=True)

    def get_resources(self) -> dict:
        """Return a resource summary dict for the /instances API response."""
        headroom = self.get_headroom()
        return {
            "free_cpu": round(headroom.available_cpu, 2),
            "free_mem": int(headroom.available_mem_gb * 1024**3),
            "can_spawn": headroom.can_spawn,
            "reason": headroom.reason,
        }
