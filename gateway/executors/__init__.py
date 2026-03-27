"""
Executor abstraction for agent instance management.

Selects the appropriate backend at startup based on runtime.mode config:
  - "kubernetes" — spawn instances as k8s Deployments (server/homelab default)
  - "local"      — spawn instances as supervised local processes (desktop/no-cluster)
"""

from .base import InstanceExecutor, InstanceConfig, SpawnedInstance, ResourceHeadroom

__all__ = [
    "InstanceExecutor",
    "InstanceConfig",
    "SpawnedInstance",
    "ResourceHeadroom",
    "build_executor",
]


def build_executor(mode: str) -> "InstanceExecutor":
    """Return the appropriate executor for the given runtime mode."""
    if mode == "kubernetes":
        from .kubernetes import KubernetesExecutor
        return KubernetesExecutor()
    elif mode == "openshell":
        from .openshell import OpenShellExecutor
        return OpenShellExecutor()
    else:
        from .local import LocalProcessExecutor
        return LocalProcessExecutor()
