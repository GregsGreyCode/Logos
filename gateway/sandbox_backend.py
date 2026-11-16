"""Sandbox backend seam for agent runtimes.

Logos v1 drove OpenShell 0.0.x directly (an executor package, per-model
route gateways, a worker registry). That runtime was removed in S1.5.
Epic E2 brings it back as one upstream hermes server per agent inside an
OpenShell 0.1 sandbox, implemented behind the :class:`SandboxBackend`
protocol below.

Two backends exist:

* :class:`NullBackend` (the default): no sandboxes. Read calls return
  empty / "missing" data so the dashboard renders empty states, and
  mutating calls raise ``NotImplementedError``.
* :class:`OpenShellBackend` (S2.6), selected with
  ``LOGOS_SANDBOX_BACKEND=openshell``: OpenShell 0.1 sandboxes created from
  ``agents/<name>.yaml`` by :mod:`gateway.agent_runtime`, the same code
  ``scripts/logos-agent`` runs.
"""

from __future__ import annotations

import os
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


# OpenShell 0.1 sandbox phase -> SandboxStatus.state.
PHASE_STATES = {
    "Ready": "running",
    "Provisioning": "creating",
    "Pending": "creating",
    "Stopped": "stopped",
    "Deleting": "stopped",
    "Error": "error",
    "Missing": "missing",
}


def state_for_phase(phase: str) -> str:
    """Map an OpenShell phase onto a ``SandboxStatus.state``. A phase this
    code does not know is reported as ``error`` rather than guessed at."""
    return PHASE_STATES.get(phase, "error")


class OpenShellBackend:
    """Agents as OpenShell 0.1 sandboxes, driven by :mod:`gateway.agent_runtime`.

    A sandbox is named after its agent and is disposable: everything that
    must survive lives on the agent's two Docker volumes. So ``stop`` deletes
    the sandbox and keeps the volumes, ``start`` recreates it from
    ``agents/<name>.yaml`` (unless it is already running), and ``delete`` is
    the same operation as ``stop``: volumes are never removed here.

    Calls block (``create``/``start`` for minutes); run them off the event loop.
    """

    def __init__(self, runtime: Any = None):
        if runtime is None:
            from gateway.agent_runtime import AgentRuntime
            runtime = AgentRuntime()
        self.runtime = runtime

    @staticmethod
    def _status(name: str, phase: str) -> SandboxStatus:
        detail = phase if phase in PHASE_STATES else f"unrecognised OpenShell phase: {phase}"
        return SandboxStatus(name=name, state=state_for_phase(phase), detail=detail)

    def _up(self, name: str) -> SandboxStatus:
        from gateway.agent_runtime import AgentRuntimeError
        if not _valid_name(name):
            return SandboxStatus(name=name, state="error", detail=f"invalid agent name: {name!r}")
        try:
            self.runtime.up(name)
        except AgentRuntimeError as e:
            return SandboxStatus(name=name, state="error", detail=str(e))
        return self.status(name)

    def create(self, agent: Mapping[str, Any]) -> SandboxStatus:
        """Create (or recreate) the sandbox for ``agent``. Only its ``name``
        is used; the definition is read from ``agents/<name>.yaml``."""
        return self._up(str(agent.get("name") or ""))

    def start(self, name: str) -> SandboxStatus:
        st = self.status(name)
        return st if st.running else self._up(name)

    def stop(self, name: str) -> SandboxStatus:
        self.delete(name)
        st = self.status(name)
        if st.state == "missing":
            return SandboxStatus(name=name, state="stopped", detail="sandbox deleted; volumes kept")
        return st

    def delete(self, name: str) -> None:
        if not _valid_name(name):
            raise ValueError(f"invalid agent name: {name!r}")
        self.runtime.down(name)

    def status(self, name: str) -> SandboxStatus:
        try:
            return self._status(name, self.runtime.phase(name))
        except Exception as e:  # e.g. the openshell binary is not installed
            return SandboxStatus(name=name, state="error", detail=f"could not query OpenShell: {e}")

    def list(self) -> list[SandboxStatus]:
        """Sandboxes that exist and belong to an agent defined in ``agents/``
        (other sandboxes on the host are not ours to report)."""
        try:
            phases = self.runtime.phases()
            names = self.runtime.defined()
        except Exception:
            return []
        return [self._status(n, phases[n]) for n in names if n in phases]


BACKEND_ENV = "LOGOS_SANDBOX_BACKEND"
_null_backend: SandboxBackend = NullBackend()
_openshell_backend: Optional[OpenShellBackend] = None


def get_backend() -> SandboxBackend:
    """Return the process-wide sandbox backend: :class:`OpenShellBackend`
    when ``LOGOS_SANDBOX_BACKEND=openshell``, else :class:`NullBackend`."""
    global _openshell_backend
    if os.environ.get(BACKEND_ENV, "").strip().lower() == "openshell":
        if _openshell_backend is None:
            _openshell_backend = OpenShellBackend()
        return _openshell_backend
    return _null_backend


def _valid_name(name: str) -> bool:
    """True when ``name`` is already a sandbox-safe label (it is also used in
    file and volume names, so nothing else is let through)."""
    return bool(name) and "." not in name and sanitize_sandbox_name(name) == name


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

