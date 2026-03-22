"""
HTTP API server for the Hermes gateway.

Provides:
  GET  /          — unified admin dashboard (no auth)
  GET  /health    — health check (no auth)
  GET  /status    — agent execution status JSON (no auth)
  GET  /sessions  — list active sessions (Bearer auth)
  POST /chat      — send a message, SSE stream (no auth — same-origin dashboard)
  GET  /canary/status                     — probe hermes-canary in-cluster health (active: bool)
  GET  /proxy/state                       — proxy → ai-router /admin/state
  POST /proxy/providers/{key}/toggle      — proxy → ai-router /admin/providers/{key}/toggle
"""

import asyncio
import dataclasses
import importlib.metadata
import json
import logging
import os
import pathlib
import re
import time
from pathlib import Path
from typing import Any

import yaml

import aiohttp
from aiohttp import web

from gateway.auth import db as auth_db
from gateway.auth.handlers import (
    handle_audit_logs,
    handle_login,
    handle_logout,
    handle_me,
    handle_refresh,
    handle_users_list,
    handle_users_me_patch,
    handle_users_patch,
    handle_users_post,
)
from gateway import admin_handlers
from gateway.auth.middleware import auth_middleware, check_rate_limit, require_csrf, require_permission
from gateway.auth.password import hash_password
from gateway.auth.rbac import can_spawn
from gateway.config import Platform
from gateway.session import SessionSource, build_session_context, build_session_context_prompt

logger = logging.getLogger(__name__)

_start_time: float = 0.0
_hermes_home: Path = Path.home() / ".hermes"
_AI_ROUTER_BASE = "http://ai-router.hermes.svc.cluster.local:9001"
_CANARY_HEALTH_URL = "http://hermes-canary.hermes.svc.cluster.local/health"
_INSTANCE_NAME = os.environ.get("HERMES_INSTANCE_NAME", "Hermes")
_IS_CANARY = os.environ.get("HERMES_IS_CANARY", "").lower() in ("1", "true", "yes")

try:
    _APP_VERSION = importlib.metadata.version("hermes-agent")
except importlib.metadata.PackageNotFoundError:
    _APP_VERSION = "dev"
_BUILD_SHA = os.environ.get("BUILD_SHA", "local")[:7]
_VERSION_LABEL = f"v{_APP_VERSION} · {_BUILD_SHA}{' · canary' if _IS_CANARY else ''}"
_HERMES_NAMESPACE = "hermes"
_INSTANCE_CPU_REQUEST = "500m"
_INSTANCE_MEM_REQUEST = "2Gi"
_INSTANCE_CPU_LIMIT = "4000m"
_INSTANCE_MEM_LIMIT = "6Gi"
# Minimum free cluster resources before auto-spawning (fault-tolerant: queue if below)
_SPAWN_CPU_THRESHOLD = 4.0   # cores
_SPAWN_MEM_THRESHOLD = 6 * 1024 ** 3  # 6 GiB in bytes

# In-memory request queue for instances that couldn't spawn due to resource constraints
_instance_queue: list[dict] = []

# ── Soul Registry ─────────────────────────────────────────────────────────────

_SOULS_DIR = pathlib.Path(__file__).parent.parent / "souls"


@dataclasses.dataclass
class SoulManifest:
    id: str
    slug: str
    name: str
    description: str
    category: str
    role_summary: str
    status: str          # "stable" | "experimental" | "deprecated"
    version: str
    created_by: str
    tags: list
    enforced_toolsets: list
    default_enabled_toolsets: list
    optional_toolsets: list
    forbidden_toolsets: list
    user_accessible: bool = True
    soul_md: str = ""

    def to_dict(self, include_soul_md: bool = False) -> dict:
        d = {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "role_summary": self.role_summary,
            "status": self.status,
            "version": self.version,
            "tags": self.tags,
            "user_accessible": self.user_accessible,
            "toolsets": {
                "enforced": self.enforced_toolsets,
                "default_enabled": self.default_enabled_toolsets,
                "optional": self.optional_toolsets,
                "forbidden": self.forbidden_toolsets,
            },
        }
        if include_soul_md:
            d["soul_md"] = self.soul_md
        return d


_SOUL_REGISTRY: dict[str, SoulManifest] = {}


def _load_souls() -> dict[str, SoulManifest]:
    """Load souls from the souls/ directory alongside the hermes-agent package."""
    global _SOUL_REGISTRY
    registry: dict[str, SoulManifest] = {}
    if not _SOULS_DIR.exists():
        logger.warning("Souls directory not found: %s", _SOULS_DIR)
        _SOUL_REGISTRY = registry
        return registry
    for soul_dir in sorted(_SOULS_DIR.iterdir()):
        if not soul_dir.is_dir():
            continue
        manifest_path = soul_dir / "soul.manifest.yaml"
        soul_md_path = soul_dir / "soul.md"
        if not manifest_path.exists():
            continue
        try:
            data = yaml.safe_load(manifest_path.read_text()) or {}
            toolsets = data.get("toolsets", {})
            soul = SoulManifest(
                id=data.get("id", soul_dir.name),
                slug=data.get("slug", soul_dir.name),
                name=data.get("name", soul_dir.name),
                description=data.get("description", ""),
                category=data.get("category", "general"),
                role_summary=data.get("role_summary", ""),
                status=data.get("status", "stable"),
                version=str(data.get("version", "1.0")),
                created_by=data.get("created_by", ""),
                tags=data.get("tags", []),
                enforced_toolsets=toolsets.get("enforced", []),
                default_enabled_toolsets=toolsets.get("default_enabled", []),
                optional_toolsets=toolsets.get("optional", []),
                forbidden_toolsets=toolsets.get("forbidden", []),
                user_accessible=data.get("user_accessible", True),
                soul_md=soul_md_path.read_text() if soul_md_path.exists() else "",
            )
            registry[soul.slug] = soul
        except Exception as e:
            logger.warning("Failed to load soul from %s: %s", soul_dir, e)
    _SOUL_REGISTRY = registry
    logger.info("Loaded %d souls: %s", len(registry), list(registry.keys()))
    return registry


def _get_soul_registry() -> dict[str, SoulManifest]:
    if not _SOUL_REGISTRY:
        _load_souls()
    return _SOUL_REGISTRY


def _validate_soul_overrides(soul: SoulManifest, overrides: dict) -> None:
    """Raise ValueError if overrides violate soul policy."""
    to_remove = set(overrides.get("remove", []))
    to_add = set(overrides.get("add", []))
    for ts in to_remove:
        if ts in soul.enforced_toolsets:
            raise ValueError(f"cannot_remove_enforced:{ts}")
    for ts in to_add:
        if ts in soul.forbidden_toolsets:
            raise ValueError(f"toolset_not_available:{ts}")
        if ts not in soul.optional_toolsets:
            raise ValueError(f"toolset_not_in_soul:{ts}")


def _compute_effective_toolsets(soul: SoulManifest, overrides: dict) -> list[str]:
    effective = set(soul.enforced_toolsets)
    effective |= set(soul.default_enabled_toolsets)
    effective -= set(overrides.get("remove", []))
    effective |= set(overrides.get("add", []))
    return sorted(effective)


# ── Kubernetes helpers ────────────────────────────────────────────────────────

def _k8s_clients():
    """Return (CoreV1Api, AppsV1Api) using in-cluster auth, falling back to kubeconfig."""
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()
    except ImportError:
        raise RuntimeError("kubernetes package not installed")


def _parse_cpu(s: str) -> float:
    """Parse k8s CPU string to float cores (e.g. '500m' → 0.5)."""
    if not s:
        return 0.0
    if s.endswith("m"):
        return int(s[:-1]) / 1000
    return float(s)


def _parse_mem(s: str) -> int:
    """Parse k8s memory string to bytes (e.g. '2Gi' → 2147483648)."""
    if not s:
        return 0
    for suffix, mult in [("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3),
                          ("Ti", 1024**4), ("K", 1000), ("M", 1000**2), ("G", 1000**3)]:
        if s.endswith(suffix):
            return int(s[:-len(suffix)]) * mult
    return int(s)


def _cluster_resources() -> dict:
    """Return total allocatable and currently requested CPU/RAM across the cluster."""
    core, _ = _k8s_clients()

    total_cpu = total_mem = 0.0
    for node in core.list_node().items:
        alloc = node.status.allocatable or {}
        total_cpu += _parse_cpu(alloc.get("cpu", "0"))
        total_mem += _parse_mem(alloc.get("memory", "0"))

    used_cpu = used_mem = 0.0
    for pod in core.list_pod_for_all_namespaces().items:
        if (pod.status.phase or "") not in ("Running", "Pending"):
            continue
        for c in (pod.spec.containers or []):
            req = (c.resources and c.resources.requests) or {}
            used_cpu += _parse_cpu(req.get("cpu", "0"))
            used_mem += _parse_mem(req.get("memory", "0"))

    return {
        "total_cpu": round(total_cpu, 2),
        "total_mem": int(total_mem),
        "used_cpu": round(used_cpu, 2),
        "used_mem": int(used_mem),
        "free_cpu": round(total_cpu - used_cpu, 2),
        "free_mem": int(total_mem - used_mem),
    }


def _list_hermes_instances() -> list[dict]:
    """List Deployments in the hermes namespace that are Hermes instances."""
    _, apps = _k8s_clients()
    core, _ = _k8s_clients()
    result = []
    for dep in apps.list_namespaced_deployment(_HERMES_NAMESPACE).items:
        name = dep.metadata.name
        if not name.startswith("hermes"):
            continue
        # Find the NodePort for this deployment
        port = None
        try:
            svcs = core.list_namespaced_service(
                _HERMES_NAMESPACE,
                label_selector=f"app={name}",
            ).items
            for svc in svcs:
                for p in (svc.spec.ports or []):
                    if p.node_port:
                        port = p.node_port
        except Exception:
            pass

        env_map = {}
        containers = (dep.spec.template.spec.containers or [])
        if containers:
            for e in (containers[0].env or []):
                if e.value:
                    env_map[e.name] = e.value

        ready = dep.status.ready_replicas or 0
        desired = dep.spec.replicas or 1
        annotations = dep.metadata.annotations or {}
        soul_meta = None
        if annotations.get("hermes.io/soul-slug"):
            ets_raw = annotations.get("hermes.io/effective-toolsets", "[]")
            try:
                ets = json.loads(ets_raw)
            except Exception:
                ets = []
            soul_meta = {
                "slug": annotations["hermes.io/soul-slug"],
                "name": annotations.get("hermes.io/soul-name", annotations["hermes.io/soul-slug"]),
                "version": annotations.get("hermes.io/soul-version", ""),
                "status": annotations.get("hermes.io/soul-status", "stable"),
                "effective_toolsets": ets,
            }
        result.append({
            "name": name,
            "instance_name": env_map.get("HERMES_INSTANCE_NAME", name),
            "ready": ready,
            "desired": desired,
            "status": "running" if ready >= desired else ("starting" if desired > 0 else "stopped"),
            "node_port": port,
            "created": dep.metadata.creation_timestamp.isoformat() if dep.metadata.creation_timestamp else None,
            "soul": soul_meta,
            "model_alias":  annotations.get("hermes.io/model-alias"),
            "machine_id":   annotations.get("hermes.io/machine-id"),
            "machine_name": annotations.get("hermes.io/machine-name"),
            "requester":    annotations.get("hermes.io/requester", ""),
        })
    return result


def _safe_k8s_name(requester: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", requester.lower()).strip("-")
    return f"hermes-{name}"[:52]  # k8s name limit is 63 chars


def _spawn_instance(
    requester: str,
    soul_slug: str = "general",
    tool_overrides: dict | None = None,
    model_alias: str = "balanced",
    machine_endpoint: str | None = None,
    machine_name: str | None = None,
    machine_id: str | None = None,
) -> dict:
    """Create Deployment + Service + PVC for a new named Hermes instance."""
    core, apps = _k8s_clients()
    dep_name = _safe_k8s_name(requester)
    if tool_overrides is None:
        tool_overrides = {}

    # Resolve soul
    registry = _get_soul_registry()
    soul = registry.get(soul_slug) or registry.get("general")
    if soul is None:
        # Fallback: no souls loaded — create a bare SoulManifest
        soul = SoulManifest(
            id="general", slug="general", name="General", description="",
            category="general", role_summary="", status="stable", version="1.0",
            created_by="", tags=[], enforced_toolsets=[], default_enabled_toolsets=[],
            optional_toolsets=[], forbidden_toolsets=[], soul_md="",
        )
    effective_toolsets = _compute_effective_toolsets(soul, tool_overrides)
    # Name: "Soul · model" e.g. "Companion · balanced"
    instance_name = soul.name + (" \u00b7 " + model_alias if model_alias and model_alias != "balanced" else "")

    # PVC
    pvc_name = f"{dep_name}-pvc"
    try:
        core.read_namespaced_persistent_volume_claim(pvc_name, _HERMES_NAMESPACE)
    except Exception:
        core.create_namespaced_persistent_volume_claim(
            _HERMES_NAMESPACE,
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc_name, "namespace": _HERMES_NAMESPACE},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            },
        )

    # Service (NodePort auto-assigned)
    svc_name = dep_name
    try:
        core.read_namespaced_service(svc_name, _HERMES_NAMESPACE)
    except Exception:
        core.create_namespaced_service(
            _HERMES_NAMESPACE,
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": svc_name, "namespace": _HERMES_NAMESPACE, "labels": {"app": dep_name}},
                "spec": {
                    "type": "NodePort",
                    "selector": {"app": dep_name},
                    "ports": [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}],
                },
            },
        )

    # Deployment
    try:
        apps.read_namespaced_deployment(dep_name, _HERMES_NAMESPACE)
        return {"status": "exists", "name": dep_name}
    except Exception:
        pass

    # Soul snapshot ConfigMap — created before the Deployment so it's available to the init container
    snap_name = f"{dep_name}-soul-snap"
    try:
        core.create_namespaced_config_map(
            _HERMES_NAMESPACE,
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": snap_name,
                    "namespace": _HERMES_NAMESPACE,
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
            raise  # 409 = already exists (partial retry), anything else is a real failure

    dep = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": dep_name,
            "namespace": _HERMES_NAMESPACE,
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
                "hermes.io/requester": requester,
                "hermes.io/model-alias":  model_alias,
                **({"hermes.io/machine-id":   machine_id}   if machine_id   else {}),
                **({"hermes.io/machine-name": machine_name} if machine_name else {}),
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
                    "tolerations": [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}],
                    "volumes": [
                        {"name": "hermes-home", "persistentVolumeClaim": {"claimName": pvc_name}},
                        {"name": "hermes-config-seed", "configMap": {"name": "hermes-config-yaml"}},
                        {"name": "hermes-soul-snap", "configMap": {"name": snap_name}},
                        {"name": "hermes-work", "emptyDir": {}},
                        {"name": "hermes-shared-memory", "persistentVolumeClaim": {"claimName": "hermes-shared-memory-pvc", "readOnly": True}},
                    ],
                    "securityContext": {"fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    # Affinity: prefer same node as the primary so the RWO shared-memory PVC
                    # (local-path, ReadWriteOnce) can be mounted read-only by both pods.
                    "affinity": {
                        "podAffinity": {
                            "preferredDuringSchedulingIgnoredDuringExecution": [{
                                "weight": 100,
                                "podAffinityTerm": {
                                    "labelSelector": {"matchLabels": {"app": "hermes"}},
                                    "topologyKey": "kubernetes.io/hostname",
                                },
                            }],
                        }
                    },
                    "initContainers": [
                        {
                            "name": "fix-perms",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", "chown -R 10001:10001 /hermes-home && chmod 750 /hermes-home"],
                            "volumeMounts": [{"name": "hermes-home", "mountPath": "/hermes-home"}],
                            "securityContext": {"runAsUser": 0},
                        },
                        {
                            "name": "seed-config",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", 'mkdir -p /hermes-home/memories && sed "s|\\${INSPECTOR_TOKEN}|${INSPECTOR_TOKEN}|g" /seed/config.yaml > /hermes-home/config.yaml && cp /soul-snap/SOUL.md /hermes-home/SOUL.md'],
                            "env": [{"name": "INSPECTOR_TOKEN", "valueFrom": {"secretKeyRef": {"name": "hermes-secret", "key": "INSPECTOR_TOKEN"}}}],
                            "volumeMounts": [
                                {"name": "hermes-home", "mountPath": "/hermes-home"},
                                {"name": "hermes-config-seed", "mountPath": "/seed", "readOnly": True},
                                {"name": "hermes-soul-snap", "mountPath": "/soul-snap", "readOnly": True},
                            ],
                            "securityContext": {"runAsUser": 10001, "runAsNonRoot": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        },
                    ],
                    "containers": [{
                        "name": "hermes",
                        "image": "ghcr.io/gregsgreycode/hermes:latest",
                        "ports": [{"name": "http", "containerPort": 8080}],
                        "env": [
                            {"name": "HOME", "value": "/home/hermes"},
                            {"name": "HERMES_INSTANCE_NAME", "value": instance_name},
                            {"name": "HERMES_LOG_LEVEL", "valueFrom": {"configMapKeyRef": {"name": "hermes-config", "key": "HERMES_LOG_LEVEL"}}},
                            {"name": "HERMES_PORT", "valueFrom": {"configMapKeyRef": {"name": "hermes-config", "key": "HERMES_PORT"}}},
                            {"name": "REQUEST_TIMEOUT_SECONDS", "valueFrom": {"configMapKeyRef": {"name": "hermes-config", "key": "REQUEST_TIMEOUT_SECONDS"}}},
                            # OPENAI_BASE_URL: use resolved machine endpoint if available, else ConfigMap default
                            *(
                                [{"name": "OPENAI_BASE_URL", "value": machine_endpoint}]
                                if machine_endpoint else
                                [{"name": "OPENAI_BASE_URL", "valueFrom": {"configMapKeyRef": {"name": "hermes-config", "key": "OPENAI_BASE_URL"}}}]
                            ),
                            {"name": "HERMES_MODEL", "valueFrom": {"configMapKeyRef": {"name": "hermes-config", "key": "HERMES_MODEL"}}},
                            {"name": "LLM_MODEL", "valueFrom": {"configMapKeyRef": {"name": "hermes-config", "key": "LLM_MODEL"}}},
                            {"name": "OPENAI_API_KEY", "valueFrom": {"secretKeyRef": {"name": "hermes-secret", "key": "OPENAI_API_KEY"}}},
                            {"name": "HERMES_INTERNAL_TOKEN", "valueFrom": {"secretKeyRef": {"name": "hermes-secret", "key": "HERMES_INTERNAL_TOKEN"}}},
                            {"name": "TELEGRAM_BOT_TOKEN", "valueFrom": {"secretKeyRef": {"name": "hermes-telegram", "key": "TELEGRAM_BOT_TOKEN"}}},
                            {"name": "TELEGRAM_ALLOWED_USERS", "value": "8754717106"},
                            {"name": "TELEGRAM_HOME_CHANNEL", "value": "-5152225827"},
                            {"name": "TELEGRAM_HOME_CHANNEL_NAME", "value": "Homelab Notifications"},
                        ],
                        "volumeMounts": [
                            {"name": "hermes-home", "mountPath": "/home/hermes/.hermes"},
                            {"name": "hermes-work", "mountPath": "/work"},
                            {"name": "hermes-shared-memory", "mountPath": "/home/hermes/.hermes-shared", "readOnly": True},
                        ],
                        "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "initialDelaySeconds": 15, "periodSeconds": 15, "failureThreshold": 3},
                        "livenessProbe": {"httpGet": {"path": "/health", "port": 8080}, "initialDelaySeconds": 30, "periodSeconds": 30, "failureThreshold": 3},
                        "resources": {
                            "requests": {"cpu": _INSTANCE_CPU_REQUEST, "memory": _INSTANCE_MEM_REQUEST},
                            "limits": {"cpu": _INSTANCE_CPU_LIMIT, "memory": _INSTANCE_MEM_LIMIT},
                        },
                        "securityContext": {"allowPrivilegeEscalation": False, "runAsNonRoot": True, "runAsUser": 10001, "readOnlyRootFilesystem": False, "capabilities": {"drop": ["ALL"]}},
                    }],
                },
            },
        },
    }
    apps.create_namespaced_deployment(_HERMES_NAMESPACE, dep)
    logger.info(json.dumps({
        "event": "instance_spawned",
        "instance": dep_name,
        "requester": requester,
        "soul_slug": soul.slug,
        "soul_version": soul.version,
        "effective_toolsets": effective_toolsets,
        "tool_overrides": tool_overrides,
        "snapshot_ref": snap_name,
    }))
    return {
        "status": "created",
        "name": dep_name,
        "instance_name": instance_name,
        "soul": {
            "slug": soul.slug,
            "name": soul.name,
            "version": soul.version,
            "effective_toolsets": effective_toolsets,
            "snapshot_ref": snap_name,
        },
    }


def _delete_instance(name: str) -> None:
    core, apps = _k8s_clients()
    from kubernetes.client.rest import ApiException
    for fn in [
        lambda: apps.delete_namespaced_deployment(name, _HERMES_NAMESPACE),
        lambda: core.delete_namespaced_service(name, _HERMES_NAMESPACE),
        lambda: core.delete_namespaced_persistent_volume_claim(f"{name}-pvc", _HERMES_NAMESPACE),
        lambda: core.delete_namespaced_config_map(f"{name}-soul-snap", _HERMES_NAMESPACE),
    ]:
        try:
            fn()
        except ApiException as e:
            if e.status != 404:
                raise


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logos</title>
<link rel="icon" type="image/svg+xml" href="/static/logo.svg">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<style>
  [x-cloak]{display:none!important}

  /* ── Design tokens ── default: midnight (indigo) ──────────────────── */
  :root,[data-theme="midnight"]{
    --bg-base:#030712;--bg-card:#111827;--bg-input:#1f2937;--border-col:#1f2937;--border-strong:#374151;
    --accent:#6366f1;--accent-dark:#4f46e5;--accent-bg:#1e1b4b;--accent-muted:#312e81;--accent-light:#a5b4fc;
    --accent-glow:rgba(99,102,241,.18);--accent-subtle:rgba(99,102,241,.08);
    --text-dim:#374151;--th-name:"Midnight";
  }
  [data-theme="crimson"]{
    --bg-base:#060308;--bg-card:#110a10;--bg-input:#1c1018;--border-col:#2a1622;--border-strong:#3f2030;
    --accent:#ef4444;--accent-dark:#b91c1c;--accent-bg:#1c0a0a;--accent-muted:#431407;--accent-light:#fca5a5;
    --accent-glow:rgba(239,68,68,.18);--accent-subtle:rgba(239,68,68,.08);
    --text-dim:#3f1f1f;--th-name:"Crimson";
  }
  [data-theme="terminal"]{
    --bg-base:#010f06;--bg-card:#071a0d;--bg-input:#0d2b15;--border-col:#143a1c;--border-strong:#1d5429;
    --accent:#22c55e;--accent-dark:#15803d;--accent-bg:#052e16;--accent-muted:#14532d;--accent-light:#86efac;
    --accent-glow:rgba(34,197,94,.18);--accent-subtle:rgba(34,197,94,.08);
    --text-dim:#143a1c;--th-name:"Terminal";
  }
  [data-theme="dusk"]{
    --bg-base:#060410;--bg-card:#0f0a1e;--bg-input:#19112e;--border-col:#281a45;--border-strong:#3d2860;
    --accent:#a855f7;--accent-dark:#7e22ce;--accent-bg:#1a0533;--accent-muted:#3b0764;--accent-light:#d8b4fe;
    --accent-glow:rgba(168,85,247,.18);--accent-subtle:rgba(168,85,247,.08);
    --text-dim:#3b0764;--th-name:"Dusk";
  }

  body{background-color:var(--bg-base);color:#f3f4f6}

  /* ── Non-midnight overrides: remap Tailwind hardcodes ─────────────── */
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-gray-950{background-color:var(--bg-base)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-gray-900{background-color:var(--bg-card)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-gray-800{background-color:var(--bg-input)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .border-gray-800,
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .border-gray-700{border-color:var(--border-col)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-indigo-600,
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-indigo-500{background-color:var(--accent)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-indigo-700{background-color:var(--accent-dark)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-indigo-800,
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-indigo-900{background-color:var(--accent-muted)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-indigo-950{background-color:var(--accent-bg)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .bg-indigo-400{background-color:var(--accent)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .border-indigo-700,
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .border-indigo-500{border-color:var(--accent)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .text-indigo-400,
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .text-indigo-500{color:var(--accent)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .text-indigo-200,
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .text-indigo-300{color:var(--accent-light)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .text-blue-400,
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .text-green-400{color:var(--accent-light)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .hover\:bg-indigo-500:hover{background-color:var(--accent-dark)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .hover\:bg-gray-700:hover{background-color:var(--bg-input)!important}
  :is([data-theme="crimson"],[data-theme="terminal"],[data-theme="dusk"]) .hover\:bg-indigo-600:hover{background-color:var(--accent)!important}

  /* ── Smooth theme transitions ─────────────────────────────────────── */
  .theme-transitioning *{
    transition:background-color 280ms ease,border-color 280ms ease,color 180ms ease,box-shadow 280ms ease!important
  }

  /* ── Semantic helpers ─────────────────────────────────────────────── */
  .tab-active{border-bottom-color:var(--accent)!important;color:var(--accent)}
  .th-ring{box-shadow:0 0 0 2px var(--accent),0 0 12px var(--accent-glow)}
  .th-btn-accent{background-color:var(--accent);color:#fff}
  .th-btn-accent:hover{background-color:var(--accent-dark)}

  /* ── Layout utils ─────────────────────────────────────────────────── */
  /* ── Scrollbars — thin iOS-style coloured bar ─────────────────────── */
  .chat-scroll,.sidebar-scroll,.exec-scroll{
    scrollbar-width:thin;
    scrollbar-color:var(--accent-muted) transparent;
  }
  .chat-scroll::-webkit-scrollbar,.sidebar-scroll::-webkit-scrollbar,.exec-scroll::-webkit-scrollbar{
    width:4px;
  }
  .chat-scroll::-webkit-scrollbar-track,.sidebar-scroll::-webkit-scrollbar-track,.exec-scroll::-webkit-scrollbar-track{
    background:transparent;
  }
  .chat-scroll::-webkit-scrollbar-thumb,.sidebar-scroll::-webkit-scrollbar-thumb,.exec-scroll::-webkit-scrollbar-thumb{
    background:var(--accent-muted);
    border-radius:9999px;
  }
  .chat-scroll::-webkit-scrollbar-thumb:hover,.sidebar-scroll::-webkit-scrollbar-thumb:hover,.exec-scroll::-webkit-scrollbar-thumb:hover{
    background:var(--accent);
  }
  .chat-scroll{flex:1;overflow-y:auto;scroll-behavior:smooth;min-height:0}
  .sidebar-scroll{overflow-y:auto;flex:1}
  .exec-scroll{overflow-y:auto;flex:1;min-height:0}
  .activity-scroll{overflow-y:auto;scrollbar-width:none;-ms-overflow-style:none}
  .activity-scroll::-webkit-scrollbar{display:none}
  .s-run{background:#14532d;color:#86efac}
  .s-slow{background:#713f12;color:#fde68a}
  .s-stuck{background:#7f1d1d;color:#fca5a5}
  .s-done{background:#052e16;color:#86efac}
  @keyframes sess-fade{0%{opacity:1;transform:translateY(0)}100%{opacity:0;transform:translateY(-6px)}}
  .session-fading{animation:sess-fade .5s ease forwards;pointer-events:none}
  pre{white-space:pre-wrap;word-break:break-word}

  /* ── Chat markdown rendering ──────────────────────────────────────── */
  .chat-md{line-height:1.55}
  .chat-md p{margin:.25em 0}
  .chat-md h1,.chat-md h2,.chat-md h3{font-weight:700;margin:.45em 0 .2em}
  .chat-md h1{font-size:1.15em}.chat-md h2{font-size:1.05em}.chat-md h3{font-size:.97em}
  .chat-md ul,.chat-md ol{padding-left:1.4em;margin:.25em 0}
  .chat-md li{margin:.1em 0}
  .chat-md code{font-family:monospace;font-size:.85em;background:#0f172a;padding:.1em .35em;border-radius:4px;color:#a5b4fc}
  .chat-md pre{background:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:.65em .85em;margin:.4em 0;overflow-x:auto}
  .chat-md pre code{background:none;padding:0;color:#e2e8f0}
  .chat-md blockquote{border-left:3px solid var(--accent);padding-left:.7em;margin:.35em 0;color:#9ca3af}
  .chat-md a{color:var(--accent-light);text-decoration:underline}
  .chat-md hr{border:none;border-top:1px solid #1f2937;margin:.5em 0}
  .chat-md table{border-collapse:collapse;font-size:.85em;margin:.4em 0}
  .chat-md th,.chat-md td{border:1px solid #374151;padding:.25em .55em}
  .chat-md th{background:#1f2937}
  .chat-mono{font-family:monospace;font-size:.82em;white-space:pre-wrap;word-break:break-word}

  /* ── Per-message stats card (click-toggled, expands below bubble) ── */
  .msg-stats{
    background:#0f172a;border-radius:0 0 8px 8px;
    padding:.5rem .7rem;white-space:nowrap;
    min-width:160px;border:1px solid #1e293b;border-top:none;
  }
  .msg-stats-row{display:flex;align-items:center;justify-content:space-between;gap:.6rem;font-size:.72rem;line-height:1.6}
  .msg-stats-label{color:#64748b}
  .msg-stats-val{color:#e2e8f0;font-family:monospace}
  /* Stats toggle — click to show/hide */
  .msg-hint{
    display:flex;align-items:center;justify-content:flex-end;gap:.2rem;
    font-size:.62rem;color:#4b5563;cursor:pointer;user-select:none;
    line-height:1.4;padding-right:4px;padding-top:2px;transition:color .15s;
  }
  .msg-hint:hover{color:#818cf8}
  /* Copy button — appears on bubble hover */
  .msg-copy{
    display:inline-flex;align-items:center;gap:.25rem;
    position:absolute;bottom:.4rem;right:.4rem;
    background:transparent;border:1px solid transparent;border-radius:4px;
    padding:.2rem .4rem;font-size:.65rem;color:#4b5563;
    cursor:pointer;opacity:0;transition:opacity .15s,color .15s,border-color .15s;
  }
  .msg-wrap:hover .msg-copy{opacity:1}
  .msg-copy:hover{color:#a5b4fc;border-color:#374151}
  .msg-copy.copied{color:#4ade80!important;border-color:#166534!important;opacity:1!important}

  /* ── Mic button ───────────────────────────────────────────────────── */
  @keyframes mic-pulse{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,.5)}50%{box-shadow:0 0 0 6px rgba(239,68,68,0)}}
  .mic-recording{animation:mic-pulse 1.2s ease infinite;background:#991b1b!important;border-color:#ef4444!important;color:#fca5a5!important}

  /* ── Render mode drop-up ──────────────────────────────────────────── */
  .render-dropup{position:relative}
  .render-menu{
    position:absolute;bottom:calc(100% + 6px);left:0;
    background:#0f172a;border:1px solid #1e293b;border-radius:8px;
    overflow:hidden;z-index:30;min-width:90px;
    opacity:0;transform:translateY(4px);pointer-events:none;
    transition:opacity .12s ease, transform .12s ease;
  }
  .render-menu.open{opacity:1;transform:translateY(0);pointer-events:auto}
  .render-menu button{display:block;width:100%;text-align:left;padding:.35rem .7rem;font-size:.75rem;transition:background .1s}
  .render-menu button:hover{background:#1e293b}
  .render-menu button.active{color:var(--accent-light);background:var(--accent-bg)}

  /* ── Theme picker card ────────────────────────────────────────────── */
  .th-card{
    position:relative;border-radius:.75rem;padding:.75rem;border:1px solid var(--border-col);
    background:var(--bg-card);cursor:pointer;transition:border-color 150ms,box-shadow 150ms;text-align:left;width:100%
  }
  .th-card:hover{border-color:var(--border-strong)}
  .th-card.th-active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 0 10px var(--accent-glow)}
  .th-swatch{height:.4rem;border-radius:.25rem;margin-bottom:.55rem;overflow:hidden;display:flex;gap:2px}
  .th-swatch span{flex:1;border-radius:2px}

  /* ── Hermes logo — wing color tokens (one set per theme) ─────── */
  :root,[data-theme="midnight"]{
    --hermes-wing-1:#a5b4fc;--hermes-wing-2:#818cf8;--hermes-wing-3:#6366f1;
  }
  [data-theme="crimson"]{
    --hermes-wing-1:#fca5a5;--hermes-wing-2:#f87171;--hermes-wing-3:#ef4444;
  }
  [data-theme="terminal"]{
    --hermes-wing-1:#86efac;--hermes-wing-2:#4ade80;--hermes-wing-3:#22c55e;
  }
  [data-theme="dusk"]{
    --hermes-wing-1:#e9d5ff;--hermes-wing-2:#c084fc;--hermes-wing-3:#a855f7;
  }

  /* ── Hermes logo component ────────────────────────────────────── */
  .hermes-logo{
    transition:transform 220ms ease,filter 220ms ease;
    display:block;
  }
  .hermes-logo:hover{
    transform:translateY(-1.5px);
    filter:drop-shadow(0 0 6px var(--accent-glow));
  }
  .hermes-wing{transition:fill 300ms ease,opacity 220ms ease}
  .hermes-wing-1{fill:var(--hermes-wing-1)}
  .hermes-wing-2{fill:var(--hermes-wing-2)}
  .hermes-wing-3{fill:var(--hermes-wing-3)}

  /* activeLayer: dim the other two, brighten the target */
  .hermes-wing-dim{opacity:.25}

  /* loading: execution layer pulses */
  @keyframes hermes-pulse{0%,100%{opacity:1}50%{opacity:.3}}
  .hermes-logo-loading .hermes-wing-3{animation:hermes-pulse 1.4s ease-in-out infinite}

  /* idle shimmer — very subtle, opt-in */
  @keyframes hermes-shimmer{0%,100%{filter:brightness(1)}50%{filter:brightness(1.12)}}
  .hermes-logo-idle{animation:hermes-shimmer 4s ease-in-out infinite}

  /* wake animation — breathe-pulse replaces bouncing balls */
  @keyframes logos-wake{0%,100%{transform:scale(0.55);opacity:0.2}45%{transform:scale(1.25);opacity:1}70%{transform:scale(0.9);opacity:0.7}}
  .logos-wake-dot{width:7px;height:7px;border-radius:9999px;background:var(--accent);display:inline-block;animation:logos-wake 1.6s ease-in-out infinite}
  .logos-wake-dot:nth-child(1){animation-delay:0ms}
  .logos-wake-dot:nth-child(2){animation-delay:220ms}
  .logos-wake-dot:nth-child(3){animation-delay:440ms}
</style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen" x-data="app()" x-init="init()">

<div class="max-w-screen-xl mx-auto px-4 pt-4 pb-0">

  <!-- Tabs + theme swatches -->
  <div class="flex items-end gap-6 border-b border-gray-800 mb-3">
    <!-- Logos brand mark -->
    <div class="pb-2 shrink-0 flex items-center gap-2">
      <!-- Inline SVG so gradient stops can use CSS theme variables -->
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 625 625" fill="none" aria-label="Logos"
           style="height:32px;width:32px;flex-shrink:0;filter:drop-shadow(0 0 6px var(--accent-glow, rgba(99,102,241,0.4)));">
        <defs>
          <linearGradient id="logoGrad-nav" x1="100" y1="100" x2="525" y2="525" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="var(--accent)"/>
            <stop offset="100%" stop-color="var(--accent-light)"/>
          </linearGradient>
        </defs>
        <g fill="url(#logoGrad-nav)" fill-rule="evenodd">
          <path d="M 466.0,133.0 L 455.0,128.0 L 442.0,128.0 L 433.0,132.0 L 426.0,139.0 L 421.0,150.0 L 423.0,170.0 L 381.0,209.0 L 380.0,131.0 L 390.0,126.0 L 400.0,114.0 L 402.0,96.0 L 397.0,84.0 L 385.0,74.0 L 369.0,72.0 L 354.0,79.0 L 348.0,86.0 L 345.0,94.0 L 345.0,109.0 L 349.0,118.0 L 357.0,126.0 L 365.0,129.0 L 367.0,132.0 L 366.0,178.0 L 319.0,134.0 L 321.0,113.0 L 315.0,100.0 L 307.0,93.0 L 299.0,90.0 L 283.0,91.0 L 273.0,97.0 L 269.0,102.0 L 265.0,111.0 L 265.0,125.0 L 270.0,136.0 L 277.0,143.0 L 287.0,148.0 L 286.0,167.0 L 235.0,167.0 L 223.0,153.0 L 213.0,149.0 L 202.0,149.0 L 192.0,153.0 L 184.0,161.0 L 180.0,170.0 L 180.0,184.0 L 182.0,190.0 L 191.0,201.0 L 199.0,205.0 L 212.0,206.0 L 222.0,202.0 L 230.0,195.0 L 236.0,180.0 L 286.0,180.0 L 286.0,218.0 L 255.0,247.0 L 192.0,248.0 L 187.0,247.0 L 182.0,238.0 L 174.0,231.0 L 166.0,228.0 L 154.0,228.0 L 146.0,231.0 L 139.0,237.0 L 133.0,248.0 L 132.0,256.0 L 137.0,273.0 L 149.0,283.0 L 164.0,285.0 L 171.0,283.0 L 180.0,277.0 L 225.0,319.0 L 231.0,321.0 L 269.0,321.0 L 277.0,327.0 L 279.0,333.0 L 278.0,421.0 L 272.0,428.0 L 269.0,429.0 L 237.0,429.0 L 233.0,432.0 L 232.0,439.0 L 236.0,443.0 L 275.0,443.0 L 282.0,440.0 L 288.0,434.0 L 292.0,426.0 L 291.0,320.0 L 283.0,311.0 L 274.0,307.0 L 234.0,307.0 L 199.0,276.0 L 187.0,262.0 L 258.0,261.0 L 265.0,258.0 L 300.0,223.0 L 300.0,148.0 L 309.0,144.0 L 367.0,200.0 L 367.0,220.0 L 316.0,268.0 L 312.0,278.0 L 312.0,421.0 L 316.0,433.0 L 322.0,439.0 L 331.0,443.0 L 483.0,444.0 L 450.0,468.0 L 175.0,468.0 L 169.0,463.0 L 168.0,460.0 L 168.0,337.0 L 166.0,333.0 L 137.0,305.0 L 132.0,304.0 L 129.0,308.0 L 129.0,500.0 L 131.0,504.0 L 135.0,506.0 L 462.0,506.0 L 477.0,496.0 L 506.0,472.0 L 507.0,434.0 L 502.0,429.0 L 335.0,429.0 L 327.0,422.0 L 326.0,419.0 L 326.0,343.0 L 371.0,304.0 L 381.0,306.0 L 388.0,315.0 L 388.0,325.0 L 346.0,377.0 L 346.0,395.0 L 349.0,396.0 L 392.0,346.0 L 402.0,329.0 L 402.0,314.0 L 400.0,308.0 L 391.0,296.0 L 379.0,291.0 L 379.0,277.0 L 430.0,247.0 L 432.0,247.0 L 436.0,253.0 L 444.0,259.0 L 450.0,261.0 L 463.0,261.0 L 472.0,257.0 L 483.0,243.0 L 485.0,233.0 L 484.0,225.0 L 478.0,213.0 L 471.0,207.0 L 464.0,204.0 L 454.0,203.0 L 446.0,205.0 L 434.0,214.0 L 430.0,221.0 L 427.0,233.0 L 373.0,263.0 L 367.0,269.0 L 365.0,274.0 L 365.0,289.0 L 327.0,322.0 L 326.0,321.0 L 327.0,278.0 L 432.0,181.0 L 442.0,185.0 L 451.0,186.0 L 460.0,184.0 L 471.0,176.0 L 476.0,167.0 L 477.0,152.0 L 472.0,139.0 Z M 157.0,241.0 L 164.0,241.0 L 168.0,243.0 L 175.0,252.0 L 175.0,261.0 L 173.0,265.0 L 168.0,270.0 L 163.0,272.0 L 157.0,272.0 L 151.0,269.0 L 145.0,259.0 L 145.0,253.0 L 148.0,247.0 Z M 453.0,217.0 L 460.0,217.0 L 466.0,220.0 L 472.0,230.0 L 472.0,235.0 L 469.0,242.0 L 460.0,248.0 L 453.0,248.0 L 447.0,245.0 L 441.0,234.0 L 443.0,224.0 L 448.0,219.0 Z M 204.0,162.0 L 214.0,163.0 L 222.0,172.0 L 221.0,184.0 L 212.0,192.0 L 202.0,192.0 L 193.0,183.0 L 192.0,177.0 L 195.0,168.0 Z M 445.0,141.0 L 456.0,142.0 L 464.0,152.0 L 463.0,164.0 L 455.0,172.0 L 447.0,173.0 L 442.0,171.0 L 434.0,161.0 L 434.0,152.0 L 436.0,148.0 Z M 289.0,103.0 L 299.0,104.0 L 306.0,110.0 L 308.0,115.0 L 308.0,123.0 L 306.0,127.0 L 297.0,134.0 L 285.0,132.0 L 278.0,123.0 L 279.0,111.0 Z M 368.0,86.0 L 376.0,85.0 L 381.0,87.0 L 386.0,91.0 L 389.0,98.0 L 387.0,110.0 L 378.0,117.0 L 369.0,117.0 L 365.0,115.0 L 358.0,107.0 L 358.0,96.0 L 360.0,92.0 Z"/>
        </g>
      </svg>
      <template x-if="isCanary">
        <span class="px-2 py-0.5 rounded-full text-[10px] font-bold tracking-widest uppercase border border-yellow-500 bg-yellow-950 text-yellow-400 self-center">canary</span>
      </template>
    </div>
    <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
      :class="tab==='sessions'?'tab-active':'text-gray-400 hover:text-white'"
      @click="tab='sessions'; if(!clusterInstances.length) loadInstances()">Chats</button>
    <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
      :class="tab==='instances'?'tab-active':'text-gray-400 hover:text-white'"
      @click="tab='instances'; loadInstances(); loadSouls()">Instances</button>
    <template x-if="can('manage_machines') || can('manage_profiles') || can('view_routing_debug')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='routing'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='routing'; loadRoutingData()">Routing</button>
    </template>
    <template x-if="can('manage_users') || can('view_audit_logs')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='admin'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='admin'; if(!can('manage_users') && adminTab==='users') adminTab='audit'; if(adminTab==='routing-log') loadAdminRoutingLog(); else loadAdminData()">Admin</button>
    </template>
    <template x-if="can('view_workflows')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='workflows'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='workflows'; loadWorkflows()">Workflows</button>
    </template>
    <template x-if="can('view_runs')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='runs'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='runs'; loadAgentRuns()">Runs</button>
    </template>
    <!-- Canary pill (inline, between Admin and Theme) -->
    <template x-if="canary.active">
      <div class="pb-2 flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-yellow-600 bg-yellow-950 text-yellow-400 text-xs font-medium animate-pulse self-center">
        <span>🐤</span>
        <span>canary live</span>
      </div>
    </template>
    <!-- Theme picker -->
    <div class="ml-auto pb-2 relative" x-data @click.away="themePickerOpen=false">
      <button @click="themePickerOpen=!themePickerOpen"
        class="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-medium transition-colors"
        :class="themePickerOpen
          ? 'border-[var(--accent)] bg-[var(--accent-subtle)] text-[var(--accent-light)]'
          : 'border-gray-700 text-gray-400 hover:border-gray-600 hover:text-gray-300'">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent)"></span>
        <span x-text="themes.find(t=>t.id===theme)?.name || theme"></span>
        <span class="opacity-50" x-text="themePickerOpen ? '▲' : '▼'"></span>
      </button>

      <!-- Dropdown -->
      <div x-show="themePickerOpen" x-cloak
        class="absolute right-0 top-full mt-2 z-50 p-3 rounded-2xl border border-gray-700 shadow-2xl"
        style="width:280px;background:var(--bg-card);border-color:var(--border-strong)">
        <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Theme</div>
        <div class="grid grid-cols-2 gap-2">
          <template x-for="t in themes" :key="t.id">
            <button class="th-card" :class="theme===t.id ? 'th-active' : ''"
              @click="setTheme(t.id); themePickerOpen=false">
              <!-- Swatch bar: base / surface / accent -->
              <div class="th-swatch">
                <span :style="`background:${t.base};flex:2`"></span>
                <span :style="`background:${t.surface};flex:2`"></span>
                <span :style="`background:${t.accent};flex:1`"></span>
              </div>
              <div class="text-xs font-semibold leading-tight" style="color:#f3f4f6" x-text="t.name"></div>
              <div class="text-xs mt-0.5 leading-tight" style="color:#6b7280" x-text="t.mood"></div>
            </button>
          </template>
        </div>
        <div class="mt-3 pt-3 border-t text-xs text-gray-600" style="border-color:var(--border-col)">
          Theme is saved and persists across sessions.
        </div>
      </div>
    </div>
    <!-- Account menu -->
    <template x-if="authUser">
      <div class="pb-2 relative ml-2 shrink-0" @click.away="accountMenuOpen=false">
        <button @click="accountMenuOpen=!accountMenuOpen"
          class="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-medium transition-colors border-b-2 border-transparent"
          :class="accountMenuOpen
            ? 'border-gray-600 bg-gray-800 text-gray-200'
            : 'border-gray-700 text-gray-400 hover:border-gray-600 hover:text-gray-300'">
          <span :class="{
            'text-red-400':    authUser.role==='admin',
            'text-indigo-400': authUser.role==='operator',
            'text-gray-400':   true
          }" x-text="authUser.display_name || authUser.email"></span>
          <span class="opacity-50" x-text="accountMenuOpen ? '▲' : '▼'"></span>
        </button>
        <div x-show="accountMenuOpen" x-cloak
          class="absolute right-0 top-full mt-2 z-50 rounded-xl border border-gray-700 shadow-2xl overflow-hidden"
          style="width:220px;background:var(--bg-card);border-color:var(--border-strong)">
          <!-- User info -->
          <div class="px-4 py-3 border-b border-gray-800">
            <div class="text-xs font-semibold text-white" x-text="authUser.display_name || authUser.email"></div>
            <div class="text-xs text-gray-500 mt-0.5 truncate" x-text="authUser.email"></div>
            <div class="text-xs mt-1 px-1.5 py-0.5 rounded inline-block"
              :class="{
                'bg-red-950 text-red-400':     authUser.role==='admin',
                'bg-indigo-950 text-indigo-400': authUser.role==='operator',
                'bg-gray-800 text-gray-500':   true
              }"
              x-text="authUser.role"></div>
          </div>
          <!-- Change password -->
          <div class="px-4 py-3 border-b border-gray-800" x-show="!changePwOpen">
            <button @click="changePwOpen=true"
              class="text-xs text-gray-400 hover:text-white transition-colors w-full text-left">Change password</button>
          </div>
          <div class="px-4 py-4 border-b border-gray-800" x-show="changePwOpen" x-cloak>
            <div class="text-sm font-semibold text-gray-300 mb-3">Change password</div>
            <input x-model="changePwCurrent" type="password" placeholder="Current password"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 mb-2 focus:outline-none focus:border-[var(--accent)]"/>
            <input x-model="changePwNew" type="password" placeholder="New password (min 8)"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 mb-2 focus:outline-none focus:border-[var(--accent)]"/>
            <input x-model="changePwConfirm" type="password" placeholder="Confirm new password"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 mb-3 focus:outline-none focus:border-[var(--accent)]"/>
            <div x-show="changePwError" class="text-xs text-red-400 mb-2" x-text="changePwError"></div>
            <div x-show="changePwSuccess" class="text-xs text-green-400 mb-2">Password updated.</div>
            <div class="flex gap-2">
              <button @click="submitChangePassword()"
                :disabled="changePwLoading"
                class="flex-1 bg-[var(--accent)] hover:opacity-90 disabled:opacity-40 text-sm text-white rounded-lg px-4 py-2 font-medium transition-opacity">
                <span x-show="!changePwLoading">Save</span>
                <span x-show="changePwLoading" class="animate-pulse">Saving…</span>
              </button>
              <button @click="changePwOpen=false; changePwCurrent=''; changePwNew=''; changePwConfirm=''; changePwError=''; changePwSuccess=false"
                class="text-sm text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg px-4 py-2 transition-colors">Cancel</button>
            </div>
          </div>
          <!-- Admin: Re-run setup wizard -->
          <template x-if="authUser?.role === 'admin'">
            <div class="px-4 py-3 border-t border-gray-800">
              <button @click="resetSetup()"
                class="text-xs text-gray-500 hover:text-amber-400 transition-colors w-full text-left">
                Re-run setup wizard…
              </button>
            </div>
          </template>
          <!-- Log out -->
          <div class="px-4 py-3 border-t border-gray-800">
            <button @click="logout()"
              class="text-xs text-gray-400 hover:text-red-400 transition-colors w-full text-left">Log out</button>
          </div>
        </div>
      </div>
    </template>
  </div>

  <!-- ── Chats Tab ────────────────────────────────────────────────── -->
  <div x-show="tab==='sessions'" x-cloak style="height:calc(100vh - 84px)">

    <!-- Agent selector — who are you talking to? -->
    <div class="flex items-center gap-2 mb-3 flex-wrap">
      <template x-for="inst in chatAgents" :key="inst.id">
        <div class="flex items-center gap-0.5">
          <button class="flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-colors"
            :class="activeInstanceId === inst.id
              ? 'bg-indigo-700 text-white'
              : inst.k8s_status === 'stopped' ? 'bg-gray-900 text-gray-600 border border-gray-800 hover:bg-gray-800 hover:text-gray-400'
              : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white'"
            :title="inst.id === 'self' ? 'Core — always available'
              : inst.k8s_status === 'stopped' ? (inst.name + ' — stopped or failed')
              : inst.soul
                ? (inst.soul.name + (inst.model_alias ? ' \u00b7 ' + inst.model_alias + (inst.machine_name ? ' \u2192 ' + inst.machine_name : '') : ''))
                : inst.url || ''"
            @click="switchInstance(inst.id)">
            <!-- Core badge for the always-on base instance -->
            <span x-show="inst.id === 'self'"
              class="text-[10px] font-bold tracking-wider uppercase px-1 py-0.5 rounded bg-indigo-900 text-indigo-300 mr-0.5">Core</span>
            <span x-text="inst.id === 'self' ? 'Hermes' : inst.name"></span>
            <!-- Status dot -->
            <span x-show="activeInstanceId === inst.id && inst.k8s_status !== 'stopped'"
              class="w-1.5 h-1.5 bg-green-400 rounded-full ml-0.5 shrink-0"></span>
            <span x-show="inst.k8s_status === 'starting'"
              class="animate-spin text-[10px] ml-0.5">&#9881;</span>
            <span x-show="inst.k8s_status === 'stopped'"
              class="text-[10px] ml-0.5 text-red-500" title="Stopped or failed">&#9888;</span>
          </button>
          <button x-show="inst.editable"
            @click.stop="removeInstance(inst.id)"
            class="text-gray-700 hover:text-red-400 text-xs w-4 h-4 flex items-center justify-center">✕</button>
          <button x-show="inst.source === 'k8s' && (can('manage_instances') || authUser?.role === 'admin')"
            @click.stop="confirmDeleteInstance(inst.id.replace('k8s-', ''))"
            class="text-gray-700 hover:text-red-400 text-xs w-4 h-4 flex items-center justify-center" title="Delete instance">✕</button>
        </div>
      </template>
      <template x-if="!showAddInstance">
        <button @click="showAddInstance=true"
          class="px-2 py-1 rounded-full text-xs text-gray-600 hover:text-gray-300 border border-dashed border-gray-700 hover:border-gray-600 transition-colors">
          + Add agent
        </button>
      </template>
      <template x-if="showAddInstance">
        <div class="flex items-center gap-2 flex-wrap">
          <input x-model="newInstanceName" placeholder="Name (e.g. Partner's Hermes)"
            class="w-40 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500">
          <input x-model="newInstanceUrl" placeholder="http://192.168.1.x:30902"
            class="w-48 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500">
          <button @click="addInstance()"
            class="px-2 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-xs text-white font-medium">Add</button>
          <button @click="showAddInstance=false; newInstanceName=''; newInstanceUrl=''"
            class="text-gray-600 hover:text-gray-400 text-xs">✕</button>
        </div>
      </template>
    </div>

    <div class="flex gap-4 h-full" style="height:calc(100% - 36px)">

      <!-- Sidebar: chat history list -->
      <div class="w-44 shrink-0 flex flex-col h-full">
        <button @click="newChat()"
          class="w-full mb-3 px-3 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-lg font-medium transition-colors flex items-center justify-center gap-2">
          <span>＋</span><span>New Chat</span>
        </button>
        <div class="sidebar-scroll space-y-1 pr-1">
          <template x-for="chat in chats" :key="chat.id">
            <div class="group relative px-3 py-2 rounded-lg border cursor-pointer transition-colors select-none"
                 :class="activeChatId===chat.id
                   ? 'bg-indigo-950 border-indigo-700'
                   : 'bg-gray-900 border-gray-800 hover:border-gray-700'"
                 @click="switchChat(chat.id)">
              <div class="pr-4">
                <div class="text-xs font-medium truncate"
                     :class="activeChatId===chat.id ? 'text-indigo-200' : 'text-gray-300'"
                     x-text="chat.name"></div>
              </div>
              <div class="flex items-center justify-between mt-0.5 pr-4">
                <span class="text-xs text-gray-600" x-text="fmtChatTime(chat.updated_at)"></span>
                <template x-if="(chat.messages||[]).length > 0">
                  <span class="shrink-0 text-xs font-mono rounded px-1 py-0 leading-tight"
                    :class="activeChatId===chat.id ? 'bg-indigo-800 text-indigo-300' : 'bg-gray-800 text-gray-500'"
                    x-text="(chat.messages||[]).length"></span>
                </template>
              </div>
              <button class="absolute top-2 right-2 opacity-0 group-hover:opacity-100 text-gray-600 hover:text-red-400 text-xs transition-opacity leading-none w-4 h-4 flex items-center justify-center"
                      @click.stop="deleteChat(chat.id)">✕</button>
            </div>
          </template>
        </div>
      </div>

      <!-- Chat panel (tall, fills height) -->
      <div class="flex-1 min-w-0 flex flex-col h-full">
        <div class="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden flex flex-col h-full">

          <!-- Card header -->
          <div class="px-4 pt-3 pb-0 border-b border-gray-800 shrink-0">

            <!-- Row 1: name + status dot + chat ID + canary -->
            <div class="flex items-center gap-3 pb-1.5">
              <div class="w-2 h-2 rounded-full shrink-0"
                :class="chatLoading ? 'bg-indigo-500 animate-pulse' : 'bg-green-500'"></div>
              <span class="font-semibold text-white" x-text="status.instance_name || 'Hermes'"></span>
              <span class="text-xs text-gray-600 font-mono"
                x-text="activeChatId ? '#' + activeChatId.slice(-6) : ''"></span>
              <template x-if="canary.active">
                <span class="text-xs px-1.5 py-0.5 rounded bg-yellow-900 text-yellow-400 font-semibold animate-pulse ml-1">🐤 canary</span>
              </template>
            </div>

            <!-- Row 2: soul + routing context chips (k8s instances only) -->
            <template x-if="activeInstance.soul || activeInstance.model_alias">
              <div class="flex items-center gap-1.5 pb-1.5 flex-wrap">
                <template x-if="activeInstance.soul">
                  <span class="text-xs px-1.5 py-0.5 rounded font-mono bg-indigo-950 text-indigo-300 border border-indigo-900"
                    x-text="activeInstance.soul.name"></span>
                </template>
                <template x-if="activeInstance.model_alias">
                  <span class="text-xs px-1.5 py-0.5 rounded font-mono border"
                    :class="activeInstance.machine_name
                      ? 'bg-[var(--accent-subtle)] text-[var(--accent)] border-[var(--accent)]/40'
                      : 'bg-gray-800 text-gray-500 border-gray-700'"
                    x-text="activeInstance.model_alias + (activeInstance.machine_name ? ' → ' + activeInstance.machine_name : '')"></span>
                </template>
                <template x-if="activeInstance.k8s_status && activeInstance.k8s_status !== 'running'">
                  <span class="text-xs px-1.5 py-0.5 rounded border border-yellow-900 bg-yellow-950 text-yellow-500 font-medium"
                    x-text="activeInstance.k8s_status"></span>
                </template>
              </div>
            </template>


          </div>

          <!-- Messages (fills remaining height) -->
          <div id="chat-messages" class="chat-scroll px-4 py-3 space-y-3">
            <!-- Ghost logo — fades out on first message -->
            <div x-show="chatMessages.length === 0"
                 x-transition:leave="transition ease-in duration-700"
                 x-transition:leave-start="opacity-100"
                 x-transition:leave-end="opacity-0"
                 class="flex items-end justify-center pb-6 h-full pointer-events-none select-none"
                 style="opacity:0.12">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 625 625" fill="none" aria-hidden="true"
                   style="width:100px;height:100px;filter:grayscale(1) brightness(2);">
                <defs>
                  <linearGradient id="logoGrad-ghost" x1="100" y1="100" x2="525" y2="525" gradientUnits="userSpaceOnUse">
                    <stop offset="0%" stop-color="var(--accent)"/>
                    <stop offset="100%" stop-color="var(--accent-light)"/>
                  </linearGradient>
                </defs>
                <g fill="url(#logoGrad-ghost)" fill-rule="evenodd">
                  <path d="M 466.0,133.0 L 455.0,128.0 L 442.0,128.0 L 433.0,132.0 L 426.0,139.0 L 421.0,150.0 L 423.0,170.0 L 381.0,209.0 L 380.0,131.0 L 390.0,126.0 L 400.0,114.0 L 402.0,96.0 L 397.0,84.0 L 385.0,74.0 L 369.0,72.0 L 354.0,79.0 L 348.0,86.0 L 345.0,94.0 L 345.0,109.0 L 349.0,118.0 L 357.0,126.0 L 365.0,129.0 L 367.0,132.0 L 366.0,178.0 L 319.0,134.0 L 321.0,113.0 L 315.0,100.0 L 307.0,93.0 L 299.0,90.0 L 283.0,91.0 L 273.0,97.0 L 269.0,102.0 L 265.0,111.0 L 265.0,125.0 L 270.0,136.0 L 277.0,143.0 L 287.0,148.0 L 286.0,167.0 L 235.0,167.0 L 223.0,153.0 L 213.0,149.0 L 202.0,149.0 L 192.0,153.0 L 184.0,161.0 L 180.0,170.0 L 180.0,184.0 L 182.0,190.0 L 191.0,201.0 L 199.0,205.0 L 212.0,206.0 L 222.0,202.0 L 230.0,195.0 L 236.0,180.0 L 286.0,180.0 L 286.0,218.0 L 255.0,247.0 L 192.0,248.0 L 187.0,247.0 L 182.0,238.0 L 174.0,231.0 L 166.0,228.0 L 154.0,228.0 L 146.0,231.0 L 139.0,237.0 L 133.0,248.0 L 132.0,256.0 L 137.0,273.0 L 149.0,283.0 L 164.0,285.0 L 171.0,283.0 L 180.0,277.0 L 225.0,319.0 L 231.0,321.0 L 269.0,321.0 L 277.0,327.0 L 279.0,333.0 L 278.0,421.0 L 272.0,428.0 L 269.0,429.0 L 237.0,429.0 L 233.0,432.0 L 232.0,439.0 L 236.0,443.0 L 275.0,443.0 L 282.0,440.0 L 288.0,434.0 L 292.0,426.0 L 291.0,320.0 L 283.0,311.0 L 274.0,307.0 L 234.0,307.0 L 199.0,276.0 L 187.0,262.0 L 258.0,261.0 L 265.0,258.0 L 300.0,223.0 L 300.0,148.0 L 309.0,144.0 L 367.0,200.0 L 367.0,220.0 L 316.0,268.0 L 312.0,278.0 L 312.0,421.0 L 316.0,433.0 L 322.0,439.0 L 331.0,443.0 L 483.0,444.0 L 450.0,468.0 L 175.0,468.0 L 169.0,463.0 L 168.0,460.0 L 168.0,337.0 L 166.0,333.0 L 137.0,305.0 L 132.0,304.0 L 129.0,308.0 L 129.0,500.0 L 131.0,504.0 L 135.0,506.0 L 462.0,506.0 L 477.0,496.0 L 506.0,472.0 L 507.0,434.0 L 502.0,429.0 L 335.0,429.0 L 327.0,422.0 L 326.0,419.0 L 326.0,343.0 L 371.0,304.0 L 381.0,306.0 L 388.0,315.0 L 388.0,325.0 L 346.0,377.0 L 346.0,395.0 L 349.0,396.0 L 392.0,346.0 L 402.0,329.0 L 402.0,314.0 L 400.0,308.0 L 391.0,296.0 L 379.0,291.0 L 379.0,277.0 L 430.0,247.0 L 432.0,247.0 L 436.0,253.0 L 444.0,259.0 L 450.0,261.0 L 463.0,261.0 L 472.0,257.0 L 483.0,243.0 L 485.0,233.0 L 484.0,225.0 L 478.0,213.0 L 471.0,207.0 L 464.0,204.0 L 454.0,203.0 L 446.0,205.0 L 434.0,214.0 L 430.0,221.0 L 427.0,233.0 L 373.0,263.0 L 367.0,269.0 L 365.0,274.0 L 365.0,289.0 L 327.0,322.0 L 326.0,321.0 L 327.0,278.0 L 432.0,181.0 L 442.0,185.0 L 451.0,186.0 L 460.0,184.0 L 471.0,176.0 L 476.0,167.0 L 477.0,152.0 L 472.0,139.0 Z M 157.0,241.0 L 164.0,241.0 L 168.0,243.0 L 175.0,252.0 L 175.0,261.0 L 173.0,265.0 L 168.0,270.0 L 163.0,272.0 L 157.0,272.0 L 151.0,269.0 L 145.0,259.0 L 145.0,253.0 L 148.0,247.0 Z M 453.0,217.0 L 460.0,217.0 L 466.0,220.0 L 472.0,230.0 L 472.0,235.0 L 469.0,242.0 L 460.0,248.0 L 453.0,248.0 L 447.0,245.0 L 441.0,234.0 L 443.0,224.0 L 448.0,219.0 Z M 204.0,162.0 L 214.0,163.0 L 222.0,172.0 L 221.0,184.0 L 212.0,192.0 L 202.0,192.0 L 193.0,183.0 L 192.0,177.0 L 195.0,168.0 Z M 445.0,141.0 L 456.0,142.0 L 464.0,152.0 L 463.0,164.0 L 455.0,172.0 L 447.0,173.0 L 442.0,171.0 L 434.0,161.0 L 434.0,152.0 L 436.0,148.0 Z M 289.0,103.0 L 299.0,104.0 L 306.0,110.0 L 308.0,115.0 L 308.0,123.0 L 306.0,127.0 L 297.0,134.0 L 285.0,132.0 L 278.0,123.0 L 279.0,111.0 Z M 368.0,86.0 L 376.0,85.0 L 381.0,87.0 L 386.0,91.0 L 389.0,98.0 L 387.0,110.0 L 378.0,117.0 L 369.0,117.0 L 365.0,115.0 L 358.0,107.0 L 358.0,96.0 L 360.0,92.0 Z"/>
                </g>
              </svg>
            </div>
            <template x-for="(msg,i) in chatMessages" :key="i">
              <div :class="msg.role==='user' ? 'text-right' : 'text-left'">
                <!-- User messages: always plain bubble -->
                <template x-if="msg.role==='user'">
                  <span class="inline-block max-w-3xl text-left px-3 py-2 rounded-xl text-sm leading-relaxed bg-indigo-700 text-white"
                    x-text="msg.content"></span>
                </template>
                <!-- Assistant messages: rendered + copy button + click-toggled stats -->
                <template x-if="msg.role!=='user'">
                  <div class="msg-wrap relative inline-block max-w-3xl" x-data="{statsOpen:false,copied:false}">
                    <div class="text-left px-3 pt-2 pb-7 rounded-xl text-sm bg-gray-800 text-gray-100"
                      :class="chatRenderMode==='mono' ? 'chat-mono' : 'chat-md'"
                      x-html="renderMsg(msg.content)"></div>
                    <!-- Copy button — visible on bubble hover -->
                    <button class="msg-copy" :class="copied?\'copied\':\'\'"
                      @click.stop="navigator.clipboard.writeText(msg.content).then(()=>{copied=true;setTimeout(()=>copied=false,1500)})"
                      title="Copy response">
                      <svg width="11" height="11" viewBox="0 0 16 16" fill="none"><rect x="5" y="5" width="9" height="9" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M3 10V3a2 2 0 012-2h7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                      <span x-text="copied?\'✓ copied\':\'copy\'"></span>
                    </button>
                    <!-- Stats card — only shown when explicitly toggled -->
                    <template x-if="msg.stats">
                      <div class="msg-stats" x-show="statsOpen">
                        <template x-if="msg.stats.model">
                          <div class="msg-stats-row">
                            <span class="msg-stats-label">model</span>
                            <span class="msg-stats-val" x-text="msg.stats.model.split(\'/\').pop()"></span>
                          </div>
                        </template>
                        <div class="msg-stats-row">
                          <span class="msg-stats-label">context</span>
                          <span class="msg-stats-val" x-text="fmtTokens(msg.stats.prompt_tokens || 0) + \' tok\'"></span>
                        </div>
                        <div class="msg-stats-row">
                          <span class="msg-stats-label">time</span>
                          <span class="msg-stats-val" x-text="(msg.stats.elapsed_s || 0) + \'s\'"></span>
                        </div>
                        <template x-if="(msg.stats.elapsed_s || 0) > 0 && (msg.stats.prompt_tokens || 0) > 0">
                          <div class="msg-stats-row">
                            <span class="msg-stats-label">speed</span>
                            <span class="msg-stats-val" x-text="Math.round((msg.stats.prompt_tokens / msg.stats.elapsed_s)) + \' tok/s\'"></span>
                          </div>
                        </template>
                        <template x-if="(msg.stats.tools_available || 0) > 0">
                          <div class="msg-stats-row">
                            <span class="msg-stats-label">tools</span>
                            <span class="msg-stats-val" x-text="(msg.stats.tools_used > 0 ? msg.stats.tools_used + \' used / \' : \'\') + msg.stats.tools_available + \' loaded\'"></span>
                          </div>
                        </template>
                        <template x-if="(msg.stats.api_calls || 0) > 1">
                          <div class="msg-stats-row">
                            <span class="msg-stats-label">calls</span>
                            <span class="msg-stats-val" x-text="msg.stats.api_calls"></span>
                          </div>
                        </template>
                      </div>
                    </template>
                    <!-- Stats toggle — click to expand/collapse -->
                    <template x-if="msg.stats">
                      <span class="msg-hint" @click="statsOpen=!statsOpen">
                        <span x-text="statsOpen ? \'▴ hide stats\' : \'⋯ stats\'"></span>
                      </span>
                    </template>
                  </div>
                </template>
              </div>
            </template>
            <!-- Thinking / wake dots -->
            <div x-show="chatLoading && !webSession" class="flex items-center gap-2 text-gray-500 text-sm">
              <!-- wake state: breathe-pulse -->
              <div x-show="isWakingUp" class="flex gap-1.5 items-center">
                <span class="logos-wake-dot"></span>
                <span class="logos-wake-dot"></span>
                <span class="logos-wake-dot"></span>
              </div>
              <!-- thinking state: classic bounce -->
              <div x-show="!isWakingUp" class="flex gap-1">
                <span class="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce" style="animation-delay:0ms"></span>
                <span class="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce" style="animation-delay:150ms"></span>
                <span class="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce" style="animation-delay:300ms"></span>
              </div>
              <span x-text="(status.instance_name||'Hermes') + ' is thinking\u2026'"></span>
            </div>
            <!-- Instance starting guard -->
            <div x-show="!chatLoading && activeInstance && activeInstance.k8s_status === 'starting'" x-cloak
                 class="flex items-center gap-2 text-sm text-yellow-600 py-1">
              <span class="animate-spin inline-block">&#9881;</span>
              <span>Instance is starting up — chat will be available once it\'s ready.</span>
            </div>
          </div>

          <!-- Active tool bar -->
          <template x-if="chatLoading && webSession">
            <div class="mx-3 mb-2 px-3 py-1.5 rounded-lg bg-gray-800 border border-gray-700 flex items-center gap-2 text-xs shrink-0">
              <span class="animate-spin text-indigo-400">&#9881;</span>
              <span class="font-mono text-gray-300" x-text="webSession.current_tool"></span>
              <span class="text-gray-600" x-text="'(' + elapsed(webSession.tool_started_at) + 's)'"></span>
              <span class="ml-auto px-1.5 py-0.5 rounded text-xs font-medium"
                :class="webSession.stuck ? 's-stuck' : elapsed(webSession.tool_started_at) > 180 ? 's-slow' : 's-run'"
                x-text="webSession.stuck ? '\\uD83D\\uDD34 stuck' : elapsed(webSession.tool_started_at) > 180 ? '\\u26A0\\uFE0F slow' : '\\u2705 running'">
              </span>
            </div>
          </template>

          <!-- Stats row -->
          <div class="px-4 py-2 border-t border-gray-800 flex items-center gap-3 text-xs text-gray-600 shrink-0 flex-wrap">
            <template x-if="webSession">
              <span class="flex items-center gap-1">
                <span>⏱</span>
                <span class="text-gray-400 font-mono" x-text="fmtUptime(webSession.elapsed_session_s || 0)"></span>
              </span>
            </template>
            <template x-if="webSession && (webSession.prompt_tokens || 0) > 0">
              <span class="flex items-center gap-1">
                <span class="text-gray-600">in</span>
                <span class="text-blue-400 font-mono" x-text="fmtTokens(webSession.prompt_tokens)"></span>
              </span>
            </template>
            <template x-if="webSession && (webSession.completion_tokens || 0) > 0">
              <span class="flex items-center gap-1">
                <span class="text-gray-600">out</span>
                <span class="text-green-400 font-mono" x-text="fmtTokens(webSession.completion_tokens)"></span>
              </span>
            </template>
            <template x-if="webSession && (webSession.api_calls || 0) > 1">
              <span class="text-gray-700" x-text="(webSession.api_calls) + ' calls'"></span>
            </template>
            <template x-if="webSession && (webSession.tool_count || 0) > 0">
              <span class="text-gray-700" x-text="(webSession.tool_count) + ' tools'"></span>
            </template>
            <template x-if="webSession && (webSession.error_count || 0) > 0">
              <span class="text-red-500"><span x-text="webSession.error_count"></span> errors</span>
            </template>
            <span class="font-mono text-gray-700" x-text="'\\u2191 ' + fmtUptime(status.uptime_s)"></span>
            <span class="text-gray-800">·</span>
            <template x-if="(status.active_sessions||[]).length > 0">
              <span class="text-green-500 font-medium"
                x-text="(status.active_sessions||[]).length + ' executing'"></span>
            </template>
            <template x-if="(status.active_sessions||[]).length === 0">
              <span class="text-gray-700">idle</span>
            </template>
            <span class="ml-auto text-gray-700" x-text="lastRefresh ? 'updated ' + lastRefresh : ''"></span>
          </div>

          <!-- Input -->
          <div class="px-4 py-3 border-t border-gray-800 flex gap-2 shrink-0 items-center">
            <!-- Render mode drop-up -->
            <div class="render-dropup" x-data="{open:false}" @click.away="open=false">
              <button @click="open=!open"
                class="flex items-center gap-1 px-2.5 py-2 rounded-lg border text-xs font-medium transition-colors"
                :class="open
                  ? 'border-[var(--accent)] bg-[var(--accent-bg)] text-[var(--accent-light)]'
                  : 'border-gray-700 bg-gray-800 text-gray-500 hover:text-gray-300 hover:border-gray-600'"
                :title="'Render: ' + chatRenderMode">
                <span x-text="chatRenderMode==='markdown'?'MD':chatRenderMode==='plain'?'TXT':'01'"></span>
                <svg class="w-2.5 h-2.5 transition-transform" :class="open?'rotate-180':''" viewBox="0 0 10 6" fill="none">
                  <path d="M1 5l4-4 4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
              </button>
              <div class="render-menu" :class="open?'open':''">
                <button @click="chatRenderMode='markdown'; open=false"
                  :class="chatRenderMode==='markdown'?'active':''">MD — Markdown</button>
                <button @click="chatRenderMode='plain'; open=false"
                  :class="chatRenderMode==='plain'?'active':''">TXT — Plain</button>
                <button @click="chatRenderMode='mono'; open=false"
                  :class="chatRenderMode==='mono'?'active':''">01 — Mono</button>
              </div>
            </div>
            <!-- Text input -->
            <div class="relative flex-1">
              <input x-model="chatInput" @keydown.enter="sendChat()"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500 transition-colors pr-2"
                :placeholder="micRecording ? ('Recording… ' + micCountdown + 's') : micTranscribing ? 'Transcribing…' : ('Message ' + (status.instance_name || 'Hermes') + '\u2026 (Enter to send)')"
                :disabled="micTranscribing || activeInstance.k8s_status === 'starting'">
              <!-- Success flash inside input -->
              <template x-if="micSuccess">
                <span class="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-green-400 pointer-events-none">✓ captured</span>
              </template>
            </div>
            <!-- Mic button — between input and Send -->
            <button @click="toggleMic()" :disabled="micTranscribing"
              :title="micRecording ? 'Stop recording (or wait for 90s limit)' : 'Voice input — up to 90 seconds'"
              class="px-2.5 py-2 rounded-lg border text-sm transition-colors disabled:opacity-40"
              :class="micRecording
                ? 'mic-recording border-red-700 bg-red-900 text-red-300'
                : micTranscribing
                  ? 'border-yellow-700 bg-yellow-900 text-yellow-300 animate-pulse'
                  : 'border-gray-700 bg-gray-800 text-gray-500 hover:text-gray-300 hover:border-gray-600'">
              <template x-if="micTranscribing">
                <svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-dasharray="31.4" stroke-dashoffset="10"/>
                </svg>
              </template>
              <template x-if="!micTranscribing">
                <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <rect x="9" y="2" width="6" height="12" rx="3"/>
                  <path d="M5 10a7 7 0 0014 0M12 19v3M8 22h8"/>
                </svg>
              </template>
            </button>
            <button @click="sendChat()" :disabled="activeInstance.k8s_status === 'starting'"
              class="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 px-4 py-2 rounded-lg text-sm font-medium transition-colors">
              Send
            </button>
          </div>
        </div>
      </div>

      <!-- Right panel: Live Executions -->
      <div class="w-72 shrink-0 flex flex-col h-full items-center">
        <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 shrink-0 w-full text-center">Live Executions</div>
        <div class="exec-scroll space-y-3 w-full">
          <template x-if="(status.active_sessions||[]).length === 0">
            <div class="text-center text-gray-700 text-xs pt-8">No active sessions</div>
          </template>
          <template x-for="s in (status.active_sessions||[])" :key="s.session_key">
            <div class="bg-gray-900 border rounded-lg p-3"
                 :class="s.stuck ? 'border-red-800' : s.session_key && s.session_key.includes(activeChatId||'__none__') ? 'border-indigo-700' : 'border-gray-800'">
              <div class="flex items-center gap-2 mb-2">
                <span x-text="platformIcon(s.platform)"></span>
                <span class="text-xs font-medium text-gray-300 capitalize truncate flex-1" x-text="s.platform"></span>
                <span class="shrink-0 text-xs px-1.5 py-0.5 rounded font-medium"
                  :class="s.stuck ? 's-stuck' : elapsed(s.tool_started_at) > 180 ? 's-slow' : 's-run'"
                  x-text="s.stuck ? '\\uD83D\\uDD34' : elapsed(s.tool_started_at) > 180 ? '\\u26A0\\uFE0F' : '\\u2705'">
                </span>
              </div>
              <div class="font-mono text-indigo-400 text-xs truncate mb-1" x-text="s.current_tool"></div>
              <div class="flex items-center gap-2 text-xs text-gray-600">
                <span x-text="elapsed(s.tool_started_at) + 's'"></span>
                <span>·</span>
                <span>tool #<span x-text="s.tool_count"></span></span>
                <template x-if="s.error_count > 0">
                  <span class="text-red-600 ml-1"><span x-text="s.error_count"></span> err</span>
                </template>
              </div>
              <template x-if="s.recent_tools && s.recent_tools.length > 1">
                <div class="mt-1.5 text-xs text-gray-700 font-mono truncate"
                  x-text="s.recent_tools.slice(-3).join(' \\u2192 ')"></div>
              </template>
            </div>
          </template>

          <!-- Completed sessions — linger for 5s with green highlight then fade out -->
          <template x-for="s in completedSessionsList()" :key="s.session_key">
            <div class="bg-gray-900 border rounded-lg p-3 transition-opacity"
                 :class="s.fading ? 'session-fading' : ''"
                 style="border-color:#166534">
              <div class="flex items-center gap-2 mb-2">
                <span x-text="platformIcon(s.platform)"></span>
                <span class="text-xs font-medium text-gray-400 capitalize truncate flex-1" x-text="s.platform"></span>
                <span class="shrink-0 text-xs px-1.5 py-0.5 rounded font-medium s-done">done</span>
              </div>
              <div class="font-mono text-green-700 text-xs truncate mb-1" x-text="s.current_tool"></div>
              <div class="text-xs text-gray-700"
                x-text="s.tool_count + ' tools · ' + s.elapsed_session_s + 's total'"></div>
            </div>
          </template>
        </div>

        <!-- Recent Activity — last N completed sessions across all platforms -->
        <template x-if="(status.recent_sessions||[]).length > 0">
          <div class="mt-4 shrink-0" style="max-height:14rem;display:flex;flex-direction:column">
            <div class="flex items-center justify-between mb-2 shrink-0">
              <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider">Recent Activity</div>
              <button @click="activityModalOpen=true"
                class="text-xs text-gray-600 hover:text-[var(--accent)] transition-colors">View all</button>
            </div>
            <!-- Fade-masked scroll area -->
            <div class="relative flex-1 min-h-0" style="min-height:4rem">
              <div class="activity-scroll space-y-2 h-full" style="max-height:12rem">
                <template x-for="r in [...(status.recent_sessions||[])].reverse().slice(0,8)" :key="r.session_key + r.ended_at">
                  <div class="bg-gray-900 border border-gray-800 rounded-lg p-2.5 cursor-pointer hover:border-gray-700 transition-colors"
                    @click="navigateToSession(r)">
                    <div class="flex items-center gap-1.5 mb-1">
                      <span x-text="platformIcon(r.platform)" class="text-xs"></span>
                      <span class="text-xs text-gray-500 capitalize" x-text="r.platform"></span>
                      <span class="text-gray-800 text-xs">·</span>
                      <span class="text-xs text-gray-600" x-text="fmtAgo(r.ended_at)"></span>
                    </div>
                    <div class="text-xs text-gray-400 leading-relaxed line-clamp-2" x-text="r.snippet || '(no response)'"></div>
                  </div>
                </template>
              </div>
              <!-- Gradient fade at bottom — only visible when content overflows -->
              <div class="pointer-events-none absolute bottom-0 left-0 right-0 h-8"
                style="background:linear-gradient(to bottom,transparent,var(--surface-col,#111827))"></div>
            </div>
          </div>
        </template>
      </div>

    </div>
  </div>

  <!-- ── Routing Tab ───────────────────────────────────────────────── -->
  <div x-show="tab==='routing'" x-cloak>

    <!-- ── Machines section ── -->
    <div x-show="can('manage_machines')" class="mb-8">

      <!-- Setup wizard banner — shown when all machines are seeded examples -->
      <template x-if="isExampleSetup() && !setupWizardDismissed">
        <div class="mb-5 rounded-xl border border-indigo-800 bg-indigo-950 p-5">
          <div class="flex items-start justify-between mb-1">
            <div class="text-sm font-semibold text-white">Quick Setup</div>
            <button @click="setupWizardDismissed=true; localStorage.setItem('hermes_wizard_dismissed','1')"
              class="text-gray-600 hover:text-gray-400 text-xs ml-4">✕ dismiss</button>
          </div>
          <p class="text-xs text-indigo-300 mb-4 leading-relaxed">
            Your deployment is running with example placeholder machines.
            Choose a setup to replace them with real configuration, or dismiss to edit manually.
          </p>
          <template x-if="!setupWizardStep">
            <div class="flex flex-wrap gap-2">
              <button @click="setupWizardStep='single'"
                class="px-3 py-2 rounded-lg border border-indigo-700 bg-indigo-900 hover:bg-indigo-800 text-xs text-white transition-colors">
                <div class="font-medium mb-0.5">Single Machine</div>
                <div class="text-indigo-400">One node, all capabilities</div>
              </button>
              <button @click="setupWizardStep='multi'"
                class="px-3 py-2 rounded-lg border border-indigo-700 bg-indigo-900 hover:bg-indigo-800 text-xs text-white transition-colors">
                <div class="font-medium mb-0.5">Multi-Machine</div>
                <div class="text-indigo-400">High perf + secondary node</div>
              </button>
              <button @click="setupWizardDismissed=true; localStorage.setItem('hermes_wizard_dismissed','1')"
                class="px-3 py-2 rounded-lg border border-gray-700 bg-gray-800 hover:bg-gray-700 text-xs text-gray-400 transition-colors">
                Skip — manual setup
              </button>
            </div>
          </template>

          <!-- Single machine: ask for endpoint -->
          <template x-if="setupWizardStep==='single'">
            <div>
              <div class="text-xs text-gray-400 mb-2">Endpoint URL for your local node:</div>
              <div class="flex gap-2">
                <input x-model="setupWizardEndpoint" type="text" placeholder="http://192.168.1.x:1234/v1"
                  class="flex-1 bg-indigo-900 border border-indigo-700 rounded-lg px-3 py-1.5 text-sm text-white placeholder-indigo-700 focus:border-indigo-400 focus:outline-none">
                <button @click="applySetupWizard('single')"
                  :disabled="setupWizardLoading"
                  class="px-4 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-medium transition-colors">
                  <span x-show="setupWizardLoading">Setting up…</span>
                  <span x-show="!setupWizardLoading">Apply</span>
                </button>
                <button @click="setupWizardStep=null" class="text-xs text-gray-600 hover:text-gray-400">← back</button>
              </div>
            </div>
          </template>

          <!-- Multi-machine: confirm -->
          <template x-if="setupWizardStep==='multi'">
            <div>
              <p class="text-xs text-indigo-300 mb-3">
                Creates <span class="text-white font-medium">High Performance Node</span> (<span class="font-mono text-indigo-200">localhost:1234</span>)
                and <span class="text-white font-medium">Secondary Node</span> (<span class="font-mono text-indigo-200">localhost:8080</span>).
                Edit endpoints afterward.
              </p>
              <div class="flex gap-2">
                <button @click="applySetupWizard('multi')"
                  :disabled="setupWizardLoading"
                  class="px-4 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-medium transition-colors">
                  <span x-show="setupWizardLoading">Setting up…</span>
                  <span x-show="!setupWizardLoading">Apply</span>
                </button>
                <button @click="setupWizardStep=null" class="text-xs text-gray-600 hover:text-gray-400">← back</button>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- Header -->
      <div class="flex items-center justify-between mb-4">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Machines</div>
        <button @click="adminMachineForm={name:'',endpoint_url:'',description:''}; adminMachineFormOpen=!adminMachineFormOpen"
          class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity">+ Register</button>
      </div>

      <!-- Register form -->
      <template x-if="adminMachineFormOpen">
        <div class="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-4">
          <div class="text-xs font-semibold text-gray-400 mb-3">Register Machine</div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div>
              <label class="text-xs text-gray-500 block mb-1">Name</label>
              <input x-model="adminMachineForm.name" type="text" placeholder="e.g. High Performance Node"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div>
              <label class="text-xs text-gray-500 block mb-1">Endpoint URL</label>
              <input x-model="adminMachineForm.endpoint_url" type="text" placeholder="http://192.168.1.x:1234/v1"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div class="col-span-2">
              <label class="text-xs text-gray-500 block mb-1">Description (optional)</label>
              <input x-model="adminMachineForm.description" type="text" placeholder="RTX 4080 SUPER, 16GB VRAM"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
          </div>
          <div class="flex gap-2 items-center">
            <button @click="createMachine()"
              class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Register</button>
            <button @click="adminMachineFormOpen=false"
              class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
          </div>
        </div>
      </template>

      <!-- Message bar -->
      <template x-if="adminMsg">
        <div class="mb-3 text-xs px-3 py-2 rounded-lg"
          :class="adminMsg.ok ? 'bg-green-950 text-green-300 border border-green-800' : 'bg-red-950 text-red-300 border border-red-800'"
          x-text="adminMsg.text"></div>
      </template>

      <!-- Machine cards -->
      <div class="grid grid-cols-1 gap-3" x-ref="machineList">
        <template x-for="m in adminMachines" :key="m.id">
          <div class="bg-gray-900 border rounded-xl overflow-hidden transition-colors"
            :data-machine-id="m.id"
            :class="adminMachineEditId===m.id ? 'border-[var(--accent)]' : 'border-gray-800'">

            <!-- Card header row -->
            <div class="flex items-center gap-3 px-4 py-3">
              <!-- Drag handle -->
              <div class="machine-drag-handle cursor-grab active:cursor-grabbing text-gray-700 hover:text-gray-400 shrink-0 select-none" title="Drag to reorder">⠿</div>
              <!-- Status dot: reflects enabled + cached health -->
              <div class="w-2 h-2 rounded-full shrink-0 transition-colors"
                :class="!m.enabled                                              ? 'bg-gray-700'
                       : m._probing                                             ? 'bg-gray-500 animate-pulse'
                       : !m._health                                             ? 'bg-gray-600'
                       : m._health.status==='ok' && (_now - m._health_at) <= 60 ? 'bg-green-500'
                       : m._health.status==='ok'                                ? 'bg-yellow-500'
                       : 'bg-red-500'"></div>

              <!-- Name + meta -->
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="font-medium text-white text-sm" x-text="m.name"></span>
                  <span class="text-xs px-1.5 py-0.5 rounded"
                    :class="m.enabled ? 'bg-green-950 text-green-500' : 'bg-gray-800 text-gray-500'"
                    x-text="m.enabled ? 'enabled' : 'disabled'"></span>

                  <!-- Health badge: green / amber (stale) / red -->
                  <template x-if="m._health && !m._probing">
                    <span class="text-xs px-1.5 py-0.5 rounded border font-mono"
                      :class="m._health.status!=='ok'
                        ? 'border-red-900 bg-red-950 text-red-400'
                        : (_now - m._health_at) > 60
                        ? 'border-yellow-900 bg-yellow-950 text-yellow-500'
                        : 'border-green-800 bg-green-950 text-green-400'"
                      x-text="m._health.status==='ok'
                        ? ((_now - m._health_at) > 60 ? 'zzz' : 'up')
                        : (m._health.http ? 'HTTP '+m._health.http : 'down')"></span>
                  </template>
                  <template x-if="m._probing">
                    <span class="text-xs text-gray-600 animate-pulse font-mono">probing…</span>
                  </template>

                  <!-- Profile usage count -->
                  <template x-if="m.profile_count > 0">
                    <span class="text-xs text-gray-600"
                      x-text="m.profile_count + '\u202fprofile' + (m.profile_count > 1 ? 's' : '')"></span>
                  </template>
                </div>

                <!-- Endpoint URL + last checked -->
                <div class="flex items-center gap-3 mt-0.5">
                  <span class="text-xs text-gray-600 font-mono truncate" x-text="m.endpoint_url"></span>
                  <span class="text-xs text-gray-700 shrink-0"
                    x-text="!m._health_at ? 'never checked'
                           : (_now - m._health_at) < 5  ? 'just now'
                           : (_now - m._health_at) < 60 ? ((_now - m._health_at) + 's ago')
                           : (Math.floor((_now - m._health_at)/60) + 'm ago')"></span>
                </div>

                <template x-if="m.description">
                  <div class="text-xs text-gray-700 mt-0.5" x-text="m.description"></div>
                </template>
              </div>

              <!-- Action buttons -->
              <div class="flex gap-1.5 items-center shrink-0">
                <button @click="probeHealth(m)" :disabled="m._probing"
                  class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-500 hover:text-white transition-colors disabled:opacity-40"
                  title="Send a health check to this machine's endpoint">
                  <span x-show="!m._probing">Ping</span>
                  <span x-show="m._probing" class="animate-pulse">…</span>
                </button>
                <button @click="adminMachineEditId===m.id ? cancelEditMachine() : startEditMachine(m)"
                  class="text-xs px-2 py-1 rounded border transition-colors"
                  :class="adminMachineEditId===m.id
                    ? 'border-gray-600 text-gray-400 hover:text-white'
                    : 'border-gray-700 text-gray-500 hover:text-white'">
                  <span x-text="adminMachineEditId===m.id ? 'Cancel' : 'Edit'"></span>
                </button>
                <button @click="toggleMachine(m)"
                  class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-500 hover:text-white transition-colors"
                  x-text="m.enabled ? 'Disable' : 'Enable'"></button>
                <button @click="deleteMachine(m.id)"
                  class="text-xs px-2 py-1 rounded border transition-colors"
                  :class="m.profile_count > 0
                    ? 'border-yellow-900 text-yellow-600 hover:text-yellow-400'
                    : 'border-red-900 text-red-600 hover:text-red-400'">Delete</button>
              </div>
            </div>

            <!-- Inline edit form -->
            <template x-if="adminMachineEditId===m.id">
              <div class="border-t border-gray-800 px-4 py-3 bg-gray-950">
                <div class="grid grid-cols-2 gap-3 mb-3">
                  <div>
                    <label class="text-xs text-gray-500 block mb-1">Name</label>
                    <input x-model="adminMachineEditForm.name" type="text"
                      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                  </div>
                  <div>
                    <label class="text-xs text-gray-500 block mb-1">Endpoint URL</label>
                    <input x-model="adminMachineEditForm.endpoint_url" type="text"
                      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                  </div>
                  <div class="col-span-2">
                    <label class="text-xs text-gray-500 block mb-1">Description</label>
                    <input x-model="adminMachineEditForm.description" type="text" placeholder="optional"
                      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                  </div>
                </div>
                <div class="flex gap-2">
                  <button @click="saveEditMachine(m.id)"
                    class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Save</button>
                  <button @click="cancelEditMachine()"
                    class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
                </div>
              </div>
            </template>

            <!-- Capabilities row -->
            <div class="border-t border-gray-800/60 px-4 py-2.5 flex items-center gap-4">
              <span class="text-xs text-gray-700 shrink-0">Capabilities</span>
              <div class="flex flex-wrap gap-2">
                <template x-for="cls in ['lightweight','coding','general','reasoning','vision','embedding']" :key="cls">
                  <label class="flex items-center gap-1 text-xs cursor-pointer select-none">
                    <input type="checkbox"
                      :checked="m.capabilities.includes(cls)"
                      @change="toggleCapability(m, cls, $event.target.checked)"
                      class="rounded border-gray-700 bg-gray-800 accent-[var(--accent)]">
                    <span :class="m.capabilities.includes(cls) ? 'text-gray-300' : 'text-gray-600'"
                      x-text="cls"></span>
                  </label>
                </template>
              </div>
            </div>

          </div>
        </template>
        <template x-if="adminMachines.length===0">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-xs text-gray-600">No machines registered. Click Register to add one.</div>
        </template>
      </div>
    </div>

    <!-- ── Profiles section ── -->
    <div x-show="can('manage_profiles')" class="mb-8">
      <div class="border-t border-gray-800 mb-6 pt-6">
        <div class="flex items-center justify-between mb-4">
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Routing Profiles</div>
        <button @click="adminPolicyForm={name:'',description:'',fallback:'any_available'}; adminPolicyFormOpen=true"
          class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity">+ New Profile</button>
      </div>
      <template x-if="adminPolicyFormOpen">
        <div class="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-4">
          <div class="text-xs font-semibold text-gray-400 mb-3">Create Profile</div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div>
              <label class="text-xs text-gray-500 block mb-1">Name</label>
              <input x-model="adminPolicyForm.name" type="text" placeholder="e.g. bere-home"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div>
              <label class="text-xs text-gray-500 block mb-1">Fallback</label>
              <select x-model="adminPolicyForm.fallback"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                <option value="any_available">any_available — use best available machine</option>
                <option value="fail">fail — error if no matching machine is up</option>
              </select>
            </div>
            <div class="col-span-2">
              <label class="text-xs text-gray-500 block mb-1">Description (optional)</label>
              <input x-model="adminPolicyForm.description" type="text" placeholder="Route lightweight to Bere's PC, fallback to server"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
          </div>
          <div class="flex gap-2">
            <button @click="createPolicy()"
              class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Create</button>
            <button @click="adminPolicyFormOpen=false"
              class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
          </div>
        </div>
      </template>
      <div class="grid grid-cols-1 gap-4">
        <template x-for="p in adminPolicies" :key="p.id">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">

            <!-- View mode header -->
            <div x-show="adminPolicyEditId !== p.id">
              <div class="flex items-start justify-between">
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-medium text-white text-sm" x-text="p.name"></span>
                    <span class="text-xs px-1.5 py-0.5 rounded border"
                      :class="p.fallback==='fail' ? 'border-red-900 bg-red-950 text-red-400' : 'border-gray-700 bg-gray-800 text-gray-500'"
                      x-text="p.fallback==='fail' ? 'fail on no match' : 'fallback: any'"></span>
                    <span class="text-xs px-1.5 py-0.5 rounded border font-medium"
                      :class="p.user_count===0 ? 'border-amber-800 bg-amber-950 text-amber-400' : 'border-gray-700 bg-gray-800 text-gray-400'"
                      x-text="p.user_count===0 ? '0 users assigned' : p.user_count+' user'+(p.user_count!==1?'s':'')"></span>
                  </div>
                  <div class="text-xs text-gray-500 mt-1" x-show="p.description" x-text="p.description"></div>
                </div>
                <div class="flex gap-1.5 ml-3 shrink-0">
                  <button @click="startEditPolicy(p)"
                    class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors">Edit</button>
                  <button @click="deletePolicy(p.id)"
                    :class="p.user_count>0 ? 'border-orange-900 text-orange-500 hover:text-orange-300' : 'border-red-900 text-red-500 hover:text-red-300'"
                    class="text-xs px-2 py-1 rounded border transition-colors"
                    :title="p.user_count>0 ? 'Warning: '+p.user_count+' user(s) will lose routing override' : 'Delete profile'">Delete</button>
                </div>
              </div>
            </div>

            <!-- Inline metadata edit mode -->
            <div x-show="adminPolicyEditId === p.id" class="space-y-2">
              <div class="grid grid-cols-2 gap-2">
                <div>
                  <label class="text-xs text-gray-500 block mb-1">Name</label>
                  <input x-model="adminPolicyEditForm.name" type="text"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                </div>
                <div>
                  <label class="text-xs text-gray-500 block mb-1">Fallback</label>
                  <select x-model="adminPolicyEditForm.fallback"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                    <option value="any_available">any_available</option>
                    <option value="fail">fail</option>
                  </select>
                </div>
                <div class="col-span-2">
                  <label class="text-xs text-gray-500 block mb-1">Description</label>
                  <input x-model="adminPolicyEditForm.description" type="text"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                </div>
              </div>
              <div class="flex gap-2">
                <button @click="saveEditPolicy()"
                  class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Save</button>
                <button @click="cancelEditPolicy()"
                  class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
              </div>
            </div>

            <!-- Rules editor -->
            <div class="border-t border-gray-800 mt-3 pt-3">
              <div class="text-xs text-gray-600 mb-2">Rules — rank order; exact class matched first, then * wildcard</div>
              <div class="space-y-1.5">
                <template x-for="(rule, idx) in p.rules" :key="idx">
                  <div class="flex items-center gap-1.5">
                    <span class="text-xs text-gray-700 w-5 text-right shrink-0" x-text="(idx+1)+'.'"></span>
                    <select :value="rule.model_class" @change="rule.model_class=$event.target.value"
                      class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:border-[var(--accent)] focus:outline-none w-32 shrink-0">
                      <option value="*">* (any)</option>
                      <option value="lightweight">lightweight</option>
                      <option value="coding">coding</option>
                      <option value="general">general</option>
                      <option value="reasoning">reasoning</option>
                      <option value="vision">vision</option>
                      <option value="embedding">embedding</option>
                    </select>
                    <span class="text-gray-700 text-xs shrink-0">→</span>
                    <select :value="rule.machine_id" @change="rule.machine_id=$event.target.value"
                      class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:border-[var(--accent)] focus:outline-none flex-1 min-w-0">
                      <template x-for="m in adminMachines" :key="m.id">
                        <option :value="m.id" x-text="m.name + (m.enabled ? '' : ' (disabled)')"></option>
                      </template>
                    </select>
                    <div class="flex gap-0.5 shrink-0">
                      <button @click="movePolicyRule(p, idx, -1)" :disabled="idx===0"
                        class="text-xs w-5 h-5 rounded flex items-center justify-center text-gray-500 hover:text-white bg-gray-800 hover:bg-gray-700 disabled:opacity-25 disabled:cursor-not-allowed">↑</button>
                      <button @click="movePolicyRule(p, idx, 1)" :disabled="idx===p.rules.length-1"
                        class="text-xs w-5 h-5 rounded flex items-center justify-center text-gray-500 hover:text-white bg-gray-800 hover:bg-gray-700 disabled:opacity-25 disabled:cursor-not-allowed">↓</button>
                    </div>
                    <button @click="p.rules.splice(idx,1)"
                      class="text-xs text-red-700 hover:text-red-400 px-1 shrink-0">✕</button>
                  </div>
                </template>
                <template x-if="p.rules.length===0">
                  <div class="text-xs text-gray-700 py-1 italic">No rules — all requests fall through to fallback.</div>
                </template>
              </div>
              <div class="flex items-center justify-between mt-3">
                <button @click="p.rules.push({model_class:'*',machine_id:adminMachines[0]?.id||''})"
                  class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors">+ Add Rule</button>
                <button @click="savePolicyRules(p)"
                  class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Save Rules</button>
              </div>
            </div>

          </div>
        </template>
        <template x-if="adminPolicies.length===0">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-xs text-gray-600">No profiles yet. Create one to control per-user routing.</div>
        </template>
      </div>
      </div><!-- /border-t wrapper -->
    </div><!-- /profiles section -->

    <!-- ── Model Map section (collapsible) ── -->
    <div class="mb-6 border-t border-gray-800 pt-6">
      <button @click="modelMapOpen=!modelMapOpen"
        class="flex items-center gap-2 w-full text-left mb-4 group">
        <span class="text-xs text-gray-600 transition-transform" :class="modelMapOpen ? 'rotate-90' : ''" style="display:inline-block">▶</span>
        <span class="text-xs font-semibold text-gray-400 uppercase tracking-wider group-hover:text-gray-300">Model Map</span>
      </button>
    <div x-show="modelMapOpen" x-cloak>
      <!-- Explanatory header -->
      <div class="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 mb-5">
        <div class="text-sm font-semibold text-white mb-1">Alias → Model Class Mapping</div>
        <p class="text-xs text-gray-500 leading-relaxed">
          When a request arrives, Hermes resolves the <span class="text-gray-300 font-medium">alias</span> (e.g. <span class="font-mono text-[var(--accent)]">coding</span>) to a <span class="text-gray-300 font-medium">model class</span> (e.g. <span class="font-mono text-gray-300">coding</span>), then picks a machine based on the user's routing profile.
          The candidates shown below are the <span class="text-gray-300 font-medium">global defaults</span> — they apply when no profile overrides the class.
          This page is not a model inventory; it shows how aliases map to routing inputs.
        </p>
      </div>

      <!-- Grouped by class -->
      <template x-for="cls in ['lightweight','general','coding','reasoning']" :key="cls">
        <div x-show="Object.entries(routerState.routes||{}).some(([a]) => (routerState.route_model_classes||{})[a]===cls)" class="mb-5">
          <div class="flex items-center gap-2 mb-2">
            <span class="text-xs font-semibold uppercase tracking-widest"
              :class="{
                'text-sky-400':    cls==='lightweight',
                'text-emerald-400': cls==='general',
                'text-violet-400': cls==='coding',
                'text-orange-400': cls==='reasoning'
              }" x-text="cls"></span>
            <div class="flex-1 h-px bg-gray-800"></div>
          </div>
          <div class="space-y-1.5">
            <template x-for="[alias, candidates] in Object.entries(routerState.routes||{}).filter(([a]) => (routerState.route_model_classes||{})[a]===cls)" :key="alias">
              <div class="bg-gray-900 rounded-lg px-4 py-2.5 border border-gray-800 flex items-center gap-3 flex-wrap">
                <!-- Alias -->
                <span class="font-mono text-[var(--accent)] text-sm w-20 shrink-0" x-text="alias"></span>
                <!-- Class badge -->
                <span class="text-xs px-1.5 py-0.5 rounded border shrink-0"
                  :class="{
                    'border-sky-900 bg-sky-950 text-sky-400':       cls==='lightweight',
                    'border-emerald-900 bg-emerald-950 text-emerald-400': cls==='general',
                    'border-violet-900 bg-violet-950 text-violet-400': cls==='coding',
                    'border-orange-900 bg-orange-950 text-orange-400': cls==='reasoning'
                  }" x-text="cls"></span>
                <!-- Separator -->
                <span class="text-gray-700 text-xs shrink-0">→</span>
                <!-- Global default candidates -->
                <div class="flex flex-wrap gap-1.5">
                  <template x-for="(c,i) in candidates" :key="i">
                    <span class="text-xs px-2 py-0.5 rounded font-mono flex items-center gap-1"
                      :class="routerState.providers?.[c.provider] && !routerState.providers[c.provider].enabled
                        ? 'bg-gray-800 text-gray-600 line-through opacity-50'
                        : i===0 ? 'bg-gray-800 text-gray-300' : 'bg-gray-900 text-gray-600 border border-gray-800'">
                      <span x-show="i===0" class="text-gray-600 text-[10px]">default</span>
                      <span x-show="i>0" class="text-gray-700 text-[10px]">fallback</span>
                      <span x-text="c.model"></span>
                    </span>
                  </template>
                </div>
              </div>
            </template>
          </div>
        </div>
      </template>
      <p class="text-xs text-gray-700 mt-2">To override routing per user, create a Profile in the <button class="underline hover:text-gray-500" @click="modelMapOpen=false">Profiles section above</button> and assign it in <button class="underline hover:text-gray-500" @click="tab='admin'; adminTab='users'">Admin → Users</button>.</p>

      <!-- AI Router endpoint health -->
      <div class="mt-6">
        <div class="flex items-center justify-between mb-3">
          <div class="text-xs font-semibold text-gray-600 uppercase tracking-wider">AI Router Endpoint Health</div>
          <button @click="loadRouterState()"
            class="text-xs text-[var(--accent)] hover:opacity-80 flex items-center gap-1">
            <span>&#8635;</span><span>Refresh</span>
          </button>
        </div>
        <div class="space-y-1.5">
          <template x-for="(p, key) in routerState.providers" :key="key">
            <div class="bg-gray-900 rounded-lg px-4 py-3 border border-gray-800 flex items-center gap-3">
              <div class="w-2 h-2 rounded-full shrink-0"
                :class="p.health==='ok' ? 'bg-green-500' : p.health==='disabled' ? 'bg-gray-600' : 'bg-red-500'"></div>
              <span class="text-sm text-white" x-text="p.name"></span>
              <span class="text-xs text-gray-600 font-mono" x-text="p.endpoint"></span>
              <span class="text-xs text-gray-700 ml-auto" x-text="(p.context_limit/1000).toFixed(0) + 'k ctx'"></span>
              <button
                class="text-xs px-2.5 py-1 rounded-md font-medium transition-colors border"
                :class="p.enabled ? 'bg-[var(--accent)] border-[var(--accent)] text-white hover:opacity-80' : 'bg-gray-800 border-gray-700 text-gray-400 hover:bg-gray-700'"
                @click="toggleProvider(key)"
                x-text="p.enabled ? 'Enabled' : 'Disabled'">
              </button>
            </div>
          </template>
        </div>
        <p class="text-xs text-gray-700 mt-2">Toggles reset on router restart — edit the providers ConfigMap to persist.</p>
      </div>
    </div><!-- /modelMapOpen -->
    </div><!-- /model map wrapper -->

    <!-- ── Benchmark section (collapsible) ── -->
    <div x-show="can('manage_machines')" class="mb-6 border-t border-gray-800 pt-6">
      <button @click="benchmarkOpen=!benchmarkOpen"
        class="flex items-center gap-2 w-full text-left mb-4 group">
        <span class="text-xs text-gray-600 transition-transform" :class="benchmarkOpen ? 'rotate-90' : ''" style="display:inline-block">▶</span>
        <span class="text-xs font-semibold text-gray-400 uppercase tracking-wider group-hover:text-gray-300">Benchmark</span>
        <span class="text-xs text-gray-700">— measure tok/s and latency per provider</span>
      </button>
      <div x-show="benchmarkOpen" x-cloak>
        <!-- Models Live drift check -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-4">
          <div class="flex items-center justify-between mb-3">
            <div>
              <div class="text-xs font-semibold text-gray-300 mb-0.5">Model Drift Check</div>
              <div class="text-xs text-gray-600">Compare what's configured in providers.yaml against what's currently loaded.</div>
            </div>
            <button @click="loadModelsLive()" :disabled="modelsLiveLoading"
              class="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors disabled:opacity-40 shrink-0 ml-4">
              <span x-show="!modelsLiveLoading">Check Live Models</span>
              <span x-show="modelsLiveLoading" class="animate-pulse">Checking…</span>
            </button>
          </div>
          <template x-if="modelsLiveResult && !modelsLiveResult._error">
            <div class="space-y-3">
              <template x-for="(info, pk) in modelsLiveResult.providers" :key="pk">
                <div class="rounded-lg border px-3 py-2"
                  :class="info.missing && info.missing.length ? 'border-yellow-900 bg-yellow-950/30' : 'border-gray-800'">
                  <div class="flex items-center gap-2 mb-1">
                    <div class="w-2 h-2 rounded-full shrink-0"
                      :class="info.status==='ok' ? 'bg-green-500' : 'bg-red-500'"></div>
                    <span class="text-xs font-medium text-gray-300" x-text="pk"></span>
                    <span class="text-xs text-gray-600" x-text="info.status==='ok' ? info.live.length + ' loaded' : info.status"></span>
                    <template x-if="info.missing && info.missing.length">
                      <span class="text-xs px-1.5 py-0.5 rounded bg-yellow-900 text-yellow-400"
                        x-text="info.missing.length + ' missing'"></span>
                    </template>
                    <template x-if="info.extra && info.extra.length">
                      <span class="text-xs px-1.5 py-0.5 rounded bg-sky-900 text-sky-400"
                        x-text="info.extra.length + ' extra'"></span>
                    </template>
                  </div>
                  <template x-if="info.missing && info.missing.length">
                    <div class="text-xs text-yellow-600 mt-1">
                      <span class="text-yellow-700">Missing: </span>
                      <span x-text="info.missing.join(', ')"></span>
                    </div>
                  </template>
                  <template x-if="info.extra && info.extra.length">
                    <div class="text-xs text-sky-700 mt-1">
                      <span class="text-sky-800">Extra: </span>
                      <span x-text="info.extra.join(', ')"></span>
                    </div>
                  </template>
                </div>
              </template>
            </div>
          </template>
          <template x-if="modelsLiveResult?._error">
            <div class="text-xs text-red-400 mt-2" x-text="modelsLiveResult._error"></div>
          </template>
        </div>

        <!-- Synthetic benchmark -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
          <div class="flex items-center gap-4 mb-4 flex-wrap">
            <div>
              <div class="text-xs font-semibold text-gray-300 mb-0.5">Synthetic Benchmark</div>
              <div class="text-xs text-gray-600">Sends short prompts to each provider, measures latency and tok/s.</div>
            </div>
            <div class="flex items-center gap-2 ml-auto shrink-0">
              <label class="text-xs text-gray-500">Runs:</label>
              <select x-model.number="benchmarkNPrompts"
                class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:border-[var(--accent)] focus:outline-none">
                <option value="1">1</option>
                <option value="3">3</option>
                <option value="5">5</option>
                <option value="10">10</option>
              </select>
              <button @click="runBenchmark()" :disabled="benchmarkRunning"
                class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-40 flex items-center gap-1.5">
                <span x-show="benchmarkRunning" class="animate-spin inline-block">⚙</span>
                <span x-show="benchmarkRunning">Running…</span>
                <span x-show="!benchmarkRunning">Run Benchmark</span>
              </button>
            </div>
          </div>
          <template x-if="benchmarkResult && benchmarkResult.results">
            <div class="space-y-2">
              <div class="grid grid-cols-5 gap-2 text-xs text-gray-600 px-2 mb-1">
                <span class="col-span-2">Provider / Model</span>
                <span class="text-right">Avg</span>
                <span class="text-right">P50</span>
                <span class="text-right">Tok/s</span>
              </div>
              <template x-for="r in benchmarkResult.results" :key="r.provider+r.model">
                <div class="rounded-lg bg-gray-800 px-3 py-2 grid grid-cols-5 gap-2 items-center"
                  :class="r.n_err > 0 && r.n_ok === 0 ? 'opacity-50' : ''">
                  <div class="col-span-2 min-w-0">
                    <div class="text-xs text-gray-400" x-text="r.provider_name || r.provider"></div>
                    <div class="text-xs text-gray-600 font-mono truncate" x-text="r.model"></div>
                  </div>
                  <span class="text-xs font-mono text-right text-gray-300"
                    x-text="r.avg_s != null ? r.avg_s.toFixed(2)+'s' : '—'"></span>
                  <span class="text-xs font-mono text-right text-gray-500"
                    x-text="r.p50_s != null ? r.p50_s.toFixed(2)+'s' : '—'"></span>
                  <span class="text-xs font-mono text-right"
                    :class="r.avg_tok_s >= 30 ? 'text-green-400' : r.avg_tok_s >= 10 ? 'text-yellow-400' : 'text-gray-500'"
                    x-text="r.avg_tok_s != null ? r.avg_tok_s+' t/s' : (r.n_err>0 ? 'error' : '—')"></span>
                </div>
              </template>
              <div class="text-xs text-gray-700 mt-2"
                x-text="'Prompt: &quot;' + (benchmarkResult.test_prompt||'') + '&quot; · ' + (benchmarkResult.n_prompts||0) + ' runs per model'"></div>
            </div>
          </template>
          <template x-if="benchmarkResult?.error">
            <div class="text-xs text-red-400 mt-2" x-text="benchmarkResult.error"></div>
          </template>
        </div>
      </div><!-- /benchmarkOpen -->
    </div><!-- /benchmark section -->

    <!-- ── Debug section (collapsible) ── -->
    <div x-show="can('view_routing_debug')" class="mb-6 border-t border-gray-800 pt-6">
      <button @click="debugOpen=!debugOpen; if(debugOpen) loadAdminUsers()"
        class="flex items-center gap-2 w-full text-left mb-4 group">
        <span class="text-xs text-gray-600 transition-transform" :class="debugOpen ? 'rotate-90' : ''" style="display:inline-block">▶</span>
        <span class="text-xs font-semibold text-gray-400 uppercase tracking-wider group-hover:text-gray-300">Debug</span>
        <span class="text-xs text-gray-700">— simulate route resolution</span>
      </button>
    <div x-show="debugOpen" x-cloak>

      <!-- Query form -->
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-4">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Simulate Route</div>
        <div class="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label class="text-xs text-gray-500 block mb-1">User</label>
            <select x-model="routeDebugUserId"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
              <option value="">— you —</option>
              <template x-for="u in adminUsers" :key="u.id">
                <option :value="u.id" x-text="(u.display_name||u.username) + ' · ' + u.email"></option>
              </template>
            </select>
          </div>
          <div>
            <label class="text-xs text-gray-500 block mb-1">Model alias</label>
            <select x-model="routeDebugModel"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
              <template x-for="group in spawnAliasGroups()" :key="group.cls">
                <optgroup :label="group.label">
                  <template x-for="alias in group.aliases" :key="alias">
                    <option :value="alias" x-text="alias"></option>
                  </template>
                </optgroup>
              </template>
            </select>
          </div>
        </div>
        <button @click="resolveRouteDebug()"
          :disabled="routeDebugLoading"
          class="px-4 py-1.5 rounded-lg bg-[var(--accent)] text-white text-xs font-medium hover:opacity-90 disabled:opacity-40 flex items-center gap-2">
          <span x-show="routeDebugLoading" class="animate-spin inline-block">&#9881;</span>
          <span>Resolve</span>
        </button>
      </div>

      <!-- Result: routing failed (503 / profile fail mode) -->
      <template x-if="routeDebugResult && routeDebugResult.error && !routeDebugResult.result">
        <div class="space-y-3">
          <div class="bg-red-950 border border-red-900 rounded-xl px-5 py-4">
            <div class="text-xs font-semibold text-red-400 mb-1">No route found</div>
            <div class="text-red-300 text-sm"
              x-text="routeDebugResult.error === 'no_available_machine'
                ? 'All profile machines are unavailable and the profile is set to fail (no fallback).'
                : routeDebugResult.error === 'no_machines_registered'
                ? 'No machines are registered in the system.'
                : routeDebugResult.error"></div>
            <template x-if="routeDebugResult.profile">
              <div class="text-xs text-red-700 mt-1" x-text="'Profile: ' + routeDebugResult.profile"></div>
            </template>
          </div>
          <!-- Still show trace if present -->
          <template x-if="routeDebugResult.trace && routeDebugResult.trace.length">
            <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Trace</div>
              <div class="space-y-1.5">
                <template x-for="(t, i) in routeDebugResult.trace" :key="i">
                  <div class="flex items-start gap-3 text-xs">
                    <span class="text-gray-700 shrink-0 w-32 font-mono" x-text="t.layer"></span>
                    <span class="shrink-0 px-1.5 py-0.5 rounded font-medium"
                      :class="t.result==='match'?'bg-green-950 text-green-400':t.result==='skip'?'bg-gray-800 text-gray-600':'bg-yellow-950 text-yellow-500'"
                      x-text="t.result"></span>
                    <span class="text-gray-600" x-text="t.machine||t.profile_name||t.reason||''"></span>
                  </div>
                </template>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- Result: success -->
      <template x-if="routeDebugResult && routeDebugResult.result">
        <div class="space-y-3">

          <!-- Resolution banner -->
          <div class="rounded-xl border border-gray-700 bg-gray-900 px-5 py-4">
            <div class="flex items-start justify-between">
              <div>
                <div class="flex items-center gap-2 mb-1">
                  <span class="font-mono text-[var(--accent)] text-sm" x-text="routeDebugResult.input.model_alias"></span>
                  <span class="text-gray-700 text-xs">→</span>
                  <span class="text-xs px-1.5 py-0.5 rounded border border-gray-700 text-gray-400"
                    x-text="routeDebugResult.input.model_class"></span>
                  <span class="text-gray-700 text-xs">→</span>
                  <span class="font-semibold text-white text-sm" x-text="routeDebugResult.result.machine_name"></span>
                </div>
                <div class="text-xs text-gray-600 font-mono" x-text="routeDebugResult.result.endpoint_url"></div>
              </div>
              <span class="text-xs px-2 py-1 rounded-full bg-green-950 text-green-400 border border-green-900 shrink-0 ml-4">resolved</span>
            </div>
            <div class="mt-2 text-xs text-gray-600"
              x-text="'For: ' + routeDebugResult.input.user_name + (routeDebugResult.input.user_id !== routeDebugResult.input.user_id ? '' : '')"></div>
          </div>

          <!-- Layer timeline -->
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Decision Layers</div>
            <div class="space-y-0">
              <template x-for="(t, i) in routeDebugResult.trace" :key="i">
                <div class="flex items-start gap-3 py-2"
                  :class="i < routeDebugResult.trace.length - 1 ? 'border-b border-gray-800/50' : ''">
                  <!-- Layer dot -->
                  <div class="w-2 h-2 rounded-full mt-1 shrink-0"
                    :class="t.result==='match' ? 'bg-green-500' : t.result==='skip' ? 'bg-gray-700' : 'bg-yellow-600'"></div>
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 mb-0.5">
                      <!-- Human layer label -->
                      <span class="text-xs font-medium text-gray-300"
                        x-text="t.layer==='instance_override' ? 'Instance Override'
                               : t.layer==='user_profile'       ? 'Profile Rules'
                               : t.layer==='best_effort'        ? 'Best-Effort Fallback'
                               : t.layer"></span>
                      <span class="text-xs px-1.5 py-0.5 rounded font-medium"
                        :class="t.result==='match'     ? 'bg-green-950 text-green-400'
                               : t.result==='skip'      ? 'bg-gray-800 text-gray-600'
                               : t.result==='exhausted' ? 'bg-yellow-950 text-yellow-500'
                               :                          'bg-gray-800 text-gray-600'"
                        x-text="t.result==='match'     ? '✓ matched'
                               : t.result==='skip'      ? 'skipped'
                               : t.result==='exhausted' ? 'all failed'
                               : t.result"></span>
                    </div>
                    <!-- Layer detail -->
                    <div class="text-xs text-gray-600"
                      x-text="t.layer==='user_profile' && t.profile_name ? 'Profile: ' + t.profile_name + (t.rule_class ? '  ·  rule: ' + t.rule_class : '') + (t.machine ? '  →  ' + t.machine : '')
                             : t.layer==='user_profile' && t.reason       ? t.reason
                             : t.layer==='best_effort' && t.machine       ? t.machine
                             : t.reason || ''"></div>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <!-- Candidate machine checks (profile evaluation) -->
          <template x-if="routeDebugResult.fallback_chain && routeDebugResult.fallback_chain.length > 0">
            <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Candidate Machines</div>
              <div class="space-y-2">
                <template x-for="(c, i) in routeDebugResult.fallback_chain" :key="i">
                  <div class="flex items-center gap-3 text-xs rounded-lg px-3 py-2"
                    :class="c.selected ? 'bg-green-950/40 border border-green-900/50' : 'bg-gray-800/40'">
                    <!-- Rank -->
                    <span class="text-gray-700 w-4 shrink-0 text-center font-mono" x-text="c.rank"></span>
                    <!-- Name + rule class -->
                    <div class="flex-1 min-w-0">
                      <div class="flex items-center gap-1.5">
                        <span class="font-medium"
                          :class="c.selected ? 'text-green-300' : 'text-gray-400'"
                          x-text="c.machine_name"></span>
                        <span class="text-gray-700 font-mono" x-text="c.rule_class !== '*' ? c.rule_class : ''"></span>
                        <template x-if="c.selected">
                          <span class="text-green-500 text-xs">✓ selected</span>
                        </template>
                      </div>
                    </div>
                    <!-- Check badges -->
                    <div class="flex gap-1 shrink-0">
                      <span class="px-1.5 py-0.5 rounded text-xs border"
                        :class="c.checks.enabled
                          ? 'border-gray-700 text-gray-500'
                          : 'border-red-900 bg-red-950 text-red-400'"
                        x-text="c.checks.enabled ? 'on' : 'disabled'"></span>
                      <span class="px-1.5 py-0.5 rounded text-xs border"
                        :class="c.checks.reachable
                          ? 'border-gray-700 text-gray-500'
                          : 'border-red-900 bg-red-950 text-red-400'"
                        x-text="c.checks.reachable ? 'up' : 'down'"></span>
                      <span class="px-1.5 py-0.5 rounded text-xs border"
                        :class="c.checks.capable
                          ? 'border-gray-700 text-gray-500'
                          : 'border-yellow-900 bg-yellow-950 text-yellow-500'"
                        x-text="c.checks.capable ? 'capable' : 'wrong class'"></span>
                    </div>
                  </div>
                </template>
              </div>
            </div>
          </template>

        </div>
      </template>

      <!-- Generic fetch error -->
      <template x-if="routeDebugResult && !routeDebugResult.result && !routeDebugResult.error && !routeDebugResult.trace">
        <div class="bg-red-950 border border-red-900 rounded-xl px-4 py-3 text-red-300 text-xs">
          Unexpected response from server.
        </div>
      </template>

    </div><!-- /debugOpen -->
    </div><!-- /debug section -->

  </div><!-- /routing tab -->

  <!-- ── Instances Tab ────────────────────────────────────────────── -->
  <div x-show="tab==='instances'" x-cloak>

    <!-- Cluster resources — compact bar -->
    <div class="flex items-center gap-3 mb-5 px-3 py-2 rounded-lg border border-gray-800 bg-gray-900 text-xs flex-wrap">
      <button @click="loadInstances()" class="text-gray-600 hover:text-gray-400 text-sm leading-none shrink-0" title="Refresh">↺</button>
      <span class="text-gray-800 shrink-0 select-none">|</span>
      <span x-show="clusterRes._error" class="text-red-500" x-text="'k8s: ' + (clusterRes._error||'')"></span>
      <span x-show="!clusterRes._error && !clusterRes.total_cpu" class="text-gray-600">Cluster data unavailable</span>
      <template x-if="!clusterRes._error && clusterRes.total_cpu">
        <div class="flex items-center gap-3 flex-wrap flex-1">
          <div class="flex items-center gap-1.5">
            <span class="text-gray-600">CPU</span>
            <div class="w-20 h-1.5 bg-gray-800 rounded-full overflow-hidden">
              <div class="h-full rounded-full transition-all"
                :class="clusterRes.free_cpu < 4 ? 'bg-orange-500' : 'bg-green-500'"
                :style="'width:' + Math.min(100, clusterRes.used_cpu / clusterRes.total_cpu * 100) + '%'"></div>
            </div>
            <span class="text-gray-400 font-mono" x-text="clusterRes.free_cpu + ' / ' + clusterRes.total_cpu + ' free'"></span>
          </div>
          <span class="text-gray-800 select-none">·</span>
          <div class="flex items-center gap-1.5">
            <span class="text-gray-600">RAM</span>
            <div class="w-20 h-1.5 bg-gray-800 rounded-full overflow-hidden">
              <div class="h-full rounded-full transition-all"
                :class="clusterRes.free_mem < 6*1024*1024*1024 ? 'bg-orange-500' : 'bg-green-500'"
                :style="'width:' + Math.min(100, clusterRes.used_mem / clusterRes.total_mem * 100) + '%'"></div>
            </div>
            <span class="text-gray-400 font-mono" x-text="fmtBytes(clusterRes.free_mem) + ' / ' + fmtBytes(clusterRes.total_mem) + ' free'"></span>
          </div>
          <span x-show="clusterRes.free_cpu < 4 || clusterRes.free_mem < 6*1024*1024*1024"
            class="text-orange-400 ml-auto">⚠ Resources low — spawns will queue</span>
        </div>
      </template>
    </div>

    <!-- Running instances -->
    <div class="mb-5">
      <div class="flex items-center justify-between mb-3">
        <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider">
          Running Instances
          <span x-show="clusterInstances.length > 0"
            class="ml-1.5 text-gray-600 normal-case font-normal" x-text="'(' + clusterInstances.length + ')'"></span>
        </div>
      </div>
      <div class="space-y-1.5">
        <template x-for="inst in clusterInstances" :key="inst.name">
          <div class="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-gray-900 border transition-colors"
            :class="inst.status==='running' ? 'border-gray-800' : 'border-yellow-900/50'">

            <!-- Status dot -->
            <span class="w-1.5 h-1.5 rounded-full shrink-0"
              :class="inst.status==='running' ? 'bg-green-500' : 'bg-yellow-500 animate-pulse'"></span>

            <!-- Identity + chips -->
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-1.5 flex-wrap">
                <span class="text-sm font-medium text-white" x-text="inst.instance_name"></span>
                <!-- Soul chip -->
                <template x-if="inst.soul">
                  <span class="text-xs px-1.5 py-0.5 rounded font-mono bg-indigo-950 text-indigo-300 border border-indigo-900"
                    x-text="inst.soul.name"></span>
                </template>
                <!-- Routing chip: [alias → machine] -->
                <template x-if="inst.model_alias">
                  <span class="text-xs px-1.5 py-0.5 rounded font-mono border"
                    :class="inst.machine_name
                      ? 'bg-[var(--accent-subtle)] text-[var(--accent)] border-[var(--accent)]/40'
                      : 'bg-gray-800 text-gray-500 border-gray-700'"
                    x-text="inst.model_alias + (inst.machine_name ? ' → ' + inst.machine_name : '')"></span>
                </template>
                <span x-show="inst.soul?.status === 'experimental'"
                  class="text-xs text-yellow-600">experimental</span>
              </div>
              <!-- Secondary: k8s name · ready · port -->
              <div class="flex items-center gap-2 mt-0.5 text-xs text-gray-700 font-mono">
                <span x-text="inst.name"></span>
                <span class="text-gray-800">·</span>
                <span x-text="inst.ready + '/' + inst.desired + ' ready'"></span>
                <template x-if="inst.node_port">
                  <span>
                    <span class="text-gray-800">·</span>
                    <span class="text-indigo-500" x-text="':' + inst.node_port"></span>
                  </span>
                </template>
              </div>
            </div>

            <!-- Actions -->
            <div class="flex items-center gap-1.5 shrink-0">
              <template x-if="inst.node_port">
                <button @click="switchInstance('k8s-' + inst.name); tab='sessions'"
                  class="text-xs px-2.5 py-1 rounded-lg bg-[var(--accent)] hover:opacity-90 text-white font-medium transition-colors">
                  Chat →
                </button>
              </template>
              <template x-if="can('view_routing_debug') && inst.model_alias">
                <button
                  @click="tab='routing'; debugOpen=true; routeDebugUserId=''; routeDebugModel=inst.model_alias; resolveRouteDebug()"
                  class="text-xs px-2 py-1 rounded border border-gray-800 text-gray-600 hover:border-gray-600 hover:text-gray-300 transition-colors font-mono"
                  title="View routing trace">trace</button>
              </template>
              <button x-show="can('delete_instance')"
                @click="confirmDeleteInstance(inst.name)"
                :disabled="inst.name === 'hermes'"
                class="text-xs px-2 py-1 rounded border border-gray-800 text-gray-700 hover:border-red-800 hover:text-red-400 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                title="Delete instance">✕</button>
            </div>
          </div>
        </template>
        <template x-if="clusterInstances.length === 0">
          <div class="text-xs text-gray-700 py-3">No instances running.</div>
        </template>
      </div>
    </div>

    <!-- Pending queue -->
    <template x-if="instanceQueue.length > 0">
      <div class="mb-5">
        <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Queued</div>
        <div class="space-y-2">
          <template x-for="(req, i) in instanceQueue" :key="i">
            <div class="bg-gray-900 border border-yellow-900 rounded-lg px-4 py-3 flex items-center gap-3 text-xs">
              <span class="w-1.5 h-1.5 bg-yellow-500 rounded-full animate-pulse shrink-0"></span>
              <span class="text-gray-300">Hermes for <span class="font-semibold" x-text="req.requester"></span></span>
              <span class="text-gray-600 ml-auto" x-text="req.reason"></span>
            </div>
          </template>
        </div>
      </div>
    </template>

    <!-- Spawn panel (two-step: soul picker → name + overrides) -->
    <div x-show="can('spawn_instance') || can('spawn_instance_restricted')"
         class="bg-gray-900 border border-gray-800 rounded-xl p-4 mt-2">
      <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Spawn New Instance</div>

      <!-- Step 1: Soul picker -->
      <template x-if="spawnStep === 1">
        <div>
          <div class="text-xs text-gray-500 mb-3">Choose a soul (starting role and tool defaults).</div>
          <template x-if="!soulsLoaded">
            <div class="text-xs text-gray-600 py-2">Loading souls...</div>
          </template>
          <template x-if="soulsLoaded && souls.filter(s => can('spawn_instance') || s.user_accessible).length === 0">
            <div class="text-xs text-gray-600 py-2">No souls available — check that the souls/ directory is present in the image.</div>
          </template>
          <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-2 mb-3">
            <template x-for="soul in souls.filter(s => can('spawn_instance') || s.user_accessible)" :key="soul.slug">
              <div class="cursor-pointer rounded-lg border p-3 transition-colors select-none"
                :class="inspectedSoul?.slug === soul.slug
                  ? 'border-indigo-500 bg-indigo-950'
                  : 'border-gray-700 bg-gray-800 hover:border-gray-600'"
                @click="inspectSoul(soul)">
                <div class="font-medium text-white text-xs mb-0.5 truncate" x-text="soul.name"></div>
                <div class="text-xs text-gray-500 leading-tight line-clamp-2" x-text="soul.description"></div>
                <div class="mt-1.5">
                  <span class="text-xs px-1 py-0.5 rounded"
                    :class="soul.status === 'stable' ? 'bg-green-950 text-green-500' : 'bg-yellow-950 text-yellow-500'"
                    x-text="soul.status"></span>
                </div>
              </div>
            </template>
          </div>

          <!-- Soul inspection panel (expands inline) -->
          <template x-if="inspectedSoul">
            <div class="border border-indigo-700 bg-indigo-950 rounded-xl p-4 mb-3">
              <div class="flex items-start justify-between mb-2">
                <div>
                  <div class="font-semibold text-white text-sm" x-text="inspectedSoul.name"></div>
                  <div class="text-xs text-indigo-300 mt-0.5" x-text="inspectedSoul.role_summary"></div>
                </div>
                <span class="text-xs font-mono text-gray-500 shrink-0 ml-3" x-text="'v' + inspectedSoul.version"></span>
              </div>
              <template x-if="inspectedSoul.status === 'experimental'">
                <div class="mb-3 px-3 py-2 rounded-lg border border-yellow-800 bg-yellow-950 text-yellow-300 text-xs">
                  ⚠ Experimental — behavior and tool defaults may change without notice.
                </div>
              </template>
              <div class="space-y-2 mb-3">
                <template x-if="inspectedSoul.toolsets?.enforced?.length">
                  <div class="flex items-start gap-2 text-xs">
                    <span class="text-gray-600 shrink-0 w-20 pt-0.5">enforced</span>
                    <div class="flex flex-wrap gap-1">
                      <template x-for="ts in inspectedSoul.toolsets.enforced" :key="ts">
                        <span class="px-1.5 py-0.5 rounded font-mono bg-red-950 text-red-300 border border-red-900" x-text="ts"></span>
                      </template>
                    </div>
                  </div>
                </template>
                <template x-if="inspectedSoul.toolsets?.default_enabled?.length">
                  <div class="flex items-start gap-2 text-xs">
                    <span class="text-gray-600 shrink-0 w-20 pt-0.5">enabled</span>
                    <div class="flex flex-wrap gap-1">
                      <template x-for="ts in inspectedSoul.toolsets.default_enabled" :key="ts">
                        <span class="px-1.5 py-0.5 rounded font-mono bg-indigo-900 text-indigo-300 border border-indigo-800" x-text="ts"></span>
                      </template>
                    </div>
                  </div>
                </template>
                <template x-if="inspectedSoul.toolsets?.optional?.length">
                  <div class="flex items-start gap-2 text-xs">
                    <span class="text-gray-600 shrink-0 w-20 pt-0.5">optional</span>
                    <div class="flex flex-wrap gap-1">
                      <template x-for="ts in inspectedSoul.toolsets.optional" :key="ts">
                        <span class="px-1.5 py-0.5 rounded font-mono bg-gray-800 text-gray-400 border border-gray-700" x-text="ts"></span>
                      </template>
                    </div>
                  </div>
                </template>
              </div>
              <button @click="selectSoul(inspectedSoul)"
                class="w-full py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition-colors">
                Select this soul →
              </button>
            </div>
          </template>

          <p class="text-xs text-gray-700">Souls are starting presets — they shape behavior and tool defaults but do not guarantee expertise or consistent performance.</p>
        </div>
      </template>

      <!-- Step 2: Name + optional overrides -->
      <template x-if="spawnStep === 2">
        <div>
          <div class="flex items-center gap-2 mb-3">
            <button @click="backToSoulPicker()" class="text-xs text-indigo-400 hover:text-indigo-300">&#8592; Back</button>
            <span class="text-xs text-gray-600">Soul:</span>
            <span class="text-xs px-1.5 py-0.5 rounded bg-indigo-950 text-indigo-300 border border-indigo-900 font-mono"
              x-text="selectedSoul?.name"></span>
          </div>
          <!-- Model alias + routing preview -->
          <div class="mb-3 space-y-2">
            <div class="flex items-end gap-3">
              <div class="flex-1">
                <label class="text-xs text-gray-500 block mb-1">Default model (GPU node selected automatically)</label>
                <select x-model="spawnModelAlias" @change="loadSpawnRoutePreview()"
                  class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                  <template x-for="group in spawnAliasGroups()" :key="group.cls">
                    <optgroup :label="group.label">
                      <template x-for="alias in group.aliases" :key="alias">
                        <option :value="alias" x-text="alias"></option>
                      </template>
                    </optgroup>
                  </template>
                </select>
              </div>
            </div>
            <!-- Routing preview card -->
            <div class="rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-xs min-h-[2rem] flex items-center">
              <template x-if="spawnRoutePreviewLoading">
                <span class="text-gray-600 animate-pulse">Resolving route&#8230;</span>
              </template>
              <template x-if="!spawnRoutePreviewLoading && spawnRoutePreview && spawnRoutePreview.machine">
                <div class="flex items-center gap-2 flex-wrap w-full">
                  <span class="font-mono text-[var(--accent)]" x-text="spawnRoutePreview.model_alias"></span>
                  <span class="text-gray-700">→</span>
                  <span class="px-1.5 py-0.5 rounded border border-gray-700 text-gray-400" x-text="spawnRoutePreview.model_class"></span>
                  <span class="text-gray-700">→</span>
                  <span class="font-medium text-white" x-text="spawnRoutePreview.machine.name"></span>
                  <span class="text-gray-600 ml-auto italic" x-text="'via ' + spawnRoutePreview.layer_label"></span>
                </div>
              </template>
              <template x-if="!spawnRoutePreviewLoading && spawnRoutePreview && !spawnRoutePreview.machine && !spawnRoutePreview.error">
                <span class="text-yellow-700">No machines registered — will use default config</span>
              </template>
              <template x-if="!spawnRoutePreviewLoading && spawnRoutePreview?.error">
                <span class="text-red-700" x-text="spawnRoutePreview.error"></span>
              </template>
              <template x-if="!spawnRoutePreviewLoading && !spawnRoutePreview">
                <span class="text-gray-700">—</span>
              </template>
            </div>
          </div>

          <!-- Machine override — requires override_routing permission -->
          <template x-if="can('override_routing')">
            <div class="mb-3">
              <label class="text-xs text-gray-500 block mb-1">
                Machine override <span class="text-gray-700">(optional)</span>
              </label>
              <select x-model="spawnMachineOverride" @change="loadSpawnRoutePreview()"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                <option value="">— auto (use routing profile) —</option>
                <template x-for="m in adminMachines" :key="m.id">
                  <option :value="m.id"
                    x-text="m.name + (m.description ? ' — ' + m.description : '')"></option>
                </template>
              </select>
            </div>
          </template>

          <div class="flex gap-3 items-end mb-3">
            <div class="flex-1">
              <label class="text-xs text-gray-500 block mb-1">Person's name</label>
              <!-- Admins: searchable dropdown of all users -->
              <template x-if="authUser && authUser.role === 'admin'">
                <div class="relative">
                  <input x-model="newInstanceRequester"
                    @input="instanceUserDropdownOpen = true"
                    @focus="instanceUserDropdownOpen = true"
                    @blur="setTimeout(() => instanceUserDropdownOpen = false, 150)"
                    @keydown.escape="instanceUserDropdownOpen = false"
                    @keydown.enter="requestInstance()"
                    placeholder="Search users…"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors">
                  <div x-show="instanceUserDropdownOpen && instanceUserList.filter(u => !newInstanceRequester || u.toLowerCase().includes(newInstanceRequester.toLowerCase())).length > 0"
                    x-cloak class="absolute z-50 top-full mt-1 w-full bg-gray-800 border border-gray-700 rounded-lg shadow-xl overflow-auto" style="max-height:180px">
                    <template x-for="u in instanceUserList.filter(u => !newInstanceRequester || u.toLowerCase().includes(newInstanceRequester.toLowerCase()))" :key="u">
                      <div @click="newInstanceRequester = u; instanceUserDropdownOpen = false"
                        class="px-3 py-2 text-sm text-white hover:bg-gray-700 cursor-pointer" x-text="u"></div>
                    </template>
                  </div>
                </div>
              </template>
              <!-- Non-admins: read-only, pre-filled with their own name -->
              <template x-if="!authUser || authUser.role !== 'admin'">
                <input :value="authUser ? (authUser.display_name || authUser.username) : ''"
                  readonly class="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-400 cursor-not-allowed">
              </template>
            </div>
            <button @click="requestInstance()"
              :disabled="!newInstanceRequester.trim() || instanceSpawning"
              class="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors flex items-center gap-2">
              <span x-show="instanceSpawning" class="animate-spin text-xs">&#9881;</span>
              <span>Spawn</span>
            </button>
          </div>
          <template x-if="can('override_toolsets') && selectedSoul?.toolsets?.optional?.length > 0">
            <div class="mb-3">
              <div class="text-xs text-gray-500 mb-2">Optional toolsets</div>
              <div class="flex flex-wrap gap-3">
                <template x-for="ts in selectedSoul.toolsets.optional" :key="ts">
                  <label class="flex items-center gap-1.5 cursor-pointer text-xs">
                    <input type="checkbox" x-model="spawnOptionalEnabled[ts]"
                      class="rounded border-gray-600 bg-gray-800 accent-indigo-500">
                    <span class="text-gray-400 font-mono" x-text="ts"></span>
                  </label>
                </template>
              </div>
            </div>
          </template>
          <template x-if="instanceMsg">
            <div class="mt-2 text-xs px-3 py-2 rounded-lg"
              :class="instanceMsg.ok ? 'bg-green-950 text-green-300 border border-green-800' : 'bg-red-950 text-red-300 border border-red-800'"
              x-text="instanceMsg.text"></div>
          </template>
          <div class="mt-2 text-xs text-gray-700">500m CPU / 2Gi RAM req · 4 cores / 6Gi limit · own PVC · auto NodePort</div>
        </div>
      </template>
    </div>

    <!-- Quick spawn templates -->
    <template x-if="(can('spawn_instance') || can('spawn_instance_restricted')) && spawnTemplates.length > 0">
      <div class="mt-4">
        <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Quick Spawn</div>
        <div class="space-y-1.5">
          <template x-for="t in spawnTemplates" :key="t.id">
            <div class="flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 hover:border-gray-700 transition-colors">
              <!-- Chips -->
              <span class="text-xs px-1.5 py-0.5 rounded font-mono bg-indigo-950 text-indigo-300 border border-indigo-900 shrink-0"
                x-text="t.soul_name"></span>
              <span class="text-xs px-1.5 py-0.5 rounded font-mono bg-[var(--accent-subtle)] text-[var(--accent)] border border-[var(--accent)]/40 shrink-0"
                x-text="t.model_alias"></span>
              <template x-for="ts in (t.optional_toolsets || [])" :key="ts">
                <span class="text-xs px-1 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700 font-mono shrink-0"
                  x-text="ts"></span>
              </template>
              <!-- Requester -->
              <span class="text-sm text-gray-300 truncate flex-1" x-text="t.requester"></span>
              <!-- Actions -->
              <button @click="spawnFromTemplate(t)" :disabled="instanceSpawning"
                class="text-xs px-2.5 py-1 rounded-lg bg-[var(--accent)] hover:opacity-90 disabled:opacity-40 text-white font-medium shrink-0 transition-colors">
                Spawn
              </button>
              <button @click="removeSpawnTemplate(t.id)"
                class="text-xs px-1.5 py-1 rounded border border-gray-800 text-gray-700 hover:border-red-800 hover:text-red-400 transition-colors shrink-0"
                title="Remove template">✕</button>
            </div>
          </template>
        </div>
      </div>
    </template>

    <!-- Grafana quick links -->
    <div class="mt-5 flex flex-wrap gap-1.5 border-t border-gray-800 pt-4" x-data="{open:false}">
      <button @click="open=!open"
        class="text-xs text-gray-700 hover:text-gray-500 transition-colors">Observability ▾</button>
      <div x-show="open" class="w-full flex flex-wrap gap-1.5 mt-1.5">
        <a :href="routerState.grafana_url+'/d/hermes-logs'" target="_blank"
           class="text-xs px-2.5 py-1 rounded border border-gray-800 text-gray-600 hover:text-gray-300 hover:border-gray-700 transition-colors">
          Hermes Logs →
        </a>
        <a :href="routerState.grafana_url+'/d/docker-host-logs'" target="_blank"
           class="text-xs px-2.5 py-1 rounded border border-gray-800 text-gray-600 hover:text-gray-300 hover:border-gray-700 transition-colors">
          Docker Logs →
        </a>
        <a :href="routerState.grafana_url" target="_blank"
           class="text-xs px-2.5 py-1 rounded border border-gray-800 text-gray-600 hover:text-gray-300 hover:border-gray-700 transition-colors">
          Grafana →
        </a>
      </div>
    </div>

  </div>

  <!-- ── Admin Tab ────────────────────────────────────────────────── -->
  <div x-show="tab==='admin'" x-cloak>

    <!-- Sub-tabs -->
    <div class="flex gap-4 border-b border-gray-800 mb-5">
      <button x-show="can('manage_users')"
        class="pb-2 text-sm font-medium"
        :class="adminTab==='users'?'tab-active':'text-gray-500 hover:text-white'"
        @click="adminTab='users'; loadAdminUsers()">Users</button>
      <button x-show="can('view_audit_logs')"
        class="pb-2 text-sm font-medium"
        :class="adminTab==='audit'?'tab-active':'text-gray-500 hover:text-white'"
        @click="adminTab='audit'; loadAdminAudit()">Audit Log</button>
      <button x-show="can('view_audit_logs')"
        class="pb-2 text-sm font-medium"
        :class="adminTab==='routing-log'?'tab-active':'text-gray-500 hover:text-white'"
        @click="adminTab='routing-log'; if(can('manage_users') && !adminUsers.length) loadAdminUsers(); loadAdminRoutingLog()">Routing Log</button>
      <button x-show="can('view_approvals')"
        class="pb-2 text-sm font-medium"
        :class="adminTab==='approvals'?'tab-active':'text-gray-500 hover:text-white'"
        @click="adminTab='approvals'; loadApprovals()">Approvals</button>
    </div>

    <!-- ── Users sub-tab ── -->
    <div x-show="adminTab==='users' && can('manage_users')">
      <div class="flex items-center justify-between mb-4">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Users</div>
        <button @click="adminUserForm={email:'',username:'',password:'',role:'user',display_name:''}; adminUserFormOpen=true"
          class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity">+ New User</button>
      </div>

      <!-- New user form -->
      <template x-if="adminUserFormOpen">
        <div class="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-4">
          <div class="text-xs font-semibold text-gray-400 mb-3">Create User</div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div>
              <label class="text-xs text-gray-500 block mb-1">Email</label>
              <input x-model="adminUserForm.email" type="email" placeholder="user@example.com"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div>
              <label class="text-xs text-gray-500 block mb-1">Username</label>
              <input x-model="adminUserForm.username" type="text" placeholder="username"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div>
              <label class="text-xs text-gray-500 block mb-1">Password</label>
              <input x-model="adminUserForm.password" type="password" placeholder="••••••••"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div>
              <label class="text-xs text-gray-500 block mb-1">Display Name</label>
              <input x-model="adminUserForm.display_name" type="text" placeholder="Full Name (optional)"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div class="col-span-2">
              <label class="text-xs text-gray-500 block mb-1">Role</label>
              <select x-model="adminUserForm.role"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                <option value="viewer">viewer — read-only access</option>
                <option value="user">user — standard access</option>
                <option value="operator">operator — manage instances &amp; routing</option>
                <option value="admin">admin — full access</option>
              </select>
            </div>
          </div>
          <div class="flex gap-2">
            <button @click="createAdminUser()"
              class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Create User</button>
            <button @click="adminUserFormOpen=false"
              class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
          </div>
          <template x-if="adminMsg">
            <div class="mt-2 text-xs px-3 py-2 rounded-lg"
              :class="adminMsg.ok ? 'bg-green-950 text-green-300 border border-green-800' : 'bg-red-950 text-red-300 border border-red-800'"
              x-text="adminMsg.text"></div>
          </template>
        </div>
      </template>

      <!-- User table -->
      <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-gray-800 text-xs text-gray-500">
              <th class="text-left px-4 py-3 font-medium">User</th>
              <th class="text-left px-4 py-3 font-medium">Role</th>
              <th class="text-left px-4 py-3 font-medium">Status</th>
              <th class="text-left px-4 py-3 font-medium">Routing Profile</th>
              <th class="text-left px-4 py-3 font-medium">Last Login</th>
              <th class="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            <template x-for="u in adminUsers" :key="u.id">
              <tr class="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors"
                :class="u.id===authUser?.id ? 'bg-[var(--accent-subtle)]' : ''">

                <!-- User identity -->
                <td class="px-4 py-3">
                  <div class="flex items-center gap-1.5">
                    <span class="text-white text-xs font-medium" x-text="u.display_name || u.username"></span>
                    <span x-show="u.id===authUser?.id"
                      class="text-xs px-1 rounded" style="background:var(--accent-subtle);color:var(--accent)">you</span>
                  </div>
                  <div class="text-gray-600 text-xs mt-0.5">
                    <span x-show="u.display_name && u.display_name!==u.username" x-text="'@'+u.username+' · '"></span>
                    <span x-text="u.email"></span>
                  </div>
                </td>

                <!-- Role inline select -->
                <td class="px-4 py-3">
                  <select :value="u.role" @change="patchUser(u.id, {role: $event.target.value})"
                    :disabled="u.id===authUser?.id"
                    :title="u.id===authUser?.id ? 'Cannot change your own role' : ''"
                    class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs focus:border-[var(--accent)] focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed"
                    :class="u.role==='admin' ? 'text-red-400' : u.role==='operator' ? 'text-indigo-400' : 'text-gray-300'">
                    <option value="viewer">viewer</option>
                    <option value="user">user</option>
                    <option value="operator">operator</option>
                    <option value="admin">admin</option>
                  </select>
                </td>

                <!-- Status toggle -->
                <td class="px-4 py-3">
                  <button
                    @click="u.id!==authUser?.id && patchUser(u.id, {status: u.status==='active' ? 'suspended' : 'active'})"
                    :disabled="u.id===authUser?.id"
                    :title="u.id===authUser?.id ? 'Cannot change your own status' : u.status==='active' ? 'Click to suspend' : 'Click to activate'"
                    class="text-xs px-2.5 py-1 rounded-full border font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                    :class="u.status==='active'
                      ? 'border-green-800 bg-green-950 text-green-400 hover:border-red-800 hover:bg-red-950/60 hover:text-red-400'
                      : 'border-red-800 bg-red-950 text-red-400 hover:border-green-800 hover:bg-green-950/60 hover:text-green-400'"
                    x-text="u.status==='active' ? '● Active' : '● Suspended'">
                  </button>
                </td>

                <!-- Routing profile inline select -->
                <td class="px-4 py-3">
                  <select :value="u.policy_id || ''"
                    @change="assignUserPolicy(u.id, $event.target.value || null)"
                    class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:border-[var(--accent)] focus:outline-none max-w-[160px]">
                    <option value="">— system default —</option>
                    <template x-for="p in adminPolicies" :key="p.id">
                      <option :value="p.id" x-text="p.name"></option>
                    </template>
                  </select>
                </td>

                <!-- Last login -->
                <td class="px-4 py-3 text-xs text-gray-600"
                  x-text="u.last_login ? new Date(u.last_login*1000).toLocaleString(undefined,{dateStyle:'short',timeStyle:'short'}) : 'never'">
                </td>

                <!-- Set password -->
                <td class="px-4 py-3 text-right">
                  <button @click="adminSetPwUserId=u.id; adminSetPwVal=''; adminSetPwMsg=null"
                    class="text-xs text-gray-600 hover:text-white transition-colors"
                    title="Set a new password for this user">⚿</button>
                </td>

              </tr>
            </template>
            <template x-if="adminUsers.length===0">
              <tr><td colspan="6" class="px-4 py-8 text-center text-xs text-gray-600">No users found</td></tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Set password panel -->
    <div x-show="adminSetPwUserId" x-cloak class="mt-3 bg-gray-900 border border-gray-700 rounded-xl p-4">
      <template x-if="adminSetPwUserId">
        <div>
          <div class="text-xs font-semibold text-gray-400 mb-3">
            Set password for
            <span class="text-white" x-text="(adminUsers.find(u=>u.id===adminSetPwUserId)||{}).display_name || (adminUsers.find(u=>u.id===adminSetPwUserId)||{}).username || adminSetPwUserId"></span>
          </div>
          <div class="flex gap-2 items-center">
            <input x-model="adminSetPwVal" type="password" placeholder="New password (min 8 chars)"
              @keydown.enter="adminSetPassword()"
              class="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-[var(--accent)] w-64"/>
            <button @click="adminSetPassword()"
              class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity">Set</button>
            <button @click="adminSetPwUserId=null; adminSetPwVal=''; adminSetPwMsg=null"
              class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
          </div>
          <template x-if="adminSetPwMsg">
            <div class="mt-2 text-xs px-3 py-2 rounded-lg"
              :class="adminSetPwMsg.ok ? 'bg-green-950 text-green-300 border border-green-800' : 'bg-red-950 text-red-300 border border-red-800'"
              x-text="adminSetPwMsg.text"></div>
          </template>
        </div>
      </template>
    </div>

    <!-- ── (Machines + Policies live under Routing tab) ── -->
    <div x-show="false" style="display:none">
      <div class="flex items-center justify-between mb-4">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Machines</div>
        <button @click="adminMachineForm={name:'',endpoint_url:'',description:''}; adminMachineFormOpen=true"
          class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity">+ Register Machine</button>
      </div>

      <!-- New machine form -->
      <template x-if="adminMachineFormOpen">
        <div class="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-4">
          <div class="text-xs font-semibold text-gray-400 mb-3">Register Machine</div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div>
              <label class="text-xs text-gray-500 block mb-1">Name</label>
              <input x-model="adminMachineForm.name" type="text" placeholder="e.g. windows-gpu"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div>
              <label class="text-xs text-gray-500 block mb-1">Endpoint URL</label>
              <input x-model="adminMachineForm.endpoint_url" type="text" placeholder="http://192.168.1.x:1234/v1"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div class="col-span-2">
              <label class="text-xs text-gray-500 block mb-1">Description (optional)</label>
              <input x-model="adminMachineForm.description" type="text" placeholder="RTX 4080 SUPER, 16GB VRAM"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
          </div>
          <div class="flex gap-2">
            <button @click="createMachine()"
              class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Register</button>
            <button @click="adminMachineFormOpen=false"
              class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
          </div>
          <template x-if="adminMsg">
            <div class="mt-2 text-xs px-3 py-2 rounded-lg"
              :class="adminMsg.ok ? 'bg-green-950 text-green-300 border border-green-800' : 'bg-red-950 text-red-300 border border-red-800'"
              x-text="adminMsg.text"></div>
          </template>
        </div>
      </template>

      <!-- Machine cards -->
      <div class="grid grid-cols-1 gap-3">
        <template x-for="m in adminMachines" :key="m.id">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <div class="flex items-start justify-between mb-3">
              <div>
                <div class="flex items-center gap-2">
                  <span class="font-medium text-white text-sm" x-text="m.name"></span>
                  <span class="text-xs px-1.5 py-0.5 rounded"
                    :class="m.enabled ? 'bg-green-950 text-green-400' : 'bg-gray-800 text-gray-500'"
                    x-text="m.enabled ? 'enabled' : 'disabled'"></span>
                </div>
                <div class="text-xs text-gray-500 mt-0.5" x-text="m.endpoint_url"></div>
                <div class="text-xs text-gray-600 mt-0.5" x-text="m.description || ''"></div>
              </div>
              <div class="flex gap-2 items-center">
                <button @click="probeHealth(m)"
                  class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors">Health</button>
                <button @click="toggleMachine(m)"
                  class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors"
                  x-text="m.enabled ? 'Disable' : 'Enable'"></button>
                <button @click="deleteMachine(m.id)"
                  class="text-xs px-2 py-1 rounded border border-red-900 text-red-500 hover:text-red-300 transition-colors">Delete</button>
              </div>
            </div>
            <!-- Health result -->
            <template x-if="m._health">
              <div class="mb-3 text-xs px-3 py-1.5 rounded-lg border"
                :class="m._health.status==='ok' ? 'border-green-800 bg-green-950 text-green-400' : 'border-red-800 bg-red-950 text-red-400'"
                x-text="m._health.status + (m._health.http ? ' · HTTP ' + m._health.http : '') + (m._health.error ? ' · ' + m._health.error : '')"></div>
            </template>
            <!-- Capabilities -->
            <div>
              <div class="text-xs text-gray-600 mb-1.5">Capabilities (model classes this machine serves)</div>
              <div class="flex flex-wrap gap-1.5">
                <template x-for="cls in ['lightweight','coding','general','reasoning','vision','embedding']" :key="cls">
                  <label class="flex items-center gap-1 text-xs cursor-pointer">
                    <input type="checkbox"
                      :checked="m.capabilities.includes(cls)"
                      @change="toggleCapability(m, cls, $event.target.checked)"
                      class="rounded border-gray-600 bg-gray-800 accent-[var(--accent)]">
                    <span class="text-gray-400" x-text="cls"></span>
                  </label>
                </template>
              </div>
            </div>
          </div>
        </template>
        <template x-if="adminMachines.length===0">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-xs text-gray-600">No machines registered. Click "Register Machine" to add one.</div>
        </template>
      </div>
    </div>

    <!-- ── (Policies live under Routing tab) ── -->
    <div x-show="false" style="display:none">
      <div class="flex items-center justify-between mb-4">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Routing Profiles</div>
        <button @click="adminPolicyForm={name:'',description:'',fallback:'any_available'}; adminPolicyFormOpen=true"
          class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity">+ New Profile</button>
      </div>

      <!-- New policy form -->
      <template x-if="adminPolicyFormOpen">
        <div class="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-4">
          <div class="text-xs font-semibold text-gray-400 mb-3">Create Profile</div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div>
              <label class="text-xs text-gray-500 block mb-1">Name</label>
              <input x-model="adminPolicyForm.name" type="text" placeholder="e.g. bere-local"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
            <div>
              <label class="text-xs text-gray-500 block mb-1">Fallback</label>
              <select x-model="adminPolicyForm.fallback"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
                <option value="any_available">any_available</option>
                <option value="fail">fail</option>
              </select>
            </div>
            <div class="col-span-2">
              <label class="text-xs text-gray-500 block mb-1">Description (optional)</label>
              <input x-model="adminPolicyForm.description" type="text" placeholder="Route lightweight to Bere's PC"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:border-[var(--accent)] focus:outline-none">
            </div>
          </div>
          <div class="flex gap-2">
            <button @click="createPolicy()"
              class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Create</button>
            <button @click="adminPolicyFormOpen=false"
              class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
          </div>
        </div>
      </template>

      <!-- Policy cards -->
      <div class="grid grid-cols-1 gap-4">
        <template x-for="p in adminPolicies" :key="p.id">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <div class="flex items-start justify-between mb-3">
              <div>
                <div class="font-medium text-white text-sm" x-text="p.name"></div>
                <div class="text-xs text-gray-500 mt-0.5" x-text="p.description || ''"></div>
                <div class="text-xs text-gray-600 mt-1">
                  <span x-text="p.user_count + ' user' + (p.user_count!==1?'s':'')"></span> ·
                  <span x-text="'fallback: ' + p.fallback"></span>
                </div>
              </div>
              <button @click="deletePolicy(p.id)"
                class="text-xs px-2 py-1 rounded border border-red-900 text-red-500 hover:text-red-300 transition-colors">Delete</button>
            </div>
            <!-- Rules editor -->
            <div class="border-t border-gray-800 pt-3">
              <div class="text-xs text-gray-600 mb-2">Rules (drag to reorder, model class → machine)</div>
              <div class="space-y-2">
                <template x-for="(rule, idx) in p.rules" :key="idx">
                  <div class="flex items-center gap-2">
                    <select :value="rule.model_class" @change="rule.model_class=$event.target.value"
                      class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:border-[var(--accent)] focus:outline-none w-36">
                      <option value="*">* (any)</option>
                      <option value="lightweight">lightweight</option>
                      <option value="coding">coding</option>
                      <option value="general">general</option>
                      <option value="reasoning">reasoning</option>
                      <option value="vision">vision</option>
                      <option value="embedding">embedding</option>
                    </select>
                    <span class="text-gray-600 text-xs">→</span>
                    <select :value="rule.machine_id" @change="rule.machine_id=$event.target.value"
                      class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:border-[var(--accent)] focus:outline-none flex-1">
                      <template x-for="m in adminMachines" :key="m.id">
                        <option :value="m.id" x-text="m.name"></option>
                      </template>
                    </select>
                    <button @click="p.rules.splice(idx,1)"
                      class="text-xs text-red-600 hover:text-red-400 px-1">✕</button>
                  </div>
                </template>
              </div>
              <div class="flex gap-2 mt-2">
                <button @click="p.rules.push({model_class:'*',machine_id:adminMachines[0]?.id||''})"
                  class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:text-white">+ Rule</button>
                <button @click="savePolicyRules(p)"
                  class="text-xs px-2 py-1 rounded bg-[var(--accent)] text-white hover:opacity-90">Save Rules</button>
              </div>
            </div>
          </div>
        </template>
        <template x-if="adminPolicies.length===0">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-xs text-gray-600">No profiles yet. Create one to control per-user routing.</div>
        </template>
      </div>
    </div>

    <!-- ── Audit Log sub-tab ── -->
    <div x-show="adminTab==='audit' && can('view_audit_logs')">
      <div class="flex items-center justify-between mb-4">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Audit Log</div>
        <button @click="loadAdminAudit()"
          class="text-xs text-[var(--accent)] hover:opacity-80 flex items-center gap-1"><span>&#8635;</span> Refresh</button>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <table class="w-full text-xs">
          <thead>
            <tr class="border-b border-gray-800 text-gray-500">
              <th class="text-left px-4 py-3 font-medium">Time</th>
              <th class="text-left px-4 py-3 font-medium">Action</th>
              <th class="text-left px-4 py-3 font-medium">Target</th>
              <th class="text-left px-4 py-3 font-medium">IP</th>
            </tr>
          </thead>
          <tbody>
            <template x-for="log in adminAuditLogs" :key="log.id">
              <tr class="border-b border-gray-800/50">
                <td class="px-4 py-2 text-gray-500" x-text="new Date(log.created_at*1000).toLocaleString()"></td>
                <td class="px-4 py-2 text-gray-300 font-mono" x-text="log.action"></td>
                <td class="px-4 py-2 text-gray-500" x-text="(log.target_type||'') + (log.target_id ? ' '+log.target_id.slice(0,8) : '')"></td>
                <td class="px-4 py-2 text-gray-600" x-text="log.ip_address||''"></td>
              </tr>
            </template>
            <template x-if="adminAuditLogs.length===0">
              <tr><td colspan="4" class="px-4 py-6 text-center text-gray-600">No audit logs</td></tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── Routing Log sub-tab ── -->
    <div x-show="adminTab==='routing-log' && can('view_audit_logs')">
      <div class="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <div class="flex items-center gap-3">
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Routing Log</div>
          <!-- Row window label -->
          <span class="text-xs text-gray-600"
            x-show="adminRoutingLogTotal > 0"
            x-text="'rows ' + (adminRoutingLogPage * 100 - 99) + '–' + Math.min(adminRoutingLogPage * 100, adminRoutingLogTotal) + ' of ' + adminRoutingLogTotal">
          </span>
          <div x-show="adminRoutingLogLoading" class="w-3 h-3 rounded-full border border-gray-600 border-t-[var(--accent)] animate-spin"></div>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <!-- User filter — only shown when user list is available -->
          <template x-if="can('manage_users') && adminUsers.length > 0">
            <select x-model="adminRoutingLogUserFilter" @change="adminRoutingLogPage=1; loadAdminRoutingLog()"
              class="bg-gray-800 border border-gray-700 rounded-lg px-2 py-1 text-xs text-white focus:border-[var(--accent)] focus:outline-none">
              <option value="">All users</option>
              <template x-for="u in adminUsers" :key="u.id">
                <option :value="u.id" x-text="u.display_name || u.username"></option>
              </template>
            </select>
          </template>
          <button @click="loadAdminRoutingLog()"
            class="text-xs text-[var(--accent)] hover:opacity-80 flex items-center gap-1"><span>&#8635;</span> Refresh</button>
        </div>
      </div>

      <!-- Chunk slider — only shown when there is more than one window -->
      <div x-show="adminRoutingLogTotal > 100" class="mb-4 flex items-center gap-3">
        <span class="text-xs text-gray-600 shrink-0">Window</span>
        <input type="range" min="1" :max="Math.ceil(adminRoutingLogTotal/100)"
          x-model.number="adminRoutingLogPage"
          @change="loadAdminRoutingLog()"
          class="flex-1 accent-[var(--accent)] h-1.5 rounded-full appearance-none bg-gray-700 cursor-pointer"/>
        <span class="text-xs text-gray-500 shrink-0 font-mono w-24 text-right"
          x-text="(adminRoutingLogPage * 100 - 99) + '–' + Math.min(adminRoutingLogPage * 100, adminRoutingLogTotal)"></span>
        <div class="flex gap-1 shrink-0">
          <button @click="adminRoutingLogPage=Math.max(1,adminRoutingLogPage-1); loadAdminRoutingLog()"
            :disabled="adminRoutingLogPage<=1"
            class="px-1.5 py-0.5 rounded border border-gray-700 text-gray-500 hover:text-white hover:border-gray-500 disabled:opacity-30 text-xs">‹</button>
          <button @click="adminRoutingLogPage=Math.min(Math.ceil(adminRoutingLogTotal/100),adminRoutingLogPage+1); loadAdminRoutingLog()"
            :disabled="adminRoutingLogPage*100>=adminRoutingLogTotal"
            class="px-1.5 py-0.5 rounded border border-gray-700 text-gray-500 hover:text-white hover:border-gray-500 disabled:opacity-30 text-xs">›</button>
        </div>
      </div>

      <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden mb-3">
        <table class="w-full text-xs">
          <thead>
            <tr class="border-b border-gray-800 text-gray-500">
              <th class="text-left px-4 py-3 font-medium w-36">Time</th>
              <th class="text-left px-4 py-3 font-medium">Instance</th>
              <th class="text-left px-4 py-3 font-medium">Alias</th>
              <th class="text-left px-4 py-3 font-medium">Class</th>
              <th class="text-left px-4 py-3 font-medium">Machine</th>
              <th class="text-left px-4 py-3 font-medium">Layer</th>
            </tr>
          </thead>
          <tbody>
            <template x-if="adminRoutingLogLoading && adminRoutingLog.length===0">
              <tr><td colspan="6" class="px-4 py-8 text-center text-gray-600">
                <div class="flex items-center justify-center gap-2">
                  <div class="w-3 h-3 rounded-full border border-gray-600 border-t-[var(--accent)] animate-spin"></div>
                  Loading…
                </div>
              </td></tr>
            </template>
            <template x-for="row in adminRoutingLog" :key="row.id">
              <tr class="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td class="px-4 py-2 text-gray-600 font-mono whitespace-nowrap"
                  x-text="new Date(row.created_at*1000).toLocaleString(undefined,{dateStyle:'short',timeStyle:'short'})"></td>
                <td class="px-4 py-2 text-gray-500 truncate max-w-24" x-text="row.instance_name || '—'"></td>
                <td class="px-4 py-2">
                  <span class="font-mono text-[var(--accent)]" x-text="row.model_alias"></span>
                </td>
                <td class="px-4 py-2 text-gray-500 font-mono" x-text="row.model_class"></td>
                <td class="px-4 py-2 text-gray-300" x-text="row.machine_name || '—'"></td>
                <td class="px-4 py-2">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-medium"
                    :class="{
                      'bg-indigo-950 text-indigo-400 border border-indigo-900': row.layer==='user_profile',
                      'bg-gray-800 text-gray-500 border border-gray-700':       row.layer==='best_effort',
                      'bg-orange-950 text-orange-400 border border-orange-900': row.layer==='instance_override',
                    }"
                    x-text="row.layer==='user_profile' ? 'profile' : row.layer==='best_effort' ? 'default' : row.layer || '—'"></span>
                </td>
              </tr>
            </template>
            <template x-if="!adminRoutingLogLoading && adminRoutingLog.length===0">
              <tr><td colspan="6" class="px-4 py-8 text-center text-gray-600">No routing decisions recorded yet</td></tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── Approvals sub-tab ── -->
    <div x-show="adminTab==='approvals' && can('view_approvals')">
      <div class="flex items-center justify-between mb-4">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Approval Requests</div>
        <div class="flex items-center gap-3">
          <select x-model="approvalsStatusFilter" @change="loadApprovals()"
            class="bg-gray-800 border border-gray-700 rounded-lg text-xs px-2 py-1 text-gray-300">
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="expired">Expired</option>
            <option value="">All</option>
          </select>
          <button @click="loadApprovals()"
            class="text-xs text-[var(--accent)] hover:opacity-80 flex items-center gap-1"><span>&#8635;</span> Refresh</button>
        </div>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <table class="w-full text-xs">
          <thead>
            <tr class="border-b border-gray-800 text-gray-500">
              <th class="text-left px-4 py-3 font-medium">Time</th>
              <th class="text-left px-4 py-3 font-medium">Tool</th>
              <th class="text-left px-4 py-3 font-medium">Action</th>
              <th class="text-left px-4 py-3 font-medium">Status</th>
              <th class="text-left px-4 py-3 font-medium" x-show="can('decide_approvals')">Decision</th>
            </tr>
          </thead>
          <tbody>
            <template x-for="req in approvalRequests" :key="req.id">
              <tr class="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td class="px-4 py-2 text-gray-600 whitespace-nowrap"
                  x-text="new Date(req.requested_at*1000).toLocaleString(undefined,{dateStyle:'short',timeStyle:'short'})"></td>
                <td class="px-4 py-2 text-gray-300 font-mono" x-text="req.tool_name"></td>
                <td class="px-4 py-2 text-gray-500" x-text="req.action_type || '—'"></td>
                <td class="px-4 py-2">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                    :class="{
                      'bg-yellow-950 text-yellow-400 border-yellow-900': req.status==='pending',
                      'bg-green-950 text-green-400 border-green-900':  req.status==='approved',
                      'bg-red-950 text-red-400 border-red-900':        req.status==='rejected',
                      'bg-gray-800 text-gray-500 border-gray-700':     req.status==='expired',
                    }"
                    x-text="req.status"></span>
                </td>
                <td class="px-4 py-2" x-show="can('decide_approvals')">
                  <template x-if="req.status==='pending'">
                    <div class="flex gap-2">
                      <button @click="decideApproval(req.id,'approve')"
                        class="px-2 py-1 rounded text-[10px] bg-green-900 text-green-300 hover:bg-green-800 border border-green-800">Approve</button>
                      <button @click="decideApproval(req.id,'reject')"
                        class="px-2 py-1 rounded text-[10px] bg-red-950 text-red-400 hover:bg-red-900 border border-red-900">Reject</button>
                    </div>
                  </template>
                  <template x-if="req.status!=='pending'">
                    <span class="text-gray-600" x-text="req.decided_by ? 'by '+req.decided_by.slice(0,8) : '—'"></span>
                  </template>
                </td>
              </tr>
            </template>
            <template x-if="approvalRequests.length===0">
              <tr><td :colspan="can('decide_approvals') ? 5 : 4" class="px-4 py-8 text-center text-gray-600">No approval requests</td></tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Shared admin message bar -->
    <template x-if="adminMsg && !adminUserFormOpen && !adminMachineFormOpen && !adminPolicyFormOpen">
      <div class="mt-4 text-xs px-3 py-2 rounded-lg"
        :class="adminMsg.ok ? 'bg-green-950 text-green-300 border border-green-800' : 'bg-red-950 text-red-300 border border-red-800'"
        x-text="adminMsg.text"></div>
    </template>

  </div>

  <!-- ── Workflows Tab ────────────────────────────────────────────── -->
  <div x-show="tab==='workflows'" x-cloak class="p-6 max-w-5xl mx-auto space-y-6">

    <!-- Header row -->
    <div class="flex items-center justify-between">
      <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Workflow Definitions</div>
      <div class="flex items-center gap-3">
        <button @click="loadWorkflows()" class="text-xs text-[var(--accent)] hover:opacity-80 flex items-center gap-1"><span>&#8635;</span> Refresh</button>
        <button x-show="can('manage_workflows')" @click="wfNewFormOpen=true"
          class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">+ Import JSON</button>
      </div>
    </div>

    <!-- Import form -->
    <template x-if="wfNewFormOpen">
      <div class="bg-gray-900 border border-gray-700 rounded-xl p-4">
        <div class="text-xs font-semibold text-gray-400 mb-3">Import Workflow (JSON)</div>
        <textarea x-model="wfImportJson" rows="8"
          placeholder='{"name":"My Workflow","description":"...","steps":[...]}'
          class="w-full bg-gray-800 border border-gray-700 rounded-lg text-xs font-mono text-gray-200 p-3 focus:outline-none focus:border-[var(--accent)] resize-y"></textarea>
        <div class="flex justify-end gap-2 mt-3">
          <button @click="wfNewFormOpen=false; wfImportJson=''"
            class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white">Cancel</button>
          <button @click="importWorkflow()"
            class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">Import</button>
        </div>
        <template x-if="wfMsg">
          <div class="mt-2 text-xs px-3 py-2 rounded-lg"
            :class="wfMsg.ok ? 'bg-green-950 text-green-300 border border-green-800' : 'bg-red-950 text-red-300 border border-red-800'"
            x-text="wfMsg.text"></div>
        </template>
      </div>
    </template>

    <!-- Definitions list -->
    <div class="space-y-3">
      <template x-for="wf in workflows" :key="wf.id">
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 mb-1">
                <span class="font-medium text-sm text-white" x-text="wf.name"></span>
                <span class="text-[10px] px-1.5 py-0.5 rounded border border-gray-700 text-gray-500" x-text="'v'+wf.version"></span>
                <template x-for="tag in (wf.tags||[])" :key="tag">
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-[var(--accent-muted)] text-[var(--accent-light)]" x-text="tag"></span>
                </template>
              </div>
              <p class="text-xs text-gray-500" x-text="wf.description || '—'"></p>
              <p class="text-[10px] text-gray-700 mt-1" x-text="(wf.step_count||0)+' steps'"></p>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <button @click="selectedWorkflow=wf; loadWorkflowRuns(wf.id)"
                class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-300 hover:border-[var(--accent)] hover:text-[var(--accent)]">Runs</button>
              <button x-show="can('trigger_workflow')" @click="triggerWorkflow(wf.id)"
                class="text-xs px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90">▶ Run</button>
              <button x-show="can('manage_workflows')" @click="deleteWorkflow(wf.id)"
                class="text-xs px-2 py-1.5 rounded-lg border border-red-900 text-red-500 hover:bg-red-950">✕</button>
            </div>
          </div>
        </div>
      </template>
      <template x-if="workflows.length===0">
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-xs text-gray-600">
          No workflow definitions yet. Import a JSON definition to get started.
        </div>
      </template>
    </div>

    <!-- Run list for selected workflow -->
    <template x-if="selectedWorkflow">
      <div>
        <div class="flex items-center justify-between mb-3">
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider"
            x-text="'Runs — ' + selectedWorkflow.name"></div>
          <button @click="selectedWorkflow=null; wfRuns=[]" class="text-xs text-gray-500 hover:text-white">✕ Close</button>
        </div>
        <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <table class="w-full text-xs">
            <thead>
              <tr class="border-b border-gray-800 text-gray-500">
                <th class="text-left px-4 py-3 font-medium">Started</th>
                <th class="text-left px-4 py-3 font-medium">Status</th>
                <th class="text-left px-4 py-3 font-medium">Triggered By</th>
                <th class="text-left px-4 py-3 font-medium">Duration</th>
                <th class="text-left px-4 py-3 font-medium">Detail</th>
              </tr>
            </thead>
            <tbody>
              <template x-for="run in wfRuns" :key="run.id">
                <tr class="border-b border-gray-800/50 hover:bg-gray-800/30">
                  <td class="px-4 py-2 text-gray-600 whitespace-nowrap"
                    x-text="run.created_at ? new Date(run.created_at).toLocaleString(undefined,{dateStyle:'short',timeStyle:'short'}) : '—'"></td>
                  <td class="px-4 py-2">
                    <span class="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                      :class="{
                        'bg-yellow-950 text-yellow-400 border-yellow-900': run.status==='pending'||run.status==='running',
                        'bg-blue-950 text-blue-400 border-blue-900':   run.status==='paused',
                        'bg-green-950 text-green-400 border-green-900': run.status==='success',
                        'bg-red-950 text-red-400 border-red-900':      run.status==='failed',
                        'bg-gray-800 text-gray-500 border-gray-700':   run.status==='cancelled',
                      }" x-text="run.status"></span>
                  </td>
                  <td class="px-4 py-2 text-gray-500 font-mono" x-text="(run.triggered_by||'system').slice(0,12)"></td>
                  <td class="px-4 py-2 text-gray-600"
                    x-text="(run.started_at && run.finished_at) ? Math.round((run.finished_at-run.started_at)/1000)+'s' : (run.started_at ? 'running…' : '—')"></td>
                  <td class="px-4 py-2">
                    <button @click="loadRunDetail(run.id)"
                      class="text-xs text-[var(--accent)] hover:opacity-80">Steps ›</button>
                  </td>
                </tr>
              </template>
              <template x-if="wfRuns.length===0">
                <tr><td colspan="5" class="px-4 py-6 text-center text-gray-600">No runs yet</td></tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- Step detail for a selected run -->
    <template x-if="selectedRun">
      <div>
        <div class="flex items-center justify-between mb-3">
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider"
            x-text="'Steps — run ' + selectedRun.run.id.slice(-8)"></div>
          <button @click="selectedRun=null" class="text-xs text-gray-500 hover:text-white">✕ Close</button>
        </div>
        <div class="space-y-2">
          <template x-for="step in selectedRun.steps" :key="step.id">
            <div class="bg-gray-900 border border-gray-800 rounded-xl p-3">
              <div class="flex items-center justify-between gap-2 mb-1">
                <div class="flex items-center gap-2">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-mono border border-gray-700 text-gray-400" x-text="step.step_type"></span>
                  <span class="text-sm text-white font-medium" x-text="step.step_name"></span>
                  <span class="text-[10px] text-gray-600 font-mono" x-text="step.step_id"></span>
                </div>
                <span class="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                  :class="{
                    'bg-gray-800 text-gray-500 border-gray-700':         step.status==='pending',
                    'bg-yellow-950 text-yellow-400 border-yellow-900':   step.status==='running',
                    'bg-green-950 text-green-400 border-green-900':      step.status==='success',
                    'bg-red-950 text-red-400 border-red-900':            step.status==='failed',
                    'bg-gray-700 text-gray-400 border-gray-600':         step.status==='skipped',
                    'bg-blue-950 text-blue-400 border-blue-900':         step.status==='waiting_approval',
                    'bg-gray-800 text-gray-600 border-gray-700':         step.status==='cancelled',
                  }" x-text="step.status"></span>
              </div>
              <template x-if="step.output_summary">
                <p class="text-[11px] text-gray-500 mt-1 whitespace-pre-wrap" x-text="step.output_summary.slice(0,400)+(step.output_summary.length>400?'…':'')"></p>
              </template>
              <template x-if="step.error">
                <p class="text-[11px] text-red-400 mt-1" x-text="step.error"></p>
              </template>
              <template x-if="step.status==='waiting_approval' && can('decide_workflow_approvals')">
                <div class="flex gap-2 mt-2">
                  <button @click="decideWorkflowApproval(step.approval_id,'approve')"
                    class="px-2 py-1 rounded text-[10px] bg-green-900 text-green-300 hover:bg-green-800 border border-green-800">Approve</button>
                  <button @click="decideWorkflowApproval(step.approval_id,'reject')"
                    class="px-2 py-1 rounded text-[10px] bg-red-950 text-red-400 hover:bg-red-900 border border-red-900">Reject</button>
                </div>
              </template>
              <div class="flex items-center gap-4 mt-1.5 text-[10px] text-gray-700">
                <template x-if="step.started_at">
                  <span x-text="new Date(step.started_at).toLocaleTimeString()"></span>
                </template>
                <template x-if="step.started_at && step.finished_at">
                  <span x-text="Math.round((step.finished_at-step.started_at)/1000)+'s'"></span>
                </template>
                <template x-if="step.parallel_group">
                  <span class="text-gray-600">⫶ <span x-text="step.parallel_group"></span></span>
                </template>
              </div>
            </div>
          </template>
        </div>
        <template x-if="selectedRun.run.status==='running'||selectedRun.run.status==='paused'">
          <div class="mt-3 flex justify-end">
            <button @click="cancelRun(selectedRun.run.id)"
              class="text-xs px-3 py-1.5 rounded-lg border border-red-900 text-red-400 hover:bg-red-950">Cancel Run</button>
          </div>
        </template>
      </div>
    </template>

  </div>

  <!-- ── Runs Tab ─────────────────────────────────────────────────────── -->
  <div x-show="tab==='runs'" x-cloak class="p-6 max-w-5xl mx-auto space-y-6">

    <!-- Header row -->
    <div class="flex items-center justify-between">
      <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Agent Runs</div>
      <div class="flex items-center gap-3">
        <select x-model="agentRunsStatusFilter" @change="agentRunsOffset=0; loadAgentRuns()"
          class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-300 focus:outline-none focus:border-[var(--accent)]">
          <option value="">All statuses</option>
          <option value="running">Running</option>
          <option value="success">Success</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <button @click="loadAgentRuns()" class="text-xs text-[var(--accent)] hover:opacity-80 flex items-center gap-1"><span>&#8635;</span> Refresh</button>
      </div>
    </div>

    <!-- Runs list table -->
    <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      <table class="w-full text-xs">
        <thead>
          <tr class="border-b border-gray-800 text-gray-500">
            <th class="text-left px-4 py-3 font-medium">Started</th>
            <th class="text-left px-4 py-3 font-medium">Status</th>
            <th class="text-left px-4 py-3 font-medium">User</th>
            <th class="text-left px-4 py-3 font-medium">Model</th>
            <th class="text-left px-4 py-3 font-medium">Tools</th>
            <th class="text-left px-4 py-3 font-medium">Duration</th>
            <th class="text-left px-4 py-3 font-medium">Detail</th>
          </tr>
        </thead>
        <tbody>
          <template x-for="run in agentRuns" :key="run.id">
            <tr class="border-b border-gray-800/50 hover:bg-gray-800/30 cursor-pointer"
                @click="loadAgentRunDetail(run.id)">
              <td class="px-4 py-2 text-gray-600 whitespace-nowrap"
                x-text="run.created_at ? new Date(run.created_at).toLocaleString(undefined,{dateStyle:'short',timeStyle:'short'}) : '—'"></td>
              <td class="px-4 py-2">
                <span class="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                  :class="_runStatusClass(run.status)"
                  x-text="run.status"></span>
              </td>
              <td class="px-4 py-2 text-gray-400" x-text="run.user_id || '—'"></td>
              <td class="px-4 py-2 text-gray-400 font-mono truncate max-w-[10rem]" x-text="run.model || '—'"></td>
              <td class="px-4 py-2 text-gray-500"
                x-text="(run.tool_sequence && run.tool_sequence.length) ? run.tool_sequence.length + ' calls' : '0'"></td>
              <td class="px-4 py-2 text-gray-500" x-text="_runDuration(run)"></td>
              <td class="px-4 py-2">
                <button @click.stop="loadAgentRunDetail(run.id)"
                  class="text-[10px] px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-[var(--accent)] hover:text-[var(--accent)]">View</button>
              </td>
            </tr>
          </template>
          <template x-if="!agentRunsLoading && agentRuns.length===0">
            <tr><td colspan="7" class="px-4 py-8 text-center text-gray-600">No runs recorded yet.</td></tr>
          </template>
          <template x-if="agentRunsLoading && agentRuns.length===0">
            <tr><td colspan="7" class="px-4 py-8 text-center text-gray-600">Loading…</td></tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- Pagination -->
    <div class="flex items-center gap-3 text-xs text-gray-500" x-show="agentRunsTotal > agentRunsLimit">
      <button @click="agentRunsOffset=Math.max(0,agentRunsOffset-agentRunsLimit); loadAgentRuns()" :disabled="agentRunsOffset===0"
        class="px-2 py-1 rounded border border-gray-700 disabled:opacity-40">← Prev</button>
      <span x-text="'Showing ' + (agentRunsOffset+1) + '–' + Math.min(agentRunsOffset+agentRunsLimit,agentRunsTotal) + ' of ' + agentRunsTotal"></span>
      <button @click="agentRunsOffset+=agentRunsLimit; loadAgentRuns()" :disabled="agentRunsOffset+agentRunsLimit>=agentRunsTotal"
        class="px-2 py-1 rounded border border-gray-700 disabled:opacity-40">Next →</button>
    </div>

    <!-- Run Detail Panel -->
    <template x-if="selectedAgentRun">
      <div class="bg-gray-900 border border-gray-700 rounded-xl p-5 space-y-4">
        <!-- Header -->
        <div class="flex items-start justify-between gap-3">
          <div>
            <div class="flex items-center gap-2 mb-1">
              <span class="text-sm font-semibold text-white font-mono" x-text="selectedAgentRun.id"></span>
              <span class="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                :class="_runStatusClass(selectedAgentRun.status)"
                x-text="selectedAgentRun.status"></span>
            </div>
            <div class="text-xs text-gray-500 flex items-center gap-4 flex-wrap">
              <span x-text="selectedAgentRun.model || 'unknown model'"></span>
              <span x-show="selectedAgentRun.api_calls" x-text="selectedAgentRun.api_calls + ' API calls'"></span>
              <span x-text="_runDuration(selectedAgentRun)"></span>
            </div>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <button @click="cloneAgentRun(selectedAgentRun.id)"
              title="Clone this run — prefill chat with the same message"
              class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-300 hover:border-[var(--accent)] hover:text-[var(--accent)]">Clone ↗</button>
            <button @click="selectedAgentRun=null" class="text-gray-500 hover:text-white text-lg">✕</button>
          </div>
        </div>

        <!-- User message -->
        <div x-show="selectedAgentRun.user_message">
          <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">User Message</div>
          <div class="text-xs text-gray-300 bg-gray-800 rounded-lg p-3 whitespace-pre-wrap font-mono max-h-32 overflow-y-auto"
            x-text="selectedAgentRun.user_message"></div>
        </div>

        <!-- Tool timeline -->
        <div x-show="selectedAgentRun.tool_detail && selectedAgentRun.tool_detail.length">
          <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-2">Tool Timeline</div>
          <div class="space-y-1">
            <template x-for="(t, i) in selectedAgentRun.tool_detail" :key="i">
              <div class="flex items-center gap-2 text-xs">
                <span class="text-gray-700 w-5 text-right shrink-0" x-text="i+1+'.'"></span>
                <span class="font-mono text-[var(--accent)] shrink-0" x-text="t.tool || t"></span>
                <span class="text-gray-600 truncate" x-text="t.preview || ''"></span>
              </div>
            </template>
          </div>
        </div>

        <!-- Output summary -->
        <div x-show="selectedAgentRun.output_summary">
          <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Output Summary</div>
          <div class="text-xs text-gray-400 bg-gray-800 rounded-lg p-3 whitespace-pre-wrap max-h-40 overflow-y-auto"
            x-text="selectedAgentRun.output_summary"></div>
        </div>

        <!-- Error -->
        <div x-show="selectedAgentRun.error">
          <div class="text-[10px] text-red-600 uppercase tracking-wider mb-1">Error</div>
          <div class="text-xs text-red-400 bg-red-950/40 border border-red-900 rounded-lg p-3 whitespace-pre-wrap"
            x-text="selectedAgentRun.error"></div>
        </div>

        <!-- Policy snapshot -->
        <div x-show="selectedAgentRun.action_policy_snapshot">
          <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Policy Snapshot</div>
          <div class="text-xs text-gray-600 font-mono bg-gray-800 rounded-lg p-3 whitespace-pre-wrap"
            x-text="JSON.stringify(JSON.parse(selectedAgentRun.action_policy_snapshot||'{}'), null, 2)"></div>
        </div>
      </div>
    </template>

  </div>

  <!-- ── Activity history modal ─────────────────────────────────────── -->
  <div x-show="activityModalOpen" x-cloak
    class="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4"
    @keydown.escape.window="activityModalOpen=false">
    <div class="absolute inset-0 bg-black/70 backdrop-blur-sm" @click="activityModalOpen=false"></div>
    <div class="relative w-full max-w-xl rounded-2xl border border-gray-700 flex flex-col"
      style="background:var(--surface-col,#111827);max-height:80vh">
      <div class="flex items-center justify-between px-5 py-4 border-b border-gray-800 shrink-0">
        <div class="text-sm font-semibold text-white">Activity History</div>
        <button @click="activityModalOpen=false"
          class="text-gray-500 hover:text-white text-xl leading-none w-7 h-7 flex items-center justify-center rounded hover:bg-gray-800 transition-colors">×</button>
      </div>
      <div class="overflow-y-auto flex-1 px-4 py-3 space-y-2">
        <template x-for="r in [...(status.recent_sessions||[])].reverse().slice(0,50)" :key="r.session_key + r.ended_at">
          <div class="bg-gray-900/80 border border-gray-800 rounded-xl p-3.5 cursor-pointer hover:border-gray-600 transition-colors"
            @click="activityModalOpen=false; navigateToSession(r)">
            <div class="flex items-center gap-2 mb-2">
              <span x-text="platformIcon(r.platform)" class="text-sm"></span>
              <span class="text-xs text-gray-400 capitalize font-medium" x-text="r.platform"></span>
              <span class="text-gray-700">·</span>
              <span class="text-xs text-gray-500"
                x-text="r.ended_at ? new Date(r.ended_at*1000).toLocaleString(undefined,{dateStyle:'short',timeStyle:'short'}) : ''"></span>
              <span class="ml-auto text-xs text-gray-600" x-text="fmtAgo(r.ended_at)"></span>
              <template x-if="r.platform==='local'">
                <span class="text-xs text-[var(--accent)] opacity-60">→ open chat</span>
              </template>
            </div>
            <div class="text-xs text-gray-300 leading-relaxed" x-text="r.snippet || '(no response)'"></div>
            <div class="mt-1.5 text-xs text-gray-700"
              x-text="(r.tool_count || 0) + ' tools'"></div>
          </div>
        </template>
        <template x-if="!(status.recent_sessions||[]).length">
          <div class="text-center text-gray-600 text-xs py-10">No activity recorded yet</div>
        </template>
      </div>
    </div>
  </div>

</div>


<script>
function getCsrfToken() {
  return document.cookie.split(';')
    .map(c => c.trim())
    .find(c => c.startsWith('csrf_token='))
    ?.split('=')[1] ?? '';
}

function app() {
  return {
    tab: localStorage.getItem('hermes_tab') || 'sessions',
    status: { uptime_s: 0, instance_name: 'Hermes', active_sessions: [], recent_sessions: [] },
    routerState: { providers: {}, routes: {}, grafana_url: 'http://192.168.1.253:3200' },
    canary: { active: false },
    chats: [],
    activeChatId: null,
    chatInput: '',
    chatRenderMode: 'markdown',
    micRecording: false,
    micTranscribing: false,
    micSuccess: false,
    micCountdown: 90,
    _micRecorder: null,
    _micChunks: [],
    _micTimer: null,
    loadingChatId: null,
    lastResponseAt: null,
    isWakingUp: false,
    lastRefresh: '',
    clientNow: Date.now(),
    chatAgents: [],
    manualAgents: [],
    activeInstanceId: 'self',
    showAddInstance: false,
    newInstanceName: '',
    newInstanceUrl: '',
    // k8s instance management
    clusterInstances: [],
    clusterRes: {},
    instanceQueue: [],
    newInstanceRequester: '',
    instanceUserList: [],
    instanceUserSearch: '',
    instanceUserDropdownOpen: false,
    spawnTemplates: [],
    instanceSpawning: false,
    instanceMsg: null,
    // soul registry + spawn flow
    souls: [],
    soulsLoaded: false,
    selectedSoul: null,
    inspectedSoul: null,
    spawnStep: 1,
    spawnOptionalEnabled: {},
    _now: Math.floor(Date.now() / 1000),
    spawnModelAlias: 'balanced',
    spawnMachineOverride: '',
    spawnRoutePreview: null,
    spawnRoutePreviewLoading: false,
    // auth
    authUser: null,
    authPermissions: [],
    isCanary: window.__LOGOS__?.isCanary || false,
    // routing
    routingTab: localStorage.getItem('hermes_routing_tab') || 'machines',
    // admin
    adminTab: localStorage.getItem('hermes_admin_tab') || 'users',
    adminSetPwUserId: null,
    adminSetPwVal: '',
    adminSetPwMsg: null,
    activityModalOpen: false,
    prevActiveSessions: [],
    completedSessions: {},
    adminRoutingLogLoading: false,
    adminUsers: [],
    adminMachines: [],
    adminPolicies: [],
    adminAuditLogs: [],
    adminRoutingLog: [],
    adminRoutingLogTotal: 0,
    adminRoutingLogPage: 1,
    adminRoutingLogUserFilter: '',
    approvalRequests: [],
    approvalsStatusFilter: 'pending',
    adminMsg: null,
    // workflows
    workflows: [],
    wfRuns: [],
    selectedWorkflow: null,
    selectedRun: null,
    wfNewFormOpen: false,
    wfImportJson: '',
    wfMsg: null,
    // agent runs
    agentRuns: [],
    agentRunsTotal: 0,
    agentRunsOffset: 0,
    agentRunsLimit: 50,
    agentRunsStatusFilter: '',
    agentRunsLoading: false,
    selectedAgentRun: null,
    agentRunsMsg: null,
    clonePayload: null,
    adminUserFormOpen: false,
    adminUserForm: {},
    adminMachineFormOpen: false,
    adminMachineForm: {},
    adminMachineEditId: null,
    adminMachineEditForm: {},
    adminPolicyFormOpen: false,
    adminPolicyForm: {},
    adminPolicyEditId: null,
    adminPolicyEditForm: {},
    // setup wizard
    setupWizardDismissed: !!localStorage.getItem('hermes_wizard_dismissed'),
    setupWizardStep: null,
    setupWizardEndpoint: '',
    setupWizardLoading: false,
    // routing sections (collapsible)
    modelMapOpen: false,
    debugOpen: false,
    benchmarkOpen: false,
    benchmarkRunning: false,
    benchmarkResult: null,
    benchmarkNPrompts: 3,
    modelsLiveLoading: false,
    modelsLiveResult: null,
    // routing debug
    routeDebugUserId: '',
    routeDebugModel: 'balanced',
    routeDebugLoading: false,
    routeDebugResult: null,
    // theme
    theme: localStorage.getItem('hermes_theme') || 'midnight',
    themePickerOpen: false,
    accountMenuOpen: false,
    changePwOpen: false,
    changePwCurrent: '',
    changePwNew: '',
    changePwConfirm: '',
    changePwError: '',
    changePwSuccess: false,
    changePwLoading: false,
    themes: [
      { id:'midnight', name:'Midnight', mood:'Calm · default',   base:'#030712', surface:'#111827', accent:'#6366f1' },
      { id:'crimson',  name:'Crimson',  mood:'Bold · high alert', base:'#060308', surface:'#110a10', accent:'#ef4444' },
      { id:'terminal', name:'Terminal', mood:'Operational',       base:'#010f06', surface:'#071a0d', accent:'#22c55e' },
      { id:'dusk',     name:'Dusk',     mood:'Ambient · creative',base:'#060410', surface:'#0f0a1e', accent:'#a855f7' },
    ],

    get activeInstance() {
      return this.chatAgents.find(i => i.id === this.activeInstanceId) || this.chatAgents[0] || {id:'self', name:'Hermes', url:''};
    },
    get instanceUrl() {
      return (this.activeInstance.url || '').replace(/\/$/, '');
    },
    get chatLoading() {
      return this.loadingChatId === this.activeChatId && this.loadingChatId !== null;
    },
    get activeChat() {
      return this.chats.find(c => c.id === this.activeChatId) || null;
    },
    get chatMessages() {
      return this.activeChat ? this.activeChat.messages : [];
    },
    get webSession() {
      const id = this.activeChatId || '';
      return (this.status.active_sessions || []).find(
        s => s.session_key && s.session_key.includes(id)
      ) || null;
    },
    get otherSessions() {
      const id = this.activeChatId || '';
      return (this.status.active_sessions || []).filter(
        s => !s.session_key || !s.session_key.includes(id)
      );
    },
    async init() {
      // Apply saved theme immediately; watch for reactive changes
      document.documentElement.setAttribute('data-theme', this.theme);
      this.$watch('theme',      val => document.documentElement.setAttribute('data-theme', val));
      this.$watch('tab',            val => localStorage.setItem('hermes_tab', val));
      this.$watch('routingTab',     val => localStorage.setItem('hermes_routing_tab', val));
      this.$watch('adminTab',       val => localStorage.setItem('hermes_admin_tab', val));
      // chatRenderMode intentionally not persisted — always defaults to markdown on load

      await this.loadAuth();

      // Validate restored tabs against actual permissions — fall back if needed.
      const routingAllowed = this.can('manage_machines') || this.can('manage_profiles') || this.can('view_routing_debug');
      const adminAllowed   = this.can('manage_users') || this.can('view_audit_logs');
      if (this.tab === 'routing' && !routingAllowed) this.tab = 'sessions';
      if (this.tab === 'admin'   && !adminAllowed)   this.tab = 'sessions';
      if (this.routingTab === 'machines' && !this.can('manage_machines'))
        this.routingTab = this.can('manage_profiles') ? 'profiles' : 'debug';
      if (this.adminTab === 'users' && !this.can('manage_users'))
        this.adminTab = 'audit';
      // Trigger initial data load for whichever tab is active on restore
      if (this.tab === 'routing') this.loadRoutingData();
      if (this.tab === 'admin') {
        if (this.adminTab === 'routing-log') this.loadAdminRoutingLog();
        else if (this.adminTab === 'approvals') this.loadApprovals();
        else this.loadAdminData();
      }
      if (this.tab === 'workflows') this.loadWorkflows();
      if (this.tab === 'runs') this.loadAgentRuns();

      this.manualAgents = this._loadManualAgents();
      this._buildChatAgents();
      this.chats = this._loadChats();
      if (this.chats.length === 0) {
        this.newChat();
      } else {
        this.activeChatId = this.chats[0].id;
      }
      // Scroll to bottom after Alpine renders the restored messages
      this.$nextTick(() => this._scrollChat());
      await Promise.all([this.loadStatus(), this.loadRouterState(), this.loadCanary(), this.loadInstances(), this.loadSpawnTemplates()]);
      // Pre-fill requester for non-admins; load user list for admins
      if (this.authUser) {
        if (this.authUser.role === 'admin') {
          try {
            const r = await fetch('/admin/users?limit=100');
            const d = await r.json();
            this.instanceUserList = (d.users || []).map(u => u.display_name || u.username).filter(Boolean);
          } catch(e) {}
        } else {
          this.newInstanceRequester = this.authUser.display_name || this.authUser.username || '';
        }
      }
      setInterval(() => this.loadStatus(), 4000);
      setInterval(() => this.loadRouterState(), 15000);
      setInterval(() => this.loadCanary(), 10000);
      setInterval(() => { this.clientNow = Date.now(); }, 1000);
      setInterval(() => { this._now = Math.floor(Date.now() / 1000); }, 15000);
      // Silently refresh access token before it expires.
      // Fire immediately on load so a near-expiry token is renewed right away,
      // then keep refreshing every 12 minutes (access token TTL is 15 minutes).
      this._refreshToken();
      setInterval(() => this._refreshToken(), 12 * 60 * 1000);
    },

    async loadAuth() {
      try {
        const r = await fetch('/auth/me', { credentials: 'same-origin' });
        if (r.status === 401 || r.status === 404) { window.location.href = '/login'; return; }
        if (r.ok) {
          const d = await r.json();
          this.authUser = d.user;
          this.authPermissions = d.permissions || [];
          // Sync saved theme with server-side setting
          if (d.settings?.ui_theme) {
            this.theme = d.settings.ui_theme;
            document.documentElement.setAttribute('data-theme', this.theme);
          }
        }
      } catch(e) { console.warn('loadAuth failed', e); }
    },

    async _refreshToken() {
      try {
        await fetch('/auth/refresh', { method: 'POST', credentials: 'same-origin' });
      } catch(e) { /* silent */ }
    },

    can(permission) {
      return this.authPermissions.includes(permission);
    },

    async logout() {
      try {
        await fetch('/auth/logout', {
          method: 'POST',
          headers: { 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
        });
      } finally {
        window.location.href = '/login';
      }
    },
    async resetSetup() {
      if (!confirm('This will re-enable the setup wizard. You will be redirected to /setup on next login. Continue?')) return;
      const r = await fetch('/api/setup/reset', {
        method: 'POST',
        headers: { 'X-CSRF-Token': getCsrfToken() },
        credentials: 'same-origin',
      });
      if (r.ok) {
        alert('Setup wizard re-enabled. It will appear on next login.');
        this.accountMenuOpen = false;
      } else {
        alert('Failed to reset setup state.');
      }
    },

    async submitChangePassword() {
      this.changePwError = '';
      this.changePwSuccess = false;
      if (this.changePwNew !== this.changePwConfirm) {
        this.changePwError = 'New passwords do not match.';
        return;
      }
      if (this.changePwNew.length < 8) {
        this.changePwError = 'Password must be at least 8 characters.';
        return;
      }
      this.changePwLoading = true;
      try {
        const r = await fetch('/users/me', {
          method: 'PATCH',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          body: JSON.stringify({ current_password: this.changePwCurrent, new_password: this.changePwNew }),
        });
        const d = await r.json();
        if (!r.ok) {
          const msgs = {
            invalid_current_password: 'Current password is incorrect.',
            password_too_short: 'Password must be at least 8 characters.',
          };
          this.changePwError = msgs[d.error] || d.error || 'Something went wrong.';
        } else {
          this.changePwSuccess = true;
          this.changePwCurrent = '';
          this.changePwNew = '';
          this.changePwConfirm = '';
          setTimeout(() => { this.changePwOpen = false; this.changePwSuccess = false; }, 1500);
        }
      } finally {
        this.changePwLoading = false;
      }
    },

    // ── Agent selector ────────────────────────────────────────────

    _loadManualAgents() {
      try {
        const stored = JSON.parse(localStorage.getItem('hermes_manual_agents') || '[]');
        return Array.isArray(stored) ? stored : [];
      } catch(_) { return []; }
    },

    _saveManualAgents() {
      try { localStorage.setItem('hermes_manual_agents', JSON.stringify(this.manualAgents)); } catch(_) {}
    },

    _buildChatAgents() {
      const self = {id: 'self', name: this.status.instance_name || 'Hermes', url: '', source: 'self', editable: false};
      const k8s = (this.clusterInstances || [])
        .filter(i => i.node_port)
        .map(i => ({
          id:           'k8s-' + i.name,
          name:         i.instance_name,
          url:          'http://' + window.location.hostname + ':' + i.node_port,
          source:       'k8s',
          editable:     false,
          soul:         i.soul         || null,
          model_alias:  i.model_alias  || null,
          machine_name: i.machine_name || null,
          k8s_status:   i.status       || null,
        }));
      this.chatAgents = [self, ...k8s, ...this.manualAgents];
    },

    switchInstance(id) {
      this.activeInstanceId = id;
      this.status = { uptime_s: 0, instance_name: 'Hermes', active_sessions: [] };
      this.canary = { active: false };
      this.loadStatus();
      this.loadCanary();
    },

    addInstance() {
      const name = this.newInstanceName.trim();
      const url = this.newInstanceUrl.trim().replace(/\/$/, '');
      if (!name || !url) return;
      const id = 'manual-' + Math.random().toString(36).slice(2, 8);
      this.manualAgents = [...this.manualAgents, {id, name, url, editable: true}];
      this._saveManualAgents();
      this._buildChatAgents();
      this.newInstanceName = '';
      this.newInstanceUrl = '';
      this.showAddInstance = false;
    },

    removeInstance(id) {
      this.manualAgents = this.manualAgents.filter(i => i.id !== id);
      this._saveManualAgents();
      this._buildChatAgents();
      if (this.activeInstanceId === id) this.switchInstance('self');
    },

    // ── Chat persistence ──────────────────────────────────────────

    _loadChats() {
      try {
        const stored = JSON.parse(localStorage.getItem('hermes_chats') || '[]');
        return Array.isArray(stored) ? stored : [];
      } catch(_) { return []; }
    },

    _saveChats() {
      try { localStorage.setItem('hermes_chats', JSON.stringify(this.chats)); } catch(_) {}
    },

    newChat() {
      const id = 'admin-' + Math.random().toString(36).slice(2, 8);
      const chat = {
        id,
        name: 'New conversation',
        messages: [],
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      this.chats = [chat, ...this.chats];
      this._saveChats();
      this.activeChatId = id;
      this._scrollChat();
    },

    switchChat(id) {
      this.activeChatId = id;
      this._scrollChat();
    },

    deleteChat(id) {
      this.chats = this.chats.filter(c => c.id !== id);
      this._saveChats();
      if (this.activeChatId === id) {
        if (this.chats.length > 0) {
          this.activeChatId = this.chats[0].id;
        } else {
          this.newChat();
        }
      }
    },

    fmtChatTime(ts) {
      if (!ts) return '';
      const d = new Date(ts);
      const now = new Date();
      if (now.getDate() === d.getDate() && now - d < 86400000)
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      if (now - d < 172800000) return 'Yesterday';
      return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    },

    // ── Theme ─────────────────────────────────────────────────────

    setTheme(t) {
      // Temporarily enable transition overrides for a smooth theme switch
      document.body.classList.add('theme-transitioning');
      this.theme = t;
      localStorage.setItem('hermes_theme', t);
      setTimeout(() => document.body.classList.remove('theme-transitioning'), 380);
      // Persist to server so loadAuth() restores the correct theme on refresh
      fetch('/users/me', {
        method: 'PATCH', credentials: 'same-origin',
        headers: {'Content-Type':'application/json','X-CSRF-Token': getCsrfToken()},
        body: JSON.stringify({ui_theme: t}),
      }).catch(() => {});
    },

    // ── k8s Instances ─────────────────────────────────────────────

    async loadInstances() {
      try {
        const r = await fetch('/instances');
        const d = await r.json();
        this.clusterInstances = d.instances || [];
        this.clusterRes = d.resources || {};
        this.instanceQueue = d.queue || [];
        this._buildChatAgents();
      } catch(e) {
        this.clusterRes = { _error: String(e) };
      }
    },

    async loadSpawnTemplates() {
      try {
        const r = await fetch('/spawn-templates', { credentials: 'same-origin' });
        if (r.ok) this.spawnTemplates = await r.json();
      } catch(e) {}
    },

    async _saveSpawnTemplates() {
      try {
        await fetch('/spawn-templates', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
          body: JSON.stringify(this.spawnTemplates),
        });
      } catch(e) {}
    },

    async requestInstance() {
      const name = this.newInstanceRequester.trim();
      if (!name) return;
      this.instanceSpawning = true;
      this.instanceMsg = null;
      try {
        const addedToolsets = Object.entries(this.spawnOptionalEnabled)
          .filter(([, v]) => v).map(([k]) => k);
        const r = await fetch('/instances', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
          body: JSON.stringify({
            requester: name,
            soul_slug: this.selectedSoul?.slug || 'general',
            tool_overrides: { add: addedToolsets },
            model_alias: this.spawnModelAlias,
            machine_id: this.spawnMachineOverride || null,
          }),
        });
        const rawText = await r.text();
        let d;
        try { d = JSON.parse(rawText); }
        catch(_) {
          this.instanceMsg = { ok: false, text: 'Server error (' + r.status + '): ' + rawText.slice(0, 200) };
          return;
        }
        if (d.error) {
          this.instanceMsg = { ok: false, text: d.message || d.error };
        } else if (d.status === 'queued') {
          this.instanceMsg = { ok: false, text: '⏳ Queued — cluster resources are low. Will retry automatically.' };
        } else if (d.status === 'exists') {
          this.instanceMsg = { ok: false, text: 'An instance for that name already exists.' };
        } else {
          this.instanceMsg = { ok: true, text: '✓ Instance requested: ' + d.instance_name + (d.node_port ? ' · port ' + d.node_port : ' · starting\u2026') };
          // Save as a quick-spawn template (deduplicated by requester+soul+model)
          const tpl = {
            id: Date.now(),
            requester: name,
            soul_slug: this.selectedSoul?.slug || 'general',
            soul_name: this.selectedSoul?.name || 'General',
            model_alias: this.spawnModelAlias,
            optional_toolsets: addedToolsets,
          };
          this.spawnTemplates = [tpl, ...this.spawnTemplates.filter(t =>
            !(t.requester === tpl.requester && t.soul_slug === tpl.soul_slug && t.model_alias === tpl.model_alias)
          )].slice(0, 12);
          await this._saveSpawnTemplates();
          this.newInstanceRequester = '';
          this.spawnStep = 1;
          this.selectedSoul = null;
          this.inspectedSoul = null;
          this.spawnOptionalEnabled = {};
          this.spawnRoutePreview = null;
          this.spawnMachineOverride = '';
          this.spawnModelAlias = 'balanced';
        }
        await this.loadInstances();
      } catch(e) {
        this.instanceMsg = { ok: false, text: 'Error: ' + e };
      } finally {
        this.instanceSpawning = false;
      }
    },

    async spawnFromTemplate(t) {
      this.instanceSpawning = true;
      this.instanceMsg = null;
      try {
        const r = await fetch('/instances', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
          body: JSON.stringify({
            requester: t.requester,
            soul_slug: t.soul_slug,
            tool_overrides: { add: t.optional_toolsets || [] },
            model_alias: t.model_alias,
            machine_id: null,
          }),
        });
        const rawText2 = await r.text();
        let d;
        try { d = JSON.parse(rawText2); }
        catch(_) {
          this.instanceMsg = { ok: false, text: 'Server error (' + r.status + '): ' + rawText2.slice(0, 200) };
          return;
        }
        if (d.error) {
          this.instanceMsg = { ok: false, text: d.message || d.error };
        } else if (d.status === 'queued') {
          this.instanceMsg = { ok: false, text: '\u23f3 Queued \u2014 cluster resources are low. Will retry automatically.' };
        } else if (d.status === 'exists') {
          this.instanceMsg = { ok: false, text: 'An instance for that name already exists.' };
        } else {
          this.instanceMsg = { ok: true, text: '\u2713 ' + d.instance_name + (d.node_port ? ' \u00b7 port ' + d.node_port : ' \u00b7 starting\u2026') };
          // Bump template to front
          this.spawnTemplates = [t, ...this.spawnTemplates.filter(x => x.id !== t.id)];
          await this._saveSpawnTemplates();
        }
        await this.loadInstances();
      } catch(e) {
        this.instanceMsg = { ok: false, text: 'Error: ' + e };
      } finally {
        this.instanceSpawning = false;
      }
    },

    async removeSpawnTemplate(id) {
      this.spawnTemplates = this.spawnTemplates.filter(t => t.id !== id);
      try {
        await fetch('/spawn-templates/' + id, {
          method: 'DELETE',
          headers: { 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
        });
      } catch(e) {}
    },

    async confirmDeleteInstance(name) {
      if (!confirm('Delete ' + name + ' and its PVC? This cannot be undone.')) return;
      try {
        await fetch('/instances/' + name, {
          method: 'DELETE',
          headers: { 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
        });
        await this.loadInstances();
      } catch(e) { alert('Delete failed: ' + e); }
    },

    fmtBytes(b) {
      if (!b) return '0';
      if (b >= 1024**3) return (b/1024**3).toFixed(1) + ' GiB';
      if (b >= 1024**2) return (b/1024**2).toFixed(0) + ' MiB';
      return (b/1024).toFixed(0) + ' KiB';
    },

    // ── Soul registry ─────────────────────────────────────────────

    async loadSouls() {
      if (this.soulsLoaded) return;
      try {
        const r = await fetch('/souls');
        const d = await r.json();
        this.souls = d.souls || [];
        this.soulsLoaded = true;
      } catch(e) { console.error('loadSouls failed', e); }
    },

    inspectSoul(soul) {
      this.inspectedSoul = (this.inspectedSoul?.slug === soul.slug) ? null : soul;
    },

    selectSoul(soul) {
      this.selectedSoul = soul;
      this.spawnOptionalEnabled = {};
      for (const ts of (soul.toolsets?.optional || [])) {
        this.spawnOptionalEnabled[ts] = false;
      }
      this.spawnStep = 2;
      this.inspectedSoul = null;
      this.instanceMsg = null;
      this.spawnRoutePreview = null;
      // Load machines for the override selector if admin/operator
      const role = this.authUser?.role;
      if ((role === 'admin' || role === 'operator') && this.adminMachines.length === 0) {
        this.loadAdminMachines();
      }
      this.loadSpawnRoutePreview();
    },

    backToSoulPicker() {
      this.spawnStep = 1;
      this.selectedSoul = null;
      this.inspectedSoul = null;
      this.spawnOptionalEnabled = {};
      this.instanceMsg = null;
      this.spawnRoutePreview = null;
      this.spawnMachineOverride = '';
    },

    spawnAliasGroups() {
      const classOrder = [
        { cls: 'lightweight', label: 'Lightweight — fast replies' },
        { cls: 'general',     label: 'General — balanced quality' },
        { cls: 'coding',      label: 'Coding — code-optimised' },
        { cls: 'reasoning',   label: 'Reasoning — deep thinking' },
        { cls: 'vision',      label: 'Vision' },
        { cls: 'embedding',   label: 'Embedding' },
      ];
      const routes = this.routerState.routes || {};
      const classes = this.routerState.route_model_classes || {};
      return classOrder
        .map(g => ({
          cls: g.cls,
          label: g.label,
          aliases: Object.keys(routes).filter(a => classes[a] === g.cls),
        }))
        .filter(g => g.aliases.length > 0);
    },

    async loadSpawnRoutePreview() {
      this.spawnRoutePreviewLoading = true;
      this.spawnRoutePreview = null;
      try {
        const params = new URLSearchParams({ model: this.spawnModelAlias });
        if (this.spawnMachineOverride) params.set('machine_id', this.spawnMachineOverride);
        const r = await fetch('/routing/preview?' + params, { credentials: 'include' });
        if (r.ok) this.spawnRoutePreview = await r.json();
      } catch(e) {
        this.spawnRoutePreview = { error: e.message, machine: null };
      } finally {
        this.spawnRoutePreviewLoading = false;
      }
    },

    // ── Server polling ────────────────────────────────────────────

    async loadStatus() {
      const prevKeys = new Set(this.prevActiveSessions.map(s => s.session_key));
      try {
        const r = await fetch(this.instanceUrl + '/status');
        this.status = await r.json();
        this.lastRefresh = new Date().toLocaleTimeString();
        // Keep self label in sync with instance name
        if (this.activeInstanceId === 'self') this._buildChatAgents();
        const nowKeys = new Set((this.status.active_sessions||[]).map(s => s.session_key));
        for (const s of this.prevActiveSessions) {
          if (!nowKeys.has(s.session_key) && !this.completedSessions[s.session_key]) {
            this.completedSessions = {...this.completedSessions, [s.session_key]: {...s, fading: false}};
            setTimeout(() => {
              if (this.completedSessions[s.session_key])
                this.completedSessions[s.session_key] = {...this.completedSessions[s.session_key], fading: true};
            }, 5000);
            setTimeout(() => {
              const cs = {...this.completedSessions};
              delete cs[s.session_key];
              this.completedSessions = cs;
            }, 5500);
          }
        }
        this.prevActiveSessions = [...(this.status.active_sessions||[])];
      } catch(e) { console.error('status poll failed', e); }
    },

    async loadRouterState() {
      try {
        const r = await fetch('/proxy/state');
        if (r.ok) this.routerState = await r.json();
      } catch(e) { console.error('router state failed', e); }
    },

    async loadCanary() {
      try {
        const r = await fetch(this.instanceUrl + '/canary/status');
        if (r.ok) this.canary = await r.json();
      } catch(e) { this.canary = { active: false }; }
    },

    // ── Helpers ───────────────────────────────────────────────────

    elapsed(toolStartedAt) {
      if (!toolStartedAt) return 0;
      return Math.max(0, Math.floor((this.clientNow - toolStartedAt * 1000) / 1000));
    },

    fmtUptime(s) {
      if (!s) return '0s';
      if (s < 60) return s + 's';
      if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
      return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
    },

    fmtTokens(n) {
      if (!n) return '0';
      if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
      return String(n);
    },

    async toggleMic() {
      if (this.micRecording) {
        // Manual stop
        clearInterval(this._micTimer);
        this._micRecorder && this._micRecorder.stop();
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this._micChunks = [];
        const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
        this._micRecorder = recorder;
        recorder.ondataavailable = e => { if (e.data.size > 0) this._micChunks.push(e.data); };
        recorder.onstop = async () => {
          clearInterval(this._micTimer);
          stream.getTracks().forEach(t => t.stop());
          this.micRecording = false;
          this.micTranscribing = true;
          try {
            const blob = new Blob(this._micChunks, { type: 'audio/webm' });
            const fd = new FormData();
            fd.append('audio', blob, 'voice.webm');
            const r = await fetch('/chat/transcribe', {
              method: 'POST',
              credentials: 'same-origin',
              headers: { 'X-CSRF-Token': getCsrfToken() },
              body: fd,
            });
            const d = await r.json();
            if (d.transcript) {
              this.chatInput = (this.chatInput + ' ' + d.transcript).trim();
              this.micSuccess = true;
              setTimeout(() => { this.micSuccess = false; }, 2500);
              await this.$nextTick();
              this.sendChat();
            } else if (d.error) {
              console.error('transcription error', d.error);
            }
          } catch(e) { console.error('transcription fetch error', e); }
          finally { this.micTranscribing = false; }
        };
        // 90-second max recording with countdown
        this.micCountdown = 90;
        recorder.start();
        this.micRecording = true;
        this._micTimer = setInterval(() => {
          this.micCountdown = Math.max(0, this.micCountdown - 1);
          if (this.micCountdown === 0) {
            clearInterval(this._micTimer);
            recorder.stop();
          }
        }, 1000);
      } catch(e) {
        alert('Microphone access denied or unavailable.');
      }
    },

    renderMsg(content) {
      // Returns safe HTML string for x-html binding.
      // mode 'markdown': parsed + sanitized; 'mono': escaped pre-wrap; 'plain': escaped.
      const text = content || '';
      if (this.chatRenderMode === 'markdown' && typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
        return DOMPurify.sanitize(marked.parse(text), {USE_PROFILES: {html: true}});
      }
      // For plain/mono: escape HTML entities so x-html is safe
      const el = document.createElement('div');
      el.textContent = text;
      return el.innerHTML;
    },

    fmtAgo(ts) {
      if (!ts) return '';
      const s = Math.floor(Date.now() / 1000 - ts);
      if (s < 60) return s + 's ago';
      if (s < 3600) return Math.floor(s / 60) + 'm ago';
      if (s < 86400) return Math.floor(s / 3600) + 'h ago';
      return Math.floor(s / 86400) + 'd ago';
    },

    platformIcon(platform) {
      const icons = {
        telegram: '📱', discord: '💬', slack: '💼',
        whatsapp: '📲', local: '🌐', signal: '🔒', email: '📧',
      };
      return icons[platform] || '⚙️';
    },

    async toggleProvider(key) {
      try {
        const r = await fetch('/proxy/providers/' + key + '/toggle', {
          method: 'POST',
          headers: { 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
        });
        const d = await r.json();
        if (this.routerState.providers[key]) this.routerState.providers[key].enabled = d.enabled;
      } catch(e) { alert('Toggle failed: ' + e); }
    },

    // ── Chat ──────────────────────────────────────────────────────

    async sendChat() {
      const msg = this.chatInput.trim();
      if (!msg) return;
      // Rate-limit: large messages (>200 words) capped at one per second
      const wordCount = msg.split(/\s+/).length;
      if (wordCount > 200) {
        const now = Date.now();
        if (this._lastSentAt && (now - this._lastSentAt) < 1000) return;
        this._lastSentAt = now;
      }
      const chat = this.activeChat;
      if (!chat) return;

      this.chatInput = '';

      // Persist user message immediately
      chat.messages = [...chat.messages, { role: 'user', content: msg }];
      if (chat.name === 'New conversation') {
        chat.name = msg.length > 35 ? msg.slice(0, 32) + '\\u2026' : msg;
      }
      chat.updated_at = Date.now();
      this.chats = [chat, ...this.chats.filter(c => c.id !== chat.id)];
      this._saveChats();

      this.loadingChatId = chat.id;
      // Show "waking up" if this is the first call or the agent has been idle >5 min
      const idleMs = this.lastResponseAt ? (Date.now() - this.lastResponseAt) : Infinity;
      this.isWakingUp = idleMs > 5 * 60 * 1000;
      this._scrollChat();

      try {
        const r = await fetch(this.instanceUrl + '/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          credentials: 'same-origin',
          body: JSON.stringify({ message: msg, session_id: chat.id }),
        });
        if (r.status === 401) {
          chat.messages = [...chat.messages, { role: 'assistant', content: '\\u274c Session expired — please reload the page to log in again.' }];
          this.chats = [...this.chats];
          this._saveChats();
          return;
        }
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '', reply = '', msgStats = null;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data:')) continue;
            try {
              const d = JSON.parse(line.slice(5).trim());
              if (d.type === 'message') { reply = d.content; this.isWakingUp = false; }
              if (d.type === 'done') {
                msgStats = {
                  elapsed_s:       d.elapsed_s       || null,
                  prompt_tokens:   d.prompt_tokens    || 0,
                  api_calls:       d.api_calls        || 0,
                  tools_used:      d.tools_used       || 0,
                  tools_available: d.tools_available  || d.tool_count || 0,
                  model:           d.model            || '',
                };
                if (reply) {
                  chat.messages = [...chat.messages, { role: 'assistant', content: reply, stats: msgStats }];
                  chat.updated_at = Date.now();
                  this.chats = [chat, ...this.chats.filter(c => c.id !== chat.id)];
                  this._saveChats();
                  reply = '';
                }
              }
              if (d.type === 'error') {
                // Server-side agent/tool error — distinguish from transport failures
                const label = d.error_class ? d.error_class + ': ' : '';
                chat.messages = [...chat.messages, { role: 'assistant', content: '\\u274c Agent error: ' + label + d.content }];
                this.chats = [...this.chats];
                this._saveChats();
              }
            } catch(_) {}
          }
        }
        // If SSE ended without a done event but we have a reply, save it
        if (reply) {
          chat.messages = [...chat.messages, { role: 'assistant', content: reply, stats: msgStats }];
          chat.updated_at = Date.now();
          this.chats = [chat, ...this.chats.filter(c => c.id !== chat.id)];
          this._saveChats();
        }
      } catch(e) {
        if (chat) {
          // Transport-level failure (stream dropped, proxy timeout, etc.)
          // This is distinct from agent errors which arrive as SSE error events.
          chat.messages = [...chat.messages, { role: 'assistant', content: '\\u274c Connection lost — the agent stream closed unexpectedly. Try sending your message again.' }];
          this.chats = [...this.chats];
          this._saveChats();
        }
      } finally {
        this.loadingChatId = null;
        this.isWakingUp = false;
        this.lastResponseAt = Date.now();
        this._scrollChat();
        await this.loadStatus();
      }
    },

    _scrollChat() {
      this.$nextTick(() => {
        const el = document.getElementById('chat-messages');
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    // ── Admin ──────────────────────────────────────────────────────────

    async loadAdminData() {
      await Promise.all([this.loadAdminUsers(), this.loadAdminPolicies()]);
    },

    async loadRoutingData() {
      await Promise.all([this.loadAdminMachines(), this.loadAdminPolicies(), this.loadRouterState()]);
      this.probeAllMachines();  // fire-and-forget; updates health badges as results arrive
    },

    isExampleSetup() {
      return this.adminMachines.length > 0
        && this.adminMachines.every(m => m.description?.startsWith('Example'));
    },

    async applySetupWizard(mode) {
      this.setupWizardLoading = true;
      try {
        const body = { mode };
        if (mode === 'single' && this.setupWizardEndpoint.trim())
          body.endpoint_url = this.setupWizardEndpoint.trim();
        const r = await fetch('/admin/setup', {
          method: 'POST', credentials: 'include',
          headers: {'Content-Type':'application/json', 'X-CSRF-Token': getCsrfToken()},
          body: JSON.stringify(body),
        });
        const d = await r.json();
        if (!r.ok) {
          this._adminMsg(false, d.error || 'Setup failed');
          return;
        }
        this.setupWizardDismissed = true;
        this.setupWizardStep = null;
        localStorage.setItem('hermes_wizard_dismissed', '1');
        await this.loadRoutingData();
        this._adminMsg(true, 'Setup applied — update endpoints to match your actual hardware.');
      } catch(e) {
        this._adminMsg(false, e.message);
      } finally {
        this.setupWizardLoading = false;
      }
    },

    async loadAdminUsers() {
      try {
        const r = await fetch('/users', {credentials:'include'});
        const d = await r.json();
        this.adminUsers = d.users || [];
      } catch(e) { this.adminUsers = []; }
    },

    async loadAdminMachines() {
      try {
        const r = await fetch('/admin/machines', {credentials:'include'});
        const d = await r.json();
        // Preserve cached health state across reloads
        const prev = Object.fromEntries(this.adminMachines.map(m => [m.id, m]));
        this.adminMachines = (d.machines || []).map(m => ({
          ...m,
          _health:    prev[m.id]?._health    ?? null,
          _health_at: prev[m.id]?._health_at ?? null,
          _probing:   false,
        }));
        this.$nextTick(() => this._initMachineSortable());
      } catch(e) { this.adminMachines = []; }
    },

    _initMachineSortable() {
      const el = this.$refs.machineList;
      if (!el || !window.Sortable) return;
      if (el._sortable) { el._sortable.destroy(); }
      el._sortable = Sortable.create(el, {
        handle: '.machine-drag-handle',
        animation: 150,
        ghostClass: 'opacity-30',
        onEnd: (evt) => {
          // Reorder the Alpine data array to match the new DOM order
          const moved = this.adminMachines.splice(evt.oldIndex, 1)[0];
          this.adminMachines.splice(evt.newIndex, 0, moved);
          this.saveMachineOrder(this.adminMachines.map(m => m.id));
        },
      });
    },

    async saveMachineOrder(ids) {
      try {
        await fetch('/admin/machines/reorder', {
          method: 'POST', credentials: 'include',
          headers: {'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify({ids}),
        });
      } catch(e) { console.error('reorder failed', e); }
    },

    async runBenchmark() {
      this.benchmarkRunning = true;
      this.benchmarkResult = null;
      try {
        const r = await fetch('/proxy/benchmark', {
          method: 'POST', credentials: 'include',
          headers: {'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify({n_prompts: this.benchmarkNPrompts}),
        });
        this.benchmarkResult = await r.json();
      } catch(e) { this.benchmarkResult = {error: String(e)}; }
      this.benchmarkRunning = false;
    },

    async loadModelsLive() {
      this.modelsLiveLoading = true;
      this.modelsLiveResult = null;
      try {
        const r = await fetch('/proxy/models-live', {credentials:'include'});
        this.modelsLiveResult = await r.json();
      } catch(e) { this.modelsLiveResult = {error: String(e)}; }
      this.modelsLiveLoading = false;
    },

    async loadAdminPolicies() {
      try {
        const r = await fetch('/admin/policies', {credentials:'include'});
        const d = await r.json();
        this.adminPolicies = d.policies || [];
      } catch(e) { this.adminPolicies = []; }
    },

    async loadAdminAudit() {
      try {
        const r = await fetch('/audit-logs?limit=50', {credentials:'include'});
        const d = await r.json();
        this.adminAuditLogs = d.logs || [];
      } catch(e) { this.adminAuditLogs = []; }
    },

    async loadAdminRoutingLog() {
      this.adminRoutingLogLoading = true;
      try {
        const params = new URLSearchParams({ page: this.adminRoutingLogPage, limit: 100 });
        if (this.adminRoutingLogUserFilter) params.set('user_id', this.adminRoutingLogUserFilter);
        const r = await fetch('/admin/routing/log?' + params, {credentials:'include'});
        const d = await r.json();
        this.adminRoutingLog      = d.entries || [];
        this.adminRoutingLogTotal = d.total   || 0;
      } catch(e) { this.adminRoutingLog = []; }
      finally { this.adminRoutingLogLoading = false; }
    },

    async loadApprovals() {
      try {
        const params = new URLSearchParams({ limit: 100 });
        if (this.approvalsStatusFilter) params.set('status', this.approvalsStatusFilter);
        const r = await fetch('/approvals?' + params, {credentials:'include'});
        if (!r.ok) { this.approvalRequests = []; return; }
        const d = await r.json();
        this.approvalRequests = d.approvals || [];
      } catch(e) { this.approvalRequests = []; }
    },

    async decideApproval(id, decision) {
      try {
        const r = await fetch('/approvals/' + id + '/' + decision, {
          method: 'POST',
          headers: { 'X-CSRF-Token': getCsrfToken(), 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({}),
        });
        if (r.ok) {
          this._adminMsg(true, 'Approval ' + decision + 'd');
          await this.loadApprovals();
        } else {
          const d = await r.json();
          this._adminMsg(false, d.error || 'Failed');
        }
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async loadWorkflows() {
      try {
        const r = await fetch('/workflows', {credentials:'include'});
        const d = await r.json();
        this.workflows = d.workflows || [];
      } catch(e) { this.workflows = []; }
    },

    async importWorkflow() {
      try {
        const parsed = JSON.parse(this.wfImportJson);
        const r = await fetch('/workflows', {
          method: 'POST',
          headers: {'X-CSRF-Token': getCsrfToken(), 'Content-Type':'application/json'},
          credentials: 'include',
          body: JSON.stringify(parsed),
        });
        const d = await r.json();
        if (r.ok) {
          this._wfMsg(true, 'Workflow imported: ' + d.workflow.name);
          this.wfNewFormOpen = false;
          this.wfImportJson = '';
          await this.loadWorkflows();
        } else {
          this._wfMsg(false, d.error || 'Import failed');
        }
      } catch(e) { this._wfMsg(false, String(e)); }
    },

    async deleteWorkflow(id) {
      if (!confirm('Delete this workflow definition?')) return;
      try {
        const r = await fetch('/workflows/' + id, {
          method: 'DELETE',
          headers: {'X-CSRF-Token': getCsrfToken()},
          credentials: 'include',
        });
        if (r.ok) { await this.loadWorkflows(); }
        else { const d = await r.json(); this._wfMsg(false, d.error || 'Delete failed'); }
      } catch(e) { this._wfMsg(false, String(e)); }
    },

    async triggerWorkflow(id) {
      try {
        const r = await fetch('/workflows/' + id + '/trigger', {
          method: 'POST',
          headers: {'X-CSRF-Token': getCsrfToken(), 'Content-Type':'application/json'},
          credentials: 'include',
          body: JSON.stringify({inputs: {}}),
        });
        const d = await r.json();
        if (r.status === 202) {
          this._wfMsg(true, 'Run started: ' + d.run_id);
          if (this.selectedWorkflow && this.selectedWorkflow.id === id) {
            await this.loadWorkflowRuns(id);
          }
        } else {
          this._wfMsg(false, d.error || 'Trigger failed');
        }
      } catch(e) { this._wfMsg(false, String(e)); }
    },

    async loadWorkflowRuns(wfId) {
      try {
        const r = await fetch('/workflow-runs?workflow_id=' + wfId + '&limit=20', {credentials:'include'});
        const d = await r.json();
        this.wfRuns = d.runs || [];
      } catch(e) { this.wfRuns = []; }
    },

    async loadRunDetail(runId) {
      try {
        const r = await fetch('/workflow-runs/' + runId, {credentials:'include'});
        const d = await r.json();
        this.selectedRun = d;
      } catch(e) {}
    },

    async cancelRun(runId) {
      if (!confirm('Cancel this workflow run?')) return;
      try {
        const r = await fetch('/workflow-runs/' + runId + '/cancel', {
          method: 'POST',
          headers: {'X-CSRF-Token': getCsrfToken()},
          credentials: 'include',
        });
        if (r.ok) { await this.loadRunDetail(runId); }
      } catch(e) {}
    },

    async decideWorkflowApproval(approvalId, decision) {
      try {
        const r = await fetch('/workflow-runs/approvals/' + approvalId + '/' + decision, {
          method: 'POST',
          headers: {'X-CSRF-Token': getCsrfToken(), 'Content-Type':'application/json'},
          credentials: 'include',
          body: JSON.stringify({}),
        });
        if (r.ok) {
          this._wfMsg(true, 'Step ' + decision + 'd');
          // Reload the run detail after a short delay to let the engine update.
          setTimeout(() => this.selectedRun && this.loadRunDetail(this.selectedRun.run.id), 1500);
        } else {
          const d = await r.json();
          this._wfMsg(false, d.error || 'Failed');
        }
      } catch(e) { this._wfMsg(false, String(e)); }
    },

    _wfMsg(ok, text) {
      this.wfMsg = {ok, text};
      setTimeout(() => { this.wfMsg = null; }, 5000);
    },

    async loadAgentRuns() {
      this.agentRunsLoading = true;
      try {
        let url = `/runs?limit=${this.agentRunsLimit}&offset=${this.agentRunsOffset}`;
        if (this.agentRunsStatusFilter) url += `&status=${this.agentRunsStatusFilter}`;
        const r = await fetch(url, {credentials: 'include'});
        if (!r.ok) throw new Error(await r.text());
        const d = await r.json();
        this.agentRuns = d.runs || [];
        this.agentRunsTotal = d.total || 0;
      } catch(e) { this.agentRuns = []; }
      this.agentRunsLoading = false;
    },

    async loadAgentRunDetail(runId) {
      try {
        const r = await fetch(`/runs/${runId}`, {credentials: 'include'});
        const d = await r.json();
        this.selectedAgentRun = d.run || null;
      } catch(e) { this.selectedAgentRun = null; }
    },

    async cloneAgentRun(runId) {
      try {
        const r = await fetch(`/runs/${runId}/clone`, {credentials: 'include'});
        const d = await r.json();
        this.clonePayload = d.clone || null;
        if (this.clonePayload) {
          this.chatInput = this.clonePayload.user_message || '';
          this.tab = 'sessions';
          this.$nextTick(() => this._scrollChat());
        }
      } catch(e) { console.error('clone failed', e); }
    },

    _runStatusClass(status) {
      return {
        'bg-yellow-950 text-yellow-400 border-yellow-900': status === 'running',
        'bg-green-950 text-green-400 border-green-900': status === 'success',
        'bg-red-950 text-red-400 border-red-900': status === 'failed',
        'bg-gray-800 text-gray-500 border-gray-700': status === 'cancelled',
      };
    },

    _runDuration(run) {
      if (!run.started_at) return '—';
      const end = run.finished_at || Date.now();
      const ms = end - run.started_at;
      if (ms < 1000) return ms + 'ms';
      if (ms < 60000) return (ms/1000).toFixed(1) + 's';
      return Math.floor(ms/60000) + 'm ' + Math.floor((ms%60000)/1000) + 's';
    },

    _adminMsg(ok, text) {
      this.adminMsg = {ok, text};
      setTimeout(() => { this.adminMsg = null; }, 4000);
    },

    completedSessionsList() {
      return Object.values(this.completedSessions);
    },

    navigateToSession(r) {
      if (r.platform !== 'local') return;
      const chat = this.chats.find(c => r.session_key && r.session_key.includes(c.id));
      if (chat) { this.tab = 'sessions'; this.switchChat(chat.id); }
    },

    async resolveRouteDebug() {
      this.routeDebugLoading = true;
      this.routeDebugResult = null;
      try {
        const params = new URLSearchParams({ model: this.routeDebugModel });
        if (this.routeDebugUserId) params.set('user_id', this.routeDebugUserId);
        const r = await fetch('/admin/routing/resolve?' + params, {credentials:'include'});
        // Both 200 and 503 return JSON — parse regardless of status
        this.routeDebugResult = await r.json();
      } catch(e) {
        this.routeDebugResult = { error: e.message, result: null };
      } finally {
        this.routeDebugLoading = false;
      }
    },

    async createAdminUser() {
      try {
        const r = await fetch('/users', {
          method:'POST', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(this.adminUserForm),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Failed'); return; }
        this.adminUsers = [...this.adminUsers, d.user];
        this.adminUserFormOpen = false;
        this._adminMsg(true, 'User created');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async patchUser(uid, updates) {
      try {
        const r = await fetch('/users/'+uid, {
          method:'PATCH', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(updates),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Failed'); return; }
        this.adminUsers = this.adminUsers.map(u => u.id===uid ? d.user : u);
        this._adminMsg(true, 'Saved');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async adminSetPassword() {
      const uid = this.adminSetPwUserId;
      const pw  = this.adminSetPwVal.trim();
      if (!pw || pw.length < 8) {
        this.adminSetPwMsg = {ok:false, text:'Password must be at least 8 characters'};
        return;
      }
      try {
        const r = await fetch('/users/'+uid, {
          method:'PATCH', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify({new_password: pw}),
        });
        const d = await r.json();
        if (!r.ok) { this.adminSetPwMsg = {ok:false, text:d.error||'Failed'}; return; }
        this.adminSetPwUserId = null;
        this.adminSetPwVal = '';
        this.adminSetPwMsg = null;
        this._adminMsg(true, 'Password updated — all sessions for this user have been revoked');
      } catch(e) { this.adminSetPwMsg = {ok:false, text:String(e)}; }
    },

    async assignUserPolicy(uid, policyId) {
      try {
        const r = await fetch('/admin/users/'+uid+'/policy', {
          method:'PATCH', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify({policy_id: policyId}),
        });
        if (!r.ok) { const d=await r.json(); this._adminMsg(false, d.error||'Failed'); return; }
        this.adminUsers = this.adminUsers.map(u => u.id===uid ? {...u, policy_id: policyId} : u);
        this._adminMsg(true, 'Profile assigned');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async createMachine() {
      try {
        const r = await fetch('/admin/machines', {
          method:'POST', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(this.adminMachineForm),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Failed'); return; }
        this.adminMachines = [...this.adminMachines, {...d.machine, _health:null}];
        this.adminMachineFormOpen = false;
        this._adminMsg(true, 'Machine registered');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async toggleMachine(m) {
      try {
        const r = await fetch('/admin/machines/'+m.id, {
          method:'PATCH', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify({enabled: !m.enabled}),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Failed'); return; }
        this.adminMachines = this.adminMachines.map(x => x.id===m.id ? {...d.machine, _health:x._health} : x);
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    startEditMachine(m) {
      this.adminMachineEditId   = m.id;
      this.adminMachineEditForm = { name: m.name, endpoint_url: m.endpoint_url, description: m.description || '' };
    },

    cancelEditMachine() {
      this.adminMachineEditId   = null;
      this.adminMachineEditForm = {};
    },

    async saveEditMachine(mid) {
      try {
        const r = await fetch('/admin/machines/'+mid, {
          method:'PATCH', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(this.adminMachineEditForm),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Save failed'); return; }
        const caps = this.adminMachines.find(m => m.id === mid)?.capabilities || [];
        const health = this.adminMachines.find(m => m.id === mid)?._health || null;
        this.adminMachines = this.adminMachines.map(m =>
          m.id===mid ? {...d.machine, capabilities: caps, profile_count: m.profile_count, _health: health} : m
        );
        this.adminMachineEditId = null;
        this._adminMsg(true, 'Saved');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async deleteMachine(mid) {
      const machine = this.adminMachines.find(m => m.id === mid);
      const count   = machine?.profile_count || 0;
      const warning = count > 0
        ? `"${machine.name}" is used by ${count} profile${count > 1 ? 's' : ''}. Deleting it will break those routing rules.\n\nDelete anyway?`
        : `Delete "${machine?.name}"?`;
      if (!confirm(warning)) return;
      try {
        const r = await fetch('/admin/machines/'+mid, {
          method:'DELETE', credentials:'include',
          headers:{'X-CSRF-Token':getCsrfToken()},
        });
        if (r.status===204) {
          this.adminMachines = this.adminMachines.filter(m => m.id!==mid);
          this._adminMsg(true, 'Deleted');
        }
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async probeHealth(m) {
      this.adminMachines = this.adminMachines.map(x => x.id===m.id ? {...x, _probing:true} : x);
      const now = Math.floor(Date.now() / 1000);
      try {
        const r = await fetch('/admin/machines/'+m.id+'/health', {credentials:'include'});
        const d = await r.json();
        const checked = d.checked_at || now;
        this.adminMachines = this.adminMachines.map(x =>
          x.id===m.id ? {...x, _health:d, _health_at:checked, _probing:false} : x
        );
      } catch(e) {
        this.adminMachines = this.adminMachines.map(x =>
          x.id===m.id ? {...x, _health:{status:'unreachable',error:String(e)}, _health_at:now, _probing:false} : x
        );
      }
    },

    async probeAllMachines() {
      await Promise.all(this.adminMachines.map(m => this.probeHealth(m)));
    },

    async toggleCapability(m, cls, enabled) {
      const caps = enabled
        ? [...m.capabilities, cls]
        : m.capabilities.filter(c => c!==cls);
      try {
        const r = await fetch('/admin/machines/'+m.id+'/capabilities', {
          method:'PUT', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(caps),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error||'Failed'); return; }
        this.adminMachines = this.adminMachines.map(x => x.id===m.id ? {...x, capabilities:d.capabilities} : x);
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async createPolicy() {
      try {
        const r = await fetch('/admin/policies', {
          method:'POST', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(this.adminPolicyForm),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Failed'); return; }
        this.adminPolicies = [...this.adminPolicies, d.policy];
        this.adminPolicyFormOpen = false;
        this._adminMsg(true, 'Profile created');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async deletePolicy(pid) {
      const p = this.adminPolicies.find(x => x.id===pid);
      const msg = p && p.user_count > 0
        ? `Delete profile "${p.name}"? ${p.user_count} user(s) will lose their routing override and fall back to system default.`
        : `Delete profile "${p?.name || ''}"?`;
      if (!confirm(msg)) return;
      try {
        const r = await fetch('/admin/policies/'+pid, {
          method:'DELETE', credentials:'include',
          headers:{'X-CSRF-Token':getCsrfToken()},
        });
        if (r.status===204) {
          this.adminPolicies = this.adminPolicies.filter(p => p.id!==pid);
          this._adminMsg(true, 'Deleted');
        }
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    async savePolicyRules(policy) {
      try {
        const r = await fetch('/admin/policies/'+policy.id+'/rules', {
          method:'PUT', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(policy.rules),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Failed'); return; }
        this.adminPolicies = this.adminPolicies.map(p => p.id===policy.id ? {...p, rules:d.rules} : p);
        this._adminMsg(true, 'Rules saved');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    startEditPolicy(p) {
      this.adminPolicyEditId = p.id;
      this.adminPolicyEditForm = { name: p.name, description: p.description || '', fallback: p.fallback };
    },

    cancelEditPolicy() {
      this.adminPolicyEditId = null;
      this.adminPolicyEditForm = {};
    },

    async saveEditPolicy() {
      const pid = this.adminPolicyEditId;
      try {
        const r = await fetch('/admin/policies/'+pid, {
          method:'PATCH', credentials:'include',
          headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify(this.adminPolicyEditForm),
        });
        const d = await r.json();
        if (!r.ok) { this._adminMsg(false, d.error || 'Failed'); return; }
        this.adminPolicies = this.adminPolicies.map(p => p.id===pid ? {...d.policy, rules:p.rules, user_count:p.user_count} : p);
        this.adminPolicyEditId = null;
        this._adminMsg(true, 'Profile updated');
      } catch(e) { this._adminMsg(false, String(e)); }
    },

    movePolicyRule(policy, idx, dir) {
      const newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= policy.rules.length) return;
      const rules = [...policy.rules];
      [rules[idx], rules[newIdx]] = [rules[newIdx], rules[idx]];
      this.adminPolicies = this.adminPolicies.map(p => p.id===policy.id ? {...p, rules} : p);
    },
  };
}
</script>
</body>
</html>"""


def _check_auth(request: web.Request) -> bool:
    """Legacy internal-token check — still used by /sessions endpoint."""
    token = os.environ.get("HERMES_INTERNAL_TOKEN", "")
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {token}"


def _ensure_admin_exists() -> None:
    """Seed the first admin account from env vars if the users table is empty."""
    admin_email = os.environ.get("HERMES_ADMIN_EMAIL", "").strip()
    admin_pass  = os.environ.get("HERMES_ADMIN_PASSWORD", "").strip()
    if not admin_email or not admin_pass:
        return
    try:
        if auth_db.get_user_by_email(admin_email):
            return
        auth_db.create_user(
            email=admin_email,
            username="admin",
            password_hash=hash_password(admin_pass),
            role="admin",
            display_name=os.environ.get("HERMES_ADMIN_NAME", "Admin"),
        )
        logger.info("Seeded admin account: %s", admin_email)
    except Exception as exc:
        logger.warning("Failed to seed admin account: %s", exc)


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Logos \u2014 Sign In</title>
  <link rel="icon" type="image/svg+xml" href="/static/logo.svg">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; }

    body {
      background-color: #020817;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
      background-image: radial-gradient(rgba(99,102,241,0.06) 1px, transparent 1px);
      background-size: 28px 28px;
    }

    /* ── Ambient glow ─────────────────────────────────────────────────────
       Huge blurred orb centred on the logo area. Colours synced to the
       actual hue-rotate progression of the logo gradient (avg hue ~260°):
         0°  → indigo, 60° → magenta, 120° → orange, 180° → yellow,
         240° → green,  300° → cyan,   360° → back to indigo.          */
    @keyframes ambient-color {
      0%   { background: #6366f1; }
      17%  { background: #d946ef; }
      33%  { background: #f97316; }
      50%  { background: #eab308; }
      67%  { background: #22c55e; }
      83%  { background: #06b6d4; }
      100% { background: #6366f1; }
    }
    body::before {
      content: '';
      position: fixed;
      top: 40%; left: 50%;
      transform: translate(-50%, -50%);
      width: 1400px; height: 900px;
      border-radius: 50%;
      filter: blur(200px);
      opacity: 0.09;
      animation: ambient-color 60s linear infinite;
      pointer-events: none;
      z-index: 0;
    }

    [x-cloak] { display: none !important; }

    /* ── Logo halo ── same colour keyframes, tighter blur, more intense */
    @keyframes halo-color {
      0%   { background: #6366f1; }
      17%  { background: #d946ef; }
      33%  { background: #f97316; }
      50%  { background: #eab308; }
      67%  { background: #22c55e; }
      83%  { background: #06b6d4; }
      100% { background: #6366f1; }
    }
    .logo-halo {
      position: absolute;
      inset: -50px;
      border-radius: 50%;
      filter: blur(50px);
      opacity: 0.12;
      pointer-events: none;
      animation: halo-color 60s linear infinite;
    }
    /* ── Logo SVG — hue-rotate in sync (360° over 60s) */
    @keyframes logo-hue {
      from { filter: hue-rotate(0deg); }
      to   { filter: hue-rotate(360deg); }
    }
    @keyframes logo-fadein {
      from { opacity: 0; transform: scale(0.96); }
      to   { opacity: 1; transform: scale(1); }
    }
    .logo-wrap {
      animation: logo-fadein 3s cubic-bezier(0.16,1,0.3,1) both;
    }
    .logo-img { animation: logo-hue 60s linear infinite; }

    /* ── Splash → login reveal ──────────────────────────────────────────
       The logo is always in normal flow; body flex-centres it.
       When hidden, the container is ~120px tall → logo centred in viewport.
       When the reveal div expands, the container grows and the logo
       naturally floats upward. max-height transition drives the motion.  */
    .login-reveal {
      max-height: 0;
      opacity: 0;
      overflow: hidden;
      transition: max-height 3s cubic-bezier(0.16,1,0.3,1),
                  opacity    2s ease 0.8s;
    }
    .login-reveal.open {
      max-height: 700px;
      opacity: 1;
    }

    /* ── Hint pulse ── */
    @keyframes hint-pulse {
      0%, 100% { opacity: 0.35; }
      50%       { opacity: 0.7; }
    }
    .hint-text {
      animation: hint-pulse 2.4s ease-in-out infinite;
      transition: opacity 0.8s ease;
    }

    /* ── Card ── */
    .login-card {
      background: rgba(10,18,35,0.85);
      border: 1px solid rgba(99,102,241,0.1);
      border-radius: 20px;
      box-shadow:
        0 0 0 1px rgba(255,255,255,0.03) inset,
        0 1px 0   rgba(255,255,255,0.05) inset,
        0 8px 32px rgba(0,0,0,0.5),
        0 32px 64px rgba(0,0,0,0.3);
      backdrop-filter: blur(12px);
    }

    /* ── Button ── */
    .btn-signin {
      width: 100%; padding: 11px; border-radius: 12px;
      font-size: 0.875rem; font-weight: 500; color: #fff;
      background: linear-gradient(135deg, #6366f1 0%, #a855f7 100%);
      box-shadow: 0 1px 2px rgba(0,0,0,0.4), 0 0 0 1px rgba(99,102,241,0.3) inset;
      transition: opacity 150ms ease, box-shadow 150ms ease, transform 80ms ease;
      animation: logo-hue 60s linear infinite;
      cursor: pointer;
    }
    .btn-signin:hover:not(:disabled) {
      opacity: 0.92;
      box-shadow: 0 1px 2px rgba(0,0,0,0.5), 0 0 28px rgba(168,85,247,0.25), 0 0 0 1px rgba(168,85,247,0.35) inset;
    }
    .btn-signin:active:not(:disabled) { transform: translateY(1px); opacity: 0.95; }
    .btn-signin:disabled { opacity: 0.45; cursor: not-allowed; animation: none; }

    /* ── Floating label inputs ── */
    .float-wrap { position: relative; }
    .float-input {
      width: 100%;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 12px;
      padding: 20px 16px 8px;
      color: #f1f5f9; font-size: 0.875rem; outline: none;
      transition: background 0.6s ease;
    }
    .float-input::placeholder { color: transparent; }
    .float-input:hover  { background: rgba(255,255,255,0.055); }
    .float-input:focus  {
      background: rgba(0,0,0,0.25);
    }
    .float-label {
      position: absolute; left: 16px; top: 50%;
      transform: translateY(-50%);
      font-size: 0.875rem; color: rgba(148,163,184,0.4);
      pointer-events: none; transition: all 140ms ease;
    }
    .float-input:focus ~ .float-label,
    .float-input:not(:placeholder-shown) ~ .float-label {
      top: 10px; transform: translateY(0);
      font-size: 0.62rem; font-weight: 600;
      letter-spacing: 0.07em; text-transform: uppercase;
      color: rgba(148,163,184,0.55);
    }
    .float-input:focus ~ .float-label { color: rgba(165,180,252,0.8); }
  </style>
</head>
<body @click="activate()" x-data="loginApp()" x-init="init()">

  <!-- Main content — flex-centred by body -->
  <div class="relative z-10 w-full px-5" style="max-width:400px;">

    <!-- Logo — always visible; floats up as .login-reveal expands below -->
    <div class="text-center logo-wrap" style="padding-top:2vh; padding-bottom:1.5rem;">
      <div class="relative inline-block">
        <div class="logo-halo"></div>
        <img src="/static/logo.svg" alt="Logos" class="logo-img relative mx-auto"
             style="width:120px;height:120px;object-fit:contain;">
      </div>
    </div>

    <!-- Reveal section — expands on activate() -->
    <div :class="phase==='login' ? 'login-reveal open' : 'login-reveal'">

      <!-- Card -->
      <div class="login-card px-7 py-7">
        <p class="text-center mb-5"
           style="font-size:0.72rem;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:rgba(148,163,184,0.5);"
           x-text="needsSetup ? 'Get started' : 'Sign in'"></p>
        <form @submit.prevent="submit()" @click.stop class="space-y-5">

          <div class="float-wrap">
            <input id="email" x-model="email" type="email" required autocomplete="email"
                   class="float-input" placeholder=" "/>
            <label class="float-label" for="email">Email</label>
          </div>

          <div x-data="{ show: false }" class="float-wrap">
            <input id="password" x-model="password" :type="show ? 'text' : 'password'"
                   required autocomplete="current-password"
                   class="float-input" style="padding-right:2.75rem;" placeholder=" "/>
            <label class="float-label" for="password">Password</label>
            <button type="button" @click.stop="show=!show" tabindex="-1"
                    class="absolute right-3.5 top-1/2 -translate-y-1/2 p-0.5 transition-colors"
                    style="color:rgba(100,116,139,0.6);"
                    onmouseover="this.style.color='rgba(148,163,184,1)'"
                    onmouseout="this.style.color='rgba(100,116,139,0.6)'">
              <svg x-show="!show" xmlns="http://www.w3.org/2000/svg" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
                <path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
              </svg>
              <svg x-show="show" x-cloak xmlns="http://www.w3.org/2000/svg" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"/>
              </svg>
            </button>
          </div>

          <div x-show="error" x-cloak
               style="background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.18);border-radius:10px;padding:10px 14px;">
            <p class="text-red-400 text-sm" x-text="error"></p>
          </div>

          <button type="submit" :disabled="loading" class="btn-signin mt-1">
            <span x-show="!loading" x-text="needsSetup ? 'Get started' : 'Sign in'"></span>
            <span x-show="loading" x-cloak class="flex items-center justify-center gap-2">
              <svg class="animate-spin w-4 h-4 opacity-80" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
              </svg>
              Signing in
            </span>
          </button>

        </form>
      </div>

      <!-- Footer -->
      <p class="text-center mt-6"
         style="font-size:0.72rem;color:rgba(71,85,105,0.7);letter-spacing:0.05em;">
        A self-hosted AI agent platform
      </p>

    </div><!-- /login-reveal -->
  </div>

  <!-- Hint — fades in after 10s inactivity in splash mode -->
  <div x-show="showHint" x-cloak
       style="position:fixed;bottom:18%;left:0;right:0;text-align:center;z-index:20;pointer-events:none;">
    <p class="hint-text"
       style="font-size:0.78rem;color:rgba(148,163,184,0.5);letter-spacing:0.08em;">
      click anywhere to continue
    </p>
  </div>

  <!-- Version badge -->
  <div style="position:fixed;bottom:16px;right:18px;z-index:50;
              font-size:0.65rem;color:rgba(71,85,105,0.45);
              letter-spacing:0.04em;font-family:ui-monospace,monospace;pointer-events:none;">
    __VERSION_LABEL__
  </div>

  <script src="https://unpkg.com/alpinejs@3/dist/cdn.min.js" defer></script>
  <script>
  function loginApp() {
    return {
      phase: 'splash',   // 'splash' | 'login'
      showHint: false,
      email: '', password: '', error: '', loading: false, needsSetup: false,
      _hintTimer: null,

      init() {
        fetch('/auth/me', { credentials: 'same-origin' })
          .then(r => { if (r.ok) window.location.href = '/'; })
          .catch(() => {});
        fetch('/api/setup/status', { credentials: 'same-origin' })
          .then(r => r.ok ? r.json() : null)
          .then(d => { if (d && !d.completed) this.needsSetup = true; })
          .catch(() => {});
        this._hintTimer = setTimeout(() => { if (this.phase === 'splash') this.showHint = true; }, 5000);
      },

      activate() {
        if (this.phase !== 'splash') return;
        clearTimeout(this._hintTimer);
        this.showHint = false;
        this.phase = 'login';
        this.$nextTick(() => {
          document.getElementById('email')?.focus();
        });
      },

      async submit() {
        this.loading = true; this.error = '';
        try {
          const res = await fetch('/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: this.email, password: this.password }),
            credentials: 'same-origin',
          });
          if (res.ok) {
            const d = await res.json().catch(() => ({}));
            window.location.href = d.setup_required ? '/setup' : '/';
          } else {
            const d = await res.json().catch(() => ({}));
            this.error = ({
              invalid_credentials: 'Invalid email or password.',
              account_locked:      'Account locked \u2014 try again in a few minutes.',
              rate_limited:        'Too many attempts \u2014 slow down.',
              missing_fields:      'Email and password required.',
            })[d.error] ?? 'Sign in failed. Please try again.';
          }
        } catch {
          this.error = 'Connection error \u2014 is Logos running?';
        } finally {
          this.loading = false;
        }
      },
    };
  }
  </script>
</body>
</html>"""


_SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Logos Setup</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <style>
    [x-cloak]{display:none!important}
    body{background:#030712}
    @keyframes dot-fade{0%,80%,100%{opacity:0}40%{opacity:1}}
  </style>
</head>
<body class="min-h-screen text-white" style="background:#030712">
<div x-data="setup()" x-init="init()" class="flex flex-col min-h-screen">

  <!-- Header -->
  <header class="flex flex-col items-center pt-10 pb-4 gap-5">
    <div class="flex items-center gap-2.5">
      <img src="/chat_logo.png" class="h-8 w-8 rounded-xl" onerror="this.style.display='none'">
      <span class="font-semibold text-lg tracking-tight">Logos</span>
    </div>
    <!-- Step indicator — visible from step 1 onward -->
    <div x-show="step > 0" x-transition.opacity class="flex items-center gap-1">
      <template x-for="i in [1,2,3,4]" :key="i">
        <div class="flex items-center gap-1">
          <div class="w-2 h-2 rounded-full transition-all duration-500"
               :class="step > i ? 'bg-indigo-400' : step === i ? 'bg-indigo-500 scale-125' : 'bg-gray-700'"></div>
          <div x-show="i < 4" class="w-6 h-px transition-colors duration-500"
               :class="step > i ? 'bg-indigo-600' : 'bg-gray-700'"></div>
        </div>
      </template>
    </div>
  </header>

  <!-- Main content -->
  <main class="flex-1 flex items-start justify-center px-4 pt-6 pb-16">
    <div class="w-full max-w-md">

      <!-- ── Step 0: Track selection ─────────────────────────────────── -->
      <div x-show="step===0" x-transition.opacity>
        <div class="text-center mb-8">
          <h1 class="text-2xl font-bold mb-2">Welcome to Logos</h1>
          <p class="text-gray-400 text-sm">One decision shapes your setup. You can change it later.</p>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <!-- Local-first -->
          <button @click="selectTrack('local')"
            class="text-left p-5 rounded-2xl border border-gray-700 bg-gray-900 hover:border-indigo-500 hover:bg-gray-800/80 transition-all duration-200 group">
            <div class="w-9 h-9 rounded-xl bg-indigo-950 flex items-center justify-center mb-3 group-hover:bg-indigo-900 transition-colors border border-indigo-800">
              <svg class="w-4 h-4 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>
              </svg>
            </div>
            <div class="font-semibold text-white mb-1">Local-first</div>
            <div class="text-xs text-gray-400 leading-relaxed mb-3">
              Your data never leaves this machine. Models run on your hardware.
            </div>
            <div class="text-xs text-indigo-400 font-medium">Free &middot; Private &middot; Ollama or LM Studio</div>
          </button>
          <!-- Frontier-first (coming soon) -->
          <div class="text-left p-5 rounded-2xl border border-gray-800 bg-gray-900/40 opacity-50 cursor-not-allowed select-none">
            <div class="w-9 h-9 rounded-xl bg-gray-800 flex items-center justify-center mb-3 border border-gray-700">
              <svg class="w-4 h-4 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064"/>
              </svg>
            </div>
            <div class="font-semibold text-gray-500 mb-1">Frontier-first</div>
            <div class="text-xs text-gray-600 leading-relaxed mb-3">
              Best available cloud models via Anthropic, OpenAI, or OpenRouter.
            </div>
            <div class="text-xs text-gray-600 font-medium">Coming soon</div>
          </div>
        </div>
      </div>

      <!-- ── Step 1: Connect model server ───────────────────────────── -->
      <div x-show="step===1" x-cloak x-transition.opacity>
        <div class="mb-6">
          <h2 class="text-xl font-bold mb-1">Connect your model server</h2>
          <p class="text-gray-400 text-sm">Logos will check for Ollama and LM Studio automatically.</p>
        </div>

        <!-- Scanning -->
        <div x-show="autoScanning" class="flex flex-col items-center py-12 gap-4">
          <div class="w-8 h-8 border-2 border-gray-800 border-t-indigo-500 rounded-full animate-spin"></div>
          <p class="text-sm text-gray-500">Checking for model servers&hellip;</p>
        </div>

        <!-- Results -->
        <div x-show="!autoScanning && autoScanDone" class="space-y-3">

          <!-- Found server cards -->
          <template x-for="s in foundServers" :key="s.endpoint">
            <div class="p-4 rounded-xl border transition-all"
              :class="isServerSelected(s) ? 'border-indigo-500 bg-indigo-950/30' : 'border-gray-700 bg-gray-900'">
              <div class="flex items-start gap-3">
                <button @click="s.status==='up' && toggleServer(s)"
                  :class="isServerSelected(s) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-600'"
                  class="w-5 h-5 rounded border-2 flex items-center justify-center flex-shrink-0 mt-0.5 transition-all">
                  <svg x-show="isServerSelected(s)" class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/>
                  </svg>
                </button>
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="text-sm font-semibold text-white" x-text="s.type==='lmstudio' ? 'LM Studio' : 'Ollama'"></span>
                    <span x-show="s.status==='up'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-green-950 text-green-400 border border-green-800 font-medium">running</span>
                    <span x-show="s.status==='auth_required'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-950 text-amber-400 border border-amber-800 font-medium">auth required</span>
                  </div>
                  <div class="text-xs text-gray-500 font-mono mt-0.5 truncate" x-text="s.endpoint.replace('/v1','')"></div>
                  <div x-show="s.status==='up'" class="text-xs text-gray-600 mt-0.5"
                    x-text="s.models.length===0 ? 'No models loaded yet' : s.models.length + ' model' + (s.models.length!==1?'s':'') + ' ready'"></div>
                  <!-- Auth required: inline key entry -->
                  <div x-show="s.status==='auth_required'" class="mt-2 flex gap-2">
                    <input type="password" placeholder="Paste API key from LM Studio \u2192 Local Server tab"
                      x-model="s._apiKey"
                      class="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
                    <button @click="retryWithKey(s)" :disabled="!s._apiKey"
                      class="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-medium flex-shrink-0 transition-colors">
                      Connect
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </template>

          <!-- Nothing found -->
          <div x-show="foundServers.length===0" class="p-5 rounded-xl bg-gray-900 border border-gray-800 text-center">
            <p class="text-sm text-gray-300 mb-1">No model servers detected</p>
            <p class="text-xs text-gray-600">Ollama (:11434) and LM Studio (:1234) were not found.</p>
          </div>

          <!-- Manual add -->
          <div>
            <button @click="showManualEntry=!showManualEntry"
              class="flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 transition-colors py-1">
              <svg class="w-3.5 h-3.5 transition-transform" :class="showManualEntry?'rotate-45':''" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
              </svg>
              Add a server at a custom address
            </button>
            <div x-show="showManualEntry" class="mt-2 p-4 rounded-xl bg-gray-900 border border-gray-800 space-y-2">
              <div class="flex gap-2">
                <select x-model="manualType"
                  class="bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
                  <option value="ollama">Ollama</option>
                  <option value="lmstudio">LM Studio</option>
                </select>
                <input x-model="manualUrl" type="text"
                  :placeholder="manualType==='ollama' ? 'http://192.168.1.50:11434 or http://your-vps-ip:11434' : 'http://192.168.1.50:1234 or http://your-vps-ip:1234'"
                  @keydown.enter="addManualServer()"
                  class="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
                <button @click="addManualServer()" :disabled="manualProbing||!manualUrl.trim()"
                  class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-medium flex-shrink-0 transition-colors">
                  <span x-show="!manualProbing">Add</span>
                  <span x-show="manualProbing" class="flex items-center gap-1"><div class="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin"></div></span>
                </button>
              </div>
              <input x-show="manualType==='lmstudio'" x-model="manualKey" type="password"
                placeholder="API key (if auth is enabled in LM Studio)"
                class="w-full bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
              <p x-show="manualError" class="text-xs text-red-400" x-text="manualError"></p>
            </div>
          </div>

          <!-- Install guides (only when nothing auto-detected) -->
          <div x-show="foundServers.length===0" class="space-y-2 pt-1">
            <details class="group">
              <summary class="text-xs text-gray-500 hover:text-gray-300 cursor-pointer select-none list-none flex items-center gap-1.5">
                <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                How to install Ollama
              </summary>
              <div class="mt-2 p-4 rounded-xl bg-gray-900 border border-gray-800 space-y-3">
                <div class="flex gap-1 mb-2">
                  <button @click="osPlatform='mac'" :class="osPlatform==='mac'?'bg-gray-700 text-white':'text-gray-500 hover:text-gray-300'" class="px-2.5 py-1 rounded-md text-xs transition-colors">macOS</button>
                  <button @click="osPlatform='linux'" :class="osPlatform==='linux'?'bg-gray-700 text-white':'text-gray-500 hover:text-gray-300'" class="px-2.5 py-1 rounded-md text-xs transition-colors">Linux</button>
                  <button @click="osPlatform='windows'" :class="osPlatform==='windows'?'bg-gray-700 text-white':'text-gray-500 hover:text-gray-300'" class="px-2.5 py-1 rounded-md text-xs transition-colors">Windows</button>
                </div>
                <div x-show="osPlatform==='mac'" class="space-y-2">
                  <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2">
                    <code class="text-xs text-indigo-300 font-mono">brew install ollama</code>
                    <button @click="copy('brew install ollama')" class="text-gray-600 hover:text-gray-400 text-xs ml-3" x-text="copied==='brew install ollama'?'copied':'copy'"></button>
                  </div>
                  <p class="text-xs text-gray-600">Or <a href="https://ollama.com/download" target="_blank" class="text-indigo-400 hover:underline">download the macOS app &nearr;</a></p>
                </div>
                <div x-show="osPlatform==='linux'">
                  <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2">
                    <code class="text-xs text-indigo-300 font-mono">curl -fsSL https://ollama.com/install.sh | sh</code>
                    <button @click="copy('curl -fsSL https://ollama.com/install.sh | sh')" class="text-gray-600 hover:text-gray-400 text-xs ml-3 flex-shrink-0" x-text="copied==='curl -fsSL https://ollama.com/install.sh | sh'?'copied':'copy'"></button>
                  </div>
                </div>
                <div x-show="osPlatform==='windows'">
                  <a href="https://ollama.com/download" target="_blank" class="text-xs text-indigo-400 hover:underline">Download Ollama for Windows &nearr;</a>
                </div>
                <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2 mt-2">
                  <code class="text-xs text-indigo-300 font-mono">ollama serve</code>
                  <button @click="copy('ollama serve')" class="text-gray-600 hover:text-gray-400 text-xs ml-3" x-text="copied==='ollama serve'?'copied':'copy'"></button>
                </div>
              </div>
            </details>
            <details class="group">
              <summary class="text-xs text-gray-500 hover:text-gray-300 cursor-pointer select-none list-none flex items-center gap-1.5">
                <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                How to install LM Studio
              </summary>
              <div class="mt-2 p-4 rounded-xl bg-gray-900 border border-gray-800 space-y-3">
                <div class="flex gap-2.5"><span class="w-4 h-4 rounded-full bg-gray-700 text-white text-[10px] flex items-center justify-center flex-shrink-0 font-medium">1</span><span class="text-xs text-gray-300">Download LM Studio from <a href="https://lmstudio.ai" target="_blank" class="text-indigo-400 hover:underline">lmstudio.ai &nearr;</a></span></div>
                <div class="flex gap-2.5"><span class="w-4 h-4 rounded-full bg-gray-700 text-white text-[10px] flex items-center justify-center flex-shrink-0 font-medium">2</span><span class="text-xs text-gray-300">Open the <span class="text-white font-medium">Discover</span> tab, search for a model, download it</span></div>
                <div class="flex gap-2.5"><span class="w-4 h-4 rounded-full bg-gray-700 text-white text-[10px] flex items-center justify-center flex-shrink-0 font-medium">3</span><span class="text-xs text-gray-300">Go to <span class="text-white font-medium">&harr; Local Server</span> &rarr; select your model &rarr; <span class="text-white font-medium">Start Server</span></span></div>
              </div>
            </details>
          </div>

          <!-- Footer -->
          <div class="flex items-center gap-3 pt-1">
            <button @click="autoDetect()" :disabled="autoScanning"
              class="text-xs text-gray-600 hover:text-gray-400 disabled:opacity-50 transition-colors flex items-center gap-1.5 flex-shrink-0">
              <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
              Re-scan
            </button>
            <button @click="goNext()" :disabled="selectedServers.length===0"
              class="flex-1 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium transition-colors">
              Continue &rarr;
            </button>
          </div>
        </div>
      </div>

      <!-- ── Step 2: Pick model ──────────────────────────────────────── -->
      <div x-show="step===2" x-cloak x-transition.opacity>
        <div class="mb-5">
          <h2 class="text-xl font-bold mb-1">Choose a model</h2>
          <p class="text-gray-400 text-sm" x-text="modelStepSubtitle()"></p>
        </div>

        <!-- Auto-continue notice -->
        <div x-show="autoAdvancing" class="p-4 rounded-xl bg-green-950/40 border border-green-800 flex items-center gap-3 mb-4">
          <div class="w-5 h-5 rounded-full bg-green-500 flex items-center justify-center flex-shrink-0">
            <svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/></svg>
          </div>
          <div>
            <div class="text-sm text-green-300 font-medium" x-text="'Using ' + selectedModel"></div>
            <div class="text-xs text-green-400/70">Continuing automatically&hellip;</div>
          </div>
        </div>

        <!-- Models available -->
        <div x-show="!autoAdvancing && getModels().length > 0" class="space-y-4">

          <!-- Recommended -->
          <div x-show="recommendedModel()" class="p-4 rounded-xl border-2 border-indigo-500 bg-indigo-950/20">
            <div class="flex items-start justify-between gap-3">
              <div>
                <div class="flex items-center gap-2 mb-1">
                  <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-900 text-indigo-300 border border-indigo-700 font-medium uppercase tracking-wider">Recommended</span>
                  <span class="text-[10px] text-gray-500" x-text="modelSizeLabel(recommendedModel()?.id)"></span>
                </div>
                <div class="text-sm font-semibold text-white" x-text="recommendedModel()?.name || recommendedModel()?.id"></div>
                <div class="text-xs text-gray-500 mt-0.5">Good balance of speed and capability for most setups.</div>
                <div x-show="recommendedModel()?.size > 0" class="text-xs text-gray-600 mt-0.5" x-text="formatSize(recommendedModel()?.size)"></div>
              </div>
              <button @click="pickModel(recommendedModel()); goNext()"
                class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium transition-colors flex-shrink-0">
                Use this &rarr;
              </button>
            </div>
          </div>

          <!-- Size picker -->
          <details class="group" x-show="getModelSizeGroups().length > 1">
            <summary class="text-xs text-gray-500 hover:text-gray-300 cursor-pointer select-none list-none flex items-center gap-1.5 py-1">
              <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
              Choose a different size
            </summary>
            <div class="mt-3 space-y-2">
              <template x-for="group in getModelSizeGroups()" :key="group.key">
                <details class="group/inner">
                  <summary class="flex items-center justify-between p-3 rounded-xl bg-gray-900 border border-gray-800 cursor-pointer list-none hover:border-gray-700 transition-colors">
                    <div class="flex items-center gap-2">
                      <span class="text-sm font-medium text-white" x-text="group.label"></span>
                      <span class="text-xs text-gray-600" x-text="group.models.length + ' model' + (group.models.length!==1?'s':'')"></span>
                    </div>
                    <div class="flex items-center gap-2">
                      <span class="text-xs text-gray-600" x-text="group.ramHint"></span>
                      <svg class="w-3 h-3 text-gray-600 transition-transform group-open/inner:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                    </div>
                  </summary>
                  <div class="mt-1 space-y-1 pl-1">
                    <template x-for="m in group.models" :key="m.id">
                      <button @click="pickModel(m); $el.closest('details').open=false; $el.closest('details.group').open=false"
                        :class="selectedModel===m.id ? 'border-indigo-500 bg-indigo-950/30' : 'border-gray-800 hover:border-gray-700'"
                        class="w-full text-left px-3 py-2 rounded-lg bg-gray-900 border transition-all text-xs">
                        <div class="flex items-center justify-between">
                          <span class="text-white font-mono" x-text="m.id"></span>
                          <div class="flex items-center gap-2">
                            <span x-show="m.size>0" class="text-gray-600" x-text="formatSize(m.size)"></span>
                            <div :class="selectedModel===m.id ? 'bg-indigo-500 border-indigo-500':'border-gray-600'"
                              class="w-3.5 h-3.5 rounded-full border-2 flex items-center justify-center flex-shrink-0">
                              <div x-show="selectedModel===m.id" class="w-1.5 h-1.5 rounded-full bg-white"></div>
                            </div>
                          </div>
                        </div>
                      </button>
                    </template>
                  </div>
                </details>
              </template>
            </div>
          </details>

          <button @click="goNext()" :disabled="!selectedModel"
            x-show="selectedModel && !autoAdvancing"
            class="w-full py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium transition-colors">
            Continue &rarr;
          </button>
        </div>

        <!-- No models on Ollama: pull catalog -->
        <div x-show="getModels().length===0 && hasOllamaServer()" class="space-y-3">
          <p class="text-sm text-gray-400">No models downloaded yet. Pick one to pull now:</p>
          <div class="space-y-2">
            <template x-for="m in ollamaModelCatalog" :key="m.id">
              <div class="p-4 rounded-xl bg-gray-900 border border-gray-800 hover:border-gray-700 transition-all">
                <div class="flex items-start justify-between gap-3">
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 flex-wrap">
                      <span class="text-sm font-medium text-white" x-text="m.name"></span>
                      <span x-show="m.recommended" class="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-950 text-indigo-400 border border-indigo-800 font-medium">recommended</span>
                    </div>
                    <div class="text-xs text-gray-500 mt-1" x-text="m.desc"></div>
                    <div class="text-xs text-gray-600 mt-0.5"><span x-text="m.size"></span><span class="mx-1">&middot;</span><span x-text="m.ram"></span><span> on the inference machine</span></div>
                  </div>
                  <button @click="startPull(m.id)" :disabled="pulling"
                    class="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-medium transition-colors flex-shrink-0">
                    Download
                  </button>
                </div>
              </div>
            </template>
          </div>
          <div x-show="pulling" class="p-4 rounded-xl bg-gray-900 border border-gray-800 space-y-2">
            <div class="flex justify-between text-xs text-gray-400">
              <span x-text="pullStatus||'Downloading\u2026'"></span><span x-text="pullProgress+'%'"></span>
            </div>
            <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">
              <div class="h-full bg-indigo-500 rounded-full transition-all" :style="'width:'+pullProgress+'%'"></div>
            </div>
          </div>
          <div x-show="pullError" class="text-xs text-red-400" x-text="pullError"></div>
        </div>

        <!-- No models on LM Studio -->
        <div x-show="getModels().length===0 && !hasOllamaServer()" class="p-5 rounded-xl bg-gray-900 border border-gray-800">
          <div class="text-sm font-medium text-white mb-3">Load a model in LM Studio first</div>
          <ol class="space-y-2.5 text-sm text-gray-400 mb-4 list-none">
            <li class="flex gap-3"><span class="w-5 h-5 rounded-full bg-gray-700 text-white text-xs flex items-center justify-center flex-shrink-0 mt-0.5 font-medium">1</span><span>Open LM Studio and go to the <span class="text-white font-medium">Discover</span> tab &mdash; search for a model and click Download.</span></li>
            <li class="flex gap-3"><span class="w-5 h-5 rounded-full bg-gray-700 text-white text-xs flex items-center justify-center flex-shrink-0 mt-0.5 font-medium">2</span><span>Go to the <span class="text-white font-medium">Local Server</span> tab, select your model, and press <span class="text-white font-medium">Start Server</span>.</span></li>
            <li class="flex gap-3"><span class="w-5 h-5 rounded-full bg-gray-700 text-white text-xs flex items-center justify-center flex-shrink-0 mt-0.5 font-medium">3</span><span>Come back here and click Refresh.</span></li>
          </ol>
          <div class="text-xs text-gray-500 bg-gray-800/60 rounded-lg px-3 py-2 mb-4">Not sure? Search for <span class="font-mono text-indigo-300">Llama 3.2 3B</span> in Discover &mdash; fast, 2 GB, works on most machines.</div>
          <button @click="refreshModels()" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition-colors">Refresh model list</button>
        </div>
      </div>

      <!-- ── Step 3: Test it ─────────────────────────────────────────── -->
      <div x-show="step===3" x-cloak x-transition.opacity>
        <div class="mb-5">
          <h2 class="text-xl font-bold mb-1">Let&rsquo;s test it
            <span class="inline-flex ml-0.5 text-gray-600">
              <span style="animation:dot-fade 1.4s infinite;animation-delay:0s">.</span>
              <span style="animation:dot-fade 1.4s infinite;animation-delay:0.2s">.</span>
              <span style="animation:dot-fade 1.4s infinite;animation-delay:0.4s">.</span>
            </span>
          </h2>
          <p class="text-gray-400 text-sm">Sending a quick message to confirm everything is working.</p>
        </div>

        <!-- Model + server info -->
        <div class="flex items-center gap-2 mb-3 text-xs text-gray-600">
          <span class="font-mono text-gray-500" x-text="selectedModel"></span>
          <span>&middot;</span>
          <span x-text="activeServer ? (activeServer.type === 'ollama' ? 'Ollama' : 'LM Studio') : ''"></span>
          <span>&middot;</span>
          <span class="font-mono truncate max-w-[160px]" x-text="activeServer ? activeServer.endpoint.replace('/v1','') : ''"></span>
        </div>

        <div class="p-4 rounded-xl bg-gray-900 border border-gray-800 mb-4 min-h-[120px] flex flex-col justify-between">
          <div>
            <!-- Waiting -->
            <div x-show="testResponse === '' && !testError" class="flex items-center gap-2 text-gray-600 text-sm">
              <div class="flex gap-1">
                <div class="w-1.5 h-1.5 rounded-full bg-gray-700 animate-bounce" style="animation-delay:0ms"></div>
                <div class="w-1.5 h-1.5 rounded-full bg-gray-700 animate-bounce" style="animation-delay:150ms"></div>
                <div class="w-1.5 h-1.5 rounded-full bg-gray-700 animate-bounce" style="animation-delay:300ms"></div>
              </div>
              <span>Waiting for first token&hellip;</span>
            </div>
            <!-- Streaming -->
            <p x-show="testResponse !== ''" class="text-sm text-gray-100 leading-relaxed" x-text="testResponse"></p>
            <!-- Error -->
            <p x-show="testError" class="text-sm text-red-400" x-text="testError"></p>
          </div>
          <!-- Metrics footer -->
          <div x-show="testDone" class="flex flex-wrap items-center gap-x-3 gap-y-1 pt-3 mt-3 border-t border-gray-800">
            <div class="flex items-center gap-1.5">
              <div class="w-2 h-2 rounded-full bg-green-500 flex-shrink-0"></div>
              <span class="text-xs text-green-400 font-medium">Live</span>
            </div>
            <span class="text-xs text-gray-500" x-text="'Total ' + testLatency + 'ms'"></span>
            <span x-show="testTtft" class="text-xs text-gray-600" x-text="'&middot; TTFT ' + testTtft + 'ms'"></span>
            <span x-show="testLatency > 0 && testResponse.length > 0" class="text-xs text-gray-600"
              x-text="'&middot; ~' + Math.max(1, Math.round((testResponse.split(/\s+/).length) / (testLatency / 1000))) + ' tok/s'"></span>
          </div>
        </div>

        <div x-show="testError" class="mb-3">
          <button @click="runTest()"
            class="w-full py-2 rounded-xl border border-gray-700 hover:border-gray-500 text-xs text-gray-400 transition-colors">
            Try again
          </button>
        </div>

        <button @click="goNext()" :disabled="!testDone"
          class="w-full py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium transition-colors">
          Continue &rarr;
        </button>
      </div>

      <!-- ── Step 4: Done ────────────────────────────────────────────── -->
      <div x-show="step===4" x-cloak x-transition.opacity>
        <div class="text-center mb-8">
          <div class="w-16 h-16 rounded-2xl bg-green-950 border border-green-800 flex items-center justify-center mx-auto mb-5">
            <svg class="w-8 h-8 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
            </svg>
          </div>
          <h2 class="text-2xl font-bold mb-2">Logos is ready</h2>
          <p class="text-gray-400 text-sm">Your first assistant is configured and working.</p>
        </div>

        <div class="p-4 rounded-xl bg-gray-900 border border-gray-800 mb-6 divide-y divide-gray-800">
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Agent</span>
            <span class="text-white font-medium">Hermes</span>
          </div>
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Server</span>
            <span class="text-white font-medium"
              x-text="activeServer ? (activeServer.type === 'ollama' ? 'Ollama' : 'LM Studio') : '&mdash;'"></span>
          </div>
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Model</span>
            <span class="text-white font-medium truncate ml-4" x-text="selectedModel || '&mdash;'"></span>
          </div>
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Response time</span>
            <span class="text-white font-medium" x-text="testLatency ? testLatency + 'ms' : '&mdash;'"></span>
          </div>
        </div>

        <button @click="complete()" :disabled="completing"
          class="w-full py-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white font-semibold transition-colors">
          <span x-show="!completing">Open Logos &rarr;</span>
          <span x-show="completing" class="flex items-center justify-center gap-2">
            <div class="w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin"></div>
            Finishing up&hellip;
          </span>
        </button>
      </div>

    </div>
  </main>
</div>

<script>
function getCsrfToken() {
  return document.cookie.split(';')
    .map(c => c.trim())
    .find(c => c.startsWith('csrf_token='))
    ?.split('=')[1] ?? '';
}

function setup() {
  return {
    step: 0,
    track: null,

    // Step 1
    autoScanning: false,
    autoScanDone: false,
    foundServers: [],
    selectedServers: [],
    activeServer: null,
    autoAdvancing: false,
    showManualEntry: false,
    manualType: 'ollama',
    manualUrl: '',
    manualKey: '',
    manualProbing: false,
    manualError: '',
    osPlatform: 'mac',
    copied: '',

    // Step 2
    selectedModel: null,
    suggestedModel: null,
    pulling: false,
    pullProgress: 0,
    pullStatus: '',
    pullError: null,
    ollamaModelCatalog: [
      { id: 'qwen3:9b',           name: 'Qwen 3 9B',           size: '5.8 GB', ram: '16 GB system RAM',           desc: 'Strong reasoning, great at following instructions. Best default for most setups.', recommended: true  },
      { id: 'llama3.2:3b',        name: 'Llama 3.2 3B',        size: '2.0 GB', ram: '8 GB system RAM',            desc: 'Fast and lightweight. Use this on machines with less RAM.',                       recommended: false },
      { id: 'llama3.1:8b',        name: 'Llama 3.1 8B',        size: '4.9 GB', ram: '16 GB system RAM',           desc: 'Solid all-rounder with good reasoning.',                                          recommended: false },
      { id: 'qwen2.5-coder:7b',   name: 'Qwen 2.5 Coder 7B',  size: '4.7 GB', ram: '16 GB system RAM',           desc: 'Excellent for coding and technical questions.',                                   recommended: false },
      { id: 'gemma2:9b',          name: 'Gemma 2 9B',          size: '5.4 GB', ram: '16 GB system RAM',           desc: "Google\u2019s efficient model, strong instruction following.",                   recommended: false },
      { id: 'llama3.1:70b',       name: 'Llama 3.1 70B',       size: '40 GB',  ram: '48 GB+ system RAM — CPU+GPU hybrid works (e.g. 64 GB RAM + 16 GB VRAM)',           desc: 'Near-frontier quality. Hybrid CPU+GPU mode gives ~2–5 tok/s — great for research and long-form tasks.', recommended: false },
    ],

    // Step 3
    testResponse: '',
    testDone: false,
    testError: null,
    testLatency: null,
    testTtft: null,

    // Step 4
    completing: false,

    init() {
      const ua = navigator.userAgent;
      if (ua.includes('Win')) this.osPlatform = 'windows';
      else if (ua.includes('Linux')) this.osPlatform = 'linux';
      else this.osPlatform = 'mac';
    },

    selectTrack(track) {
      this.track = track;
      this.step = 1;
      this.$nextTick(() => this.autoDetect());
    },

    async autoDetect() {
      this.autoScanning = true;
      this.autoScanDone = false;
      this.foundServers = [];
      this.selectedServers = [];
      this.activeServer = null;
      try {
        const r = await fetch('/api/setup/probe', { credentials: 'include' });
        const d = await r.json();
        // Add _apiKey field for auth_required inline entry
        this.foundServers = (d.servers || [])
          .filter(s => s.status !== 'down')
          .map(s => ({ ...s, _apiKey: '' }));
        // Auto-select all 'up' servers
        this.selectedServers = this.foundServers.filter(s => s.status === 'up');
        this.activeServer = this.selectedServers[0] || null;
      } catch {
        this.foundServers = [];
      }
      this.autoScanning = false;
      this.autoScanDone = true;
    },

    isServerSelected(s) {
      return this.selectedServers.some(x => x.endpoint === s.endpoint);
    },

    toggleServer(s) {
      const idx = this.selectedServers.findIndex(x => x.endpoint === s.endpoint);
      if (idx >= 0) this.selectedServers.splice(idx, 1);
      else this.selectedServers.push(s);
      this.activeServer = this.selectedServers[0] || null;
    },

    async retryWithKey(s) {
      const base = s.endpoint.replace('/v1', '');
      try {
        const params = new URLSearchParams({ url: base, prefer: 'lmstudio', api_key: s._apiKey });
        const r = await fetch('/api/setup/probe?' + params, { credentials: 'include' });
        const d = await r.json();
        const result = (d.servers || [])[0];
        if (result && result.status === 'up') {
          const idx = this.foundServers.findIndex(x => x.endpoint === s.endpoint);
          if (idx >= 0) this.foundServers[idx] = { ...result, _apiKey: s._apiKey };
          else this.foundServers.push({ ...result, _apiKey: s._apiKey });
          const updated = this.foundServers[idx >= 0 ? idx : this.foundServers.length - 1];
          if (!this.selectedServers.find(x => x.endpoint === updated.endpoint)) {
            this.selectedServers.push(updated);
          }
          this.activeServer = this.selectedServers[0] || null;
        }
      } catch {}
    },

    async addManualServer() {
      this.manualProbing = true;
      this.manualError = '';
      try {
        const params = new URLSearchParams({ url: this.manualUrl.trim(), prefer: this.manualType });
        if (this.manualType === 'lmstudio' && this.manualKey.trim()) params.set('api_key', this.manualKey.trim());
        const r = await fetch('/api/setup/probe?' + params, { credentials: 'include' });
        const d = await r.json();
        const server = (d.servers || [])[0];
        if (server && server.status === 'up') {
          const enriched = { ...server, _apiKey: this.manualKey };
          if (!this.foundServers.find(s => s.endpoint === server.endpoint)) this.foundServers.push(enriched);
          if (!this.selectedServers.find(s => s.endpoint === server.endpoint)) this.selectedServers.push(enriched);
          this.activeServer = this.selectedServers[0] || null;
          this.showManualEntry = false;
          this.manualUrl = '';
          this.manualKey = '';
        } else {
          this.manualError = server?.status === 'auth_required'
            ? 'Auth required \u2014 enter an API key'
            : 'Could not connect. Check the address and that the server is running.';
        }
      } catch (e) {
        this.manualError = 'Connection error: ' + e.message;
      }
      this.manualProbing = false;
    },

    goNext() {
      if (this.step === 1) {
        this.step = 2;
        this.$nextTick(() => this.initModelStep());
        return;
      }
      if (this.step === 2) { this.step = 3; this.$nextTick(() => this.runTest()); return; }
      if (this.step === 3) { this.step = 4; return; }
    },

    initModelStep() {
      const models = this.getModels();
      if (models.length === 0) return;
      const rec = this.recommendedModel();
      if (rec) {
        this.pickModel(rec);
        // Auto-advance if every model is in the same size bucket (clear winner)
        const sizes = new Set(models.map(m => this.modelSize(m.id)));
        if (sizes.size === 1) {
          this.autoAdvancing = true;
          setTimeout(() => {
            if (this.step === 2) { this.autoAdvancing = false; this.step = 3; this.$nextTick(() => this.runTest()); }
          }, 2000);
        }
      }
    },

    getModels() {
      const seen = new Set();
      const out = [];
      for (const s of this.selectedServers) {
        for (const m of (s.models || [])) {
          if (!seen.has(m.id)) { seen.add(m.id); out.push({ ...m, _serverEndpoint: s.endpoint, _serverType: s.type }); }
        }
      }
      return out;
    },

    pickModel(m) {
      this.selectedModel = m.id;
      const server = this.selectedServers.find(s => (s.models || []).some(x => x.id === m.id));
      if (server) this.activeServer = server;
    },

    modelSize(id) {
      if (!id) return 'unknown';
      const match = (id || '').toLowerCase().match(/(\d+\.?\d*)b/);
      if (!match) return 'unknown';
      const b = parseFloat(match[1]);
      if (b < 3)  return 'xs';
      if (b < 7)  return 's';
      if (b < 13) return 'm';
      if (b < 35) return 'l';
      return 'xl';
    },

    modelSizeLabel(id) {
      const labels = { xs: 'Tiny (<3B)', s: 'Small (3\u20136B)', m: 'Medium (7\u201312B)', l: 'Large (13\u201334B)', xl: 'Extra Large (35B+)', unknown: '' };
      return labels[this.modelSize(id)] || '';
    },

    recommendedModel() {
      const models = this.getModels();
      if (!models.length) return null;
      // Preferred specific models in order — good agentic capability vs. resource use
      const preferred = ['qwen3:9b','qwen2.5:9b','qwen3:8b','qwen2.5:7b','llama3.1:8b','llama3.2:3b','gemma2:9b'];
      for (const p of preferred) {
        const found = models.find(m => m.id === p || m.id.startsWith(p.split(':')[0] + ':'));
        if (found) return found;
      }
      // Fall back to best available size: medium → small → large → anything
      for (const size of ['m', 's', 'l', 'xs', 'xl']) {
        const found = models.find(m => this.modelSize(m.id) === size);
        if (found) return found;
      }
      return models[0];
    },

    getModelSizeGroups() {
      const models = this.getModels();
      const info = {
        xs: { label: 'Tiny',        order: 0, ramHint: '< 8 GB RAM' },
        s:  { label: 'Small',       order: 1, ramHint: '8\u201316 GB RAM' },
        m:  { label: 'Medium',      order: 2, ramHint: '16 GB RAM' },
        l:  { label: 'Large',       order: 3, ramHint: '16\u201332 GB RAM' },
        xl: { label: 'Extra Large', order: 4, ramHint: '48+ GB RAM or GPU' },
      };
      const groups = {};
      for (const m of models) {
        const key = this.modelSize(m.id);
        if (!groups[key]) groups[key] = { key, ...info[key] || { label: key, order: 9, ramHint: '' }, models: [] };
        groups[key].models.push(m);
      }
      return Object.values(groups).sort((a, b) => a.order - b.order);
    },

    modelStepSubtitle() {
      const total = this.getModels().length;
      const servers = this.selectedServers.length;
      if (total === 0) return 'No models loaded yet';
      return total + ' model' + (total !== 1 ? 's' : '') + ' found across ' + servers + ' server' + (servers !== 1 ? 's' : '');
    },

    hasOllamaServer() {
      return this.selectedServers.some(s => s.type === 'ollama');
    },

    formatSize(bytes) {
      if (!bytes) return '';
      const gb = bytes / 1e9;
      return gb >= 1 ? gb.toFixed(1) + ' GB' : (bytes / 1e6).toFixed(0) + ' MB';
    },

    async startPull(modelName) {
      this.pulling = true;
      this.pullProgress = 0;
      this.pullStatus = 'Starting download\u2026';
      this.pullError = null;
      try {
        const r = await fetch('/api/setup/pull', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          body: JSON.stringify({
            model: modelName,
            base_url: this.activeServer ? this.activeServer.endpoint.replace(/\/v1$/, '') : 'http://localhost:11434',
          }),
        });
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\\n'); buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const ev = JSON.parse(line.slice(6));
              if (ev.error) { this.pullError = ev.error; this.pulling = false; return; }
              this.pullProgress = ev.pct || 0;
              this.pullStatus = ev.status || '';
              if (ev.done) {
                this.pulling = false;
                this.selectedModel = modelName;
                if (this.activeServer) this.activeServer.models = [{ id: modelName, name: modelName, size: 0 }];
              }
            } catch {}
          }
        }
      } catch (e) {
        this.pullError = e.message;
        this.pulling = false;
      }
    },

    async refreshModels() {
      if (!this.activeServer) return;
      try {
        const baseUrl = this.activeServer.endpoint.replace(/\/v1$/, '');
        const params = new URLSearchParams({ url: baseUrl });
        if (this.activeServer._apiKey && this.activeServer._apiKey.trim()) params.set('api_key', this.activeServer._apiKey.trim());
        const r = await fetch('/api/setup/probe?' + params, { credentials: 'include' });
        const d = await r.json();
        const found = (d.servers || []).find(s => s.status === 'up');
        if (found) this.activeServer = { ...found, _apiKey: this.activeServer._apiKey || '' };
      } catch {}
    },

    async runTest() {
      this.testResponse = ''; this.testDone = false;
      this.testError = null; this.testLatency = null; this.testTtft = null;
      const t0 = Date.now();
      try {
        const r = await fetch('/api/setup/test', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          body: JSON.stringify({
            endpoint: this.activeServer ? this.activeServer.endpoint : '',
            model: this.selectedModel,
            api_key: (this.activeServer && this.activeServer._apiKey) || 'ollama',
          }),
        });
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\\n'); buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const ev = JSON.parse(line.slice(6));
              if (ev.error) { this.testError = ev.error; return; }
              if (ev.token) {
                if (this.testTtft === null) this.testTtft = Date.now() - t0;
                this.testResponse += ev.token;
              }
              if (ev.done) { this.testDone = true; this.testLatency = ev.latency; }
            } catch {}
          }
        }
      } catch (e) {
        this.testError = 'Connection error \u2014 ' + e.message;
      }
    },

    async complete() {
      this.completing = true;
      try {
        const r = await fetch('/api/setup/complete', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          body: JSON.stringify({
            endpoint: this.activeServer ? this.activeServer.endpoint : '',
            model: this.selectedModel,
            server_type: this.activeServer ? this.activeServer.type : '',
          }),
        });
        if (r.ok) { window.location.href = '/'; return; }
        const d = await r.json().catch(() => ({}));
        console.error('setup/complete failed', r.status, d);
        alert(d.error || 'Setup failed \u2014 please try again. (Check browser console for details)');
        this.completing = false;
      } catch (e) {
        console.error('setup/complete error', e);
        alert('Error: ' + e.message);
        this.completing = false;
      }
    },

    copy(text) {
      navigator.clipboard.writeText(text).catch(() => {});
      this.copied = text;
      setTimeout(() => { this.copied = ''; }, 1500);
    },
  };
}
</script>
</body>
</html>"""


async def _handle_setup_page(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if is_setup_completed():
        raise web.HTTPFound("/")
    return web.Response(text=_SETUP_HTML, content_type="text/html")


async def _handle_setup_status(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    return web.json_response({"completed": is_setup_completed()})


async def _handle_setup_reset(request: web.Request) -> web.Response:
    from gateway.auth.db import reset_setup_completed, write_audit_log
    user_id = request["current_user"]["sub"]
    reset_setup_completed()
    write_audit_log(user_id, "setup_reset", ip_address=request.remote)
    return web.json_response({"ok": True})


async def _handle_index(request: web.Request) -> web.Response:
    inject = f'<script>window.__LOGOS__={{isCanary:{str(_IS_CANARY).lower()}}};</script>'
    html = _ADMIN_HTML.replace("</head>", inject + "</head>", 1)
    return web.Response(text=html, content_type="text/html")


async def _handle_login_page(request: web.Request) -> web.Response:
    html = _LOGIN_HTML.replace("__VERSION_LABEL__", _VERSION_LABEL)
    return web.Response(text=html, content_type="text/html")


async def _handle_status(request: web.Request) -> web.Response:
    runner: Any = request.app["runner"]
    uptime = int(time.time() - _start_time)
    now = time.time()

    active = []
    for session_key, s in list(runner._session_status.items()):
        tool_started = s.get("tool_started_at") or now
        session_started = s.get("session_started_at") or now

        # Pull live token counts from the running agent if available
        agent = runner._running_agents.get(session_key)
        prompt_tokens = 0
        completion_tokens = 0
        api_calls = 0
        if agent is not None:
            prompt_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
            completion_tokens = getattr(agent, "session_completion_tokens", 0) or 0
            api_calls = getattr(agent, "session_api_calls", 0) or 0

        active.append({
            "session_key": session_key,
            "platform": s.get("platform", "unknown"),
            "current_tool": s.get("current_tool", "unknown"),
            "elapsed_s": int(now - tool_started),
            "tool_started_at": tool_started,
            "tool_count": s.get("tool_count", 0),
            "error_count": s.get("error_count", 0),
            "recent_tools": s.get("recent_tools", []),
            "stuck": s.get("stuck", False),
            "session_started_at": session_started,
            "elapsed_session_s": int(now - session_started),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "api_calls": api_calls,
        })

    # Recent completed sessions (ring buffer, newest last)
    recent = list(getattr(runner, "_recent_sessions", []))

    return web.json_response({
        "status": "ok",
        "uptime_s": uptime,
        "instance_name": _INSTANCE_NAME,
        "active_sessions": active,
        "recent_sessions": recent,
    })


async def _handle_canary_status(request: web.Request) -> web.Response:
    """Check if the canary pod is alive by probing its in-cluster health endpoint."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _CANARY_HEALTH_URL,
                timeout=aiohttp.ClientTimeout(total=2),
            ) as r:
                return web.json_response({"active": r.status < 400})
    except Exception:
        return web.json_response({"active": False})


async def _handle_proxy_state(request: web.Request) -> web.Response:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_AI_ROUTER_BASE}/admin/state",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                from gateway import admin_handlers
                routes = data.get("routes", {})
                data["route_model_classes"] = {
                    alias: admin_handlers.ALIAS_TO_CLASS.get(alias, "general")
                    for alias in routes
                }
                return web.json_response(data)
    except Exception as e:
        return web.json_response({
            "providers": {},
            "routes": {},
            "route_model_classes": {},
            "grafana_url": "http://192.168.1.253:3200",
            "_error": str(e),
        })


@require_permission("manage_platform")
@require_csrf
async def _handle_proxy_toggle(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_AI_ROUTER_BASE}/admin/providers/{key}/toggle",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                return web.json_response(data)
    except Exception as e:
        raise web.HTTPBadGateway(reason=str(e))


async def _handle_proxy_models_live(request: web.Request) -> web.Response:
    """GET /proxy/models-live — proxy to ai-router /admin/models-live."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_AI_ROUTER_BASE}/admin/models-live",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                return web.json_response(await r.json())
    except Exception as e:
        return web.json_response({"providers": {}, "_error": str(e)})


@require_permission("manage_machines")
@require_csrf
async def _handle_proxy_benchmark(request: web.Request) -> web.Response:
    """POST /proxy/benchmark — proxy to ai-router /admin/benchmark."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_AI_ROUTER_BASE}/admin/benchmark",
                json=body,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                return web.json_response(await r.json())
    except Exception as e:
        raise web.HTTPBadGateway(reason=str(e))


async def _handle_routing_claims(request: web.Request) -> web.Response:
    """GET /internal/routing/claims — full machine→user claim map for the MCP routing tool."""
    claims = auth_db.list_all_claims()
    machines = auth_db.list_machines()
    users = auth_db.list_users(limit=500)
    return web.json_response({
        "claims": claims,
        "machines": machines,
        "users": [{"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                   "email": u["email"], "policy_id": u.get("policy_id")} for u in users],
    })


async def _handle_routing_apply(request: web.Request) -> web.Response:
    """POST /internal/routing/apply — Hermes MCP tool applies a suggested profile.
    Body: {"user_id": str, "policy_name": str, "description": str, "rules": [...], "fallback": str}
    """
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid_json")

    user_id = body.get("user_id")
    policy_name = body.get("policy_name")
    rules = body.get("rules", [])
    description = body.get("description", "Auto-configured by Hermes")
    fallback = body.get("fallback", "any_available")

    if not user_id or not policy_name:
        raise web.HTTPBadRequest(reason="user_id and policy_name required")

    user = auth_db.get_user_by_id(user_id)
    if not user:
        raise web.HTTPNotFound(reason="user_not_found")

    # Create or reuse policy with this name
    existing = next((p for p in auth_db.list_policies() if p["name"] == policy_name), None)
    if existing:
        policy = auth_db.update_policy(existing["id"], description=description, fallback=fallback)
        pid = existing["id"]
    else:
        policy = auth_db.create_policy(policy_name, description=description, fallback=fallback)
        pid = policy["id"]

    auth_db.set_policy_rules(pid, rules)
    auth_db.assign_user_policy(user_id, pid)

    return web.json_response({"ok": True, "policy": auth_db.get_policy(pid),
                              "rules": auth_db.get_policy_rules(pid)})


async def _handle_souls_get(request: web.Request) -> web.Response:
    registry = _get_soul_registry()
    return web.json_response({"souls": [s.to_dict() for s in registry.values()]})


async def _handle_soul_detail(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    registry = _get_soul_registry()
    soul = registry.get(slug)
    if not soul:
        raise web.HTTPNotFound(reason=f"soul not found: {slug}")
    return web.json_response(soul.to_dict(include_soul_md=True))


async def _handle_instances_get(request: web.Request) -> web.Response:
    loop = asyncio.get_event_loop()
    try:
        res = await loop.run_in_executor(None, _cluster_resources)
    except Exception as e:
        res = {"_error": str(e)}
    try:
        inst = await loop.run_in_executor(None, _list_hermes_instances)
    except Exception as e:
        inst = []
        if "_error" not in res:
            res = {"_error": str(e)}
    caller = request.get("current_user") or {}
    caller_role = caller.get("role", "viewer")
    caller_name = (caller.get("display_name") or caller.get("username") or "").lower()
    # Non-admins only see instances spawned for themselves
    if caller_role not in ("admin", "operator"):
        inst = [i for i in inst if i.get("requester", "").lower() == caller_name]
    return web.json_response({
        "instances": inst,
        "resources": res,
        "queue": _instance_queue,
    })


@require_csrf
async def _handle_instances_post(request: web.Request) -> web.Response:
    caller = request.get("current_user") or {}
    caller_role = caller.get("role", "viewer")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    requester = (body.get("requester") or "").strip()
    if not requester:
        return web.json_response({"error": "requester is required"}, status=400)

    soul_slug = (body.get("soul_slug") or "general").strip()
    tool_overrides = body.get("tool_overrides") or {}
    model_alias = (body.get("model_alias") or "balanced").strip()
    machine_id_override = body.get("machine_id") or None

    # Validate soul and overrides before checking resources
    registry = _get_soul_registry()
    soul = registry.get(soul_slug)
    if not soul:
        return web.json_response(
            {"error": "soul_not_found", "soul_slug": soul_slug},
            status=400,
        )

    # RBAC: check if caller can spawn this soul
    if not can_spawn(caller_role, soul.to_dict()):
        return web.json_response(
            {"error": "forbidden", "message": "You don't have permission to spawn this soul"},
            status=403,
        )

    from gateway.auth.rbac import has_permission as _has_perm

    # RBAC: machine routing override requires override_routing permission
    if machine_id_override and not _has_perm(caller_role, "override_routing"):
        return web.json_response(
            {"error": "forbidden", "message": "Machine routing override requires operator or admin role"},
            status=403,
        )

    # RBAC: toolset overrides require override_toolsets permission
    if tool_overrides and not _has_perm(caller_role, "override_toolsets"):
        return web.json_response(
            {"error": "forbidden", "message": "Toolset overrides require operator or admin role"},
            status=403,
        )

    try:
        _validate_soul_overrides(soul, tool_overrides)
    except ValueError as exc:
        code, _, detail = str(exc).partition(":")
        messages = {
            "cannot_remove_enforced": f"toolset '{detail}' is enforced by soul '{soul_slug}' and cannot be removed",
            "toolset_not_available": f"toolset '{detail}' is forbidden by soul '{soul_slug}'",
            "toolset_not_in_soul": f"toolset '{detail}' is not in the optional list for soul '{soul_slug}'",
        }
        return web.json_response(
            {"error": code, "message": messages.get(code, str(exc)), "toolset": detail},
            status=400,
        )

    # Resolve routing — must happen before spawn so we can pin the machine
    caller_id = caller.get("sub")
    try:
        route = await admin_handlers.resolve_route(
            user_id=caller_id,
            model_alias=model_alias,
            machine_id_override=machine_id_override,
        )
    except admin_handlers.RoutingError as exc:
        return web.json_response(
            {"error": "routing_failed", "message": str(exc), "profile": exc.profile_name},
            status=503,
        )

    resolved_machine   = route["machine"]
    resolved_endpoint  = resolved_machine["endpoint_url"] if resolved_machine else None
    resolved_machine_name = resolved_machine["name"] if resolved_machine else None
    resolved_machine_id   = resolved_machine["id"]   if resolved_machine else None
    logger.info(
        "routing resolved: user=%s model=%s layer=%s machine=%s",
        caller_id, model_alias, route["layer"],
        resolved_machine_name or "none",
    )

    loop = asyncio.get_event_loop()

    # Check resources
    try:
        res = await loop.run_in_executor(None, _cluster_resources)
        has_cpu = res.get("free_cpu", 0) >= _SPAWN_CPU_THRESHOLD
        has_mem = res.get("free_mem", 0) >= _SPAWN_MEM_THRESHOLD
    except Exception:
        has_cpu = has_mem = False  # k8s unavailable — queue it

    if not has_cpu or not has_mem:
        reason = f"insufficient resources (free: {res.get('free_cpu',0):.1f} CPU, {res.get('free_mem',0)//1024**3}Gi RAM)"
        _instance_queue.append({"requester": requester, "soul_slug": soul_slug, "reason": reason, "requested_at": time.time()})
        logger.info("Instance request queued for %s: %s", requester, reason)
        return web.json_response({"status": "queued", "requester": requester, "reason": reason})

    try:
        result = await loop.run_in_executor(
            None, _spawn_instance,
            requester, soul_slug, tool_overrides,
            model_alias, resolved_endpoint, resolved_machine_name, resolved_machine_id,
        )
    except Exception as e:
        logger.exception("Failed to spawn instance for %s", requester)
        return web.json_response({"error": "spawn_failed", "message": str(e)}, status=500)

    # Log routing decision
    auth_db.log_routing_decision(
        user_id=caller_id,
        model_alias=model_alias,
        model_class=route["model_class"],
        machine_id=resolved_machine_id,
        machine_name=resolved_machine_name,
        layer=route["layer"],
        instance_name=f"Hermes for {requester}",
    )

    # Audit: who spawned what
    auth_db.write_audit_log(
        caller.get("sub"), "spawn_instance",
        target_type="instance", target_id=requester,
        metadata={
            "soul_slug": soul_slug,
            "requester": requester,
            "model_alias": model_alias,
            "machine": resolved_machine_name,
            "routing_layer": route["layer"],
        },
        ip_address=request.remote,
    )

    # Try to resolve NodePort (may take a moment to assign)
    await asyncio.sleep(1)
    try:
        instances = await loop.run_in_executor(None, _list_hermes_instances)
        dep_name = _safe_k8s_name(requester)
        match = next((i for i in instances if i["name"] == dep_name), {})
        result["node_port"] = match.get("node_port")
        result["instance_name"] = match.get("instance_name", f"Hermes for {requester}")
    except Exception:
        pass

    return web.json_response(result)


@require_permission("delete_instance")
@require_csrf
async def _handle_instances_delete(request: web.Request) -> web.Response:
    name   = request.match_info["name"]
    caller = request.get("current_user") or {}
    if name == "hermes":
        raise web.HTTPForbidden(reason="Cannot delete the primary hermes deployment")
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _delete_instance, name)
    except Exception as e:
        raise web.HTTPInternalServerError(reason=str(e))
    auth_db.write_audit_log(
        caller.get("sub"), "delete_instance",
        target_type="instance", target_id=name,
        ip_address=request.remote,
    )
    return web.json_response({"status": "deleted", "name": name})


def _spawn_templates_path() -> Path:
    return _hermes_home / "spawn_templates.json"


def _read_spawn_templates() -> list:
    p = _spawn_templates_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _write_spawn_templates(templates: list) -> None:
    p = _spawn_templates_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(templates, indent=2))


@require_permission("view_instances")
async def _handle_spawn_templates_get(request: web.Request) -> web.Response:
    return web.json_response(_read_spawn_templates())


@require_permission("view_instances")
@require_csrf
async def _handle_spawn_templates_put(request: web.Request) -> web.Response:
    """Replace the full list (client sends the already-deduped, ordered list)."""
    body = await request.json()
    if not isinstance(body, list):
        raise web.HTTPBadRequest(reason="Expected a JSON array")
    _write_spawn_templates(body[:12])
    return web.json_response({"status": "ok"})


@require_permission("view_instances")
@require_csrf
async def _handle_spawn_templates_delete(request: web.Request) -> web.Response:
    tpl_id = request.match_info["id"]
    templates = [t for t in _read_spawn_templates() if str(t.get("id")) != tpl_id]
    _write_spawn_templates(templates)
    return web.json_response({"status": "ok"})


async def _handle_logo(request: web.Request) -> web.Response:
    """Serve the chat logo image from the baked-in app directory."""
    import pathlib
    logo = pathlib.Path("/app/chat_logo.png")
    if not logo.exists():
        raise web.HTTPNotFound()
    data = logo.read_bytes()
    return web.Response(
        body=data,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _handle_health(request: web.Request) -> web.Response:
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    uptime = int(time.time() - _start_time)
    return web.json_response({
        "status": "ok",
        "sessions": len(sessions),
        "uptime_s": uptime,
    })


async def _handle_health_ready(request: web.Request) -> web.Response:
    """Deep readiness check: verifies auth DB and soul registry are operational.

    Returns 200 when ready, 503 when not. Used as the k8s readiness probe so
    traffic is only sent to pods that have fully initialised their subsystems.
    """
    checks: dict[str, str] = {}
    ok = True

    # Auth DB — a simple list_users call exercises the connection
    try:
        auth_db.list_users(limit=1)
        checks["auth_db"] = "ok"
    except Exception as exc:
        checks["auth_db"] = f"fail: {exc}"
        ok = False

    # Soul registry — must have loaded at least one soul
    souls = _SOUL_REGISTRY
    if souls:
        checks["souls"] = f"ok ({len(souls)} loaded)"
    else:
        checks["souls"] = "empty"
        ok = False

    status = 200 if ok else 503
    return web.json_response(
        {"status": "ready" if ok else "not_ready", "checks": checks},
        status=status,
    )


async def _handle_sessions(request: web.Request) -> web.Response:
    if not _check_auth(request):
        raise web.HTTPUnauthorized()
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    return web.json_response([s.to_dict() for s in sessions])


async def _handle_transcribe(request: web.Request) -> web.Response:
    """POST /chat/transcribe — accept a webm/wav/ogg audio blob, return transcript."""
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "audio":
            return web.json_response({"error": "missing audio field"}, status=400)
        audio_bytes = await field.read(decode=True)
    except Exception as e:
        return web.json_response({"error": f"read failed: {e}"}, status=400)

    if not audio_bytes:
        return web.json_response({"error": "empty audio"}, status=400)

    # Write to a temp file so transcribe_audio can read it
    import tempfile
    suffix = ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        from tools.transcription_tools import transcribe_audio
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, transcribe_audio, tmp_path),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            result = {"success": False, "error": "transcription timed out (30s)"}
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    if not result.get("success"):
        return web.json_response({"error": result.get("error", "transcription failed")}, status=500)

    return web.json_response({"transcript": result.get("transcript", "")})


# ── Action policy handlers ─────────────────────────────────────────────────

async def _handle_action_policies_list(request: web.Request) -> web.Response:
    rows = auth_db.list_action_policies()
    return web.json_response({"action_policies": rows})


async def _handle_action_policies_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    name = body.get("name", "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="name required")
    try:
        row = auth_db.create_action_policy(
            name=name,
            description=body.get("description", ""),
            network_policy=body.get("network_policy", "internet_enabled"),
            network_allowlist=body.get("network_allowlist", "[]")
                if isinstance(body.get("network_allowlist"), str)
                else json.dumps(body.get("network_allowlist", [])),
            filesystem_policy=body.get("filesystem_policy", "workspace_only"),
            exec_policy=body.get("exec_policy", "restricted"),
            write_policy=body.get("write_policy", "auto_apply"),
            provider_policy=body.get("provider_policy", "any"),
            secret_policy=body.get("secret_policy", "tool_only"),
        )
    except Exception as e:
        raise web.HTTPConflict(reason=str(e))
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "create_action_policy",
        target_type="action_policy", target_id=row["id"],
    )
    return web.json_response({"action_policy": row}, status=201)


async def _handle_action_policies_get(request: web.Request) -> web.Response:
    row = auth_db.get_action_policy(request.match_info["id"])
    if not row:
        raise web.HTTPNotFound(reason="Action policy not found")
    return web.json_response({"action_policy": row})


async def _handle_action_policies_patch(request: web.Request) -> web.Response:
    policy_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    # Serialise allowlist if passed as list
    if "network_allowlist" in body and isinstance(body["network_allowlist"], list):
        body["network_allowlist"] = json.dumps(body["network_allowlist"])
    row = auth_db.update_action_policy(policy_id, **body)
    if not row:
        raise web.HTTPNotFound(reason="Action policy not found")
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "update_action_policy",
        target_type="action_policy", target_id=policy_id,
    )
    return web.json_response({"action_policy": row})


async def _handle_action_policies_delete(request: web.Request) -> web.Response:
    policy_id = request.match_info["id"]
    deleted = auth_db.delete_action_policy(policy_id)
    if not deleted:
        raise web.HTTPNotFound(reason="Action policy not found")
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "delete_action_policy",
        target_type="action_policy", target_id=policy_id,
    )
    return web.json_response({"deleted": True})


async def _handle_user_action_policy_patch(request: web.Request) -> web.Response:
    user_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    policy_id = body.get("action_policy_id")  # None to clear
    auth_db.assign_user_action_policy(user_id, policy_id)
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "assign_action_policy",
        target_type="user", target_id=user_id,
        metadata={"action_policy_id": policy_id},
    )
    return web.json_response({"user_id": user_id, "action_policy_id": policy_id})


# ── Approval request handlers ──────────────────────────────────────────────

async def _handle_approvals_list(request: web.Request) -> web.Response:
    current_user = request.get("current_user") or {}
    role = current_user.get("role", "viewer")
    user_id = current_user.get("sub")
    # Non-admin/operator users can only see their own session's approvals
    session_id = request.rel_url.query.get("session_id")
    status_filter = request.rel_url.query.get("status")
    if role not in ("admin", "operator") and not session_id:
        # Safety: require session_id for non-privileged users
        raise web.HTTPForbidden(reason="session_id required for non-admin users")
    page = int(request.rel_url.query.get("page", 1))
    rows, total = auth_db.list_approval_requests(
        session_id=session_id, status=status_filter, page=page
    )
    return web.json_response({"approvals": rows, "total": total, "page": page})


async def _handle_approvals_get(request: web.Request) -> web.Response:
    row = auth_db.get_approval_request(request.match_info["id"])
    if not row:
        raise web.HTTPNotFound(reason="Approval request not found")
    return web.json_response({"approval": row})


async def _handle_approvals_approve(request: web.Request) -> web.Response:
    approval_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = body.get("note", "")
    decided_by = (request.get("current_user") or {}).get("sub")
    updated = auth_db.resolve_approval_request(approval_id, "approved", decided_by, note)
    if not updated:
        row = auth_db.get_approval_request(approval_id)
        if not row:
            raise web.HTTPNotFound(reason="Approval request not found")
        raise web.HTTPConflict(reason=f"Request is already {row['status']}")
    auth_db.write_audit_log(
        decided_by, "approve_tool_request",
        target_type="approval_request", target_id=approval_id,
        metadata={"note": note},
    )
    return web.json_response({"approved": True, "approval_id": approval_id})


async def _handle_approvals_reject(request: web.Request) -> web.Response:
    approval_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = body.get("note", "")
    decided_by = (request.get("current_user") or {}).get("sub")
    updated = auth_db.resolve_approval_request(approval_id, "rejected", decided_by, note)
    if not updated:
        row = auth_db.get_approval_request(approval_id)
        if not row:
            raise web.HTTPNotFound(reason="Approval request not found")
        raise web.HTTPConflict(reason=f"Request is already {row['status']}")
    auth_db.write_audit_log(
        decided_by, "reject_tool_request",
        target_type="approval_request", target_id=approval_id,
        metadata={"note": note},
    )
    return web.json_response({"rejected": True, "approval_id": approval_id})


# ── Workflow handlers ──────────────────────────────────────────────────────

async def _handle_workflows_list(request: web.Request) -> web.Response:
    rows = auth_db.list_workflow_definitions()
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflows": [_WD.from_row(r).to_dict() for r in rows]})


async def _handle_workflows_post(request: web.Request) -> web.Response:
    caller = request.get("current_user") or {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    # Validate steps
    steps_raw = body.get("steps", [])
    if not isinstance(steps_raw, list):
        return web.json_response({"error": "steps must be an array"}, status=400)
    try:
        from workflows.model import StepDefinition as _SD
        _ = [_SD.from_dict(s) for s in steps_raw]
    except Exception as exc:
        return web.json_response({"error": f"invalid step definition: {exc}"}, status=400)

    import json as _json
    row = auth_db.create_workflow_definition(
        name=name,
        steps_json=_json.dumps(steps_raw),
        description=body.get("description", ""),
        version=body.get("version", "1.0"),
        tags=_json.dumps(body.get("tags", [])),
        created_by=caller.get("sub"),
    )
    auth_db.write_audit_log(
        caller.get("sub"), "create_workflow",
        target_type="workflow", target_id=row["id"],
        metadata={"name": name},
    )
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()}, status=201)


async def _handle_workflows_get(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    row = auth_db.get_workflow_definition(wf_id)
    if not row:
        raise web.HTTPNotFound(reason="Workflow not found")
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()})


async def _handle_workflows_patch(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    import json as _json
    kwargs = {}
    if "name" in body:
        kwargs["name"] = body["name"]
    if "description" in body:
        kwargs["description"] = body["description"]
    if "version" in body:
        kwargs["version"] = body["version"]
    if "tags" in body:
        kwargs["tags"] = _json.dumps(body["tags"])
    if "steps" in body:
        try:
            from workflows.model import StepDefinition as _SD
            _ = [_SD.from_dict(s) for s in body["steps"]]
            kwargs["steps_json"] = _json.dumps(body["steps"])
        except Exception as exc:
            return web.json_response({"error": f"invalid step definition: {exc}"}, status=400)
    row = auth_db.update_workflow_definition(wf_id, **kwargs)
    if not row:
        raise web.HTTPNotFound(reason="Workflow not found")
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()})


async def _handle_workflows_delete(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    deleted = auth_db.delete_workflow_definition(wf_id)
    if not deleted:
        raise web.HTTPNotFound(reason="Workflow not found")
    auth_db.write_audit_log(
        caller.get("sub"), "delete_workflow",
        target_type="workflow", target_id=wf_id,
    )
    return web.json_response({"deleted": True})


async def _handle_workflow_trigger(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    caller_id = caller.get("sub")
    try:
        body = await request.json()
    except Exception:
        body = {}
    inputs = body.get("inputs") or {}

    # Resolve caller's action policy for the run.
    _action_policy = None
    if caller_id and caller_id.startswith("usr_"):
        try:
            from gateway.auth.policy import ActionPolicy as _AP
            _pr = auth_db.get_user_action_policy_row(caller_id)
            _action_policy = _AP.from_row(_pr) if _pr else None
        except Exception:
            pass

    engine = request.app.get("workflow_engine")
    if not engine:
        return web.json_response({"error": "workflow engine not available"}, status=503)
    try:
        run_id = await engine.start_run(
            workflow_id=wf_id,
            triggered_by=caller_id,
            inputs=inputs,
            action_policy=_action_policy,
            auth_user_id=caller_id,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except Exception as exc:
        logger.exception("Failed to start workflow run")
        return web.json_response({"error": str(exc)}, status=500)

    auth_db.write_audit_log(
        caller_id, "trigger_workflow",
        target_type="workflow_run", target_id=run_id,
        metadata={"workflow_id": wf_id, "inputs": inputs},
    )
    return web.json_response({"run_id": run_id, "workflow_id": wf_id}, status=202)


async def _handle_workflow_runs_list(request: web.Request) -> web.Response:
    wf_id  = request.rel_url.query.get("workflow_id")
    status = request.rel_url.query.get("status")
    limit  = min(int(request.rel_url.query.get("limit", 50)), 200)
    offset = int(request.rel_url.query.get("offset", 0))
    runs, total = auth_db.list_workflow_runs(workflow_id=wf_id, status=status,
                                              limit=limit, offset=offset)
    return web.json_response({"runs": runs, "total": total})


async def _handle_workflow_run_get(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    run = auth_db.get_workflow_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="Workflow run not found")
    steps = auth_db.get_workflow_step_runs(run_id)
    return web.json_response({"run": run, "steps": steps})


async def _handle_workflow_run_cancel(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    run = auth_db.get_workflow_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="Workflow run not found")
    if run["status"] in ("success", "failed", "cancelled"):
        return web.json_response({"error": "run already terminal"}, status=409)
    engine = request.app.get("workflow_engine")
    if engine:
        await engine.cancel_run(run_id)
    else:
        auth_db.update_workflow_run(run_id, status="cancelled",
                                    finished_at=int(time.time() * 1000))
    auth_db.write_audit_log(
        caller.get("sub"), "cancel_workflow_run",
        target_type="workflow_run", target_id=run_id,
    )
    return web.json_response({"cancelled": True, "run_id": run_id})


async def _handle_workflow_approval_decide(request: web.Request) -> web.Response:
    """Approve or reject a workflow approval step via its approval_request id."""
    approval_id = request.match_info["id"]
    decision    = request.match_info["decision"]   # 'approve' | 'reject'
    if decision not in ("approve", "reject"):
        return web.json_response({"error": "decision must be 'approve' or 'reject'"}, status=400)
    caller = request.get("current_user") or {}
    decided_by = caller.get("sub")

    engine = request.app.get("workflow_engine")
    if engine:
        await engine.resume_approval(
            approval_id=approval_id,
            approved=(decision == "approve"),
            decided_by=decided_by,
        )
    else:
        # Engine not running (e.g. tests) — just update the DB record.
        status = "approved" if decision == "approve" else "rejected"
        auth_db.resolve_approval_request(approval_id, status=status, decided_by=decided_by)
    return web.json_response({"decided": True, "decision": decision, "approval_id": approval_id})


async def _handle_chat(request: web.Request) -> web.StreamResponse:
    # /chat is intentionally unauthenticated (same-origin dashboard, LAN-only NodePort).
    # Rate limiting prevents runaway agent spawning from a single IP.
    ip = request.remote or "unknown"
    if not check_rate_limit(ip, max_requests=30, window=60):
        raise web.HTTPTooManyRequests(
            text='{"error":"rate_limited"}',
            content_type="application/json",
        )

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    message = body.get("message", "")
    session_id = body.get("session_id", "http-default")

    # Use authenticated identity; fall back to body fields for backwards-compat
    auth_user = request.get("current_user") or {}
    user_name = (
        auth_db.get_user_by_id(auth_user.get("sub", ""))or {}
    ).get("display_name") or auth_user.get("email") or body.get("user_name", "User")
    user_id = auth_user.get("sub") or body.get("user_id", "http-user")

    if not message:
        raise web.HTTPBadRequest(reason="message is required")

    runner: Any = request.app["runner"]

    # Resolve the authenticated user's action policy (if any).
    # Applies only to auth-db users (usr_... IDs); platform/anonymous users get DEFAULT_POLICY.
    _action_policy = None
    _auth_user_id = None
    _real_user_id = auth_user.get("sub", "")
    if _real_user_id and _real_user_id.startswith("usr_"):
        _auth_user_id = _real_user_id
        try:
            from gateway.auth.policy import ActionPolicy as _AP, merge_policies as _merge
            _policy_row = auth_db.get_user_action_policy_row(_real_user_id)
            _action_policy = _AP.from_row(_policy_row) if _policy_row else None
            # Session-level tightening: caller may request a stricter policy for this request only.
            # Requires manage_action_policies permission (admins/operators creating sandboxed sessions).
            _session_policy_id = body.get("action_policy_id")
            from gateway.auth.rbac import has_permission as _has_perm
            if _session_policy_id and _has_perm(auth_user.get("role", "viewer"), "manage_action_policies"):
                _sess_row = auth_db.get_action_policy(_session_policy_id)
                if _sess_row:
                    _action_policy = _merge(_action_policy, _AP.from_row(_sess_row))
        except Exception as _pe:
            logger.warning("Failed to resolve action policy for %s: %s", _real_user_id, _pe)

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id=session_id,
        chat_type="dm",
        user_id=user_id,
        user_name=user_name,
    )

    session_entry = runner.session_store.get_or_create_session(source)
    session_key = session_entry.session_key
    history = runner.session_store.load_transcript(session_entry.session_id)
    context = build_session_context(source, runner.config, session_entry)
    context_prompt = build_session_context_prompt(context)

    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
    )
    await resp.prepare(request)

    async def send_event(data: dict) -> None:
        try:
            await resp.write(f"data: {json.dumps(data)}\n\n".encode())
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass  # Client disconnected mid-stream — nothing we can do

    async def heartbeat_loop() -> None:
        """Send SSE comments every 20s to keep the connection alive through proxies."""
        while True:
            await asyncio.sleep(20)
            try:
                await resp.write(b": heartbeat\n\n")
            except Exception:
                break

    await send_event({"type": "start"})

    heartbeat = asyncio.ensure_future(heartbeat_loop())
    result = {}
    t_agent_start = time.time()
    try:
        result = await runner._run_agent(
            message=message,
            context_prompt=context_prompt,
            history=history,
            source=source,
            session_id=session_entry.session_id,
            session_key=session_key,
            action_policy=_action_policy,
            auth_user_id=_auth_user_id,
        )
        final = result.get("final_response", "")
        await send_event({"type": "message", "content": final})
    except Exception as exc:
        # Distinguish real tool/agent errors from transport failures so the UI
        # can show a more informative message than "network error".
        logger.exception("Error running agent for HTTP /chat")
        err_str = str(exc)
        # Surface as a typed error so the frontend can decide how to display it
        await send_event({"type": "error", "content": err_str, "error_class": type(exc).__name__})
    finally:
        heartbeat.cancel()

    await send_event({
        "type":            "done",
        "elapsed_s":       round(time.time() - t_agent_start, 1),
        "prompt_tokens":   result.get("last_prompt_tokens", 0),
        "api_calls":       result.get("api_calls", 0),
        "tools_used":      result.get("tools_used", 0),
        "tools_available": len(result.get("tools", [])),
        "model":           result.get("model", ""),
    })
    return resp


# ── Agent Runs handlers ──────────────────────────────────────────────────────

async def _handle_runs_list(request: web.Request) -> web.Response:
    user = request.get("current_user") or {}
    role = user.get("role", "viewer")
    uid = user.get("sub", "")
    # Operators/admins see all runs; users see only their own
    from gateway.auth.rbac import has_permission
    see_all = has_permission(role, "manage_users")
    params = request.rel_url.query
    status_f = params.get("status") or None
    session_f = params.get("session_id") or None
    limit = min(int(params.get("limit", 50)), 200)
    offset = int(params.get("offset", 0))
    runs, total = auth_db.list_agent_runs(
        user_id=None if see_all else uid,
        status=status_f,
        session_id=session_f,
        limit=limit,
        offset=offset,
    )
    # Parse JSON fields
    for r in runs:
        for field in ("tool_sequence", "tool_detail", "approval_ids"):
            try:
                r[field] = json.loads(r[field] or "[]")
            except Exception:
                r[field] = []
    return web.json_response({"runs": runs, "total": total})


async def _handle_run_get(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    run = auth_db.get_agent_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="run_not_found")
    for field in ("tool_sequence", "tool_detail", "approval_ids"):
        try:
            run[field] = json.loads(run[field] or "[]")
        except Exception:
            run[field] = []
    return web.json_response({"run": run})


async def _handle_run_clone(request: web.Request) -> web.Response:
    """Clone a run — return a prefilled payload the UI can use to start a new chat."""
    run_id = request.match_info["id"]
    run = auth_db.get_agent_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="run_not_found")
    tool_seq = []
    try:
        tool_seq = json.loads(run.get("tool_sequence") or "[]")
    except Exception:
        pass
    destructive_tools = {"write_file", "patch", "terminal", "execute_code", "delete_file"}
    had_destructive = bool(set(tool_seq) & destructive_tools)
    return web.json_response({
        "clone": {
            "user_message": run.get("user_message", ""),
            "session_id": run.get("session_id", ""),
            "model": run.get("model", ""),
            "original_run_id": run_id,
            "had_destructive_tools": had_destructive,
            "warning": (
                "This run used destructive tools. Review carefully before running."
                if had_destructive else None
            ),
        }
    })


async def start_http_api(runner: Any, port: int = 8080) -> None:
    """Start the aiohttp server. Call as an asyncio task."""
    global _start_time
    _start_time = time.time()

    # Initialise auth DB alongside existing hermes state
    global _hermes_home
    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    _hermes_home = hermes_home
    auth_db.init_db(hermes_home)
    # Env-var admin seeding (takes priority over generic seed)
    _ensure_admin_exists()
    # Generic seed: machines → profiles → admin user (all no-ops on existing data)
    from gateway import seed as _seed
    _seed.run_seed()

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-CSRF-Token",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app["runner"] = runner

    # Workflow engine — lazily imported to avoid circular deps at module load.
    try:
        from workflows.engine import WorkflowEngine as _WFEngine
        app["workflow_engine"] = _WFEngine(runner)
        logger.info("Workflow engine initialised")
    except Exception as _wf_err:
        logger.warning("Workflow engine failed to initialise: %s", _wf_err)
        app["workflow_engine"] = None

    _load_souls()

    # ── Public routes ──────────────────────────────────────────────────────
    app.router.add_get("/health",        _handle_health)
    app.router.add_get("/healthz",       _handle_health)       # K8s liveness probe alias
    app.router.add_get("/health/ready",  _handle_health_ready)
    app.router.add_get("/chat_logo.png", _handle_logo)
    app.router.add_get("/login",         _handle_login_page)

    # ── Auth routes (no cookie required) ───────────────────────────────────
    app.router.add_post("/auth/login",   handle_login)
    app.router.add_post("/auth/logout",  handle_logout)
    app.router.add_post("/auth/refresh", handle_refresh)

    # ── Authenticated routes ───────────────────────────────────────────────
    from gateway import setup_handlers as _sh
    app.router.add_get("/setup",              _handle_setup_page)
    app.router.add_get("/api/setup/probe",    _sh.handle_setup_probe)
    app.router.add_get("/api/setup/scan",     _sh.handle_setup_scan)
    app.router.add_get("/api/setup/status",   _handle_setup_status)
    app.router.add_post("/api/setup/pull",    require_csrf(_sh.handle_setup_pull))
    app.router.add_post("/api/setup/test",    require_csrf(_sh.handle_setup_test))
    app.router.add_post("/api/setup/complete", require_csrf(_sh.handle_setup_complete))
    app.router.add_post("/api/setup/reset",
        require_csrf(require_permission("admin")(_handle_setup_reset)))

    app.router.add_get("/",              _handle_index)
    app.router.add_get("/auth/me",       handle_me)
    app.router.add_get("/users/me",      handle_me)
    app.router.add_patch("/users/me",    handle_users_me_patch)
    app.router.add_get(
        "/users",
        require_permission("manage_users")(handle_users_list),
    )
    app.router.add_post(
        "/users",
        require_permission("manage_users")(require_csrf(handle_users_post)),
    )
    app.router.add_patch(
        "/users/{id}",
        require_permission("manage_users")(require_csrf(handle_users_patch)),
    )
    app.router.add_get(
        "/audit-logs",
        require_permission("view_audit_logs")(handle_audit_logs),
    )
    app.router.add_get("/souls",         _handle_souls_get)
    app.router.add_get("/souls/{slug}",  _handle_soul_detail)
    app.router.add_get(
        "/instances",
        require_permission("view_instances")(_handle_instances_get),
    )
    app.router.add_post("/instances",    _handle_instances_post)
    app.router.add_delete("/instances/{name}", _handle_instances_delete)
    app.router.add_get("/spawn-templates",         _handle_spawn_templates_get)
    app.router.add_put("/spawn-templates",         _handle_spawn_templates_put)
    app.router.add_delete("/spawn-templates/{id}", _handle_spawn_templates_delete)
    app.router.add_get("/status",        _handle_status)
    app.router.add_get("/sessions",      _handle_sessions)
    app.router.add_post("/chat",               _handle_chat)
    app.router.add_post("/chat/transcribe",    require_csrf(_handle_transcribe))
    app.router.add_route("OPTIONS", "/chat",   _handle_index)
    app.router.add_get("/canary/status", _handle_canary_status)
    app.router.add_get("/proxy/state",        _handle_proxy_state)
    app.router.add_post("/proxy/providers/{key}/toggle", _handle_proxy_toggle)
    app.router.add_get("/proxy/models-live",  _handle_proxy_models_live)
    app.router.add_post("/proxy/benchmark",   _handle_proxy_benchmark)
    app.router.add_get("/internal/routing/claims",  _handle_routing_claims)
    app.router.add_post("/internal/routing/apply",  require_csrf(_handle_routing_apply))

    # ── Admin routes ───────────────────────────────────────────────────────
    _mm  = require_permission("manage_machines")
    _mp  = require_permission("claim_machine")
    _mpr = require_permission("manage_profiles")
    _mu  = require_permission("manage_users")
    _ap  = require_permission("assign_profile")
    _vrd = require_permission("view_routing_debug")

    app.router.add_get("/admin/model-classes", _mm(admin_handlers.handle_model_classes))
    app.router.add_get("/admin/machines",      _mm(admin_handlers.handle_machines_list))
    app.router.add_post("/admin/machines",     _mm(require_csrf(admin_handlers.handle_machines_post)))
    app.router.add_patch("/admin/machines/{id}", _mm(require_csrf(admin_handlers.handle_machines_patch)))
    app.router.add_delete("/admin/machines/{id}", _mm(require_csrf(admin_handlers.handle_machines_delete)))
    app.router.add_post("/admin/machines/reorder", _mm(require_csrf(admin_handlers.handle_machines_reorder)))
    app.router.add_get("/admin/machines/{id}/claims",  _mm(admin_handlers.handle_machine_claims_get))
    app.router.add_put("/machines/{id}/claim",         _mp(require_csrf(admin_handlers.handle_machine_claim_put)))
    app.router.add_delete("/machines/{id}/claim",      _mp(require_csrf(admin_handlers.handle_machine_claim_delete)))
    app.router.add_put("/admin/machines/{id}/capabilities", _mm(require_csrf(admin_handlers.handle_machine_capabilities_put)))
    app.router.add_get("/admin/machines/{id}/health", _mm(admin_handlers.handle_machine_health))
    app.router.add_get("/admin/policies",      _mpr(admin_handlers.handle_policies_list))
    app.router.add_post("/admin/policies",     _mpr(require_csrf(admin_handlers.handle_policies_post)))
    app.router.add_patch("/admin/policies/{id}", _mpr(require_csrf(admin_handlers.handle_policies_patch)))
    app.router.add_delete("/admin/policies/{id}", _mpr(require_csrf(admin_handlers.handle_policies_delete)))
    app.router.add_put("/admin/policies/{id}/rules", _mpr(require_csrf(admin_handlers.handle_policy_rules_put)))
    app.router.add_patch("/admin/users/{id}/policy", _ap(require_csrf(admin_handlers.handle_user_policy_patch)))

    # ── Action policies (behaviour enforcement) ────────────────────────────
    _map = require_permission("manage_action_policies")
    _aap = require_permission("assign_action_policy")
    _vap = require_permission("view_approvals")
    _dap = require_permission("decide_approvals")

    app.router.add_get("/action-policies",         _map(_handle_action_policies_list))
    app.router.add_post("/action-policies",        _map(require_csrf(_handle_action_policies_post)))
    app.router.add_get("/action-policies/{id}",    _map(_handle_action_policies_get))
    app.router.add_patch("/action-policies/{id}",  _map(require_csrf(_handle_action_policies_patch)))
    app.router.add_delete("/action-policies/{id}", _map(require_csrf(_handle_action_policies_delete)))
    app.router.add_patch("/users/{id}/action-policy", _aap(require_csrf(_handle_user_action_policy_patch)))

    # ── Approval requests ──────────────────────────────────────────────────
    app.router.add_get("/approvals",              _vap(_handle_approvals_list))
    app.router.add_get("/approvals/{id}",         _vap(_handle_approvals_get))
    app.router.add_post("/approvals/{id}/approve", _dap(require_csrf(_handle_approvals_approve)))
    app.router.add_post("/approvals/{id}/reject",  _dap(require_csrf(_handle_approvals_reject)))

    # ── Workflow execution layer ────────────────────────────────────────────
    _mwf = require_permission("manage_workflows")
    _twf = require_permission("trigger_workflow")
    _vwf = require_permission("view_workflows")
    _dwf = require_permission("decide_workflow_approvals")

    app.router.add_get("/workflows",               _vwf(_handle_workflows_list))
    app.router.add_post("/workflows",              _mwf(require_csrf(_handle_workflows_post)))
    app.router.add_get("/workflows/{id}",          _vwf(_handle_workflows_get))
    app.router.add_patch("/workflows/{id}",        _mwf(require_csrf(_handle_workflows_patch)))
    app.router.add_delete("/workflows/{id}",       _mwf(require_csrf(_handle_workflows_delete)))
    app.router.add_post("/workflows/{id}/trigger", _twf(require_csrf(_handle_workflow_trigger)))
    app.router.add_get("/workflow-runs",           _vwf(_handle_workflow_runs_list))
    app.router.add_get("/workflow-runs/{id}",      _vwf(_handle_workflow_run_get))
    app.router.add_post("/workflow-runs/{id}/cancel", _twf(require_csrf(_handle_workflow_run_cancel)))
    app.router.add_post("/workflow-runs/approvals/{id}/{decision}", _dwf(require_csrf(_handle_workflow_approval_decide)))

    # ── Agent run records ───────────────────────────────────────────────────
    _vrun = require_permission("view_runs")
    app.router.add_get("/runs",            _vrun(_handle_runs_list))
    app.router.add_get("/runs/{id}",       _vrun(_handle_run_get))
    app.router.add_get("/runs/{id}/clone", _vrun(_handle_run_clone))

    app.router.add_get("/admin/routing/resolve",  _vrd(admin_handlers.handle_routing_resolve))
    app.router.add_get("/admin/routing/log",      require_permission("view_audit_logs")(admin_handlers.handle_routing_log))
    app.router.add_post("/admin/setup",           _mm(require_csrf(admin_handlers.handle_setup_wizard)))
    app.router.add_get("/routing/preview",        admin_handlers.handle_routing_preview)

    # Serve static assets (logo, etc.)
    import pathlib as _pathlib
    _static_dir = _pathlib.Path(__file__).parent.parent / "assets"
    if _static_dir.exists():
        app.router.add_static("/static", str(_static_dir), show_index=False)

    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP API listening on port %d", port)

    async def _queue_retry_loop():
        """Retry queued instance requests when cluster resources free up."""
        while True:
            await asyncio.sleep(60)
            if not _instance_queue:
                continue
            try:
                loop = asyncio.get_event_loop()
                res = await loop.run_in_executor(None, _cluster_resources)
                if res.get("free_cpu", 0) >= _SPAWN_CPU_THRESHOLD and res.get("free_mem", 0) >= _SPAWN_MEM_THRESHOLD:
                    req = _instance_queue.pop(0)
                    logger.info("Retrying queued instance for %s", req["requester"])
                    await loop.run_in_executor(None, _spawn_instance, req["requester"])
            except Exception as e:
                logger.warning("Queue retry failed: %s", e)

    asyncio.create_task(_queue_retry_loop())

    # ── Workspace TTL cleanup ───────────────────────────────────────────────
    # Run once at startup to remove any workspaces left over from a previous
    # pod lifecycle, then schedule periodic sweeps.
    _ws_cleanup_interval_hours = float(
        os.environ.get("HERMES_WORKSPACE_CLEANUP_INTERVAL_HOURS", "1")
    )

    async def _workspace_cleanup_loop():
        """Delete ephemeral workspace directories whose TTL has expired."""
        # Startup sweep — workspaces from crashed/restarted pods accumulate
        try:
            from gateway import workspace as _ws_mod
            loop = asyncio.get_event_loop()
            removed = await loop.run_in_executor(None, _ws_mod.cleanup_expired)
            if removed:
                logger.info("Startup workspace cleanup: removed %d expired workspaces", removed)
            else:
                logger.debug("Startup workspace cleanup: no expired workspaces found")
        except Exception as _wse:
            logger.warning("Startup workspace cleanup failed: %s", _wse)

        # Periodic sweeps
        while True:
            await asyncio.sleep(_ws_cleanup_interval_hours * 3600)
            try:
                from gateway import workspace as _ws_mod
                loop = asyncio.get_event_loop()
                removed = await loop.run_in_executor(None, _ws_mod.cleanup_expired)
                if removed:
                    logger.info(
                        "Periodic workspace cleanup: removed %d expired workspaces", removed
                    )
            except Exception as _wse:
                logger.warning("Periodic workspace cleanup error: %s", _wse)

    asyncio.create_task(_workspace_cleanup_loop())
