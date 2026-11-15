"""Sandbox backend seam for agent runtimes.

Logos v1 drove OpenShell 0.0.x directly (an executor package, per-model
route gateways, a worker registry). That runtime was removed in S1.5.
Epic E2 brings it back as one upstream hermes server per agent inside an
OpenShell 0.1 sandbox, implemented behind the :class:`SandboxBackend`
protocol below.

Until then the gateway runs with :class:`NullBackend`: it has no
sandboxes, read calls return empty / "missing" data so the dashboard
renders empty states, and mutating calls raise ``NotImplementedError``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

NO_BACKEND_MESSAGE = "no sandbox backend configured (E2)"


@dataclass(frozen=True)
class SandboxStatus:
    """Point-in-time view of one agent sandbox."""

    name: str
    state: str = "missing"  # missing | creating | running | stopped | error
    detail: str = ""
    url: Optional[str] = None  # base URL of the agent's hermes server, when running

    @property
    def running(self) -> bool:
        return self.state == "running"

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class SandboxBackend(Protocol):
    """Operations the gateway needs from an agent sandbox runtime (E2)."""

    def create(self, agent: Mapping[str, Any]) -> SandboxStatus:
        """Provision a sandbox for ``agent`` (an ``agents`` DB row)."""
        ...

    def start(self, name: str) -> SandboxStatus: ...

    def stop(self, name: str) -> SandboxStatus: ...

    def delete(self, name: str) -> None: ...

    def status(self, name: str) -> SandboxStatus: ...

    def list(self) -> list[SandboxStatus]: ...


class NullBackend:
    """Backend with no sandboxes. Used until E2 lands a real one."""

    def create(self, agent: Mapping[str, Any]) -> SandboxStatus:
        raise NotImplementedError(NO_BACKEND_MESSAGE)

    def start(self, name: str) -> SandboxStatus:
        raise NotImplementedError(NO_BACKEND_MESSAGE)

    def stop(self, name: str) -> SandboxStatus:
        raise NotImplementedError(NO_BACKEND_MESSAGE)

    def delete(self, name: str) -> None:
        raise NotImplementedError(NO_BACKEND_MESSAGE)

    def status(self, name: str) -> SandboxStatus:
        return SandboxStatus(name=name, state="missing", detail=NO_BACKEND_MESSAGE)

    def list(self) -> list[SandboxStatus]:
        return []


_backend: SandboxBackend = NullBackend()


def get_backend() -> SandboxBackend:
    """Return the process-wide sandbox backend (``NullBackend`` until E2)."""
    return _backend


def sanitize_sandbox_name(name: str) -> str:
    """Coerce ``name`` into an RFC 1123 label (lowercase, ``[a-z0-9.-]``,
    max 63 chars) usable as a sandbox name."""
    s = re.sub(r"[^a-z0-9.-]+", "-", name.lower())
    s = re.sub(r"-+", "-", s).strip("-.")
    if not s:
        s = "agent"
    if not s[0].isalnum():
        s = "a" + s
    return s[:63].rstrip("-.")

