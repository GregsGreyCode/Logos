"""
KubernetesExecutor — wraps the existing k8s spawn/list/delete helpers in http_api.py.

Phase 4 will extract those helpers here and delegate from http_api.py.
For now this is a thin stub that calls the existing functions directly.
"""

from __future__ import annotations

import logging
from typing import List

from .base import InstanceConfig, InstanceExecutor, ResourceHeadroom, SpawnedInstance

logger = logging.getLogger(__name__)


class KubernetesExecutor:
    """
    Manages agent instances as Kubernetes Deployments.

    Delegates to the existing helpers in gateway.http_api until Phase 4
    fully extracts them here.
    """

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        # Deferred to http_api._spawn_instance() until Phase 4 extraction.
        raise NotImplementedError(
            "KubernetesExecutor.spawn() is not yet wired — "
            "http_api._spawn_instance() handles this path directly."
        )

    def list_instances(self) -> List[dict]:
        from gateway.http_api import _list_hermes_instances
        return _list_hermes_instances()

    def delete_instance(self, name: str) -> None:
        from gateway.http_api import _delete_instance
        _delete_instance(name)

    def get_headroom(self) -> ResourceHeadroom:
        try:
            from gateway.http_api import _cluster_resources
            resources = _cluster_resources()
            free_cpu = resources.get("free_cpu", 0.0)
            free_mem = resources.get("free_mem_gb", 0.0)
            # Mirror the threshold check in http_api._spawn_instance
            from gateway.http_api import _SPAWN_CPU_THRESHOLD, _SPAWN_MEM_THRESHOLD_GB
            can_spawn = free_cpu >= _SPAWN_CPU_THRESHOLD and free_mem >= _SPAWN_MEM_THRESHOLD_GB
            reason = "" if can_spawn else (
                f"Insufficient cluster resources: {free_cpu:.1f} CPU cores, "
                f"{free_mem:.1f} GB RAM free"
            )
            return ResourceHeadroom(
                available_cpu=free_cpu,
                available_mem_gb=free_mem,
                can_spawn=can_spawn,
                reason=reason,
            )
        except Exception as exc:
            logger.warning("get_headroom failed: %s", exc)
            return ResourceHeadroom(can_spawn=True)  # fault-open
