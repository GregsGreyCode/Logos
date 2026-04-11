"""
Executor abstraction for agent instance management.

Selects the appropriate backend at startup based on runtime.mode config:
  - "openshell"  — spawn as OpenShell sandboxes (full policy enforcement, default)
  - "docker"     — spawn as plain Docker containers (container isolation, no policy engine)

Any other value (including the legacy "local") is coerced to OpenShell.
LocalProcessExecutor was removed when OpenShell became the only sandbox
runtime exposed in /setup — agents are no longer spawned as supervised
local subprocesses.
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
    """Return the appropriate executor for the given runtime mode.

    Unknown modes (including legacy "local") fall through to OpenShell.
    """
    if mode == "docker":
        from .docker import DockerSandboxExecutor
        return DockerSandboxExecutor()
    from .openshell import OpenShellExecutor
    return OpenShellExecutor()
