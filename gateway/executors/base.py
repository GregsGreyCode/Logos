"""
InstanceExecutor Protocol and supporting dataclasses.

Any executor (currently only OpenShellExecutor) must satisfy this interface.
Uses structural subtyping (Protocol) — no ABC inheritance required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable


@dataclass
class InstanceConfig:
    """Parameters for spawning a new agent instance."""
    name: str
    soul_name: str = "default"
    model: str = ""
    requester: str = ""
    instance_label: str = ""        # user-chosen label (e.g. "researcher")
    port: int = 0                   # 0 = allocate automatically
    toolsets: List[str] = field(default_factory=list)
    policy: str = ""                # policy level passed to child (e.g. WORKSPACE_ONLY)
    # OpenShell route binding — id of a row in auth.db model_routes that
    # determines which OpenShell gateway the sandbox is spawned inside.
    # When None, the executor falls back to (a) the row marked is_default=1
    # in model_routes, or (b) the first available route — see
    # _resolve_route in gateway/executors/openshell.py. Lets multiple
    # agents target different models at the same time without OpenShell's
    # "one forced model per gateway" design becoming a bottleneck.
    model_route_id: Optional[str] = None
    tool_overrides: dict = field(default_factory=dict)
    machine_endpoint: Optional[str] = None
    machine_name: Optional[str] = None
    machine_id: Optional[str] = None


@dataclass
class SpawnedInstance:
    """Descriptor for a running agent instance."""
    name: str
    url: str                        # Reachable base URL (e.g. http://127.0.0.1:8082)
    port: int
    source: str = "openshell"       # Free-form string; current executors emit "openshell"
    pid: Optional[int] = None       # unused by OpenShell; kept for compat
    soul_name: str = "default"
    model: str = ""
    requester: str = ""
    healthy: bool = False


@dataclass
class ResourceHeadroom:
    """Available resources before the executor will block/queue spawns."""
    available_cpu: float = 0.0      # cores
    available_mem_gb: float = 0.0
    can_spawn: bool = True
    reason: str = ""                # Human-readable explanation when can_spawn=False


@runtime_checkable
class InstanceExecutor(Protocol):
    """
    Protocol satisfied by OpenShellExecutor.

    All methods are synchronous; async callers should run them in a thread pool.
    """

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        """Start a new agent instance and return its descriptor."""
        ...

    def list_instances(self) -> List[dict]:
        """Return a list of running instances as JSON-serialisable dicts."""
        ...

    def delete_instance(self, name: str) -> None:
        """Terminate and clean up a named instance."""
        ...

    def get_headroom(self) -> ResourceHeadroom:
        """Return current resource headroom to inform spawn decisions."""
        ...

    def get_resources(self) -> dict:
        """
        Return a JSON-serialisable resource summary for the /instances API response.

        Current shape: {free_cpu, free_mem, can_spawn, reason, executor}.
        """
        ...


def safe_k8s_name(requester: str, label: str = "") -> str:
    """Convert a requester string (and optional instance label) to a valid
    RFC 1123 subdomain used for sandbox/instance naming.

    When *label* is provided the result is ``hermes-{requester}-{label}``
    (sanitised, max 52 chars). This allows multiple instances per user.

    Name retains the ``k8s`` prefix for historical reasons — every
    executor reuses this sanitiser for instance naming, not just the
    (long-removed) Kubernetes one. A future rename to ``sandbox_name``
    is fine but not urgent.
    """
    raw = f"{requester}-{label}" if label else requester
    name = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return f"hermes-{name}"[:52]  # k8s name limit is 63 chars
