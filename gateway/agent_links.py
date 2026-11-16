"""Connections from the gateway to each agent's hermes API server (S2.8, S3.5).

An agent's hermes server listens on 127.0.0.1:8642 *inside* its OpenShell
sandbox. The gateway reaches it through ``openshell forward service``, which
binds a free loopback port on the host and tunnels it over the OpenShell
gateway's gRPC channel. :class:`AgentLinks` keeps one such forward process and
one :class:`~gateway.hermes_client.HermesClient` per agent, starts them on
first use and replaces them when they die (for example after the sandbox was
recreated).

The API key is the ``API_SERVER_KEY`` the runtime wrote into the agent's home
volume (``.env``); it is read once per link and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from gateway.hermes_client import HermesClient, HermesError

logger = logging.getLogger(__name__)

HERMES_PORT = 8642
_FORWARDING = re.compile(r"Forwarding\s+127\.0\.0\.1:(\d+)\s")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class AgentLinkError(Exception):
    """The agent's hermes server could not be reached."""


@dataclass
class _Link:
    process: Any          # asyncio.subprocess.Process (or a test double with .returncode/.terminate/.wait)
    port: int
    client: HermesClient


def forward_argv(name: str) -> list[str]:
    return ["openshell", "forward", "service", name,
            "--target-port", str(HERMES_PORT), "--local", "127.0.0.1:0"]


def _die_with_parent() -> None:  # pragma: no cover - runs in the child before exec
    """Linux: have the kernel end the forward if the gateway dies without cleaning up."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


async def _spawn_forward(name: str, timeout: float = 20.0) -> tuple[Any, int]:
    """Start the forward and return ``(process, local_port)`` once it reports the port."""
    proc = await asyncio.create_subprocess_exec(
        *forward_argv(name),
        preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    seen: list[str] = []

    async def _read_port() -> int:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise AgentLinkError(
                    f"openshell forward for {name} exited: {' '.join(seen)[-300:] or 'no output'}")
            text = _ANSI.sub("", line.decode(errors="replace")).strip()
            seen.append(text)
            m = _FORWARDING.search(text)
            if m:
                return int(m.group(1))

    try:
        port = await asyncio.wait_for(_read_port(), timeout)
    except (asyncio.TimeoutError, AgentLinkError) as e:
        await _stop(proc)
        if isinstance(e, AgentLinkError):
            raise
        raise AgentLinkError(f"openshell forward for {name} did not start within {timeout:.0f}s") from None
    return proc, port


async def _stop(proc: Any) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)
    except ProcessLookupError:
        pass
    except asyncio.TimeoutError:
        proc.kill()


def api_key_from_env(text: str) -> str:
    """``API_SERVER_KEY`` from the contents of an agent's ``.env``."""
    for line in (text or "").splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


class AgentLinks:
    """One port-forward and hermes client per agent, created lazily."""

    def __init__(
        self,
        runtime: Any,
        *,
        spawn: Optional[Callable[[str], Awaitable[tuple[Any, int]]]] = None,
        client_factory: Callable[..., HermesClient] = HermesClient,
    ) -> None:
        self.runtime = runtime
        self._spawn = spawn or _spawn_forward
        self._client_factory = client_factory
        self._links: Dict[str, _Link] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def _api_key(self, name: str) -> str:
        from gateway.agent_runtime import volumes
        home, _brain = volumes(name)
        try:
            text = await asyncio.to_thread(self.runtime.get, home, ".env")
        except Exception as e:
            raise AgentLinkError(f"could not read the API key for {name}: {e}") from None
        key = api_key_from_env(text)
        if not key:
            raise AgentLinkError(f"agent {name} has no API_SERVER_KEY yet; run scripts/logos-agent up {name}")
        return key

    async def client(self, name: str) -> HermesClient:
        """The client for ``name``, starting or restarting its forward if needed."""
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            link = self._links.get(name)
            if link and link.process.returncode is None:
                return link.client
            if link:
                await self._discard(name)
            key = await self._api_key(name)
            process, port = await self._spawn(name)
            client = self._client_factory(f"http://127.0.0.1:{port}", key)
            self._links[name] = _Link(process, port, client)
            logger.info("agent link: %s -> 127.0.0.1:%d", name, port)
            return client

    async def _discard(self, name: str) -> None:
        link = self._links.pop(name, None)
        if not link:
            return
        try:
            await link.client.close()
        finally:
            await _stop(link.process)

    async def drop(self, name: str) -> None:
        """Forget the link so the next :meth:`client` call builds a new one."""
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            await self._discard(name)

    async def health(self, name: str) -> dict:
        """``/health`` of the agent's hermes server, retrying once on a fresh link."""
        for attempt in (1, 2):
            client = await self.client(name)
            try:
                return await client.health()
            except HermesError:
                await self.drop(name)
                if attempt == 2:
                    raise
        raise AgentLinkError(f"agent {name} is unreachable")  # pragma: no cover

    async def close(self) -> None:
        for name in list(self._links):
            await self.drop(name)
