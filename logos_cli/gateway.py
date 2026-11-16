"""``logos gateway run|start|stop|restart|status``.

Process control for the host-side gateway. A running gateway is identified by
the PID file in LOGOS_HOME (``gateway.status``), so each LOGOS_HOME controls
only its own gateway.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from gateway.local_config import get_logos_home
from gateway.status import get_running_pid

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

_START_TIMEOUT_SECONDS = 30.0
_STOP_GRACE_SECONDS = 10.0


def gateway_port() -> int:
    """HTTP port of the gateway (same resolution as ``gateway.run``)."""
    return int(os.getenv("LOGOS_PORT") or os.getenv("HERMES_PORT") or "8091")


def background_log_path() -> Path:
    return get_logos_home() / "logs" / "gateway.bg.log"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _port_listening(port: int) -> bool:
    """Return True if anything accepts TCP connections on localhost:``port``."""
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),
                         (socket.AF_INET6, ("::1", port))):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex(addr) == 0:
                    return True
        except Exception:
            continue
    return False


def _shell_env_overlay(env: dict) -> dict:
    """Overlay KEY=VALUE lines from LOGOS_HOME/env; existing values win."""
    env_file = get_logos_home() / "env"
    if not env_file.exists():
        return env
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    except OSError:
        pass
    return env


def run_gateway(replace: bool = False) -> None:
    """Run the gateway in the foreground. Exits 1 if it fails to start.

    ``replace`` stops an already-running gateway for this LOGOS_HOME first.
    """
    from gateway.run import start_gateway

    print(f"Logos gateway starting on port {gateway_port()} (Ctrl+C to stop)")
    success = asyncio.run(start_gateway(replace=replace))
    if not success:
        sys.exit(1)


def run_gateway_detached() -> int:
    """Start the gateway as a detached background process; return its PID.

    stdout/stderr go to ``LOGOS_HOME/logs/gateway.bg.log``.
    """
    log_file = background_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if not env.get("LOGOS_JWT_SECRET"):
        secret_path = get_logos_home() / ".jwt_secret"
        if secret_path.exists():
            env["LOGOS_JWT_SECRET"] = secret_path.read_text().strip()
    env = _shell_env_overlay(env)

    with open(log_file, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "gateway.run"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=env,
        )
    return proc.pid


def stop_gateway(grace_seconds: float = _STOP_GRACE_SECONDS) -> Optional[int]:
    """SIGTERM the running gateway, escalate to SIGKILL after the grace window.

    Returns the PID that was stopped, or None if no gateway was running.
    """
    pid = get_running_pid()
    if pid is None:
        return None

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return pid
    except PermissionError:
        print(f"✗ Permission denied stopping gateway PID {pid}")
        sys.exit(1)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.25)

    if _pid_alive(pid):
        print(f"⚠ Gateway PID {pid} did not exit within {grace_seconds:.0f}s, sending SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    port = gateway_port()
    port_deadline = time.monotonic() + 3
    while time.monotonic() < port_deadline and _port_listening(port):
        time.sleep(0.2)
    return pid


def _start_and_wait() -> None:
    port = gateway_port()
    if _port_listening(port):
        print(f"✗ Port {port} is already in use; not starting a second gateway.")
        sys.exit(1)

    pid = run_gateway_detached()
    print(f"Starting gateway in background (PID {pid})…")
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            print(f"✗ Gateway PID {pid} exited before binding. See {background_log_path()}")
            sys.exit(1)
        if _port_listening(port):
            print(f"✓ Gateway is listening on port {port}")
            return
        time.sleep(0.5)
    print(f"⚠ Gateway did not bind to {port} within {_START_TIMEOUT_SECONDS:.0f}s "
          f"(PID {pid} still running). See {background_log_path()}")
    sys.exit(1)


def start_gateway_background() -> None:
    pid = get_running_pid()
    if pid is not None:
        print(f"Gateway is already running (PID {pid}). Use `logos gateway restart` to cycle it.")
        return
    _start_and_wait()


def restart_gateway() -> None:
    pid = stop_gateway()
    if pid is not None:
        print(f"✓ Stopped gateway (PID {pid})")
    _start_and_wait()


def print_status() -> bool:
    """Print gateway status; return True when it is running."""
    pid = get_running_pid()
    port = gateway_port()
    if pid is None:
        print("✗ Gateway is not running")
        if _port_listening(port):
            print(f"  (port {port} is in use by another process)")
        print("  Start it with: logos gateway start")
        return False
    print(f"✓ Gateway is running (PID {pid})")
    if _port_listening(port):
        print(f"✓ Listening on port {port}")
    else:
        print(f"⚠ Not listening on port {port} yet")
    print(f"  Home: {get_logos_home()}")
    return True


def gateway_command(args) -> None:
    """Dispatch ``logos gateway <subcommand>``."""
    subcmd = getattr(args, "gateway_command", None) or "run"

    if subcmd == "run":
        run_gateway(replace=getattr(args, "replace", False))
    elif subcmd == "start":
        start_gateway_background()
    elif subcmd == "stop":
        pid = stop_gateway()
        if pid is None:
            print("✗ Gateway is not running")
        else:
            print(f"✓ Stopped gateway (PID {pid})")
    elif subcmd == "restart":
        restart_gateway()
    elif subcmd == "status":
        if not print_status():
            sys.exit(1)
    else:  # pragma: no cover - argparse rejects unknown subcommands
        print(f"Unknown gateway subcommand: {subcmd}")
        sys.exit(2)
