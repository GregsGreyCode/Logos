"""
InstanceExecutor Protocol and supporting dataclasses.

Any executor (kubernetes, local, ...) must satisfy this interface.
Uses structural subtyping (Protocol) — no ABC inheritance required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable


@dataclass
class InstanceConfig:
    """Parameters for spawning a new agent instance."""
    name: str
    soul_name: str = "default"
    model: str = ""
    requester: str = ""
    port: int = 0                   # 0 = allocate automatically
    toolsets: List[str] = field(default_factory=list)
    policy: str = ""                # policy level passed to child (e.g. WORKSPACE_ONLY)
    # k8s-specific: resolved before passing to KubernetesExecutor
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
    source: str = "local"           # "local" | "k8s"
    pid: Optional[int] = None       # local executor only
    soul_name: str = "default"
    model: str = ""
    requester: str = ""
    healthy: bool = False


@dataclass
class ResourceHeadroom:
    """Available resources before the executor will block/queue spawns."""
    available_cpu: float = 0.0      # cores (k8s: allocatable − requested; local: psutil idle)
    available_mem_gb: float = 0.0
    can_spawn: bool = True
    reason: str = ""                # Human-readable explanation when can_spawn=False


@runtime_checkable
class InstanceExecutor(Protocol):
    """
    Protocol satisfied by KubernetesExecutor and LocalProcessExecutor.

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

        K8s shape: {total_cpu, total_mem, used_cpu, used_mem, free_cpu, free_mem}
        Local shape: {free_cpu, free_mem, can_spawn, reason}
        """
        ...
