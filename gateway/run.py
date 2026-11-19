"""The Logos gateway process: the dashboard, its API and the services it looks after.

``logos gateway run`` (systemd: ``logos-gateway.service``) and ``python -m gateway.run``
both end up in :func:`start_gateway`, which

- loads ``~/.logos/.env`` (and ``~/.logos/env``) into the environment,
- refuses to start a second gateway under the same ``LOGOS_HOME`` (or replaces it),
- writes ``~/.logos/logs/gateway.log`` and ``errors.log`` with secrets redacted,
- runs the HTTP server (``gateway.http_api``) until SIGINT or SIGTERM.

Agents run in their own sandboxes, never in this process.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

LOGOS_HOME = Path(os.getenv("LOGOS_HOME") or str(Path.home() / ".logos"))

from dotenv import load_dotenv  # noqa: E402


def load_env(home: Path = LOGOS_HOME) -> None:
    """``.env`` first, then the project ``.env``, then ``env`` (never overriding what is set)."""
    for path, override in ((home / ".env", True), (None, False), (home / "env", False)):
        if path is None:
            load_dotenv()
            continue
        if path.exists():
            try:
                load_dotenv(path, encoding="utf-8", override=override)
            except UnicodeDecodeError:
                load_dotenv(path, encoding="latin-1", override=override)


logger = logging.getLogger(__name__)


class Gateway:
    """What the HTTP layer gets as ``app["runner"]``: the sandbox backend and a stop switch."""

    def __init__(self) -> None:
        from gateway.sandbox_backend import get_backend
        self.sandbox_backend = get_backend()
        self.started = time.time()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        logger.info("Stopping gateway...")
        self._stop.set()

    async def wait(self) -> None:
        await self._stop.wait()


def _setup_logs(home: Path = LOGOS_HOME) -> None:
    from gateway.lib.redact import RedactingFormatter
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    for name, size, keep, level in (("gateway.log", 5, 3, logging.INFO), ("errors.log", 2, 2, logging.WARNING)):
        handler = RotatingFileHandler(log_dir / name, maxBytes=size * 1024 * 1024, backupCount=keep)
        handler.setLevel(level)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _replace_or_refuse(replace: bool) -> bool:
    """Deal with a gateway already running under this LOGOS_HOME. False means: do not start."""
    from gateway.status import get_running_pid, remove_pid_file
    pid = get_running_pid()
    if pid is None or pid == os.getpid():
        return True
    if not replace:
        logger.error("Another gateway is already running (PID %d, LOGOS_HOME=%s).", pid, LOGOS_HOME)
        print(f"\nGateway already running (PID {pid}).\n"
              "   Use 'logos gateway restart' to replace it, or 'logos gateway stop' first.\n")
        return False
    logger.info("Replacing the running gateway (PID %d).", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.error("Not allowed to stop PID %d, so it cannot be replaced.", pid)
        return False
    for _ in range(20):                     # up to 10 seconds
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except OSError:
            break
    else:
        logger.warning("Gateway PID %d did not exit after SIGTERM; sending SIGKILL.", pid)
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        except OSError:
            pass
    remove_pid_file()
    return True


async def start_gateway(replace: bool = False, **_: Any) -> bool:
    """Run the gateway until it is stopped. False if it could not start (systemd restarts it)."""
    load_env()
    if not _replace_or_refuse(replace):
        return False
    _setup_logs()

    from gateway.auth import db as auth_db
    auth_db.init_db(LOGOS_HOME)

    gateway = Gateway()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, gateway.stop)
        except NotImplementedError:
            pass

    port = int(os.getenv("LOGOS_PORT") or "8091")
    from gateway.http_api import start_http_api
    server = asyncio.create_task(start_http_api(gateway, port))
    failed = []

    def _server_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("HTTP server failed to start: %s", task.exception())
            failed.append(task.exception())
            gateway.stop()
    server.add_done_callback(_server_done)

    import atexit
    from gateway.status import remove_pid_file, write_pid_file
    write_pid_file()
    atexit.register(remove_pid_file)

    await gateway.wait()
    server.cancel()
    remove_pid_file()
    logger.info("Gateway stopped")
    return not failed


def main() -> None:
    if not asyncio.run(start_gateway(replace="--replace" in sys.argv[1:])):
        sys.exit(1)


if __name__ == "__main__":
    main()
