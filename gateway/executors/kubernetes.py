"""
KubernetesExecutor — manages agent instances as Kubernetes Deployments.

Extracted from gateway/http_api.py.
"""

from __future__ import annotations

import json
import logging
from typing import List

from .base import InstanceConfig, ResourceHeadroom, SpawnedInstance
from .k8s_helpers import (
    HERMES_NAMESPACE,
    INSTANCE_CPU_LIMIT,
    INSTANCE_CPU_REQUEST,
    INSTANCE_MEM_LIMIT,
    INSTANCE_MEM_REQUEST,
    SPAWN_CPU_THRESHOLD,
    SPAWN_MEM_THRESHOLD,
    cluster_resources,
    delete_hermes_instance,
    k8s_clients,
    list_hermes_instances,
    safe_k8s_name,
)
from gateway.souls import (
    SoulManifest,
    compute_effective_toolsets,
    get_soul_registry,
)

logger = logging.getLogger(__name__)


class KubernetesExecutor:
    """Manages agent instances as Kubernetes Deployments."""

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        """Create Deployment + Service + PVC + soul ConfigMap for a new agent instance."""
        core, apps = k8s_clients()
        dep_name = safe_k8s_name(config.requester)
        tool_overrides = config.tool_overrides or {}

        # ── Soul resolution ───────────────────────────────────────────────────
        registry = get_soul_registry()
        soul = registry.get(config.soul_name) or registry.get("general")
        if soul is None:
            soul = SoulManifest(
                id="general", slug="general", name="General", description="",
                category="general", role_summary="", status="stable", version="1.0",
                created_by="", tags=[], enforced_toolsets=[], default_enabled_toolsets=[],
                optional_toolsets=[], forbidden_toolsets=[], soul_md="",
            )
        effective_toolsets = compute_effective_toolsets(soul, tool_overrides)
        instance_name = soul.name + (
            " \u00b7 " + config.model if config.model and config.model != "balanced" else ""
        )

        # ── PVC ───────────────────────────────────────────────────────────────
        pvc_name = f"{dep_name}-pvc"
        try:
            core.read_namespaced_persistent_volume_claim(pvc_name, HERMES_NAMESPACE)
        except Exception:
            core.create_namespaced_persistent_volume_claim(
                HERMES_NAMESPACE,
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": pvc_name, "namespace": HERMES_NAMESPACE},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": "local-path",
                        "resources": {"requests": {"storage": "1Gi"}},
                    },
                },
            )

        # ── Service ───────────────────────────────────────────────────────────
        svc_name = dep_name
        try:
            core.read_namespaced_service(svc_name, HERMES_NAMESPACE)
        except Exception:
            core.create_namespaced_service(
                HERMES_NAMESPACE,
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        "name": svc_name,
                        "namespace": HERMES_NAMESPACE,
                        "labels": {"app": dep_name},
                    },
                    "spec": {
                        "type": "NodePort",
                        "selector": {"app": dep_name},
                        "ports": [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}],
                    },
                },
            )

        # ── Early-exit if Deployment already exists ───────────────────────────
        try:
            apps.read_namespaced_deployment(dep_name, HERMES_NAMESPACE)
            return SpawnedInstance(
                name=dep_name, url="", port=0, source="k8s",
                soul_name=soul.slug, model=config.model, requester=config.requester,
            )
        except Exception:
            pass

        # ── Soul snapshot ConfigMap ───────────────────────────────────────────
        snap_name = f"{dep_name}-soul-snap"
        try:
            core.create_namespaced_config_map(
                HERMES_NAMESPACE,
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": snap_name,
                        "namespace": HERMES_NAMESPACE,
                        "labels": {
                            "hermes.io/soul-snapshot": "true",
                            "hermes.io/soul-slug": soul.slug,
                            "hermes.io/instance": dep_name,
                        },
                    },
                    "data": {
                        "SOUL.md": soul.soul_md,
                        "effective-toolsets.json": json.dumps(effective_toolsets),
                    },
                },
            )
        except Exception as e:
            from kubernetes.client.rest import ApiException as _ApiException
            if not (isinstance(e, _ApiException) and e.status == 409):
                raise  # 409 = already exists (partial retry); anything else is real

        # ── Deployment ────────────────────────────────────────────────────────
        machine_endpoint = config.machine_endpoint
        dep = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": dep_name,
                "namespace": HERMES_NAMESPACE,
                "labels": {
                    "app": dep_name,
                    "hermes.io/has-soul": "true",
                    "hermes.io/soul-slug": soul.slug,
                },
                "annotations": {
                    "hermes.io/soul-slug": soul.slug,
                    "hermes.io/soul-name": soul.name,
                    "hermes.io/soul-version": soul.version,
                    "hermes.io/soul-status": soul.status,
                    "hermes.io/soul-snapshot-ref": snap_name,
                    "hermes.io/effective-toolsets": json.dumps(effective_toolsets),
                    "hermes.io/tool-overrides": json.dumps(tool_overrides),
                    "hermes.io/requester": config.requester,
                    "hermes.io/model-alias": config.model,
                    **({"hermes.io/machine-id": config.machine_id} if config.machine_id else {}),
                    **({"hermes.io/machine-name": config.machine_name} if config.machine_name else {}),
                    **({"hermes.io/machine-endpoint": machine_endpoint} if machine_endpoint else {}),
                },
            },
            "spec": {
                "replicas": 1,
                "revisionHistoryLimit": 1,
                "selector": {"matchLabels": {"app": dep_name}},
                "template": {
                    "metadata": {"labels": {"app": dep_name}},
                    "spec": {
                        "serviceAccountName": "hermes",
                        "imagePullSecrets": [{"name": "ghcr-creds"}],
                        "tolerations": [{
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }],
                        "volumes": [
                            {"name": "hermes-home", "persistentVolumeClaim": {"claimName": pvc_name}},
                            {"name": "hermes-config-seed", "configMap": {"name": "hermes-config-yaml"}},
                            {"name": "hermes-soul-snap", "configMap": {"name": snap_name}},
                            {"name": "hermes-work", "emptyDir": {}},
                            {"name": "hermes-shared-memory", "persistentVolumeClaim": {
                                "claimName": "hermes-shared-memory-pvc", "readOnly": True,
                            }},
                        ],
                        "securityContext": {
                            "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        # Prefer same node as primary so the RWO shared-memory PVC can be
                        # mounted read-only by both pods (local-path is ReadWriteOnce).
                        "affinity": {
                            "podAffinity": {
                                "preferredDuringSchedulingIgnoredDuringExecution": [{
                                    "weight": 100,
                                    "podAffinityTerm": {
                                        "labelSelector": {"matchLabels": {"app": "hermes"}},
                                        "topologyKey": "kubernetes.io/hostname",
                                    },
                                }],
                            },
                        },
                        "initContainers": [
                            {
                                "name": "fix-perms",
                                "image": "busybox:1.36",
                                "command": ["sh", "-c",
                                    "chown -R 10001:10001 /hermes-home && chmod 750 /hermes-home"],
                                "volumeMounts": [{"name": "hermes-home", "mountPath": "/hermes-home"}],
                                "securityContext": {"runAsUser": 0},
                            },
                            {
                                "name": "seed-config",
                                "image": "busybox:1.36",
                                "command": ["sh", "-c",
                                    'mkdir -p /hermes-home/memories && '
                                    'sed "s|\\${INSPECTOR_TOKEN}|${INSPECTOR_TOKEN}|g" '
                                    '/seed/config.yaml > /hermes-home/config.yaml && '
                                    'cp /soul-snap/SOUL.md /hermes-home/SOUL.md'],
                                "env": [{
                                    "name": "INSPECTOR_TOKEN",
                                    "valueFrom": {"secretKeyRef": {
                                        "name": "hermes-secret", "key": "INSPECTOR_TOKEN",
                                    }},
                                }],
                                "volumeMounts": [
                                    {"name": "hermes-home", "mountPath": "/hermes-home"},
                                    {"name": "hermes-config-seed", "mountPath": "/seed", "readOnly": True},
                                    {"name": "hermes-soul-snap", "mountPath": "/soul-snap", "readOnly": True},
                                ],
                                "securityContext": {
                                    "runAsUser": 10001, "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            },
                        ],
                        "containers": [{
                            "name": "hermes",
                            "image": "ghcr.io/gregsgreycode/hermes:latest",
                            "ports": [{"name": "http", "containerPort": 8080}],
                            "env": [
                                {"name": "HOME", "value": "/home/hermes"},
                                {"name": "HERMES_INSTANCE_NAME", "value": instance_name},
                                {"name": "HERMES_LOG_LEVEL", "valueFrom": {"configMapKeyRef": {
                                    "name": "hermes-config", "key": "HERMES_LOG_LEVEL",
                                }}},
                                {"name": "HERMES_PORT", "valueFrom": {"configMapKeyRef": {
                                    "name": "hermes-config", "key": "HERMES_PORT",
                                }}},
                                {"name": "REQUEST_TIMEOUT_SECONDS", "valueFrom": {"configMapKeyRef": {
                                    "name": "hermes-config", "key": "REQUEST_TIMEOUT_SECONDS",
                                }}},
                                # Use resolved machine endpoint if available, else ConfigMap default
                                *(
                                    [{"name": "OPENAI_BASE_URL", "value": machine_endpoint}]
                                    if machine_endpoint else
                                    [{"name": "OPENAI_BASE_URL", "valueFrom": {"configMapKeyRef": {
                                        "name": "hermes-config", "key": "OPENAI_BASE_URL",
                                    }}}]
                                ),
                                {"name": "HERMES_MODEL", "valueFrom": {"configMapKeyRef": {
                                    "name": "hermes-config", "key": "HERMES_MODEL",
                                }}},
                                {"name": "LLM_MODEL", "valueFrom": {"configMapKeyRef": {
                                    "name": "hermes-config", "key": "LLM_MODEL",
                                }}},
                                {"name": "OPENAI_API_KEY", "valueFrom": {"secretKeyRef": {
                                    "name": "hermes-secret", "key": "OPENAI_API_KEY",
                                }}},
                                {"name": "HERMES_INTERNAL_TOKEN", "valueFrom": {"secretKeyRef": {
                                    "name": "hermes-secret", "key": "HERMES_INTERNAL_TOKEN",
                                }}},
                                # Telegram intentionally omitted — only the primary logos pod
                                # handles Telegram; spawned instances use the web/API interface only.
                            ],
                            "volumeMounts": [
                                {"name": "hermes-home", "mountPath": "/home/hermes/.hermes"},
                                {"name": "hermes-work", "mountPath": "/work"},
                                {"name": "hermes-shared-memory",
                                    "mountPath": "/home/hermes/.hermes-shared", "readOnly": True},
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8080},
                                "initialDelaySeconds": 15, "periodSeconds": 15, "failureThreshold": 3,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": 8080},
                                "initialDelaySeconds": 30, "periodSeconds": 30, "failureThreshold": 3,
                            },
                            "resources": {
                                "requests": {
                                    "cpu": INSTANCE_CPU_REQUEST,
                                    "memory": INSTANCE_MEM_REQUEST,
                                },
                                "limits": {
                                    "cpu": INSTANCE_CPU_LIMIT,
                                    "memory": INSTANCE_MEM_LIMIT,
                                },
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "runAsNonRoot": True,
                                "runAsUser": 10001,
                                "readOnlyRootFilesystem": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }],
                    },
                },
            },
        }
        apps.create_namespaced_deployment(HERMES_NAMESPACE, dep)
        logger.info(json.dumps({
            "event": "instance_spawned",
            "instance": dep_name,
            "requester": config.requester,
            "soul_slug": soul.slug,
            "soul_version": soul.version,
            "effective_toolsets": effective_toolsets,
            "tool_overrides": tool_overrides,
            "snapshot_ref": snap_name,
        }))
        return SpawnedInstance(
            name=dep_name,
            url="",   # NodePort assigned by k8s; caller queries list_instances() for port
            port=0,
            source="k8s",
            soul_name=soul.slug,
            model=config.model,
            requester=config.requester,
            healthy=False,  # pod is starting; readiness probe will confirm
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
