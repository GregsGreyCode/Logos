"""
KubernetesExecutor — manages agent instances as Kubernetes Deployments.

Extracted from gateway/http_api.py. The spawn path (_spawn_instance) still
lives in http_api.py because it depends on soul/toolset logic there; it will
be migrated here in a later phase.
"""

from __future__ import annotations

import logging
from typing import List

from .base import InstanceConfig, ResourceHeadroom, SpawnedInstance
from .k8s_helpers import (
    SPAWN_CPU_THRESHOLD,
    SPAWN_MEM_THRESHOLD,
    cluster_resources,
    delete_hermes_instance,
    list_hermes_instances,
)

logger = logging.getLogger(__name__)


class KubernetesExecutor:
    """Manages agent instances as Kubernetes Deployments."""

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        """
        Spawn is handled directly in http_api._spawn_instance for k8s mode
        (requires soul/toolset resolution that still lives there).

        This method is a placeholder until that logic is extracted here.
        """
        raise NotImplementedError(
            "KubernetesExecutor.spawn() is not yet extracted — "
            "http_api._handle_instances_post calls _spawn_instance directly "
            "for the kubernetes path."
        )

    def list_instances(self) -> List[dict]:
        return list_hermes_instances()

    def delete_instance(self, name: str) -> None:
        delete_hermes_instance(name)

    def get_headroom(self) -> ResourceHeadroom:
        try:
            res = cluster_resources()
            free_cpu = res.get("free_cpu", 0.0)
            free_mem_bytes = res.get("free_mem", 0)
            free_mem_gb = free_mem_bytes / (1024**3)
            can_spawn = (free_cpu >= SPAWN_CPU_THRESHOLD
                         and free_mem_bytes >= SPAWN_MEM_THRESHOLD)
            reason = "" if can_spawn else (
                f"Insufficient cluster resources: "
                f"{free_cpu:.1f} CPU cores, {free_mem_gb:.1f} GiB RAM free"
            )
            return ResourceHeadroom(
                available_cpu=free_cpu,
                available_mem_gb=free_mem_gb,
                can_spawn=can_spawn,
                reason=reason,
            )
        except Exception as exc:
            logger.warning("get_headroom: k8s unavailable — %s", exc)
            return ResourceHeadroom(can_spawn=False, reason=str(exc))

    def get_resources(self) -> dict:
        """Return the full k8s cluster resource dict for the /instances API response."""
        try:
            return cluster_resources()
        except Exception as exc:
            return {"_error": str(exc)}
