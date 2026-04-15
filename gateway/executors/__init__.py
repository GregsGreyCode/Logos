"""
Executor abstraction for agent instance management.

OpenShell is the only supported runtime. ``build_executor()`` is kept as a
one-liner factory so call sites don't have to import the concrete class,
and so the Protocol in ``base.py`` remains the interface contract.
"""

from .base import InstanceExecutor, InstanceConfig, SpawnedInstance, ResourceHeadroom

__all__ = [
    "InstanceExecutor",
    "InstanceConfig",
    "SpawnedInstance",
    "ResourceHeadroom",
    "build_executor",
]


def build_executor(mode: str | None = None) -> "InstanceExecutor":
    """Return the OpenShell executor (the only supported runtime).

    The ``mode`` parameter is accepted for backwards compatibility with
    legacy call sites but is ignored — OpenShell is the only runtime.
    """
    del mode  # ignored
    from .openshell import OpenShellExecutor
    return OpenShellExecutor()
