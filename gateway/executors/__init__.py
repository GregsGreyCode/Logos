"""
Executor abstraction for agent instance management.

Selects the appropriate backend at startup based on runtime.mode config:
  - "openshell"  — spawn as OpenShell sandboxes (full policy enforcement, default)
  - "docker"     — spawn as plain Docker containers (container isolation, no policy engine)
  - "local"      — spawn as supervised local processes (no isolation, desktop fallback)
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
    if mode == "openshell":
        from .openshell import OpenShellExecutor
        return OpenShellExecutor()
    elif mode == "docker":
        from .docker import DockerSandboxExecutor
        return DockerSandboxExecutor()
    else:
        from .local import LocalProcessExecutor
        return LocalProcessExecutor()
