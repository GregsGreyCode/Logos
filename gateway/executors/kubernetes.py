"""
KubernetesExecutor — manages agent instances as Kubernetes Deployments.

Extracted from gateway/http_api.py.
"""

from __future__ import annotations

import base64
import json
import logging
import os
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


def _ensure_namespace_prerequisites(core) -> None:
    """Create prerequisite resources in the hermes namespace if they don't exist.

    Spawned agent pods need: ServiceAccount, Secret (API keys),
    ConfigMap (runtime config), shared-memory PVC, and image pull secret.
    These are created once and reused across all instances.
    """
    ns = HERMES_NAMESPACE

    # ── Namespace ─────────────────────────────────────────────────────
    try:
        core.read_namespace(ns)
    except Exception:
        try:
            core.create_namespace({
                "apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": ns},
            })
            logger.info("Created namespace %s", ns)
        except Exception as e:
            logger.debug("Namespace %s creation skipped: %s", ns, e)

    # ── ServiceAccount ────────────────────────────────────────────────
    try:
        core.read_namespaced_service_account("hermes", ns)
    except Exception:
        try:
            core.create_namespaced_service_account(ns, {
                "apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": "hermes", "namespace": ns},
            })
            logger.info("Created ServiceAccount hermes in %s", ns)
        except Exception as e:
            logger.warning("Failed to create ServiceAccount: %s", e)

    # ── Secret (hermes-secret) ────────────────────────────────────────
    # Populated from the gateway's own env vars so spawned pods can
    # reach the same inference providers.
    try:
        core.read_namespaced_secret("hermes-secret", ns)
    except Exception:
        secret_data = {}
        for key in ("OPENAI_API_KEY", "HERMES_INTERNAL_TOKEN", "INSPECTOR_TOKEN",
                     "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
            val = os.environ.get(key, "")
            if val:
                secret_data[key] = base64.b64encode(val.encode()).decode()
        # Ensure required keys exist even if empty
        for key in ("OPENAI_API_KEY", "HERMES_INTERNAL_TOKEN", "INSPECTOR_TOKEN"):
            if key not in secret_data:
                secret_data[key] = base64.b64encode(b"not-set").decode()
        try:
            core.create_namespaced_secret(ns, {
                "apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "hermes-secret", "namespace": ns},
                "type": "Opaque",
                "data": secret_data,
            })
            logger.info("Created Secret hermes-secret in %s", ns)
        except Exception as e:
            logger.warning("Failed to create Secret: %s", e)

    # ── ConfigMap (hermes-config) ─────────────────────────────────────
    # Runtime config for spawned pods — model, endpoint, log level etc.
    try:
        core.read_namespaced_config_map("hermes-config", ns)
    except Exception:
        config_data = {
            "HERMES_LOG_LEVEL": os.environ.get("HERMES_LOG_LEVEL", "INFO"),
            "HERMES_PORT": os.environ.get("HERMES_PORT", "8080"),
            "REQUEST_TIMEOUT_SECONDS": os.environ.get("REQUEST_TIMEOUT_SECONDS", "300"),
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
            "HERMES_MODEL": os.environ.get("HERMES_MODEL", ""),
            "LLM_MODEL": os.environ.get("LLM_MODEL", ""),
        }
        try:
            core.create_namespaced_config_map(ns, {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": "hermes-config", "namespace": ns},
                "data": config_data,
            })
            logger.info("Created ConfigMap hermes-config in %s", ns)
        except Exception as e:
            logger.warning("Failed to create ConfigMap: %s", e)

    # ── ConfigMap (hermes-config-yaml) ────────────────────────────────
    # Seed config.yaml for agent pods
    try:
        core.read_namespaced_config_map("hermes-config-yaml", ns)
    except Exception:
        from pathlib import Path
        _hermes_home = Path(os.environ.get("LOGOS_HOME", os.environ.get("HERMES_HOME", str(Path.home() / ".logos"))))
        _config_yaml = ""
        try:
            _cfg_path = _hermes_home / "config.yaml"
            if _cfg_path.exists():
                _config_yaml = _cfg_path.read_text(encoding="utf-8")
        except Exception:
            pass
        if not _config_yaml:
            _config_yaml = "model:\n  default: ''\n"
        try:
            core.create_namespaced_config_map(ns, {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": "hermes-config-yaml", "namespace": ns},
                "data": {"config.yaml": _config_yaml},
            })
            logger.info("Created ConfigMap hermes-config-yaml in %s", ns)
        except Exception as e:
            logger.warning("Failed to create ConfigMap hermes-config-yaml: %s", e)

    # ── Shared memory PVC ─────────────────────────────────────────────
    try:
        core.read_namespaced_persistent_volume_claim("hermes-shared-memory-pvc", ns)
    except Exception:
        try:
            core.create_namespaced_persistent_volume_claim(ns, {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": "hermes-shared-memory-pvc", "namespace": ns},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            })
            logger.info("Created PVC hermes-shared-memory-pvc in %s", ns)
        except Exception as e:
            logger.warning("Failed to create shared memory PVC: %s", e)

    # ── Image pull secret (ghcr-creds) ────────────────────────────────
    # Copy from logos namespace if it exists there, otherwise skip
    # (public images don't need pull secrets)
    try:
        core.read_namespaced_secret("ghcr-creds", ns)
    except Exception:
        try:
            src = core.read_namespaced_secret("ghcr-creds", "logos")
            core.create_namespaced_secret(ns, {
                "apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "ghcr-creds", "namespace": ns},
                "type": src.type,
                "data": {k: base64.b64encode(v).decode() if isinstance(v, bytes) else v
                         for k, v in (src.data or {}).items()},
            })
            logger.info("Copied ghcr-creds from logos to %s", ns)
        except Exception:
            # No source secret — spawned pods will use public image or fail with
            # ImagePullBackOff (which is visible in the UI as "starting" state)
            logger.debug("ghcr-creds not available in logos namespace — skipping")


class KubernetesExecutor:
    """Manages agent instances as Kubernetes Deployments."""

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        """Create Deployment + Service + PVC + soul ConfigMap for a new agent instance."""
        core, apps = k8s_clients()

        # Ensure all prerequisite resources exist (ServiceAccount, Secret, ConfigMaps, PVCs)
        _ensure_namespace_prerequisites(core)

        dep_name = config.name  # already sanitised by the API layer via safe_k8s_name()
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

        # ── Early-exit if Deployment already exists ───────────────────────────
        # Check BEFORE creating any resources to avoid wasted PVC/Service creation.
        try:
            apps.read_namespaced_deployment(dep_name, HERMES_NAMESPACE)
            return SpawnedInstance(
                name=dep_name, url="", port=0, source="k8s",
                soul_name=soul.slug, model=config.model, requester=config.requester,
            )
        except Exception:
            pass

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
                            "image": os.environ.get("LOGOS_AGENT_IMAGE", "ghcr.io/gregsgreycode/logos:latest"),
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
