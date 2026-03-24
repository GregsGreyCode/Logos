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
_hermes_home: Path = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".logos"))
_AI_ROUTER_BASE = os.environ.get(
    "AI_ROUTER_BASE",
    "http://ai-router.hermes.svc.cluster.local:9001",
)
_CANARY_HEALTH_URL = "http://hermes-canary.hermes.svc.cluster.local/health"
_INSTANCE_NAME = os.environ.get("HERMES_INSTANCE_NAME", "Hermes")
_IS_CANARY = os.environ.get("HERMES_IS_CANARY", "").lower() in ("1", "true", "yes")
_RUNTIME_MODE = os.environ.get("HERMES_RUNTIME_MODE", "kubernetes")  # "local" | "kubernetes"

try:
    # Read directly from pyproject.toml — immune to stale installed metadata
    import tomllib as _tomllib
    with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb") as _f:
        _APP_VERSION = _tomllib.load(_f)["project"]["version"]
except Exception:
    try:
        _APP_VERSION = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        _APP_VERSION = "dev"
_BUILD_SHA = os.environ.get("BUILD_SHA", "local")[:7]
_VERSION_LABEL = f"v{_APP_VERSION} · {_BUILD_SHA}{' · canary' if _IS_CANARY else ''}"
_SERVER_START_TS = str(int(__import__("time").time()))  # unique per pod start; used to invalidate setup localStorage
# K8s constants and helpers — extracted to gateway/executors/k8s_helpers.py
from gateway.executors.k8s_helpers import (
    HERMES_NAMESPACE as _HERMES_NAMESPACE,
    INSTANCE_CPU_REQUEST as _INSTANCE_CPU_REQUEST,
    INSTANCE_MEM_REQUEST as _INSTANCE_MEM_REQUEST,
    INSTANCE_CPU_LIMIT as _INSTANCE_CPU_LIMIT,
    INSTANCE_MEM_LIMIT as _INSTANCE_MEM_LIMIT,
    SPAWN_CPU_THRESHOLD as _SPAWN_CPU_THRESHOLD,
    SPAWN_MEM_THRESHOLD as _SPAWN_MEM_THRESHOLD,
    k8s_clients as _k8s_clients,
    safe_k8s_name as _safe_k8s_name,
    cluster_resources as _cluster_resources,
    list_hermes_instances as _list_hermes_instances,
    delete_hermes_instance as _delete_instance,
)

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


# ── Kubernetes helpers — see gateway/executors/k8s_helpers.py ────────────────
# _k8s_clients, _cluster_resources, _list_hermes_instances, _delete_instance,
# _safe_k8s_name, and all k8s constants are imported from k8s_helpers at the
# top of this file. _spawn_instance (below) remains here because it depends on
# soul/toolset resolution logic also in this module.


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


# Stable epoch for hue-cycle phase-locking across all browser tabs and the tray icon.
_HUE_EPOCH_MS: int = int(time.time() * 1000)

_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logos</title>
<meta name="theme-color" content="#0d0d0d">
<link rel="icon" type="image/svg+xml" href="/static/logo.svg">
<link rel="shortcut icon" href="/favicon.ico">
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
  .thin-scroll{overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--accent-muted) transparent}
  .thin-scroll::-webkit-scrollbar{width:4px}
  .thin-scroll::-webkit-scrollbar-track{background:transparent}
  .thin-scroll::-webkit-scrollbar-thumb{background:var(--accent-muted);border-radius:9999px}
  .thin-scroll::-webkit-scrollbar-thumb:hover{background:var(--accent)}
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
  /* Bottom bar: stats toggle (left) + copy button (right) — always visible, inside bubble */
  .msg-bar{
    display:flex;align-items:center;justify-content:space-between;
    padding:.2rem .4rem .2rem .5rem;min-height:1.5rem;
  }
  .msg-hint{
    display:inline-flex;align-items:center;gap:.2rem;
    font-size:.62rem;color:#4b5563;cursor:pointer;user-select:none;
    line-height:1.4;transition:color .15s;
  }
  .msg-hint:hover{color:#818cf8}
  .msg-copy{
    display:inline-flex;align-items:center;gap:.25rem;
    background:transparent;border:1px solid transparent;border-radius:4px;
    padding:.15rem .35rem;font-size:.65rem;color:#4b5563;
    cursor:pointer;transition:color .15s,border-color .15s;flex-shrink:0;
  }
  .msg-copy:hover{color:#a5b4fc;border-color:#374151}
  .msg-copy.copied{color:#4ade80!important;border-color:#166534!important}

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

  .inst-active{background:linear-gradient(135deg,#6366f1 0%,#a855f7 100%)!important;color:#fff!important;filter:hue-rotate(var(--hue-deg,0deg))}

  /* wake animation — breathe-pulse replaces bouncing balls */
  @keyframes logos-wake{0%,100%{transform:scale(0.55);opacity:0.2}45%{transform:scale(1.25);opacity:1}70%{transform:scale(0.9);opacity:0.7}}
  .logos-wake-dot{width:7px;height:7px;border-radius:9999px;background:var(--accent);display:inline-block;animation:logos-wake 1.6s ease-in-out infinite}
  .logos-wake-dot:nth-child(1){animation-delay:0ms}
  .logos-wake-dot:nth-child(2){animation-delay:220ms}
  .logos-wake-dot:nth-child(3){animation-delay:440ms}

  /* First-load staggered fade-in (triggered via sessionStorage logos_fl flag) */
  @keyframes fl-fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
  .fl-hidden{opacity:0}
  .fl-anim{animation:fl-fade 0.6s ease forwards}
</style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen" x-data="app()" x-init="init()">

<div class="max-w-screen-xl mx-auto px-4 pt-4 pb-0" style="position:relative;z-index:10">

  <!-- Tabs + theme swatches -->
  <div class="flex items-end gap-6 border-b border-gray-800 mb-3 relative z-40" data-fl="0">
    <!-- Logos brand mark — matches the logo.svg the login page animates to this position -->
    <div class="pb-2 shrink-0 flex items-center gap-2">
      <img src="/static/logo.svg" alt="Logos"
           style="height:32px;width:32px;flex-shrink:0;object-fit:contain;filter:hue-rotate(var(--hue-deg,0deg)) drop-shadow(0 0 6px rgba(99,102,241,0.35));">
      <template x-if="isCanary">
        <span class="px-2 py-0.5 rounded-full text-[10px] font-bold tracking-widest uppercase border border-yellow-500 bg-yellow-950 text-yellow-400 self-center">canary</span>
      </template>
    </div>
    <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
      :class="tab==='sessions'?'tab-active':'text-gray-400 hover:text-white'"
      @click="tab='sessions'; if(!clusterInstances.length) loadInstances()">Chats</button>
    <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
      :class="tab==='instances'?'tab-active':'text-gray-400 hover:text-white'"
      @click="tab='instances'; loadInstances(); loadSouls()">Agents</button>
    <template x-if="can('view_runs')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='runs'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='runs'; loadAgentRuns()">Runs</button>
    </template>
    <template x-if="can('view_workflows')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='workflows'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='workflows'; loadWorkflows()">Workflows</button>
    </template>
    <template x-if="can('manage_machines') || can('manage_profiles') || can('view_routing_debug')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='routing'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='routing'; loadRoutingData()">Routing</button>
    </template>
    <template x-if="can('view_evolution')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='evolution'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='evolution'; loadEvolutionProposals(); loadEvolutionSettings()">Evolution</button>
    </template>
    <template x-if="can('manage_users') || can('view_audit_logs')">
      <button class="pb-2 text-sm font-medium border-b-2 border-transparent"
        :class="tab==='admin'?'tab-active':'text-gray-400 hover:text-white'"
        @click="tab='admin'; if(!can('manage_users') && adminTab==='users') adminTab='audit'; if(adminTab==='routing-log') loadAdminRoutingLog(); else loadAdminData()">Admin</button>
    </template>
    <!-- Canary pill (inline, between Admin and Theme) -->
    <template x-if="canary.active">
      <div class="pb-2 flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-yellow-600 bg-yellow-950 text-yellow-400 text-xs font-medium animate-pulse self-center">
        <span>🐤</span>
        <span>canary live</span>
      </div>
    </template>
    <!-- Account menu -->
    <template x-if="authUser">
      <div class="pb-2 relative ml-auto shrink-0" @click.away="accountMenuOpen=false">
        <button @click="accountMenuOpen=!accountMenuOpen"
          class="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-medium transition-colors border-b-2 border-transparent"
          :class="accountMenuOpen
            ? 'border-gray-600 bg-gray-800 text-gray-200'
            : 'border-gray-700 text-gray-400 hover:border-gray-600 hover:text-gray-300'">
          <span class="text-gray-300" x-text="authUser.display_name || authUser.email"></span>
          <span class="opacity-50" x-text="accountMenuOpen ? '▲' : '▼'"></span>
        </button>
        <div x-show="accountMenuOpen" x-cloak
          class="absolute right-0 top-full mt-2 z-50 bg-gray-900 rounded-xl border border-gray-700 shadow-2xl overflow-hidden w-60"
          style="background:var(--bg-card);border-color:var(--border-strong)">
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
          <!-- Theme picker -->
          <div class="px-4 py-3 border-t border-gray-800">
            <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Theme</div>
            <div class="grid grid-cols-2 gap-2">
              <template x-for="t in themes" :key="t.id">
                <button class="th-card" :class="theme===t.id ? 'th-active' : ''"
                  @click="setTheme(t.id)">
                  <div class="th-swatch">
                    <span :style="`background:${t.base};flex:2`"></span>
                    <span :style="`background:${t.surface};flex:2`"></span>
                    <span :style="`background:${t.accent};flex:1`"></span>
                  </div>
                  <div class="text-xs font-semibold leading-tight" style="color:#f3f4f6" x-text="t.name"></div>
                </button>
              </template>
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
    <div class="flex items-center gap-2 mb-3 flex-wrap" data-fl="1">
      <template x-for="inst in chatAgents" :key="inst.id">
        <div class="flex items-center gap-0.5">
          <button class="flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-colors"
            :class="activeInstanceId === inst.id
              ? 'inst-active'
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

    <div class="flex gap-4 h-full" style="height:calc(100% - 36px)" data-fl="2">

      <!-- Sidebar: chat history list -->
      <div class="w-44 shrink-0 flex flex-col h-full">
        <button @click="newChat()"
          class="w-full mb-2 px-3 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-lg font-medium transition-colors flex items-center justify-center gap-2">
          <span>＋</span><span>New Chat</span>
        </button>
        <button @click="tab='instances'; loadInstances(); loadSouls()"
          class="w-full mb-3 px-3 py-1.5 bg-gray-900 hover:bg-gray-800 text-gray-500 hover:text-gray-300 text-xs rounded-lg transition-colors flex items-center justify-center gap-1.5 border border-gray-800 hover:border-gray-700"
          title="Spawn or manage agent instances">
          <span>＋</span><span>Add Agent</span>
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

            <!-- Row 1: Core badge + name + status dot (after name, like chips) + chat ID + canary -->
            <div class="flex items-center gap-2 pb-1.5">
              <span class="text-[10px] font-bold tracking-wider uppercase px-1 py-0.5 rounded bg-indigo-900 text-indigo-300 shrink-0">Core</span>
              <span class="font-semibold text-white" x-text="status.instance_name || 'Hermes'"></span>
              <span class="w-2 h-2 rounded-full shrink-0"
                :class="chatLoading ? 'bg-indigo-500 animate-pulse icon-hue' : 'bg-green-500'"></span>
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
                 style="opacity:0.15">
              <img src="/static/logo.svg" aria-hidden="true"
                   style="width:100px;height:100px;object-fit:contain;filter:hue-rotate(var(--hue-deg,0deg));">
            </div>
            <template x-for="(msg,i) in chatMessages" :key="i">
              <div :class="msg.role==='user' ? 'text-right' : 'text-left'">
                <!-- User messages: always plain bubble -->
                <template x-if="msg.role==='user'">
                  <span class="inline-block max-w-3xl text-left px-3 py-2 rounded-xl text-sm leading-relaxed bg-indigo-700 text-white"
                    x-text="msg.content"></span>
                </template>
                <!-- Assistant messages: rendered + bottom bar (stats toggle + copy) -->
                <template x-if="msg.role!=='user'">
                  <div class="msg-wrap inline-block max-w-3xl" x-data="{statsOpen:false,copied:false}">
                    <div class="text-left px-3 pt-2 pb-2 rounded-t-xl text-sm bg-gray-800 text-gray-100"
                      :class="[chatRenderMode==='mono' ? 'chat-mono' : 'chat-md', !msg.stats ? 'rounded-b-xl' : '']"
                      x-html="renderMsg(msg.content)"></div>
                    <!-- Bottom bar: stats toggle (left) + copy (right) — only when stats available -->
                    <template x-if="msg.stats">
                      <div class="msg-bar bg-gray-800 rounded-b-xl border-t border-gray-700/40">
                        <span class="msg-hint" @click.stop="statsOpen=!statsOpen">
                          <span x-text="statsOpen ? \'▴ hide stats\' : \'⋯ stats\'"></span>
                        </span>
                        <button class="msg-copy" :class="copied?\'copied\':\'\'"
                          @click.stop="navigator.clipboard.writeText(msg.content).then(()=>{copied=true;setTimeout(()=>copied=false,1500)})"
                          title="Copy response">
                          <svg width="11" height="11" viewBox="0 0 16 16" fill="none"><rect x="5" y="5" width="9" height="9" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M3 10V3a2 2 0 012-2h7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                          <span x-text="copied?\'✓ copied\':\'copy\'"></span>
                        </button>
                      </div>
                    </template>
                    <!-- Stats panel — expandable -->
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
              <!-- thinking state: classic bounce, hue-cycles with logo -->
              <div x-show="!isWakingUp" class="flex gap-1">
                <span class="w-1.5 h-1.5 rounded-full animate-bounce icon-hue" style="background:#6366f1;animation-delay:0ms"></span>
                <span class="w-1.5 h-1.5 rounded-full animate-bounce icon-hue" style="background:#6366f1;animation-delay:150ms"></span>
                <span class="w-1.5 h-1.5 rounded-full animate-bounce icon-hue" style="background:#6366f1;animation-delay:300ms"></span>
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
      <div class="w-60 shrink-0 flex flex-col h-full items-center">
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
        <div class="text-sm font-semibold text-white">Machines</div>
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
          <div class="text-sm font-semibold text-white">Routing Profiles</div>
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
        class="flex items-center justify-between w-full text-left mb-4 group">
        <span class="text-sm font-semibold text-white group-hover:text-gray-200">Model Map</span>
        <span class="text-xs text-gray-600 transition-transform" :class="modelMapOpen ? 'rotate-90' : ''" style="display:inline-block">▶</span>
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
        class="flex items-center justify-between w-full text-left mb-4 group">
        <span class="text-sm font-semibold text-white group-hover:text-gray-200">Benchmark</span>
        <span class="text-xs text-gray-600 transition-transform" :class="benchmarkOpen ? 'rotate-90' : ''" style="display:inline-block">▶</span>
      </button>
      <div x-show="benchmarkOpen" x-cloak>

        <!-- Capability Benchmark — runs setup compare against all registered machines in parallel -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-4">
          <div class="flex items-center justify-between mb-3">
            <div>
              <div class="text-xs font-semibold text-gray-300 mb-0.5">Capability Benchmark</div>
              <div class="text-xs text-gray-600">Runs capability evals (instruction, reasoning, JSON, tool call) against all registered machines in parallel.</div>
            </div>
            <button @click="runCapabilityBenchmark()" :disabled="capBenchRunning"
              class="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors disabled:opacity-40 shrink-0 ml-4">
              <span x-show="!capBenchRunning">Run Capability Benchmark</span>
              <span x-show="capBenchRunning" class="animate-pulse">Running…</span>
            </button>
          </div>
          <!-- Per-machine progress bars -->
          <template x-if="Object.keys(capBenchServers).length > 0">
            <div class="mb-3 space-y-2">
              <template x-for="(srv, ep) in capBenchServers" :key="ep">
                <div class="flex items-center gap-2">
                  <div class="text-xs text-gray-500 font-mono truncate flex-1" x-text="ep"></div>
                  <div class="text-xs shrink-0"
                    :class="srv.done ? (srv.error ? 'text-red-400' : 'text-green-400') : 'text-indigo-400 animate-pulse'"
                    x-text="srv.done ? (srv.error ? 'error' : (srv.best ? srv.best + ' · ' + (srv.score ?? '?') + '/6' : 'done')) : (srv.testing ? srv.testing.split(':')[0].slice(-20) + '…' : 'queued')"></div>
                </div>
              </template>
            </div>
          </template>
          <!-- Log stream -->
          <div x-show="capBenchLog.length > 0">
            <div class="flex items-center justify-between mb-1">
              <button @click="capBenchLogOpen=!capBenchLogOpen"
                class="flex items-center gap-1.5 text-[11px] text-gray-600 hover:text-gray-400 transition-colors select-none">
                <svg :class="capBenchLogOpen ? 'rotate-90' : ''" class="w-3 h-3 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                <span x-text="capBenchLogOpen ? 'Hide log' : 'Show log (' + capBenchLog.length + ' lines)'"></span>
              </button>
            </div>
            <div x-show="capBenchLogOpen"
              class="thin-scroll rounded-xl bg-black/60 border border-gray-800 px-3 py-2.5 font-mono text-[11px] leading-relaxed space-y-0.5"
              style="max-height:14rem;overflow-y:auto"
              x-ref="capBenchLogEl">
              <template x-for="(entry, idx) in capBenchLog" :key="idx">
                <div :class="entry.startsWith('      ') ? 'text-gray-600 pl-2' : entry.startsWith('Recommendation') ? 'text-indigo-400' : entry.startsWith('→') ? 'text-gray-300' : entry.includes('✓') ? 'text-green-500/80' : entry.includes('✗') ? 'text-red-500/70' : 'text-gray-500'"
                  x-text="entry"></div>
              </template>
            </div>
          </div>
        </div>

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
        class="flex items-center justify-between w-full text-left mb-4 group">
        <span class="text-sm font-semibold text-white group-hover:text-gray-200">Debug</span>
        <span class="text-xs text-gray-600 transition-transform" :class="debugOpen ? 'rotate-90' : ''" style="display:inline-block">▶</span>
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

  <!-- ── Instances Tab (kubernetes mode only) ─────────────────────── -->
  <div x-show="tab==='instances'" x-cloak>

    <!-- Resource bar — adapts to k8s (cluster totals) or local (psutil free) -->
    <div class="flex items-center gap-3 mb-5 px-3 py-2 rounded-lg border border-gray-800 bg-gray-900 text-xs flex-wrap">
      <button @click="loadInstances()" class="text-gray-600 hover:text-gray-400 text-sm leading-none shrink-0" title="Refresh">↺</button>
      <span class="text-gray-800 shrink-0 select-none">|</span>
      <span x-show="clusterRes._error" class="text-red-500" x-text="(runtimeMode === 'kubernetes' ? 'k8s: ' : 'sys: ') + (clusterRes._error||'')"></span>
      <!-- k8s: show used/total with bars -->
      <template x-if="!clusterRes._error && clusterRes.total_cpu && runtimeMode === 'kubernetes'">
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
      <!-- local: show psutil free resources -->
      <template x-if="!clusterRes._error && runtimeMode !== 'kubernetes'">
        <div class="flex items-center gap-3 flex-wrap flex-1">
          <div class="flex items-center gap-1.5">
            <span class="text-gray-600">CPU available</span>
            <span class="text-gray-400 font-mono" x-text="(clusterRes.free_cpu || 0).toFixed(1) + ' cores'"></span>
          </div>
          <span class="text-gray-800 select-none">·</span>
          <div class="flex items-center gap-1.5">
            <span class="text-gray-600">RAM available</span>
            <span class="text-gray-400 font-mono" x-text="fmtBytes(clusterRes.free_mem || 0)"></span>
          </div>
          <span x-show="clusterRes.can_spawn === false" class="text-orange-400 ml-auto" x-text="'⚠ ' + (clusterRes.reason || 'Resources low')"></span>
        </div>
      </template>
      <span x-show="!clusterRes._error && !clusterRes.total_cpu && !clusterRes.free_cpu && runtimeMode === 'kubernetes'" class="text-gray-600">Cluster data unavailable</span>
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
              <!-- Secondary: name · ready/status · port · CPU/RAM (local) -->
              <div class="flex items-center gap-2 mt-0.5 text-xs text-gray-700 font-mono flex-wrap">
                <span x-text="inst.name"></span>
                <span class="text-gray-800">·</span>
                <span x-text="inst.ready + '/' + inst.desired + ' ready'"></span>
                <template x-if="inst.node_port">
                  <span>
                    <span class="text-gray-800">·</span>
                    <span class="text-indigo-500" x-text="':' + inst.node_port"></span>
                  </span>
                </template>
                <template x-if="inst.cpu_percent != null">
                  <span class="text-gray-700">
                    <span class="text-gray-800">·</span>
                    <span :class="inst.cpu_percent > 80 ? 'text-orange-500' : 'text-gray-600'" x-text="inst.cpu_percent + '% CPU'"></span>
                  </span>
                </template>
                <template x-if="inst.mem_mb != null">
                  <span class="text-gray-700">
                    <span class="text-gray-800">·</span>
                    <span :class="inst.mem_mb > 14000 ? 'text-orange-500' : 'text-gray-600'" x-text="inst.mem_mb >= 1024 ? (inst.mem_mb/1024).toFixed(1)+'GB' : inst.mem_mb+'MB'"></span>
                  </span>
                </template>
              </div>
            </div>

            <!-- Actions -->
            <div class="flex items-center gap-1.5 shrink-0">
              <template x-if="inst.node_port && inst.status === 'running'">
                <button @click="switchInstance((inst.source === 'local' ? 'local-' : 'k8s-') + inst.name); tab='sessions'"
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

  <!-- ── Evolution Tab ────────────────────────────────────────────────── -->
  <div x-show="tab==='evolution'" x-cloak class="p-6 max-w-5xl mx-auto space-y-6">

    <!-- Sub-nav -->
    <div class="flex items-center gap-5 border-b border-gray-800 pb-2">
      <button class="pb-2 text-sm font-medium border-b-2 -mb-2.5"
        :class="evoPanel==='proposals'?'border-[var(--accent)] text-[var(--accent)]':'border-transparent text-gray-400 hover:text-white'"
        @click="evoPanel='proposals'">Proposals</button>
      <template x-if="can('manage_evolution')">
        <button class="pb-2 text-sm font-medium border-b-2 -mb-2.5"
          :class="evoPanel==='settings'?'border-[var(--accent)] text-[var(--accent)]':'border-transparent text-gray-400 hover:text-white'"
          @click="evoPanel='settings'; loadEvolutionSettings()">Settings</button>
      </template>
    </div>

    <!-- ── Proposals panel ─────────────────────────────────── -->
    <div x-show="evoPanel==='proposals'" class="space-y-4">

      <!-- Filter row -->
      <div class="flex items-center justify-between">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Self-Improvement Proposals</div>
        <div class="flex items-center gap-3">
          <select x-model="evoStatusFilter" @change="evoOffset=0; loadEvolutionProposals()"
            class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-300 focus:outline-none focus:border-[var(--accent)]">
            <option value="">All statuses</option>
            <option value="pending">Pending</option>
            <option value="accepted">Accepted</option>
            <option value="declined">Declined</option>
            <option value="questioned">Questioned</option>
            <option value="in_progress">In Progress</option>
            <option value="merged">Merged</option>
            <option value="cancelled">Cancelled</option>
          </select>
          <button @click="loadEvolutionProposals()" class="text-xs text-[var(--accent)] hover:opacity-80 flex items-center gap-1"><span>&#8635;</span> Refresh</button>
        </div>
      </div>

      <!-- Loading -->
      <div x-show="evoLoading" class="text-center text-gray-600 py-12 text-sm">Loading…</div>

      <!-- Empty state -->
      <div x-show="!evoLoading && evolutionProposals.length === 0"
        class="text-center py-16 text-gray-600">
        <div class="text-4xl mb-3">🌱</div>
        <div class="text-sm font-medium text-gray-500">No proposals yet</div>
        <div class="text-xs text-gray-600 mt-1">Agents will submit self-improvement proposals on the configured schedule.</div>
      </div>

      <!-- Proposals list -->
      <template x-for="p in evolutionProposals" :key="p.id">
        <div class="bg-gray-900 border rounded-xl overflow-hidden"
          :class="selectedProposalId===p.id ? 'border-[var(--accent)]/60' : 'border-gray-800 hover:border-gray-700'"
          @click="selectedProposalId = selectedProposalId===p.id ? null : p.id; evoConsultResult=null; evoQuestionText=''">

          <!-- Row header -->
          <div class="flex items-start gap-3 px-4 py-3 cursor-pointer">
            <!-- Status badge -->
            <span class="mt-0.5 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider shrink-0"
              :class="{
                'bg-yellow-900/60 text-yellow-400 border border-yellow-800': p.status==='pending',
                'bg-green-900/60 text-green-400 border border-green-800': p.status==='accepted',
                'bg-red-900/60 text-red-400 border border-red-800': p.status==='declined',
                'bg-blue-900/60 text-blue-300 border border-blue-800': p.status==='questioned',
                'bg-purple-900/60 text-purple-300 border border-purple-800': p.status==='in_progress',
                'bg-teal-900/60 text-teal-300 border border-teal-800': p.status==='merged',
                'bg-gray-800 text-gray-500 border border-gray-700': p.status==='cancelled'
              }" x-text="p.status"></span>
            <!-- Type badge -->
            <span class="mt-0.5 px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wider shrink-0 bg-gray-800 text-gray-400 border border-gray-700"
              x-text="p.proposal_type"></span>
            <div class="min-w-0 flex-1">
              <div class="text-sm font-medium text-white truncate" x-text="p.title"></div>
              <div class="text-xs text-gray-500 mt-0.5" x-text="new Date(p.created_at).toLocaleString()"></div>
            </div>
            <span class="text-gray-600 text-xs mt-1 shrink-0" x-text="selectedProposalId===p.id ? '▲' : '▼'"></span>
          </div>

          <!-- Expanded detail -->
          <div x-show="selectedProposalId===p.id" class="border-t border-gray-800 px-4 py-4 space-y-4" @click.stop>

            <!-- Summary -->
            <div>
              <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Summary</div>
              <div class="text-xs text-gray-300 whitespace-pre-wrap" x-text="p.summary"></div>
            </div>

            <!-- Target files -->
            <div x-show="p.target_files && p.target_files.length">
              <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Target Files</div>
              <div class="flex flex-wrap gap-1">
                <template x-for="f in p.target_files" :key="f">
                  <span class="text-[11px] font-mono bg-gray-800 text-gray-300 px-2 py-0.5 rounded border border-gray-700" x-text="f"></span>
                </template>
              </div>
            </div>

            <!-- Diff -->
            <div x-show="p.diff_text">
              <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Proposed Diff</div>
              <pre class="text-[11px] font-mono bg-gray-950 text-gray-300 rounded-lg p-3 overflow-x-auto max-h-64 overflow-y-auto border border-gray-800 whitespace-pre" x-text="p.diff_text"></pre>
            </div>

            <!-- Question / Answer -->
            <div x-show="p.question_text">
              <div class="text-[10px] text-blue-600 uppercase tracking-wider mb-1">Question from Reviewer</div>
              <div class="text-xs text-blue-300 bg-blue-950/40 border border-blue-900 rounded-lg p-3 whitespace-pre-wrap" x-text="p.question_text"></div>
            </div>
            <div x-show="p.answer_text">
              <div class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Agent Answer</div>
              <div class="text-xs text-gray-300 bg-gray-800 rounded-lg p-3 whitespace-pre-wrap" x-text="p.answer_text"></div>
            </div>

            <!-- Frontier output -->
            <div x-show="p.frontier_output || evoConsultResult">
              <div class="text-[10px] text-purple-500 uppercase tracking-wider mb-1">
                Frontier Advice <span x-show="p.frontier_model || evoConsultModel" class="lowercase normal-case font-normal" x-text="'(' + (p.frontier_model || evoConsultModel) + ')'"></span>
              </div>
              <div class="text-xs text-gray-300 bg-gray-800 rounded-lg p-3 whitespace-pre-wrap max-h-64 overflow-y-auto"
                x-text="evoConsultResult || p.frontier_output"></div>
            </div>

            <!-- Action buttons -->
            <template x-if="can('decide_evolution') && p.status === 'pending'">
              <div class="flex flex-wrap gap-2 pt-2 border-t border-gray-800">
                <button @click="decideProposal(p.id, 'accept')"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium bg-green-800 hover:bg-green-700 text-green-100 border border-green-700">Accept</button>
                <button @click="decideProposal(p.id, 'decline')"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900 hover:bg-red-800 text-red-200 border border-red-800">Decline</button>
                <button @click="evoAskingQuestion = evoAskingQuestion===p.id ? null : p.id"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-900 hover:bg-blue-800 text-blue-200 border border-blue-800">Ask Question</button>
                <template x-if="can('manage_evolution')">
                  <button @click="evoConsultingFrontier = evoConsultingFrontier===p.id ? null : p.id"
                    class="px-3 py-1.5 rounded-lg text-xs font-medium bg-purple-900 hover:bg-purple-800 text-purple-200 border border-purple-800">Consult Frontier AI</button>
                </template>
              </div>
            </template>

            <!-- Question input -->
            <div x-show="evoAskingQuestion===p.id" class="space-y-2" @click.stop>
              <textarea x-model="evoQuestionText" rows="3" placeholder="Ask the agent a question about this proposal…"
                class="w-full text-xs bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-200 resize-none focus:outline-none focus:border-[var(--accent)]"></textarea>
              <div class="flex gap-2">
                <button @click="decideProposal(p.id, 'question', evoQuestionText); evoAskingQuestion=null"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-800 hover:bg-blue-700 text-white">Send Question</button>
                <button @click="evoAskingQuestion=null; evoQuestionText=''"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium bg-gray-800 hover:bg-gray-700 text-gray-300">Cancel</button>
              </div>
            </div>

            <!-- Frontier consult input -->
            <div x-show="evoConsultingFrontier===p.id" class="space-y-2" @click.stop>
              <div class="flex items-center gap-2">
                <label class="text-xs text-gray-400 shrink-0">Model</label>
                <select x-model="evoConsultModel"
                  class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 focus:outline-none focus:border-[var(--accent)]">
                  <option value="claude-opus-4-6">Claude Opus 4.6</option>
                  <option value="claude-sonnet-4-6">Claude Sonnet 4.6</option>
                  <option value="gpt-4o">GPT-4o</option>
                  <option value="gpt-4o-mini">GPT-4o Mini</option>
                </select>
                <button @click="consultFrontier(p.id); evoConsultingFrontier=null"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium bg-purple-800 hover:bg-purple-700 text-white">Ask</button>
                <button @click="evoConsultingFrontier=null"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium bg-gray-800 hover:bg-gray-700 text-gray-300">Cancel</button>
              </div>
            </div>

          </div>
        </div>
      </template>

      <!-- Pagination -->
      <div x-show="evolutionTotal > evoPageSize" class="flex items-center justify-between pt-2">
        <div class="text-xs text-gray-600" x-text="'Showing ' + (evoOffset+1) + '–' + Math.min(evoOffset+evoPageSize, evolutionTotal) + ' of ' + evolutionTotal"></div>
        <div class="flex gap-2">
          <button @click="evoOffset=Math.max(0,evoOffset-evoPageSize); loadEvolutionProposals()"
            :disabled="evoOffset===0"
            class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 disabled:opacity-40 hover:enabled:bg-gray-800">← Prev</button>
          <button @click="evoOffset+=evoPageSize; loadEvolutionProposals()"
            :disabled="evoOffset+evoPageSize>=evolutionTotal"
            class="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 disabled:opacity-40 hover:enabled:bg-gray-800">Next →</button>
        </div>
      </div>
    </div>

    <!-- ── Settings panel ──────────────────────────────────── -->
    <div x-show="evoPanel==='settings'" class="space-y-6">
      <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Evolution Settings</div>

      <!-- Loading -->
      <div x-show="evoSettingsLoading" class="text-center text-gray-600 py-12 text-sm">Loading…</div>

      <div x-show="!evoSettingsLoading" class="space-y-6">

        <!-- Schedule section -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
          <div class="text-xs font-semibold text-gray-300 uppercase tracking-wider">Schedule</div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Enabled</label>
            <button @click="evoSettingsForm.enabled = !evoSettingsForm.enabled"
              class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors"
              :class="evoSettingsForm.enabled ? 'bg-[var(--accent)]' : 'bg-gray-700'">
              <span class="inline-block h-3 w-3 transform rounded-full bg-white shadow transition-transform"
                :class="evoSettingsForm.enabled ? 'translate-x-5' : 'translate-x-1'"></span>
            </button>
          </div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Interval</label>
            <select x-model="evoSettingsForm.schedule_label" @change="evoSettingsForm.schedule_minutes = evoScheduleMinutes(evoSettingsForm.schedule_label)"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 focus:outline-none focus:border-[var(--accent)]">
              <option value="1 hour">1 hour</option>
              <option value="6 hours">6 hours</option>
              <option value="1 day">1 day</option>
              <option value="3 days">3 days</option>
              <option value="1 week">1 week (default)</option>
              <option value="1 month">1 month</option>
              <option value="1 year">1 year</option>
            </select>
          </div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Max pending</label>
            <input type="number" x-model.number="evoSettingsForm.max_pending" min="1" max="50"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 w-20 focus:outline-none focus:border-[var(--accent)]" />
            <span class="text-xs text-gray-600">proposals at once</span>
          </div>
        </div>

        <!-- Git section -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
          <div class="text-xs font-semibold text-gray-300 uppercase tracking-wider">Git Integration</div>
          <div class="text-xs text-gray-500">Fork the canonical Logos repo into your own GitHub account, then configure it here. The agent uses your fork as its source of truth — reading the latest code and opening PRs there when proposals are accepted.</div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Fork remote URL</label>
            <input type="text" x-model="evoSettingsForm.git_remote_url" placeholder="https://github.com/you/logos"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 flex-1 focus:outline-none focus:border-[var(--accent)]" />
          </div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Username</label>
            <input type="text" x-model="evoSettingsForm.git_username" placeholder="your-github-username"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 flex-1 focus:outline-none focus:border-[var(--accent)]" />
          </div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Personal access token</label>
            <input type="password" x-model="evoSettingsForm.git_pat" placeholder="ghp_…"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 flex-1 focus:outline-none focus:border-[var(--accent)]" />
          </div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Base branch</label>
            <input type="text" x-model="evoSettingsForm.git_base_branch" placeholder="main"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 w-40 focus:outline-none focus:border-[var(--accent)]" />
          </div>
        </div>

        <!-- Frontier section -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
          <div class="text-xs font-semibold text-gray-300 uppercase tracking-wider">Frontier Model</div>
          <div class="text-xs text-gray-500">Used when you request a frontier AI review of a proposal.</div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">Model</label>
            <select x-model="evoSettingsForm.frontier_model"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 focus:outline-none focus:border-[var(--accent)]">
              <option value="claude-opus-4-6">Claude Opus 4.6</option>
              <option value="claude-sonnet-4-6">Claude Sonnet 4.6</option>
              <option value="gpt-4o">GPT-4o</option>
              <option value="gpt-4o-mini">GPT-4o Mini</option>
            </select>
          </div>
          <div class="flex items-center gap-3">
            <label class="text-xs text-gray-400 w-28 shrink-0">API key env var</label>
            <input type="text" x-model="evoSettingsForm.frontier_api_key_env" placeholder="ANTHROPIC_API_KEY"
              class="text-xs bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-gray-200 flex-1 focus:outline-none focus:border-[var(--accent)]" />
            <span class="text-xs text-gray-600">env var name on the server</span>
          </div>
        </div>

        <!-- Save button -->
        <div class="flex justify-end">
          <button @click="saveEvolutionSettings()"
            class="px-4 py-2 rounded-lg text-sm font-medium text-white"
            style="background:var(--accent)">Save Settings</button>
        </div>
      </div>
    </div>

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
    runtimeMode: window.__LOGOS__?.runtimeMode || 'kubernetes',
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
    // evolution
    evoPanel: 'proposals',
    evolutionProposals: [],
    evolutionTotal: 0,
    evoOffset: 0,
    evoPageSize: 20,
    evoLoading: false,
    evoStatusFilter: '',
    selectedProposalId: null,
    evoQuestionText: '',
    evoAskingQuestion: null,
    evoConsultingFrontier: null,
    evoConsultModel: 'claude-opus-4-6',
    evoConsultResult: null,
    evoSettingsLoading: false,
    evolutionSettings: {},
    evoSettingsForm: {},
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
    // capability benchmark (routing tab)
    capBenchRunning: false,
    capBenchLog: [],
    capBenchLogOpen: false,
    capBenchServers: {},   // ep → {done, error, testing, best, score}
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
      // Hue-cycle: offset+rate approach so speed can change smoothly (no jump on transition).
      // Normal: 6 deg/s (1 rotation/min). Thinking: 30 deg/s (5× faster).
      // Seed offset from server epoch so all tabs and the tray icon stay phase-locked.
      const _epochOffset = window._hueEpochMs ? (((Date.now() - window._hueEpochMs) * 6 / 1000) % 360 + 360) % 360 : 0;
      let _hueOffset = _epochOffset, _hueRef = Date.now(), _hueRate = 6;
      const _setHueRate = r => { _hueOffset = (((_hueOffset + _hueRate * (Date.now() - _hueRef) / 1000) % 360) + 360) % 360; _hueRef = Date.now(); _hueRate = r; };
      // Favicon hue sync — canvas-based, throttled to ~10 fps to avoid GC pressure.
      const _favCanvas = document.createElement('canvas'); _favCanvas.width = _favCanvas.height = 32;
      const _favCtx = _favCanvas.getContext('2d');
      const _favImg = new Image(); _favImg.src = '/static/logo.svg';
      const _favLink = document.querySelector('link[rel="icon"][type="image/svg+xml"]');
      let _favTs = 0;
      const _hueTick = () => {
        const deg = (((_hueOffset + _hueRate * (Date.now() - _hueRef) / 1000) % 360) + 360) % 360;
        document.documentElement.style.setProperty('--hue-deg', deg.toFixed(1) + 'deg');
        const _now = Date.now();
        if (_now - _favTs > 100 && _favCtx && _favImg.complete && _favLink) {
          _favTs = _now;
          _favCtx.clearRect(0, 0, 32, 32);
          _favCtx.filter = `hue-rotate(${deg.toFixed(1)}deg)`;
          _favCtx.drawImage(_favImg, 0, 0, 32, 32);
          _favLink.href = _favCanvas.toDataURL();
        }
        requestAnimationFrame(_hueTick);
      };
      requestAnimationFrame(_hueTick);
      // Apply saved theme immediately; watch for reactive changes
      document.documentElement.setAttribute('data-theme', this.theme);
      this.$watch('theme',      val => document.documentElement.setAttribute('data-theme', val));
      this.$watch('chatLoading', v => _setHueRate(v ? 30 : 6));
      // Staggered first-load fade-in (set by login page on successful auth)
      try {
        if (sessionStorage.getItem('logos_fl') === '1') {
          sessionStorage.removeItem('logos_fl');
          document.querySelectorAll('[data-fl]').forEach(el => {
            el.classList.add('fl-hidden');
            const delay = parseFloat(el.dataset.fl) * 0.2;
            setTimeout(() => { el.classList.remove('fl-hidden'); el.classList.add('fl-anim'); }, delay * 1000);
          });
        }
      } catch {}
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
        .filter(i => i.node_port && i.source !== 'local')
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
      const local = (this.clusterInstances || [])
        .filter(i => i.source === 'local' && i.node_port && i.status === 'running')
        .map(i => ({
          id:           'local-' + i.name,
          name:         i.instance_name,
          url:          'http://127.0.0.1:' + i.node_port,
          source:       'local',
          editable:     false,
          soul:         i.soul         || null,
          model_alias:  i.model_alias  || null,
          machine_name: null,
          k8s_status:   i.status       || null,
        }));
      this.chatAgents = [self, ...k8s, ...local, ...this.manualAgents];
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
        if (el && el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
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

    async runCapabilityBenchmark() {
      if (this.capBenchRunning) return;
      const machines = (this.adminMachines || []).filter(m => m.endpoint_url);
      if (!machines.length) { this.capBenchLog = ['No registered machines with endpoints found.']; return; }

      this.capBenchRunning = true;
      this.capBenchLog = [];
      this.capBenchServers = {};
      this.capBenchLogOpen = true;

      // Probe each machine for models (in parallel)
      const probeResults = await Promise.all(machines.map(async m => {
        try {
          const r = await fetch('/api/setup/probe?url=' + encodeURIComponent(m.endpoint_url), {credentials:'include'});
          const d = await r.json();
          const servers = d.servers || [];
          const srv = servers[0] || {};
          return { machine: m, endpoint: srv.endpoint || m.endpoint_url, type: srv.type || 'unknown', models: srv.models || [] };
        } catch(e) {
          return { machine: m, endpoint: m.endpoint_url, type: 'unknown', models: [] };
        }
      }));

      // Initialise per-server status
      probeResults.forEach(p => {
        this.capBenchServers = {...this.capBenchServers, [p.endpoint]: {done: false, error: false, testing: null, best: null, score: null}};
      });

      const allModels = probeResults.flatMap(p =>
        p.models.length ? p.models.map(mod => ({id: mod.id || mod.name, endpoint: p.endpoint, api_key: 'ollama', server_type: p.type}))
        : [{id: '__probe__', endpoint: p.endpoint, api_key: 'ollama', server_type: p.type}]
      ).filter(m => m.id !== '__probe__');

      if (!allModels.length) {
        this.capBenchLog = ['No models found on registered machines. Check that Ollama / LM Studio is running.'];
        this.capBenchRunning = false;
        return;
      }

      const fallback = probeResults[0];
      try {
        const r = await fetch('/api/setup/compare', {
          method: 'POST', credentials: 'include',
          headers: {'Content-Type':'application/json','X-CSRF-Token':getCsrfToken()},
          body: JSON.stringify({endpoint: fallback.endpoint, api_key: 'ollama', server_type: fallback.type, models: allModels}),
        });
        if (!r.ok) { this.capBenchLog = ['Error starting benchmark: HTTP ' + r.status]; this.capBenchRunning = false; return; }
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          buf += dec.decode(value, {stream: true});
          const lines = buf.split('\\n'); buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const ev = JSON.parse(line.slice(6));
              if (ev.log) {
                this.capBenchLog = [...this.capBenchLog, ev.log];
                this.$nextTick(() => { const el = this.$refs.capBenchLogEl; if(el) el.scrollTop = el.scrollHeight; });
              }
              if (ev.testing) {
                // Find which server this model belongs to
                const spec = allModels.find(m => m.id === ev.testing);
                if (spec) {
                  const srv = {...(this.capBenchServers[spec.endpoint] || {}), testing: ev.testing};
                  this.capBenchServers = {...this.capBenchServers, [spec.endpoint]: srv};
                }
              }
              if (ev.result) {
                const ep = ev.result.endpoint;
                if (ep && this.capBenchServers[ep] !== undefined) {
                  const existing = this.capBenchServers[ep] || {};
                  const score = ev.result.eval?.score ?? 0;
                  const best = existing.best == null || score > (existing._bestScore || 0)
                    ? ev.result.model : existing.best;
                  const bestScore = existing.best == null || score > (existing._bestScore || 0)
                    ? score : (existing._bestScore || 0);
                  this.capBenchServers = {...this.capBenchServers, [ep]: {...existing, testing: null, best, score: bestScore, _bestScore: bestScore}};
                }
              }
              if (ev.done) {
                Object.keys(this.capBenchServers).forEach(ep => {
                  this.capBenchServers = {...this.capBenchServers, [ep]: {...this.capBenchServers[ep], done: true}};
                });
              }
            } catch(_) {}
          }
        }
      } catch(e) {
        this.capBenchLog = [...this.capBenchLog, 'Error: ' + String(e)];
      }
      this.capBenchRunning = false;
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

    // ── Evolution methods ──────────────────────────────────────────────
    async loadEvolutionProposals() {
      this.evoLoading = true;
      try {
        let url = `/evolution/proposals?limit=${this.evoPageSize}&offset=${this.evoOffset}`;
        if (this.evoStatusFilter) url += `&status=${encodeURIComponent(this.evoStatusFilter)}`;
        const r = await fetch(url, {credentials: 'include'});
        if (!r.ok) throw new Error(await r.text());
        const d = await r.json();
        this.evolutionProposals = d.items || [];
        this.evolutionTotal = d.total || 0;
      } catch(e) { this.evolutionProposals = []; }
      this.evoLoading = false;
    },

    async loadEvolutionSettings() {
      this.evoSettingsLoading = true;
      try {
        const r = await fetch('/evolution/settings', {credentials: 'include'});
        if (!r.ok) throw new Error(await r.text());
        this.evolutionSettings = await r.json();
        this.evoSettingsForm = Object.assign({}, this.evolutionSettings);
      } catch(e) { /* ignore */ }
      this.evoSettingsLoading = false;
    },

    async saveEvolutionSettings() {
      try {
        const r = await fetch('/evolution/settings', {
          method: 'PATCH',
          credentials: 'include',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': this.csrfToken},
          body: JSON.stringify(this.evoSettingsForm),
        });
        if (!r.ok) throw new Error(await r.text());
        this.evolutionSettings = await r.json();
        this.evoSettingsForm = Object.assign({}, this.evolutionSettings);
      } catch(e) { alert('Save failed: ' + e.message); }
    },

    async decideProposal(id, action, questionText) {
      const body = {action};
      if (action === 'question') body.question_text = questionText || '';
      try {
        const r = await fetch(`/evolution/proposals/${id}/decide`, {
          method: 'POST',
          credentials: 'include',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': this.csrfToken},
          body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(await r.text());
        const updated = await r.json();
        const idx = this.evolutionProposals.findIndex(p => p.id === id);
        if (idx >= 0) this.evolutionProposals[idx] = updated;
        this.evoQuestionText = '';
      } catch(e) { alert('Action failed: ' + e.message); }
    },

    async consultFrontier(id) {
      this.evoConsultResult = null;
      try {
        const r = await fetch(`/evolution/proposals/${id}/consult`, {
          method: 'POST',
          credentials: 'include',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': this.csrfToken},
          body: JSON.stringify({model: this.evoConsultModel}),
        });
        if (!r.ok) throw new Error(await r.text());
        const d = await r.json();
        this.evoConsultResult = d.output;
        // Update local proposal cache
        const idx = this.evolutionProposals.findIndex(p => p.id === id);
        if (idx >= 0) {
          this.evolutionProposals[idx].frontier_output = d.output;
          this.evolutionProposals[idx].frontier_model = d.model;
        }
      } catch(e) { alert('Frontier consultation failed: ' + e.message); }
    },

    evoScheduleMinutes(label) {
      const map = {
        '1 hour': 60, '6 hours': 360, '1 day': 1440,
        '3 days': 4320, '1 week': 10080, '1 month': 43200, '1 year': 525600
      };
      return map[label] || 10080;
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
  <meta name="theme-color" content="#0d0d0d">
  <link rel="icon" type="image/svg+xml" href="/static/logo.svg">
  <link rel="shortcut icon" href="/favicon.ico">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; }

    html { background: #010409; }

    body {
      background-color: #010409;
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
      top: 50%; left: 50%;
      transform: translate(-50%, -50%);
      width: 2400px; height: 2400px;
      border-radius: 50%;
      filter: blur(280px);
      opacity: 0.038;
      animation: ambient-color 60s linear infinite;
      pointer-events: none;
      z-index: 0;
    }

    [x-cloak] { display: none !important; }

    /* ── Logo halo — driven by --hue-deg so it stays in sync with the logo */
    .logo-halo {
      position: absolute;
      inset: -280px;
      border-radius: 50%;
      background: #6366f1;
      filter: blur(160px) hue-rotate(var(--hue-deg, 0deg));
      opacity: 0.16;
      pointer-events: none;
    }
    @keyframes logo-fadein {
      from { opacity: 0; }
      to   { opacity: 1; }
    }
    .logo-wrap {
      animation: logo-fadein 6s cubic-bezier(0.16,1,0.3,1) both;
      /* spring upward when card reveals */
      transition: transform 2.4s cubic-bezier(0.22, 1, 0.36, 1);
    }
    .logo-wrap.logo-up  { transform: translateY(-24px); }
    /* Fast close when logging in */
    .login-reveal.closing {
      max-height: 0 !important; opacity: 0 !important;
      transition: max-height 0.5s ease, opacity 0.35s ease !important;
    }
    .logo-img { filter: hue-rotate(var(--hue-deg, 0deg)) brightness(1.15); }

    /* ── Splash → login reveal ─────────────────────────────────────────
       max-height reserves the space so the logo floats up naturally.
       The card itself slides in from below via its own transform — these
       two motions run independently, giving an organic layered feel.     */
    .login-reveal {
      max-height: 0;
      opacity: 0;
      overflow: visible;
      pointer-events: none;
      clip-path: inset(0 -60px -100px -60px);
      transition: max-height 2.8s cubic-bezier(0.22, 1, 0.36, 1),
                  opacity    0.01s linear,
                  clip-path  2.8s cubic-bezier(0.22, 1, 0.36, 1);
    }
    .login-reveal.open {
      max-height: 700px;
      opacity: 1;
      pointer-events: auto;
      clip-path: inset(-60px -60px -100px -60px);
    }
    /* Card enters from below on its own spring — delayed slightly */
    .login-card {
      transform: translateY(32px);
      opacity: 0;
      transition: transform 2s cubic-bezier(0.34, 1.4, 0.64, 1) 0.3s,
                  opacity   1.4s ease 0.3s;
    }
    .login-reveal.open .login-card {
      transform: translateY(0);
      opacity: 1;
    }
    /* Footer text same entry */
    .login-footer {
      transform: translateY(16px);
      opacity: 0;
      transition: transform 2s cubic-bezier(0.34, 1.4, 0.64, 1) 0.7s,
                  opacity   1.4s ease 0.7s;
    }
    .login-reveal.open .login-footer {
      transform: translateY(0);
      opacity: 1;
    }

    /* ── Hint pulse ── */
    @keyframes hint-pulse {
      0%, 100% { opacity: 0.35; }
      50%       { opacity: 0.7; }
    }
    .hint-text {
      animation: hint-pulse 4.8s ease-in-out infinite;
      transition: opacity 1.6s ease;
    }

    /* ── Card ── */
    .login-card {
      background: rgba(10,18,35,0.85);
      border: 1px solid rgba(99,102,241,0.1);
      border-radius: 28px;
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
      filter: hue-rotate(var(--hue-deg, 0deg)) brightness(1.15);
      cursor: pointer;
    }
    .btn-signin:hover:not(:disabled) {
      opacity: 0.92;
      box-shadow: 0 1px 2px rgba(0,0,0,0.5), 0 0 28px rgba(168,85,247,0.25), 0 0 0 1px rgba(168,85,247,0.35) inset;
    }
    .btn-signin:active:not(:disabled) { transform: translateY(1px); opacity: 0.95; }
    .btn-signin:disabled { opacity: 0.45; cursor: not-allowed; animation: none; }

    /* ── Navigation progress bar ── */
    @keyframes nav-progress{from{width:0%}to{width:100%}}
    .nav-progress-hue{filter:hue-rotate(var(--hue-deg,0deg));}

    /* ── Floating label inputs ── */
    /* Hide browser-native password reveal eye (Edge/Chrome) — we have our own */
    input[type="password"]::-ms-reveal,
    input[type="password"]::-ms-clear { display: none !important; }
    input::-webkit-credentials-auto-fill-button { display: none !important; }
    .float-wrap { position: relative; }
    .float-input {
      width: 100%;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 12px;
      padding: 14px 16px;
      color: #f1f5f9; font-size: 0.875rem; outline: none;
      caret-color: rgba(165,180,252,0.6);
      transition: background 1.2s ease;
    }
    .float-input::placeholder { color: transparent; }
    .float-input:hover  { background: rgba(255,255,255,0.055); }
    .float-input:focus  {
      background: rgba(0,0,0,0.25);
    }
    .float-input:-webkit-autofill,
    .float-input:-webkit-autofill:hover,
    .float-input:-webkit-autofill:focus {
      -webkit-box-shadow: 0 0 0 1000px rgba(0,0,0,0.25) inset;
      -webkit-text-fill-color: #f1f5f9;
    }
    .float-label {
      position: absolute; left: 16px; top: 50%;
      transform: translateY(-50%);
      font-size: 0.875rem; color: rgba(148,163,184,0.4);
      pointer-events: none;
      transition: opacity 0.8s ease, color 0.8s ease;
    }
    /* hide when focused or has value */
    .float-input:focus ~ .float-label,
    .float-input:not(:placeholder-shown) ~ .float-label {
      opacity: 0;
    }
  </style>
</head>
<body @click="toggle()" @mousemove.window.throttle.2000ms="resetInactivity()" @keydown.window="resetInactivity()" x-data="loginApp()" x-init="init()">

  <!-- Main content — flex-centred by body -->
  <div class="relative z-10 w-full px-5" style="max-width:400px;">

    <!-- Logo — springs upward when card reveals -->
    <div class="text-center logo-wrap" :class="{ 'logo-up': phase === 'login' }" style="padding-top:2vh; padding-bottom:1.5rem;">
      <div class="relative inline-block">
        <div class="logo-halo"></div>
        <img src="/static/logo.svg" alt="Logos" class="logo-img relative mx-auto"
             style="width:100px;height:100px;object-fit:contain;">
      </div>
      <!-- Hint — space always reserved so the logo doesn't shift when it appears -->
      <div style="margin-top:2.5rem;height:1.2rem;transition:opacity 0.6s ease;"
           :style="(showHint && phase === 'splash') ? 'opacity:1' : 'opacity:0'">
        <p class="hint-text"
           style="font-size:0.78rem;color:rgba(148,163,184,0.5);letter-spacing:0.08em;">
          click anywhere to continue
        </p>
      </div>
    </div>

    <!-- Reveal section — expands on activate(); static class prevents pre-Alpine flash -->
    <div class="login-reveal" @click.stop :class="{ open: phase === 'login', closing: phase === 'loggedin' }">

      <!-- Card -->
      <div class="login-card px-7 py-7">
        <form @submit.prevent="submit()" @click.stop class="space-y-5">

          <div class="float-wrap">
            <input id="identifier" x-model="identifier" type="text" required autocomplete="username"
                   class="float-input" placeholder=" "/>
            <label class="float-label" for="identifier">Email or Username</label>
          </div>

          <div x-data="{ show: false }" class="float-wrap">
            <input id="password" x-model="password" :type="show ? 'text' : 'password'"
                   required autocomplete="current-password"
                   @keydown.enter.prevent="$el.closest('form').dispatchEvent(new Event('submit'))"
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
      <p class="login-footer text-center mt-6"
         style="font-size:0.72rem;color:rgba(71,85,105,0.7);letter-spacing:0.05em;">
        A self-hosted AI agent platform
      </p>

    </div><!-- /login-reveal -->
  </div>

  <!-- Version badge -->
  <div style="position:fixed;bottom:16px;right:18px;z-index:50;
              font-size:0.65rem;color:rgba(71,85,105,0.45);
              letter-spacing:0.04em;font-family:ui-monospace,monospace;pointer-events:none;">
    __VERSION_LABEL__
  </div>

  <!-- Navigation progress bar — appears when navigating to /setup or / -->
  <div x-show="navigating" x-cloak style="position:fixed;bottom:0;left:0;right:0;height:2px;background:rgba(255,255,255,0.04);z-index:9999;pointer-events:none">
    <div class="nav-progress-hue" style="height:100%;width:0%;background:linear-gradient(90deg,#6366f1,#a855f7);animation:nav-progress linear forwards;"
         :style="`animation-duration:${navBarDur}ms`"></div>
  </div>

  <script src="https://unpkg.com/alpinejs@3/dist/cdn.min.js" defer></script>
  <script>
  function loginApp() {
    return {
      phase: 'splash',   // 'splash' | 'login' | 'setup' | 'loggedin'
      showHint: false,
      identifier: '', password: '', error: '', loading: false, needsSetup: false,
      navigating: false, navBarDur: 2600,
      _hintTimer: null,
      _inactivityTimer: null,
      _setupRedirectTimer: null,

      init() {
        fetch('/auth/me', { credentials: 'same-origin' })
          .then(r => { if (r.ok) window.location.href = '/'; })
          .catch(() => {});
        fetch('/api/setup/status', { credentials: 'same-origin' })
          .then(r => r.ok ? r.json() : null)
          .then(d => { if (d && !d.completed) this.needsSetup = true; })
          .catch(() => {});
        this._hintTimer = setTimeout(() => { if (this.phase === 'splash') this.showHint = true; }, 10000);
        // Drive hue for logo + button via rAF so they're always in sync
        const tick = () => {
          // Anchor hue to wall-clock time so every page load/refresh
          // shows the same colour at the same real-world second.
          const deg = ((Date.now() / 1000) * 6 % 360).toFixed(1);
          document.documentElement.style.setProperty('--hue-deg', deg + 'deg');
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      },

      activate() {
        if (this.phase !== 'splash') { this.resetInactivity(); return; }
        clearTimeout(this._hintTimer);
        this.showHint = false;
        if (this.needsSetup) {
          this.phase = 'setup';
          this.navigating = true; this.navBarDur = 2600;
          // Animate the logo to land on the setup page logo position:
          // Setup page header: pt-10 (40px) + logo is 56×56px centred horizontally.
          // toCX: centre of viewport (setup logo is perfectly centred).
          // toCY: 40 + 28 = 68px (pt-10 padding + half of 56px logo height).
          // scale: target size / login logo size = 56 / 100 = 0.56
          const logoWrap = document.querySelector('.logo-wrap');
          if (logoWrap) {
            const r = logoWrap.getBoundingClientRect();
            const fromCX = r.left + r.width / 2;
            const fromCY = r.top  + r.height / 2;
            const toCX   = window.innerWidth / 2;
            const toCY   = 68;
            const scale  = 56 / 120;
            logoWrap.style.transform = `translate(${toCX - fromCX}px, ${toCY - fromCY}px) scale(${scale.toFixed(4)})`;
          }
          this._setupRedirectTimer = setTimeout(() => { // matches 2.4s logo transition + 200ms settle
            // Freeze all animations before page handoff so nothing is mid-fade
            document.querySelectorAll('.logo-wrap, .logo-halo, .logo-img').forEach(el => {
              const computed = getComputedStyle(el);
              el.style.filter    = computed.filter;
              el.style.opacity   = computed.opacity;
              el.style.animation = 'none';
            });
            // Also freeze the body ambient orb (body::before can't be targeted,
            // so set animation-play-state which pauses it in place)
            document.body.style.setProperty('animation-play-state', 'paused');
            const elapsed = (performance.now() / 1000).toFixed(1);
            window.location.href = `/setup?t=${elapsed}`;
          }, 2600);
        } else {
          this.phase = 'login';
          this._startInactivity();
        }
      },

      toggle() {
        if (this.phase === 'login') { this._revertSplash(); return; }
        this.activate();
      },

      _startInactivity() {
        clearTimeout(this._inactivityTimer);
        this._inactivityTimer = setTimeout(() => this._revertSplash(), 30000);
      },

      resetInactivity() {
        if (this.phase !== 'login') return;
        this._startInactivity();
      },

      _revertSplash() {
        this.phase = 'splash';
        clearTimeout(this._hintTimer);
        this._hintTimer = setTimeout(() => { if (this.phase === 'splash') this.showHint = true; }, 10000);
      },

      async submit() {
        this.loading = true; this.error = '';
        try {
          const res = await fetch('/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ identifier: this.identifier, password: this.password }),
            credentials: 'same-origin',
          });
          if (res.ok) {
            const d = await res.json().catch(() => ({}));
            clearTimeout(this._inactivityTimer);
            // Animate logo to the exact nav logo position on the main page, then navigate.
            // Nav logo layout: max-w-screen-xl mx-auto px-4 pt-4, first item in items-end flex.
            // Logo is 32×32, center = (containerLeft + 16 + 16, 32).
            this.phase = 'loggedin';
            const logoEl = document.querySelector('.logo-wrap');
            if (logoEl) {
              const rect = logoEl.getBoundingClientRect();
              const fromCX = rect.left + rect.width / 2;
              const fromCY = rect.top  + rect.height / 2;
              const vw = window.innerWidth;
              const navLogoX = Math.max(0, (vw - 1280) / 2) + 16 + 16;
              const navLogoY = 32;
              const targetX = navLogoX - fromCX;
              // Compensate: phase='loggedin' queues removal of logo-up (translateY(-24px)) but
              // getBoundingClientRect() still reflects the class transform at this point.
              const targetY = navLogoY - fromCY - 24;
              logoEl.style.transition = 'transform 0.9s cubic-bezier(0.4,0,0.2,1)';
              logoEl.style.transform = `translate(${targetX}px, ${targetY}px) scale(0.27)`;
            }
            // Signal the main page to do a staggered first-load fade-in
            try { sessionStorage.setItem('logos_fl', '1'); } catch {}
            this.navigating = true; this.navBarDur = 900;
            await new Promise(r => setTimeout(r, 900));
            window.location.href = d.setup_required ? '/setup' : '/';
          } else {
            const d = await res.json().catch(() => ({}));
            this.error = ({
              invalid_credentials: 'Invalid credentials.',
              account_locked:      'Account locked \u2014 try again in a few minutes.',
              rate_limited:        'Too many attempts \u2014 slow down.',
              missing_fields:      'Username/email and password required.',
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
  <meta name="setup-ts" content="__SETUP_TS__">
  <title>Logos Setup</title>
  <meta name="theme-color" content="#0d0d0d">
  <link rel="icon" type="image/svg+xml" href="/static/logo.svg">
  <link rel="shortcut icon" href="/favicon.ico">
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <style>
    [x-cloak]{display:none!important}
    html{background:#010409}
    @keyframes page-fadein{from{opacity:0}to{opacity:1}}
    @keyframes page-fadeout{from{opacity:1}to{opacity:0}}
    body{background-color:#010409;background-image:radial-gradient(rgba(99,102,241,0.06) 1px,transparent 1px);background-size:28px 28px;overflow-x:hidden}
    .setup-content{animation:page-fadein 1s ease 0.2s both}
    .setup-fadeout{animation:page-fadeout 0.45s ease forwards!important}
    @keyframes dot-fade{0%,80%,100%{opacity:0}40%{opacity:1}}
    /* All colour cycling driven by --hue-deg from the rAF wall-clock loop */
    @keyframes orb-fadein{to{opacity:0.033}}
    @keyframes halo-fadein{to{opacity:0.14}}
    body::before{
      content:'';position:fixed;top:50%;left:50%;
      transform:translate(-50%,-50%);
      width:2400px;height:2400px;border-radius:50%;
      background:#6366f1;
      filter:blur(280px) hue-rotate(var(--hue-deg,0deg));
      opacity:0;
      pointer-events:none;z-index:0;
      animation:orb-fadein 0.8s ease 0.1s forwards;
    }
    .setup-logo{filter:hue-rotate(var(--hue-deg,0deg))}
    .setup-halo{
      position:absolute;inset:-280px;border-radius:50%;
      background:#6366f1;
      filter:blur(160px) hue-rotate(var(--hue-deg,0deg));
      opacity:0;
      pointer-events:none;
      animation:halo-fadein 0.8s ease 0.1s forwards;
    }
    .btn-primary{
      background:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
      color:#fff;font-weight:500;cursor:pointer;
      box-shadow:0 1px 2px rgba(0,0,0,0.4),0 0 0 1px rgba(99,102,241,0.3) inset;
      transition:opacity 150ms ease,box-shadow 150ms ease;
      filter:hue-rotate(var(--hue-deg,0deg));
    }
    .btn-primary:hover:not(:disabled){opacity:0.9;box-shadow:0 1px 2px rgba(0,0,0,0.5),0 0 20px rgba(168,85,247,0.2),0 0 0 1px rgba(168,85,247,0.35) inset;}
    .btn-primary:active:not(:disabled){opacity:0.95;transform:translateY(1px);}
    .btn-primary:disabled{opacity:0.4;cursor:not-allowed;filter:none;background:#4f46e5;}
    .spinner-hue{filter:hue-rotate(var(--hue-deg,0deg));}
    .icon-hue{filter:hue-rotate(var(--hue-deg,0deg));}
    /* animation-delay set inline by JS to sync with login page cycle */
  </style>
  <script>
    // Drive all hue-synced elements from a single rAF loop via --hue-deg.
    // This guarantees sync regardless of when elements appear in the DOM.
    (function(){
      // Anchor to wall-clock time — same colour at the same real-world second
      // on every page and after every refresh. 6 deg/s → full 360° in 60s.
      function tick() {
        const deg = ((Date.now() / 1000) * 6 % 360).toFixed(1);
        document.documentElement.style.setProperty('--hue-deg', deg + 'deg');
        requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    })();
  </script>
</head>
<body class="min-h-screen text-white">
<div x-data="setup()" x-init="init()" class="flex flex-col min-h-screen">

  <!-- Header -->
  <header class="flex flex-col items-center pt-10 pb-4 gap-4" style="position:relative;z-index:1">
    <div class="relative inline-block">
      <div class="setup-halo"></div>
      <img src="/static/logo.svg" class="setup-logo relative" style="width:56px;height:56px;object-fit:contain;">
    </div>
    <!-- Step indicator — visible from step 1 onward -->
    <div x-show="step > 0" x-transition.opacity class="spinner-hue flex items-center gap-1">
      <template x-for="i in [1,2,3,4,5,6,7]" :key="i">
        <div class="flex items-center gap-1">
          <div class="w-2 h-2 rounded-full transition-all duration-500"
               :class="step > i ? 'bg-indigo-400 cursor-pointer hover:scale-125' : step === i ? 'bg-indigo-500 scale-125' : 'bg-gray-700'"
               @click="goTo(i)" :title="step > i ? 'Go back to step ' + i : ''"></div>
          <div x-show="i < 7" class="w-4 h-px transition-colors duration-500"
               :class="step > i ? 'bg-indigo-600' : 'bg-gray-700'"></div>
        </div>
      </template>
    </div>
  </header>

  <!-- Main content -->
  <main class="setup-content flex-1 flex items-start justify-center px-4 pt-6 pb-16">
    <div class="w-full" :style="'max-width:' + ((step===0 && setupMode === 'new' && !introConfirmed) ? '58rem' : '34rem') + '; transition:max-width 0.4s cubic-bezier(0.4,0,0.2,1)'">
    <!-- Step content — opacity controlled by stepFading for a clean fade-out/fade-in between steps -->
    <div :style="{opacity: stepFading ? 0 : 1, transition: 'opacity 0.18s ease'}" style="will-change:opacity">

      <!-- ── Pre-step: New install vs Connect to existing ────────────── -->
      <div x-show="step===0 && setupMode === null">
        <div class="text-center mb-8">
          <h1 class="text-2xl font-bold mb-2">Welcome to Logos</h1>
          <p class="text-gray-400 text-sm">How would you like to get started?</p>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 max-w-xl mx-auto">
          <!-- New install -->
          <button @click="setupMode = 'new'"
            class="text-left p-6 rounded-2xl border border-gray-700 bg-gray-900 hover:border-indigo-500 hover:bg-gray-800/80 transition-all duration-200 group">
            <div class="w-10 h-10 rounded-xl bg-indigo-950 border border-indigo-800 flex items-center justify-center mb-4 group-hover:bg-indigo-900 transition-colors">
              <svg class="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7"/>
              </svg>
            </div>
            <div class="font-semibold text-white mb-1">Set up a new server</div>
            <div class="text-xs text-gray-400 leading-relaxed">
              First-time install. Configure inference, agents, and your account on this machine.
            </div>
          </button>
          <!-- Connect to existing -->
          <button @click="setupMode = 'connect'; startConnectScan()"
            class="text-left p-6 rounded-2xl border border-gray-700 bg-gray-900 hover:border-indigo-500 hover:bg-gray-800/80 transition-all duration-200 group">
            <div class="w-10 h-10 rounded-xl bg-indigo-950 border border-indigo-800 flex items-center justify-center mb-4 group-hover:bg-indigo-900 transition-colors">
              <svg class="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>
              </svg>
            </div>
            <div class="font-semibold text-white mb-1">Connect to existing server</div>
            <div class="text-xs text-gray-400 leading-relaxed">
              Someone else runs the Logos server. Enter the URL or let us find it on your network.
            </div>
          </button>
        </div>
      </div>

      <!-- ── Connect-to-existing flow ──────────────────────────────────── -->
      <div x-show="step===0 && setupMode === 'connect'" x-cloak>
        <div class="text-center mb-6">
          <h2 class="text-xl font-bold mb-2">Connect to a Logos server</h2>
          <p class="text-gray-400 text-sm">We&rsquo;ll scan your local network, or you can enter a URL directly.</p>
        </div>

        <!-- LAN scan results -->
        <div class="mb-5">
          <div class="flex items-center gap-2 mb-3 text-xs text-gray-500 uppercase tracking-wider font-semibold">
            <span>Nearby servers</span>
            <span x-show="connectScanning" class="inline-block w-3 h-3 border border-indigo-500 border-t-transparent rounded-full animate-spin"></span>
            <span x-show="!connectScanning && connectInstances.length === 0" class="text-gray-700">— none found</span>
          </div>
          <template x-if="connectInstances.length > 0">
            <div class="space-y-2">
              <template x-for="inst in connectInstances" :key="inst.url">
                <button @click="connectUrl = inst.url"
                  class="w-full text-left flex items-center justify-between px-4 py-3 rounded-xl border transition-all duration-150"
                  :class="connectUrl === inst.url ? 'border-indigo-500 bg-indigo-950/40 text-white' : 'border-gray-700 bg-gray-900 hover:border-gray-600 text-gray-300'">
                  <div>
                    <div class="text-sm font-medium" x-text="inst.url"></div>
                    <div class="text-xs text-gray-500 mt-0.5" x-text="inst.setup_completed ? 'Ready' : 'Needs setup'"></div>
                  </div>
                  <svg x-show="connectUrl === inst.url" class="w-4 h-4 text-indigo-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/>
                  </svg>
                </button>
              </template>
            </div>
          </template>
        </div>

        <!-- Manual URL entry -->
        <div class="mb-5">
          <label class="block text-xs text-gray-500 uppercase tracking-wider font-semibold mb-2">Server URL</label>
          <input x-model="connectUrl" type="url" placeholder="http://192.168.1.x:8080"
            class="w-full bg-gray-900 border border-gray-700 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors">
        </div>

        <div x-show="connectError" class="mb-4 px-4 py-3 rounded-xl bg-red-950/40 border border-red-800 text-red-400 text-sm" x-text="connectError"></div>

        <div class="flex items-center gap-3">
          <button @click="setupMode = null; connectError = ''" class="text-sm text-gray-500 hover:text-gray-300 transition-colors">&larr; Back</button>
          <button @click="saveRemoteConnect()" :disabled="!connectUrl || connectSaving"
            class="flex-1 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white font-semibold text-sm transition-all duration-200">
            <span x-show="!connectSaving">Connect &rarr;</span>
            <span x-show="connectSaving">Connecting&hellip;</span>
          </button>
        </div>
      </div>

      <!-- ── Step 0a: Setup overview (wide intro) ─────────────────────── -->
      <div x-show="step===0 && setupMode === 'new' && !introConfirmed">
        <div class="text-center mb-7 relative">
          <h1 class="text-2xl font-bold mb-2" x-text="tldr ? 'welcome to logos no cap' : 'Welcome to Logos'"></h1>
          <p class="text-gray-400 text-sm" x-text="tldr ? 'it does AI things on ur computer fr' : 'A control plane for agentic AI.'"></p>
          <!-- TL;DR toggle — top-right corner -->
          <button @click="tldr=!tldr"
            class="absolute top-0 right-0 spinner-hue text-[10px] px-2.5 py-1 rounded-full border font-medium transition-all"
            :class="tldr ? 'bg-indigo-950 border-indigo-600 text-indigo-200' : 'bg-gray-900 border-gray-700 text-gray-500 hover:border-gray-500 hover:text-gray-400'"
            title="Toggle TL;DR mode">
            <span x-text="tldr ? '⚡ tl;dr on' : '⚡ tl;dr'"></span>
          </button>
        </div>

        <!-- Overview card -->
        <div class="rounded-2xl border border-gray-800 bg-gray-900/60 overflow-hidden mb-6">
          <!-- Header text -->
          <div class="px-7 py-6 border-b border-gray-800/60">
            <div class="text-base font-semibold text-white mb-1" x-text="tldr ? 'what ur about to do' : 'What setup configures'"></div>
            <div class="text-sm text-gray-500 mb-4" x-text="tldr ? '7 steps, its not that deep' : 'Seven steps, from model discovery to launch.'"></div>
            <p class="text-sm text-gray-500 leading-relaxed" x-show="!tldr">
              Setup establishes how Logos routes inference, which models and runtimes are available, and what operating boundaries apply to agent runs.
              These choices define the platform&rsquo;s initial profile. Everything here can be adjusted from the dashboard after launch.
            </p>
            <p class="text-sm text-gray-500 leading-relaxed" x-show="tldr" x-cloak>
              this makes logos find ur AI model and hook everything up. u pick the model, set the vibe, make an account. everything can be changed later its fine
            </p>
            <p class="text-sm text-gray-500 leading-relaxed mt-2" x-show="!tldr">
              Each agent run is recorded as a <span class="text-gray-300 font-medium">STAMP</span> &mdash;
              Soul &plus; Tools &plus; Agent &plus; Model &plus; Policy &mdash; making sessions reproducible, comparable, and auditable.
              Setup defines the defaults that every STAMP inherits.
            </p>
            <p class="text-sm text-gray-500 leading-relaxed mt-2" x-show="tldr" x-cloak>
              every chat gets stamped so u can replay it or compare models. ur just setting the defaults rn
            </p>
          </div>

          <!-- 2-column step grid -->
          <div class="px-7 py-5 grid grid-cols-2 gap-3">
            <template x-for="s in setupSteps" :key="s.n">
              <div class="flex items-start gap-3 p-4 rounded-xl bg-gray-800/30 border border-gray-700/30"
                :class="s.n === setupSteps.length ? 'col-span-2 max-w-[calc(50%-6px)] mx-auto w-full' : ''">
                <div class="w-6 h-6 rounded-full bg-gray-800 border border-gray-700 flex items-center justify-center text-xs font-mono text-gray-400 flex-shrink-0 mt-0.5"
                  x-text="s.n"></div>
                <div class="min-w-0 flex-1">
                  <div class="flex items-center justify-between gap-2 mb-1">
                    <div class="text-sm font-semibold text-gray-200 leading-tight" x-text="s.name"></div>
                    <span class="text-[9px] px-1.5 py-0.5 rounded-full border border-indigo-800 text-indigo-400 font-medium uppercase tracking-wider flex-shrink-0"
                      x-text="s.tag"></span>
                  </div>
                  <div class="text-xs text-gray-500 leading-relaxed" x-text="tldr ? s.tldrDesc : s.desc"></div>
                </div>
              </div>
            </template>
          </div>

          <!-- When complete — same treatment as the header section above the grid -->
          <div class="px-7 py-6 border-t border-gray-800/60">
            <div class="text-base font-semibold text-white mb-1" x-text="tldr ? 'when ur done' : 'When complete'"></div>
            <p class="text-sm text-gray-500 leading-relaxed" x-show="!tldr">
              You&rsquo;ll have a fully configured platform: inference routed to a benchmarked local model,
              an agent runtime and soul selected, and a secured admin account.
              The platform is ready for agent runs.
            </p>
            <p class="text-sm text-gray-500 leading-relaxed" x-show="tldr" x-cloak>
              ur good. AI is ready, account is set up. just go off at this point
            </p>
          </div>
        </div>

        <!-- Continue -->
        <div class="flex justify-center">
          <button @click="introConfirmed = true"
            class="spinner-hue px-8 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold transition-all duration-200">
            Continue &rarr;
          </button>
        </div>
      </div>

      <!-- ── Step 0b: Track selection ──────────────────────────────────── -->
      <div x-show="step===0 && setupMode === 'new' && introConfirmed" x-cloak>
        <div class="text-center mb-6">
          <h2 class="text-xl font-bold mb-2">One decision shapes what follows</h2>
          <p class="text-gray-400 text-sm">Choose your inference path to begin.</p>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <!-- Local-first -->
          <button @click="selectTrack('local')"
            class="icon-hue text-left p-5 rounded-2xl border border-gray-700 bg-gray-900 hover:border-indigo-500 hover:bg-gray-800/80 transition-all duration-200 group">
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
            <div class="text-xs text-indigo-400 font-medium">Free &middot; Private</div>
            <div class="text-xs text-indigo-300/60 mt-0.5">Ollama &middot; LM Studio</div>
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
      <div x-show="step===1" x-cloak>
        <div class="mb-5">
          <h2 class="text-xl font-bold mb-1">Connect your inference server(s)</h2>
          <p class="text-gray-400 text-sm">Logos will scan for Ollama and LM Studio on your local network. You can also add remote servers — on a LAN, VPC, or cloud VM — using a custom address.</p>
        </div>

        <!-- Scanning -->
        <div x-show="autoScanning" class="flex flex-col items-center py-12 gap-4">
          <div class="spinner-hue">
            <div class="w-8 h-8 border-2 border-gray-800 border-t-indigo-500 rounded-full animate-spin"></div>
          </div>
          <p class="text-sm text-gray-500">Scanning local network for model servers&hellip;</p>
        </div>

        <!-- Results -->
        <div x-show="!autoScanning && autoScanDone" class="space-y-3">

          <!-- Rename hint -->
          <p x-show="foundServers.length > 0" class="text-xs text-gray-600 mb-2">Click a server name to rename it.</p>

          <!-- This machine: grouped card when multiple localhost servers found -->
          <template x-if="localServers.length > 1">
            <div class="p-4 rounded-xl border border-gray-700 bg-gray-900 space-y-3">
              <div class="text-xs text-gray-500 uppercase tracking-wider font-semibold">This machine</div>
              <template x-for="s in localServers" :key="s.endpoint">
                <div class="flex items-start gap-3 pt-2 border-t border-gray-800 first:border-t-0 first:pt-0">
                  <button @click="s.status==='up' && toggleServer(s)"
                    :class="isServerSelected(s) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-600'"
                    class="w-5 h-5 rounded border-2 flex items-center justify-center flex-shrink-0 mt-0.5 transition-all">
                    <svg x-show="isServerSelected(s)" class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/>
                    </svg>
                  </button>
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 flex-wrap" x-data="{ editing: false, draft: '' }">
                      <span x-show="!editing" @click="editing=true; draft=s.customName||serverDefaultName(s)"
                        class="text-sm font-semibold text-white cursor-text hover:text-indigo-300 transition-colors"
                        x-text="serverName(s)" title="Click to rename"></span>
                      <input x-show="editing" x-model="draft" type="text"
                        @blur="s.customName=draft.trim()||''; editing=false"
                        @keydown.enter="s.customName=draft.trim()||''; editing=false"
                        @keydown.escape="editing=false"
                        x-init="$watch('editing', v => v && $nextTick(() => $el.focus()))"
                        class="text-sm font-semibold bg-transparent border-b border-indigo-400 text-white focus:outline-none w-32">
                      <span class="text-[10px] font-mono text-gray-600"
                        x-text="':' + new URL(s.endpoint).port"></span>
                      <span x-show="s.status==='up'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-950 text-indigo-300 border border-indigo-800 font-medium">running</span>
                      <span x-show="s.status==='auth_required'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-950 text-amber-400 border border-amber-800 font-medium">auth required</span>
                    </div>
                    <div x-show="s.status==='up'" class="text-xs text-gray-600 mt-0.5"
                      x-text="s.models.length===0 ? 'No models loaded yet' : s.models.length + ' model' + (s.models.length!==1?'s':'') + ' ready'"></div>
                    <div x-show="s.status==='auth_required'" class="mt-2 space-y-1.5">
                      <div class="flex gap-2">
                        <input type="password" placeholder="API key (optional)"
                          x-model="s._apiKey"
                          class="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
                        <button @click="retryWithKey(s)" :disabled="!s._apiKey"
                          class="btn-primary px-3 py-1.5 rounded-lg text-xs flex-shrink-0">Connect</button>
                      </div>
                      <button @click="s._apiKey=''; retryWithKey(s)" class="text-xs text-gray-600 hover:text-gray-400 transition-colors">Skip — connect without key</button>
                    </div>
                  </div>
                </div>
              </template>
            </div>
          </template>

          <!-- Single localhost server — shown as a normal card -->
          <template x-if="localServers.length === 1">
            <template x-for="s in localServers" :key="s.endpoint">
              <div class="p-4 rounded-xl border transition-all"
                :class="isServerSelected(s) ? 'border-indigo-500 bg-indigo-950/30 spinner-hue' : 'border-gray-700 bg-gray-900'">
                <div class="flex items-start gap-3">
                  <button @click="s.status==='up' && toggleServer(s)"
                    :class="isServerSelected(s) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-600'"
                    class="w-5 h-5 rounded border-2 flex items-center justify-center flex-shrink-0 mt-0.5 transition-all">
                    <svg x-show="isServerSelected(s)" class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/>
                    </svg>
                  </button>
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 flex-wrap" x-data="{ editing: false, draft: '' }">
                      <span x-show="!editing" @click="editing=true; draft=s.customName||serverDefaultName(s)"
                        class="text-sm font-semibold text-white cursor-text hover:text-indigo-300 transition-colors"
                        x-text="serverName(s)" title="Click to rename"></span>
                      <input x-show="editing" x-model="draft" type="text"
                        @blur="s.customName=draft.trim()||''; editing=false"
                        @keydown.enter="s.customName=draft.trim()||''; editing=false"
                        @keydown.escape="editing=false"
                        x-init="$watch('editing', v => v && $nextTick(() => $el.focus()))"
                        class="text-sm font-semibold bg-transparent border-b border-indigo-400 text-white focus:outline-none w-32">
                      <span x-show="s.status==='up'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-950 text-indigo-300 border border-indigo-800 font-medium">running</span>
                      <span x-show="s.status==='auth_required'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-950 text-amber-400 border border-amber-800 font-medium">auth required</span>
                    </div>
                    <div class="text-xs text-gray-500 font-mono mt-0.5 truncate" x-text="s.endpoint.replace('/v1','')"></div>
                    <div x-show="s.status==='up'" class="text-xs text-gray-600 mt-0.5"
                      x-text="s.models.length===0 ? 'No models loaded yet' : s.models.length + ' model' + (s.models.length!==1?'s':'') + ' ready'"></div>
                    <div x-show="s.status==='auth_required'" class="mt-2 space-y-1.5">
                      <div class="flex gap-2">
                        <input type="password" placeholder="API key (optional — from LM Studio \u2192 Local Server)"
                          x-model="s._apiKey"
                          class="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
                        <button @click="retryWithKey(s)" :disabled="!s._apiKey"
                          class="btn-primary px-3 py-1.5 rounded-lg text-xs flex-shrink-0">Connect</button>
                      </div>
                      <button @click="s._apiKey=''; retryWithKey(s)" class="text-xs text-gray-600 hover:text-gray-400 transition-colors">Skip — connect without key</button>
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </template>

          <!-- Remote servers — always individual cards -->
          <template x-for="s in remoteServers" :key="s.endpoint">
            <div class="p-4 rounded-xl border transition-all"
              :class="isServerSelected(s) ? 'border-indigo-500 bg-indigo-950/30 spinner-hue' : 'border-gray-700 bg-gray-900'">
              <div class="flex items-start gap-3">
                <button @click="s.status==='up' && toggleServer(s)"
                  :class="isServerSelected(s) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-600'"
                  class="w-5 h-5 rounded border-2 flex items-center justify-center flex-shrink-0 mt-0.5 transition-all">
                  <svg x-show="isServerSelected(s)" class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/>
                  </svg>
                </button>
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2 flex-wrap" x-data="{ editing: false, draft: '' }">
                    <span x-show="!editing" @click="editing=true; draft=s.customName||serverDefaultName(s)"
                      class="text-sm font-semibold text-white cursor-text hover:text-indigo-300 transition-colors"
                      x-text="serverName(s)" title="Click to rename"></span>
                    <input x-show="editing" x-model="draft" type="text"
                      @blur="s.customName=draft.trim()||''; editing=false"
                      @keydown.enter="s.customName=draft.trim()||''; editing=false"
                      @keydown.escape="editing=false"
                      x-init="$watch('editing', v => v && $nextTick(() => $el.focus()))"
                      class="text-sm font-semibold bg-transparent border-b border-indigo-400 text-white focus:outline-none w-32">
                    <span x-show="s.status==='up'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-950 text-indigo-300 border border-indigo-800 font-medium">running</span>
                    <span x-show="s.status==='auth_required'" class="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-950 text-amber-400 border border-amber-800 font-medium">auth required</span>
                  </div>
                  <div class="text-xs text-gray-500 font-mono mt-0.5 truncate" x-text="s.endpoint.replace('/v1','')"></div>
                  <div x-show="s.status==='up'" class="text-xs text-gray-600 mt-0.5"
                    x-text="s.models.length===0 ? 'No models loaded yet' : s.models.length + ' model' + (s.models.length!==1?'s':'') + ' ready'"></div>
                  <div x-show="s.status==='auth_required'" class="mt-2 space-y-1.5">
                    <div class="flex gap-2">
                      <input type="password" placeholder="API key (optional)"
                        x-model="s._apiKey"
                        class="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
                      <button @click="retryWithKey(s)" :disabled="!s._apiKey"
                        class="btn-primary px-3 py-1.5 rounded-lg text-xs flex-shrink-0">Connect</button>
                    </div>
                    <button @click="s._apiKey=''; retryWithKey(s)" class="text-xs text-gray-600 hover:text-gray-400 transition-colors">Skip — connect without key</button>
                  </div>
                </div>
              </div>
            </div>
          </template>

          <!-- Nothing found -->
          <div x-show="foundServers.length===0" class="p-4 rounded-xl bg-gray-900 border border-gray-800">
            <p class="text-sm font-semibold text-gray-300 mb-1">No model servers detected</p>
            <p class="text-xs text-gray-500">Ollama (port 11434) and LM Studio (port 1234) were not found. Use the setup guides below to get started, then scan again.</p>
          </div>

          <!-- Manual add -->
          <div>
            <button @click="showManualEntry=!showManualEntry"
              class="icon-hue flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 transition-colors py-1">
              <svg class="w-3.5 h-3.5 transition-transform" :class="showManualEntry?'rotate-45':''" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
              </svg>
              Add a server at a custom address
            </button>
            <div x-show="showManualEntry" class="mt-2 p-4 rounded-xl bg-gray-900 border border-gray-800 space-y-2">
              <!-- VPC / remote context note -->
              <div class="flex gap-2 p-2.5 rounded-lg bg-indigo-950/40 border border-indigo-900/50 text-xs text-indigo-300">
                <svg class="w-3.5 h-3.5 mt-0.5 shrink-0 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                <span>Point this at any machine running an inference server — your LAN, a VPC, a cloud VM, or a remote server. <strong class="text-indigo-200">No Logos installation is needed there</strong> — just the inference software (Ollama, vLLM, etc.) and a reachable IP/port.</span>
              </div>
              <div class="flex gap-2">
                <select x-model="manualType"
                  class="bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
                  <option value="ollama">Ollama</option>
                  <option value="lmstudio">LM Studio</option>
                  <option value="vllm">vLLM</option>
                  <option value="openai">OpenAI-compatible</option>
                </select>
                <input x-model="manualUrl" type="text"
                  :placeholder="manualType==='ollama' ? 'http://192.168.1.50:11434 or http://gpu-vm.vpc:11434'
                    : manualType==='lmstudio' ? 'http://192.168.1.50:1234'
                    : manualType==='vllm' ? 'http://gpu-vm.vpc:8000'
                    : 'https://api.openai.com/v1 or http://custom-host/v1'"
                  @keydown.enter="addManualServer()"
                  class="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
                <button @click="addManualServer()" :disabled="manualProbing||!manualUrl.trim()"
                  class="btn-primary px-4 py-2 rounded-lg text-sm flex-shrink-0">
                  <span x-show="!manualProbing">Add</span>
                  <span x-show="manualProbing" class="flex items-center gap-1"><div class="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin"></div></span>
                </button>
              </div>
              <input x-model="manualName" type="text"
                placeholder="Custom name (optional, e.g. &ldquo;VPC GPU Node&rdquo; or &ldquo;Gaming PC&rdquo;)"
                class="w-full bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500">
              <input x-show="manualType==='lmstudio' || manualType==='openai'" x-model="manualKey" type="password"
                :placeholder="manualType==='openai' ? 'API key' : 'API key (if auth is enabled in LM Studio)'"
                class="w-full bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 font-mono">
              <p x-show="manualError" class="text-xs text-red-400" x-text="manualError"></p>
            </div>
          </div>

          <!-- Install guides — always available, collapsed by default -->
          <div class="space-y-1 pt-1 border-t border-gray-800/60">
            <p class="text-[10px] text-gray-700 uppercase tracking-wider py-1">Setup guides</p>
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
                <div class="flex gap-2.5"><span class="w-4 h-4 rounded-full bg-gray-700 text-white text-[10px] flex items-center justify-center flex-shrink-0 font-medium">2</span><span class="text-xs text-gray-300">Open the <span class="text-white font-medium">Model Search</span> tab, download models you like that are advised as being able to run on your hardware</span></div>
                <div class="flex gap-2.5"><span class="w-4 h-4 rounded-full bg-gray-700 text-white text-[10px] flex items-center justify-center flex-shrink-0 font-medium">3</span><span class="text-xs text-gray-300">Go to the <span class="text-white font-medium">Developer</span> tab and start your server by clicking the slider button</span></div>
              </div>
            </details>
            <details class="group">
              <summary class="text-xs text-gray-500 hover:text-gray-300 cursor-pointer select-none list-none flex items-center gap-1.5">
                <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                Running on a VPC, cloud VM, or remote server
              </summary>
              <div class="mt-2 p-4 rounded-xl bg-gray-900 border border-gray-800 space-y-4 text-xs text-gray-400">
                <p class="text-gray-300 font-medium">You only need to install the inference software on the remote machine — not Logos.</p>

                <!-- Ollama on remote Linux -->
                <div class="space-y-2">
                  <p class="text-[10px] text-gray-600 uppercase tracking-wider font-semibold">Ollama on a Linux VM</p>
                  <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2">
                    <code class="text-indigo-300 font-mono">curl -fsSL https://ollama.com/install.sh | sh</code>
                    <button @click="copy('curl -fsSL https://ollama.com/install.sh | sh')" class="text-gray-600 hover:text-gray-400 ml-3 flex-shrink-0" x-text="copied==='curl -fsSL https://ollama.com/install.sh | sh'?'copied':'copy'"></button>
                  </div>
                  <p class="text-gray-600">Then expose the API publicly (binds to all interfaces):</p>
                  <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2">
                    <code class="text-indigo-300 font-mono">OLLAMA_HOST=0.0.0.0 ollama serve</code>
                    <button @click="copy('OLLAMA_HOST=0.0.0.0 ollama serve')" class="text-gray-600 hover:text-gray-400 ml-3 flex-shrink-0" x-text="copied==='OLLAMA_HOST=0.0.0.0 ollama serve'?'copied':'copy'"></button>
                  </div>
                  <p class="text-gray-600">Then pull your model: <code class="text-gray-500 font-mono">ollama pull qwen2.5:14b</code></p>
                </div>

                <!-- vLLM on remote GPU -->
                <div class="space-y-2">
                  <p class="text-[10px] text-gray-600 uppercase tracking-wider font-semibold">vLLM on a GPU VM</p>
                  <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2">
                    <code class="text-indigo-300 font-mono">pip install vllm</code>
                    <button @click="copy('pip install vllm')" class="text-gray-600 hover:text-gray-400 ml-3 flex-shrink-0" x-text="copied==='pip install vllm'?'copied':'copy'"></button>
                  </div>
                  <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2">
                    <code class="text-indigo-300 font-mono">vllm serve Qwen/Qwen2.5-14B-Instruct --host 0.0.0.0 --port 8000</code>
                    <button @click="copy('vllm serve Qwen/Qwen2.5-14B-Instruct --host 0.0.0.0 --port 8000')" class="text-gray-600 hover:text-gray-400 ml-3 flex-shrink-0" x-text="copied==='vllm serve Qwen/Qwen2.5-14B-Instruct --host 0.0.0.0 --port 8000'?'copied':'copy'"></button>
                  </div>
                </div>

                <!-- Networking note -->
                <div class="p-3 rounded-lg border border-amber-900/50 bg-amber-950/20 text-amber-300/80 space-y-1.5">
                  <p class="font-medium text-amber-200">Networking checklist</p>
                  <ul class="space-y-1 list-disc list-inside text-amber-300/70">
                    <li>Open the inference port in the VM's firewall / security group (e.g. TCP 11434 for Ollama, 8000 for vLLM)</li>
                    <li>If behind a private VPC with no public IP, use a VPN, SSH tunnel, or reverse proxy</li>
                    <li>On AWS/GCP/Azure: add an inbound rule in the security group / firewall for the port</li>
                  </ul>
                </div>

                <!-- SSH tunnel fallback -->
                <div class="space-y-2">
                  <p class="text-[10px] text-gray-600 uppercase tracking-wider font-semibold">Quick SSH tunnel (no firewall changes needed)</p>
                  <div class="flex items-center justify-between bg-gray-950 rounded-lg px-3 py-2">
                    <code class="text-indigo-300 font-mono">ssh -L 11434:localhost:11434 user@your-vm-ip</code>
                    <button @click="copy('ssh -L 11434:localhost:11434 user@your-vm-ip')" class="text-gray-600 hover:text-gray-400 ml-3 flex-shrink-0" x-text="copied==='ssh -L 11434:localhost:11434 user@your-vm-ip'?'copied':'copy'"></button>
                  </div>
                  <p class="text-gray-600">Then add <code class="text-gray-500 font-mono">http://localhost:11434</code> as your server URL above — traffic tunnels securely.</p>
                </div>
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
              class="btn-primary flex-1 py-2.5 rounded-xl text-sm">
              Continue &rarr;
            </button>
          </div>
        </div>
      </div>

      <!-- ── Step 2: Auto-compare models ─────────────────────────────── -->
      <div x-show="step===2" x-cloak>

        <!-- No models on Ollama: pull catalog -->
        <div x-show="getModels().length===0 && hasOllamaServer()" class="space-y-3">
          <div class="mb-5">
            <h2 class="text-xl font-bold mb-1">Download a model first</h2>
            <p class="text-gray-400 text-sm">No models found on your Ollama server. Pull one to continue.</p>
            <p class="text-gray-500 text-xs mt-2">Not sure which to pick? Ask an AI chatbot: <span class="italic text-gray-600">"What's the best Ollama model for an AI agent on a machine with X GB RAM?"</span> — then come back and download it. Our benchmark will assess whether it&rsquo;s the right fit.</p>
          </div>
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
                    class="btn-primary px-3 py-1.5 rounded-lg text-xs flex-shrink-0">
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

        <!-- No models on LM Studio / llama.cpp / other OpenAI-compat server -->
        <div x-show="getModels().length===0 && !hasOllamaServer()" class="p-5 rounded-xl bg-gray-900 border border-gray-800">
          <div class="text-sm font-medium text-white mb-3">No models found on your server</div>
          <p class="text-xs text-gray-500 mb-3">Your server is running but has no models loaded yet. Load or download a model and then refresh.</p>
          <div class="space-y-2 mb-4">
            <details class="group">
              <summary class="text-xs text-indigo-400 cursor-pointer hover:text-indigo-300 list-none flex items-center gap-1.5">
                <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                LM Studio instructions
              </summary>
              <ol class="mt-2 space-y-1.5 text-xs text-gray-400 list-none pl-4">
                <li>1. Open the <span class="text-white font-medium">Model Search</span> tab and download a model (it will warn you if it won&apos;t fit in VRAM).</li>
                <li>2. Go to <span class="text-white font-medium">Developer &rarr; Local Server</span> and load the model there.</li>
                <li>3. Come back here and click Refresh.</li>
              </ol>
            </details>
            <details class="group">
              <summary class="text-xs text-indigo-400 cursor-pointer hover:text-indigo-300 list-none flex items-center gap-1.5">
                <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                llama.cpp / llama-server instructions
              </summary>
              <ol class="mt-2 space-y-1.5 text-xs text-gray-400 list-none pl-4">
                <li>1. Download a GGUF model from <span class="text-white font-medium">Hugging Face</span> (search for &ldquo;Q4_K_M GGUF&rdquo; variants of models like Qwen3 or Gemma3).</li>
                <li>2. Restart llama-server with: <code class="text-indigo-300 font-mono">llama-server -m /path/to/model.gguf --port 8080</code></li>
                <li>3. Come back here and click Refresh.</li>
              </ol>
            </details>
          </div>
          <button @click="refreshModels()" class="btn-primary px-4 py-2 rounded-lg text-xs">Refresh model list</button>
        </div>

        <!-- Models available: auto-compare -->
        <div x-show="getModels().length > 0">

          <!-- Header -->
          <div class="mb-5">
            <h2 class="text-xl font-bold mb-1" x-text="compareDone ? 'How your models performed' : 'Finding your best model\u2026'"></h2>
            <p class="text-gray-400 text-sm" x-show="!compareDone">Testing available models on your hardware — this takes a moment.</p>
            <p class="text-gray-400 text-sm" x-show="compareDone">We&rsquo;ve pre-selected the best fit. You can override below.</p>
          </div>

          <!-- Per-server benchmark sections -->
          <div class="space-y-5 mb-4">
            <template x-for="server in selectedServers" :key="server.endpoint">
              <div>
                <!-- Server header — only shown when multiple servers are selected -->
                <div x-show="selectedServers.length > 1" class="flex items-center gap-2 mb-2 pb-1.5 border-b border-gray-800/40">
                  <span class="text-xs font-semibold text-gray-300" x-text="serverName(server)"></span>
                  <span class="text-[11px] text-gray-600 font-mono" x-text="server.endpoint"></span>
                </div>

                <!-- Model rows for this server -->
                <div class="space-y-2">
                  <template x-for="mid in compareTargetsForServer(server.endpoint)" :key="mid">
                    <div class="rounded-xl border transition-all duration-300 overflow-hidden"
                      :class="serverModelForServer(server.endpoint) === mid && compareDone ? 'border-indigo-500 bg-indigo-950/20' : 'border-gray-800 bg-gray-900'">
                      <!-- Summary row — clickable once results are in -->
                      <div class="flex items-center justify-between px-3 py-2.5"
                        :class="compareDone && compareResults[mid] && !compareResults[mid].error ? 'cursor-pointer hover:bg-white/5' : ''"
                        @click="compareDone && compareResults[mid] && !compareResults[mid].error ? (compareExpanded = compareExpanded === mid ? null : mid) : null">
                        <div class="flex items-center gap-2 min-w-0 flex-wrap">
                          <span x-show="serverModelForServer(server.endpoint) === mid && compareDone"
                            class="spinner-hue text-[9px] px-1.5 py-0.5 rounded-full bg-indigo-900 text-indigo-300 border border-indigo-700 font-semibold uppercase tracking-wider flex-shrink-0">Best</span>
                          <span class="font-mono text-xs text-gray-200 truncate" x-text="mid"></span>
                        </div>
                        <div class="flex items-center gap-2 flex-shrink-0 ml-3 text-xs">
                          <!-- Queued -->
                          <span x-show="compareTesting !== mid && !compareResults[mid]" class="text-gray-700">queued</span>
                          <!-- Testing / loading -->
                          <template x-if="compareTesting === mid">
                            <div class="flex items-center gap-1.5">
                              <div class="spinner-hue"><div class="w-3 h-3 border border-gray-700 border-t-indigo-400 rounded-full animate-spin"></div></div>
                              <span class="text-gray-500" x-text="compareLoadingFor === mid ? 'loading\u2026' : 'testing\u2026'"></span>
                            </div>
                          </template>
                          <!-- Result -->
                          <template x-if="compareResults[mid]">
                            <div class="flex items-center gap-2">
                              <template x-if="compareResults[mid].error">
                                <span class="text-red-500 cursor-help underline decoration-dotted"
                                  :title="compareResults[mid].error">error</span>
                              </template>
                              <template x-if="!compareResults[mid].error">
                                <div class="flex items-center gap-2">
                                  <span class="font-mono font-semibold"
                                    :class="{
                                      'text-green-400':  compareResults[mid].tok_s >= 30,
                                      'text-indigo-400': compareResults[mid].tok_s >= 15 && compareResults[mid].tok_s < 30,
                                      'text-yellow-400': compareResults[mid].tok_s >= 6  && compareResults[mid].tok_s < 15,
                                      'text-red-400':    compareResults[mid].tok_s < 6,
                                    }"
                                    x-text="compareResults[mid].tok_s + ' tok/s'"></span>
                                  <span :class="compareResults[mid].quality_pass ? 'text-green-500' : 'text-yellow-600'"
                                    x-text="compareResults[mid].quality_pass ? '\u2713' : '\u26a0'"></span>
                                  <span x-show="compareDone" class="text-gray-700 text-[10px]"
                                    x-text="compareExpanded === mid ? '\u25b4' : '\u25be'"></span>
                                </div>
                              </template>
                            </div>
                          </template>
                        </div>
                      </div>
                      <!-- Expanded detail panel -->
                      <div x-show="compareExpanded === mid && compareResults[mid] && !compareResults[mid].error"
                        class="px-3 pb-3 border-t border-gray-800/60 pt-2.5 space-y-2">
                        <!-- Speed + TTFT -->
                        <div class="flex items-center gap-4 text-xs">
                          <div>
                            <span class="text-gray-600">Speed</span>
                            <span class="ml-1.5 font-mono font-semibold"
                              :class="{
                                'text-green-400':  compareResults[mid]?.tok_s >= 30,
                                'text-indigo-400': compareResults[mid]?.tok_s >= 15 && compareResults[mid]?.tok_s < 30,
                                'text-yellow-400': compareResults[mid]?.tok_s >= 6  && compareResults[mid]?.tok_s < 15,
                                'text-red-400':    compareResults[mid]?.tok_s < 6,
                              }"
                              x-text="compareResults[mid]?.tok_s + ' tok/s'"></span>
                            <span class="ml-1 text-gray-600"
                              x-text="compareResults[mid]?.tok_s >= 30 ? '(fast)' : compareResults[mid]?.tok_s >= 15 ? '(good)' : compareResults[mid]?.tok_s >= 6 ? '(usable \u2014 may feel slow)' : '(too slow for agent use)'"></span>
                          </div>
                          <div x-show="compareResults[mid]?.ttft_ms">
                            <span class="text-gray-600">TTFT</span>
                            <span class="ml-1.5 font-mono text-gray-300" x-text="compareResults[mid]?.ttft_ms + 'ms'"></span>
                          </div>
                        </div>
                        <!-- Eval breakdown -->
                        <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
                          <div class="flex items-center gap-1.5">
                            <span :class="compareResults[mid]?.eval?.instruction ? 'text-green-500' : 'text-red-400'"
                              x-text="compareResults[mid]?.eval?.instruction ? '\u2713' : '\u2717'"></span>
                            <span class="text-gray-400">Instruction following</span>
                          </div>
                          <div class="flex items-center gap-1.5">
                            <span :class="compareResults[mid]?.eval?.reasoning ? 'text-green-500' : 'text-red-400'"
                              x-text="compareResults[mid]?.eval?.reasoning ? '\u2713' : '\u2717'"></span>
                            <span class="text-gray-400">Reasoning</span>
                          </div>
                          <div class="flex items-center gap-1.5">
                            <span :class="compareResults[mid]?.eval?.format ? 'text-green-500' : 'text-red-400'"
                              x-text="compareResults[mid]?.eval?.format ? '\u2713' : '\u2717'"></span>
                            <span class="text-gray-400">JSON format</span>
                          </div>
                          <div class="flex items-center gap-1.5">
                            <span :class="compareResults[mid]?.eval?.tool_call ? 'text-green-500' : 'text-red-400'"
                              x-text="compareResults[mid]?.eval?.tool_call ? '\u2713' : '\u2717'"></span>
                            <span class="text-gray-400">Tool selection</span>
                          </div>
                        </div>
                        <!-- Why not selected (if not the best for this server) -->
                        <div x-show="serverModelForServer(server.endpoint) !== mid && compareDone" class="text-[11px] text-gray-600 pt-0.5">
                          <template x-if="compareResults[mid]?.eval?.score < 3">
                            <span>Not selected: failed
                              <span class="text-red-400/80" x-text="[
                                !compareResults[mid]?.eval?.instruction && 'instruction following',
                                !compareResults[mid]?.eval?.reasoning   && 'reasoning',
                                !compareResults[mid]?.eval?.format      && 'JSON format',
                                !compareResults[mid]?.eval?.tool_call   && 'tool selection',
                              ].filter(Boolean).join(', ')"></span>
                              — agent loops may break.
                            </span>
                          </template>
                          <template x-if="compareResults[mid]?.eval?.score >= 3 && compareResults[mid]?.tok_s < 6">
                            <span>Not selected: too slow for interactive agent use.</span>
                          </template>
                          <template x-if="compareResults[mid]?.eval?.score >= 3 && compareResults[mid]?.tok_s >= 6">
                            <span>Not selected: another model scored higher overall.</span>
                          </template>
                        </div>
                        <!-- Use this model for this machine -->
                        <button @click="pickServerModel(server.endpoint, mid); compareExpanded = null"
                          class="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors">
                          Use this model for this machine &rarr;
                        </button>
                      </div>
                    </div>
                  </template>

                  <!-- Waiting to start for this server -->
                  <div x-show="compareTargetsForServer(server.endpoint).length === 0 && compareRunning" class="flex items-center gap-2 py-2 text-gray-600 text-sm">
                    <div class="spinner-hue"><div class="w-3 h-3 border border-gray-700 border-t-indigo-400 rounded-full animate-spin"></div></div>
                    <span>Scanning models&hellip;</span>
                  </div>
                </div>
              </div>
            </template>

            <!-- Loading model notice (global) -->
            <div x-show="compareLoadingFor" class="text-xs text-amber-400/80 px-1 flex items-center gap-1.5">
              <div class="spinner-hue"><div class="w-2.5 h-2.5 border border-amber-900 border-t-amber-400 rounded-full animate-spin"></div></div>
              <span>Loading <span class="font-mono" x-text="compareLoadingFor"></span> into memory&hellip;</span>
            </div>
          </div>

          <!-- Load warning — shown while benchmark is running -->
          <div x-show="compareRunning"
            class="mb-3 p-2.5 rounded-xl bg-gray-900/50 border border-gray-800 text-[11px] text-gray-500 flex items-start gap-2">
            <span class="mt-0.5 flex-shrink-0">⚠</span>
            <span>Results depend on current server load. For accurate scores, avoid running other workloads on your inference machines during the benchmark.</span>
          </div>

          <!-- Top pick recommendation -->
          <div x-show="compareDone && compareRecommended"
            class="mb-2 p-3 rounded-xl bg-indigo-950/40 border border-indigo-800 text-xs leading-relaxed space-y-1">
            <div class="flex items-center gap-2 text-indigo-300 font-semibold text-[11px] uppercase tracking-wider">
              <span>🎯</span><span>Top pick</span>
              <span class="font-mono font-normal normal-case tracking-normal text-indigo-400" x-text="compareRecommended"></span>
            </div>
            <div class="text-indigo-200" x-text="compareReason"></div>
            <div class="text-indigo-400/60 text-[10px]">Best overall for agent tasks — reasoning, tool use, and response quality.</div>
          </div>

          <!-- Speed pick (only shown when different from top pick) -->
          <div x-show="compareDone && compareFastRecommended"
            class="mb-3 p-3 rounded-xl bg-gray-900/60 border border-gray-700 text-xs leading-relaxed space-y-1">
            <div class="flex items-center gap-2 text-gray-300 font-semibold text-[11px] uppercase tracking-wider">
              <span>⚡</span><span>Speed pick</span>
              <span class="font-mono font-normal normal-case tracking-normal text-gray-400" x-text="compareFastRecommended"></span>
            </div>
            <div class="text-gray-400" x-text="compareFastReason"></div>
            <div class="text-gray-600 text-[10px]">Faster responses — good for quick questions and high-volume use.</div>
          </div>

          <!-- Server load notice -->
          <div x-show="compareDone" class="mb-3 text-[10px] text-gray-600 px-1 leading-relaxed">
            Scores reflect conditions at test time. A server handling other requests or services during testing may show lower throughput than usual.
          </div>

          <!-- Secondary models available for delegation -->
          <div x-show="compareDone && secondaryModels().length > 0" class="mb-4">
            <details class="group">
              <summary class="text-xs text-gray-500 hover:text-gray-300 cursor-pointer list-none flex items-center gap-1.5 py-1">
                <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                <span x-text="secondaryModels().length + ' more model' + (secondaryModels().length !== 1 ? 's' : '') + ' available for delegation'"></span>
              </summary>
              <div class="mt-2 space-y-1">
                <template x-for="m in secondaryModels()" :key="m.id">
                  <div class="flex items-center justify-between px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 text-xs">
                    <span class="font-mono text-gray-400 truncate" x-text="m.id"></span>
                    <span class="text-gray-600 font-mono flex-shrink-0 ml-2"
                      x-text="modelServer(m.id) ? serverName(modelServer(m.id)) : ''"></span>
                  </div>
                </template>
                <p class="text-[11px] text-gray-600 px-1 pt-1">The agent can delegate heavier tasks to these models automatically.</p>
              </div>
            </details>
          </div>
          <div x-show="compareDone && !compareRecommended"
            class="mb-4 p-3 rounded-xl bg-red-950/30 border border-red-900 text-xs text-red-400 space-y-1.5">
            <div class="font-medium">Could not benchmark any models.</div>
            <div x-show="compareReason" class="font-mono opacity-80" x-text="compareReason"></div>
            <div>Make sure a model is loaded and reachable, then try again.</div>
          </div>

          <!-- Override / re-run (after done) -->
          <div x-show="compareDone" class="mb-4 space-y-2">
            <details class="group">
              <summary class="text-xs text-gray-500 hover:text-gray-300 cursor-pointer list-none flex items-center gap-1.5 py-1">
                <svg class="w-3 h-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                Override model selection
              </summary>
              <div class="mt-2 space-y-1">
                <template x-for="m in getModels()" :key="m.id">
                  <button @click="pickServerModel(m._serverEndpoint || activeServer.endpoint, m.id); compareRecommended = m.id"
                    :class="selectedModel === m.id ? 'border-indigo-500 bg-indigo-950/30 text-white' : 'border-gray-800 hover:border-gray-700 text-gray-400'"
                    class="w-full text-left px-3 py-2 rounded-lg bg-gray-900 border transition-all text-xs font-mono">
                    <span x-text="m.id"></span>
                  </button>
                </template>
              </div>
            </details>
            <button @click="startCompare()" class="text-xs text-gray-600 hover:text-gray-400 transition-colors">&#8635; Re-run comparison</button>
          </div>

          <!-- Continue (only shown after comparison completes) -->
          <div x-show="compareDone && allServersHaveModel()" x-transition.opacity>
            <button @click="goNext()"
              class="btn-primary w-full py-2.5 rounded-xl text-sm"
              x-text="selectedServers.length > 1 ? 'Confirm model assignments \u2192' : 'Confirm default model \u2192'">
            </button>
          </div>

          <!-- TTFT outlier warning — shown after benchmark if any model was suspiciously slow -->
          <div x-show="compareDone && compareTtftWarnings.length > 0"
            class="mt-3 p-2.5 rounded-xl bg-amber-950/30 border border-amber-900 text-[11px] text-amber-400 space-y-1">
            <div class="font-medium flex items-center gap-1.5">
              <span>⚠</span><span>High TTFT detected — inference server may be under load</span>
            </div>
            <template x-for="w in compareTtftWarnings" :key="w.model">
              <div class="font-mono opacity-80">
                <span x-text="w.model"></span>
                <span class="text-amber-600"> — </span>
                <span x-text="(w.ttft_ms / 1000).toFixed(1) + 's TTFT'"></span>
              </div>
            </template>
            <div class="text-amber-600/80">Re-run the benchmark with no other workloads on the inference machine for more accurate results.</div>
          </div>

          <!-- Debug log — bottom of step so it expands into free space -->
          <div x-show="compareLog.length > 0" class="mt-4">
            <div class="flex items-center justify-between">
              <button @click="compareLogOpen = !compareLogOpen"
                class="flex items-center gap-1.5 text-[11px] text-gray-600 hover:text-gray-400 transition-colors select-none">
                <svg :class="compareLogOpen ? 'rotate-90' : ''" class="w-3 h-3 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                <span x-text="compareLogOpen ? 'Hide debug log' : 'Show debug log (' + compareLog.length + ' lines)'"></span>
              </button>
              <button x-show="compareLogOpen" @click="copyDebugLog()"
                class="flex items-center gap-1 text-[11px] transition-colors select-none"
                :class="compareCopied ? 'text-green-400' : 'text-gray-600 hover:text-gray-400'">
                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
                <span x-text="compareCopied ? 'Copied!' : 'Copy'"></span>
              </button>
            </div>
            <div x-show="compareLogOpen" class="relative">
              <div class="thin-scroll mt-1.5 rounded-xl bg-black/60 border border-gray-800 px-3 py-2.5 font-mono text-[11px] leading-relaxed space-y-0.5"
                style="max-height:16rem;overflow-y:auto"
                x-ref="compareLogEl"
                @scroll="compareLogPinned = $el.scrollTop < $el.scrollHeight - $el.clientHeight - 20">
                <template x-for="(entry, idx) in compareLog" :key="idx">
                  <div :class="entry.startsWith('      ') ? 'text-gray-600 pl-2' : entry.startsWith('Recommendation') ? 'text-indigo-400' : entry.startsWith('→') ? 'text-gray-300' : entry.includes('✓') ? 'text-green-500/80' : entry.includes('✗') ? 'text-red-500/70' : 'text-gray-500'"
                    x-text="entry"></div>
                </template>
              </div>
              <button x-show="compareLogPinned" @click="compareLogPinned=false; $nextTick(()=>{ const el=$refs.compareLogEl; if(el) el.scrollTop=el.scrollHeight; })"
                class="absolute bottom-2 right-2 flex items-center gap-1 rounded-lg bg-gray-800/90 border border-gray-700 px-2 py-0.5 text-[10px] text-gray-400 hover:text-white hover:border-gray-500 transition-colors">
                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                <span>scroll to bottom</span>
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- ── Step 3: Choose your agent runtime ─────────────────────── -->
      <div x-show="step===3" x-cloak>
        <div class="mb-5">
          <h2 class="text-xl font-bold mb-1">Choose your agent</h2>
          <p class="text-gray-400 text-sm">Select the agent runtime that will handle your conversations and tasks.</p>
        </div>

        <div class="space-y-2 mb-4">
          <!-- Hermes — available -->
          <button @click="selectedAgentType = 'hermes'"
            class="w-full text-left p-4 rounded-xl border transition-all duration-200"
            :class="selectedAgentType === 'hermes' ? 'border-indigo-500 bg-indigo-950/30 spinner-hue' : 'border-gray-800 bg-gray-900 hover:border-gray-700'">
            <div class="flex items-start gap-3">
              <div class="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 mt-0.5 text-lg transition-colors"
                :class="selectedAgentType === 'hermes' ? 'bg-indigo-900 border border-indigo-700' : 'bg-gray-800 border border-gray-700'">✦</div>
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 mb-0.5">
                  <span class="text-sm font-semibold text-white">Hermes</span>
                  <span class="text-[9px] px-1.5 py-0.5 rounded-full bg-green-950 text-green-400 border border-green-800 font-semibold uppercase tracking-wider">Available</span>
                  <span x-show="selectedAgentType === 'hermes'" class="text-[9px] px-1.5 py-0.5 rounded-full bg-indigo-900 text-indigo-300 border border-indigo-700 font-semibold uppercase tracking-wider">Selected</span>
                </div>
                <p class="text-xs text-gray-400 leading-relaxed">General-purpose agent with a full tool loop — research, coding, file operations, web browsing, and more. The default runtime for all conversation types.</p>
                <div class="flex flex-wrap gap-1 mt-2">
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700">Web search</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700">File I/O</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700">Code execution</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700">Terminal</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700">Browsing</span>
                </div>
              </div>
            </div>
          </button>

          <!-- Future runtimes — coming soon -->
          <div class="w-full text-left p-4 rounded-xl border border-gray-800 bg-gray-900/40 opacity-50 cursor-not-allowed select-none">
            <div class="flex items-start gap-3">
              <div class="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 mt-0.5 text-lg bg-gray-800 border border-gray-700 text-gray-600">◈</div>
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 mb-0.5">
                  <span class="text-sm font-semibold text-gray-500">More agents</span>
                  <span class="text-[9px] px-1.5 py-0.5 rounded-full bg-gray-800 text-gray-600 border border-gray-700 font-semibold uppercase tracking-wider">Coming soon</span>
                </div>
                <p class="text-xs text-gray-600 leading-relaxed">Additional agent runtimes will appear here. Once Hermes is running, you can ask it to help you configure and launch other agent types.</p>
              </div>
            </div>
          </div>
        </div>

        <div x-show="selectedAgentType" x-transition.opacity>
          <button @click="goNext()" class="btn-primary w-full py-2.5 rounded-xl text-sm">
            Continue &rarr;
          </button>
        </div>
      </div>

      <!-- ── Step 4: Where to run agents ───────────────────────────── -->
      <div x-show="step===4" x-cloak>
        <div class="mb-5">
          <h2 class="text-xl font-bold mb-1">Where to run agents</h2>
          <p class="text-gray-400 text-sm">Choose where agent tasks execute. This affects isolation between sessions, resource limits, and how Logos scales.</p>
        </div>

        <div class="space-y-2 mb-4">
          <!-- In-process (local) -->
          <button @click="execEnv = 'local'"
            class="w-full text-left p-4 rounded-xl border transition-all duration-200"
            :class="execEnv === 'local' ? 'border-indigo-500 bg-indigo-950/30 spinner-hue' : 'border-gray-800 bg-gray-900 hover:border-gray-700'">
            <div class="flex items-start gap-3">
              <div class="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 mt-0.5 text-lg transition-colors"
                :class="execEnv === 'local' ? 'bg-indigo-900 border border-indigo-700' : 'bg-gray-800 border border-gray-700'">
                <svg class="w-4 h-4" :class="execEnv==='local' ? 'text-indigo-300' : 'text-gray-500'" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>
                </svg>
              </div>
              <div class="flex-1">
                <div class="flex items-center gap-2 mb-0.5">
                  <span class="text-sm font-semibold text-white">In-process</span>
                  <span x-show="execEnv === 'local'" class="text-[9px] px-1.5 py-0.5 rounded-full bg-indigo-900 text-indigo-300 border border-indigo-700 font-semibold uppercase tracking-wider">Selected</span>
                </div>
                <p class="text-xs text-gray-400">Agent runs execute as threads inside the Logos process — whether that is a Windows app, a Linux install, or a Docker container. Simple setup, no extra infrastructure.</p>
                <p class="text-xs text-gray-600 mt-1">All concurrent sessions share the same process boundary and resource ceiling. Workspace isolation is handled by Logos internally, not at the OS level.</p>
                <div class="mt-2 flex flex-wrap gap-3 text-[10px] text-gray-600">
                  <span>~600 MB–1 GB RAM per agent run (shared pool)</span>
                  <span>&middot; 0.5–2 CPU cores (spikes during tool use)</span>
                  <span>&middot; model load on inference machine</span>
                </div>
              </div>
            </div>
          </button>

          <!-- Kubernetes -->
          <button @click="execEnv = 'k8s'"
            class="w-full text-left p-4 rounded-xl border transition-all duration-200"
            :class="execEnv === 'k8s' ? 'border-indigo-500 bg-indigo-950/30 spinner-hue' : 'border-gray-800 bg-gray-900 hover:border-gray-700'">
            <div class="flex items-start gap-3">
              <div class="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 mt-0.5 text-base transition-colors font-bold"
                :class="execEnv === 'k8s' ? 'bg-indigo-900 border border-indigo-700 text-indigo-300' : 'bg-gray-800 border border-gray-700 text-gray-500'">k8s</div>
              <div class="flex-1">
                <div class="flex items-center gap-2 mb-0.5">
                  <span class="text-sm font-semibold text-white">Kubernetes cluster</span>
                  <span x-show="execEnv === 'k8s'" class="text-[9px] px-1.5 py-0.5 rounded-full bg-indigo-900 text-indigo-300 border border-indigo-700 font-semibold uppercase tracking-wider">Selected</span>
                </div>
                <p class="text-xs text-gray-400">Each agent run spawns a dedicated Kubernetes Job — a fully isolated pod with its own filesystem, process space, and resource limits.</p>
                <p class="text-xs text-gray-600 mt-1">Works whether Logos itself runs inside the cluster or externally (Windows, Docker, bare metal). Choose the connection mode below.</p>
                <div class="mt-2 flex flex-wrap gap-3 text-[10px] text-gray-600">
                  <span>500m CPU · 2 Gi memory (requests)</span>
                  <span>&middot; 4 CPU / 8 Gi (limits)</span>
                </div>
              </div>
            </div>
          </button>
        </div>

        <!-- Kubernetes config (shown when k8s selected) -->
        <div x-show="execEnv === 'k8s'" x-transition.opacity class="mb-4 space-y-3">
          <div class="p-4 rounded-xl bg-gray-900 border border-gray-800 space-y-3">
            <!-- Mode selector -->
            <div class="flex gap-2">
              <button @click="k8sMode = 'incluster'"
                class="flex-1 py-1.5 rounded-lg text-xs font-medium transition-colors"
                :class="k8sMode === 'incluster' ? 'bg-indigo-900 text-indigo-200 border border-indigo-700 spinner-hue' : 'bg-gray-800 text-gray-400 border border-gray-700 hover:border-gray-600'">
                Logos is in the cluster
              </button>
              <button @click="k8sMode = 'kubeconfig'"
                class="flex-1 py-1.5 rounded-lg text-xs font-medium transition-colors"
                :class="k8sMode === 'kubeconfig' ? 'bg-indigo-900 text-indigo-200 border border-indigo-700 spinner-hue' : 'bg-gray-800 text-gray-400 border border-gray-700 hover:border-gray-600'">
                Logos is outside the cluster
              </button>
            </div>

            <!-- Mode description -->
            <div x-show="k8sMode === 'incluster'" class="text-xs text-gray-500 leading-relaxed">
              Logos is deployed as a pod inside your Kubernetes cluster. It authenticates using the service account token automatically mounted in its pod — no credentials or cluster address needed.
            </div>
            <div x-show="k8sMode === 'kubeconfig'" class="text-xs text-gray-500 leading-relaxed">
              Logos is running outside the cluster — on a PC, in a standalone Docker container, or on a server. Paste a kubeconfig below so Logos can reach the cluster API to spawn agent Jobs there.
            </div>

            <!-- Auto namespace (read-only) -->
            <div class="flex items-center gap-2">
              <span class="text-xs text-gray-500">Namespace</span>
              <span class="font-mono text-xs text-indigo-300 bg-indigo-950/40 border border-indigo-900 px-2 py-0.5 rounded" x-text="selectedAgentType || 'hermes'"></span>
              <span class="text-xs text-gray-600">— derived from agent runtime</span>
            </div>

            <!-- Kubeconfig paste / drag-and-drop / browse -->
            <div x-show="k8sMode === 'kubeconfig'"
                 @dragover.prevent="$el.classList.add('border-indigo-600')"
                 @dragleave="$el.classList.remove('border-indigo-600')"
                 @drop.prevent="$el.classList.remove('border-indigo-600'); $event.dataTransfer.files[0] && $event.dataTransfer.files[0].text().then(t => kubeconfig = t)"
                 class="border-2 border-dashed border-gray-700 rounded-lg p-2 transition-colors">
              <label class="block text-xs text-gray-500 mb-1">
                Paste kubeconfig YAML, drag-and-drop a file here, or
                <span class="text-indigo-400 cursor-pointer hover:text-indigo-300 underline" @click="$refs.kubeconfigFile.click()">browse</span>
                <span class="text-gray-600">(contains cluster address and credentials)</span>
              </label>
              <input type="file" x-ref="kubeconfigFile" accept=".yaml,.yml,.conf,text/plain" class="hidden"
                     @change="$event.target.files[0] && $event.target.files[0].text().then(t => { kubeconfig = t; $event.target.value = ''; })">
              <textarea x-model="kubeconfig" rows="6" placeholder="apiVersion: v1&#10;kind: Config&#10;..."
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs text-white placeholder-gray-600 font-mono focus:outline-none focus:border-indigo-500 resize-none appearance-none"
                style="box-sizing:border-box;"></textarea>
            </div>

            <!-- Test connection button + result -->
            <div class="flex items-center gap-3">
              <button @click="testK8s()"
                :disabled="k8sTesting || (k8sMode === 'kubeconfig' && !kubeconfig.trim())"
                class="px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-500 text-xs text-gray-300 transition-colors disabled:opacity-40">
                <span x-show="!k8sTesting">Test connection</span>
                <span x-show="k8sTesting" class="flex items-center gap-1.5">
                  <div class="w-3 h-3 border border-gray-600 border-t-indigo-400 rounded-full animate-spin"></div>
                  Testing&hellip;
                </span>
              </button>
              <span x-show="k8sTestResult === 'ok'" class="text-xs text-green-400">&#x2713; Connected to namespace <span class="font-mono" x-text="selectedAgentType || 'hermes'"></span></span>
              <span x-show="k8sTestResult === 'error'" class="text-xs text-red-400" x-text="k8sTestError"></span>
            </div>
          </div>
        </div>

        <div x-show="execEnv" x-transition.opacity>
          <button @click="goNext()" class="btn-primary w-full py-2.5 rounded-xl text-sm">
            Continue &rarr;
          </button>
        </div>
      </div>

      <!-- ── Step 5: Choose a soul ───────────────────────────────────── -->
      <div x-show="step===5" x-cloak>
        <div class="mb-5">
          <h2 class="text-xl font-bold mb-1">Choose a soul</h2>
          <p class="text-gray-400 text-sm">A soul shapes how your agent thinks and communicates and is a starting point for its character. It works alongside the tools you enable &mdash; pick one that fits what you want for your first agent instance.</p>
        </div>

        <!-- 4×2 grid — each card offset by 45° so the hue wave sweeps left→right -->
        <div class="grid grid-cols-2 gap-2 mb-6">
          <template x-for="(soul, idx) in soulOptions" :key="soul.slug">
            <button @click="selectedSoul = soul.slug"
              class="text-left p-3 rounded-xl border transition-all duration-200 relative overflow-hidden"
              :class="selectedSoul === soul.slug ? 'border-indigo-500 bg-indigo-950/30' : 'border-gray-800 bg-gray-900 hover:border-gray-600'"
              :style="selectedSoul === soul.slug ? 'filter: hue-rotate(var(--hue-deg, 0deg))' : ''">
              <!-- icon -->
              <div class="w-8 h-8 rounded-lg flex items-center justify-center mb-2 text-sm transition-colors"
                :class="selectedSoul === soul.slug ? 'bg-indigo-900 border border-indigo-700' : 'bg-gray-800 border border-gray-700'"
                x-text="soul.icon"></div>
              <!-- name + selected badge -->
              <div class="flex items-center gap-1.5 mb-1 flex-wrap">
                <span class="text-xs font-semibold text-white leading-tight" x-text="soul.name"></span>
                <span x-show="selectedSoul === soul.slug"
                  class="text-[8px] px-1 py-0.5 rounded-full bg-indigo-900 text-indigo-300 border border-indigo-700 font-semibold uppercase tracking-wider flex-shrink-0">✓</span>
              </div>
              <!-- desc -->
              <p class="text-[10px] text-gray-500 leading-relaxed line-clamp-3" x-text="soul.desc"></p>
            </button>
          </template>
        </div>

        <div x-show="selectedSoul" x-transition.opacity>
          <button @click="goNext()" class="btn-primary w-full py-2.5 rounded-xl text-sm">
            Continue &rarr;
          </button>
        </div>
      </div>

      <!-- ── Step 6: Your account ────────────────────────────────────── -->
      <div x-show="step===6" x-cloak>
        <div class="mb-6">
          <h2 class="text-xl font-bold mb-1">Your account</h2>
          <p class="text-gray-400 text-sm">Set your login credentials. You'll use these to sign in to Logos going forward.</p>
        </div>
        <div class="space-y-3 mb-6">
          <div>
            <label class="block text-xs text-gray-500 mb-1.5">Email address</label>
            <input x-model="setupEmail" type="email" placeholder="" autocomplete="off"
              class="w-full bg-gray-900 border border-gray-700 rounded-xl px-3 py-2.5 text-sm focus:border-indigo-500 focus:outline-none transition-colors"
              style="-webkit-box-shadow:0 0 0 1000px #111827 inset;-webkit-text-fill-color:#f3f4f6;color:#f3f4f6" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1.5">Username</label>
            <input x-model="setupUsername" type="text" placeholder="" autocomplete="off"
              class="w-full bg-gray-900 border border-gray-700 rounded-xl px-3 py-2.5 text-sm focus:border-indigo-500 focus:outline-none transition-colors"
              style="-webkit-box-shadow:0 0 0 1000px #111827 inset;-webkit-text-fill-color:#f3f4f6;color:#f3f4f6" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1.5">Password</label>
            <input x-model="setupPassword" type="password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;"
              class="w-full bg-gray-900 border border-gray-700 rounded-xl px-3 py-2.5 text-sm focus:border-indigo-500 focus:outline-none transition-colors" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1.5">Confirm password</label>
            <input x-model="setupPasswordConfirm" type="password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;"
              @keydown.enter="setupEmail.trim() && setupUsername.trim() && setupPassword && setupPassword === setupPasswordConfirm && goNext()"
              class="w-full bg-gray-900 border border-gray-700 rounded-xl px-3 py-2.5 text-sm focus:border-indigo-500 focus:outline-none transition-colors" />
          </div>
          <div x-show="setupPassword && setupPasswordConfirm && setupPassword !== setupPasswordConfirm"
            class="text-xs text-red-400 px-1">Passwords do not match.</div>
        </div>
        <button @click="goNext()"
          :disabled="!setupEmail.trim() || !setupUsername.trim() || !setupPassword || setupPassword !== setupPasswordConfirm"
          class="btn-primary w-full py-2.5 rounded-xl text-sm">
          Continue &rarr;
        </button>
      </div>

      <!-- ── Step 7: Review & launch ─────────────────────────────────── -->
      <div x-show="step===7" x-cloak>
        <div class="text-center mb-8">
          <div class="w-16 h-16 rounded-2xl bg-green-950 border border-green-800 flex items-center justify-center mx-auto mb-5">
            <svg class="w-8 h-8 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
            </svg>
          </div>
          <h2 class="text-2xl font-bold mb-2">Logos is ready</h2>
          <p class="text-gray-400 text-sm">Review your configuration and launch.</p>
        </div>

        <div class="p-4 rounded-xl bg-gray-900 border border-gray-800 mb-6 divide-y divide-gray-800">
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Agent</span>
            <span class="text-white font-medium capitalize" x-text="selectedAgentType || '&mdash;'"></span>
          </div>
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Soul</span>
            <span class="text-white font-medium" x-text="soulOptions.find(s=>s.slug===selectedSoul)?.name || selectedSoul || '&mdash;'"></span>
          </div>
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Server</span>
            <span class="text-white font-medium"
              x-text="activeServer ? (activeServer.type === 'ollama' ? 'Ollama' : 'LM Studio') : '&mdash;'"></span>
          </div>
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Model</span>
            <span class="text-white font-medium truncate ml-4 font-mono text-xs" x-text="selectedModel || '&mdash;'"></span>
          </div>
          <div class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Execution</span>
            <span class="text-white font-medium" x-text="execEnv === 'k8s' ? 'Kubernetes (' + (k8sMode === 'incluster' ? 'in-cluster' : 'external') + ')' : 'Local'"></span>
          </div>
          <div x-show="execEnv === 'k8s'" class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Namespace</span>
            <span class="text-white font-medium font-mono text-xs" x-text="selectedAgentType || 'hermes'"></span>
          </div>
          <div x-show="testLatency" class="flex justify-between text-sm py-2.5">
            <span class="text-gray-500">Model latency</span>
            <span class="text-white font-medium" x-text="testLatency + 'ms avg'"></span>
          </div>
        </div>

        <!-- Error / warning from complete() -->
        <div x-show="completeError" class="mb-4 p-3 rounded-xl text-sm"
          :class="completeError?.warning ? 'bg-amber-950/40 border border-amber-800 text-amber-300' : 'bg-red-950/40 border border-red-800 text-red-300'">
          <div class="font-semibold mb-1" x-text="completeError?.warning ? '⚠ Setup saved — but model server unreachable' : '✗ Setup failed'"></div>
          <div class="text-xs opacity-80" x-text="completeError?.message"></div>
          <template x-if="completeError?.warning">
            <div class="mt-2 text-xs">
              Logos is configured. Make sure your model server is running and reachable from this machine, then
              <a href="/" class="underline hover:text-amber-200">go to the dashboard</a>.
            </div>
          </template>
          <template x-if="!completeError?.warning">
            <div class="mt-2 text-xs">Check the steps above or go back and adjust your settings.</div>
          </template>
        </div>

        <button @click="complete()" :disabled="completing"
          class="btn-primary w-full py-3 rounded-xl font-semibold">
          <span x-show="!completing" x-text="completeError?.warning ? 'Go to Dashboard \u2192' : 'Launch Logos \u2192'"></span>
          <span x-show="completing" class="flex items-center justify-center gap-2">
            <div class="w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin"></div>
            Finishing up&hellip;
          </span>
        </button>
      </div>

    </div><!-- /step fade wrapper -->
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
    stepFading: false,

    // Pre-step: mode selection (null = choice screen, 'new' = new install, 'connect' = connect to existing)
    setupMode: null,

    // Connect-to-existing state
    connectScanning: false,
    connectInstances: [],
    connectUrl: '',
    connectError: '',
    connectSaving: false,

    // Step 0 — intro panel
    introConfirmed: false,
    tldr: false,
    setupSteps: [
      { n: 1, name: 'Connect inference servers',  tag: 'detects',    desc: 'Logos scans your local network for Ollama and LM Studio. You can also add remote servers — on a LAN, VPC, or cloud VM — using a custom address. No Logos installation is needed on the inference machine.', tldrDesc: 'finds where ur AI lives (ollama, lm studio etc). no logos needed on the other machine, its just scanning' },
      { n: 2, name: 'Benchmark models',       tag: 'measures',   desc: 'Candidate models run 6 eval tests: instruction following, reasoning, JSON format, tool selection, nested JSON, and multi-step arithmetic. The best fit is pre-selected; you can override freely.', tldrDesc: 'makes each model answer 6 questions to see which one is actually smart. picks the best one for u' },
      { n: 3, name: 'Agent runtime',          tag: 'configures', desc: 'Choose which agent engine handles your sessions. Hermes is available now; additional runtimes plug in as they are released.', tldrDesc: 'picks which brain runs ur AI sessions. hermes for now, more later' },
      { n: 4, name: 'Execution target',       tag: 'configures', desc: 'Decide where agent processes run — in-process alongside Logos, or as isolated Kubernetes Jobs. Affects resource isolation, scaling, and where logs appear.', tldrDesc: 'decides if the AI runs here or in a lil box. affects logs and stuff. local = easiest' },
      { n: 5, name: 'Soul',                   tag: 'configures', desc: "A soul defines the agent's communication style and default behaviour. It is a starting point — editable at any time from the dashboard.", tldrDesc: 'personality for the AI. vibes only. change it later whenever' },
      { n: 6, name: 'Your account',           tag: 'secures',    desc: 'Set the email, username, and password for the admin account that protects the dashboard and API.', tldrDesc: 'make a password so randos cant use ur AI. basic security bestie' },
      { n: 7, name: 'Review & launch',        tag: 'confirms',   desc: 'Review every setting, confirm the model endpoint is reachable, and launch the platform.', tldrDesc: 'check it all looks right then go brr. thats it' },
    ],

    // Step 1
    autoScanning: false,
    autoScanDone: false,
    foundServers: [],
    selectedServers: [],
    activeServer: null,
    showManualEntry: false,
    manualType: 'ollama',
    manualUrl: '',
    manualName: '',
    manualKey: '',
    manualProbing: false,
    manualError: '',
    osPlatform: 'mac',
    copied: '',

    // Step 2 — auto-compare
    compareRunning: false,
    compareDone: false,
    compareTargets: [],
    compareResults: {},
    compareTesting: null,
    compareLoadingFor: null,
    compareRecommended: null,
    compareReason: '',
    compareFastRecommended: null,
    compareFastReason: '',
    compareServerRecs: {},
    serverModelSelections: {},
    compareExpanded: null,

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
    testTokS: null,
    testScoreLabel: '',
    testScoreColour: 'indigo',
    testQualityPass: null,
    testBenchmarking: false,
    testLoadingModel: false,
    testRuns: 0,
    testLog: [],

    // Step 4 — agent runtime
    selectedAgentType: 'hermes',

    // Account setup (step 7)
    setupEmail: '',
    setupUsername: '',
    setupPassword: '',
    setupPasswordConfirm: '',

    // Step 6 — soul
    selectedSoul: 'general',
    soulOptions: [
      {
        slug:  'general',
        icon:  '✦',
        name:  'General',
        desc:  'All-round peer assistant — research, writing, analysis, conversation. Treats you like a capable adult. Best starting point.',
        tools: ['Web search', 'File I/O', 'Code execution', 'Browsing'],
      },
      {
        slug:  'app-development',
        icon:  '⌥',
        name:  'App Development',
        desc:  'Technical collaborator for building software. Architecture first, simple over clever. Asks about constraints before designing.',
        tools: ['Terminal', 'Git', 'File I/O', 'Code execution', 'Web search'],
      },
      {
        slug:  'homelab-investigator',
        icon:  '◎',
        name:  'Homelab Investigator',
        desc:  'Investigates infrastructure — containers, Kubernetes, metrics, logs, network. Reports findings; lets you decide what to do.',
        tools: ['Terminal', 'SSH', 'Kubectl', 'Logs', 'File I/O'],
      },
      {
        slug:  'homelab-code-fix',
        icon:  '⌗',
        name:  'Homelab Code Fix',
        desc:  'Fixes code in homelab repositories. Reads before writing, makes targeted changes, explains what changed and why.',
        tools: ['File I/O', 'Terminal', 'Git', 'Code execution'],
      },
      {
        slug:  'news-anchor',
        icon:  '◉',
        name:  'News Anchor',
        desc:  "Researches and summarises news into structured briefings. Sources claims, doesn't editorialize.",
        tools: ['Web search', 'Firecrawl', 'Summarisation'],
      },
      {
        slug:  'studying',
        icon:  '◑',
        name:  'Studying',
        desc:  "Helps you learn from first principles. Explains concepts, checks understanding, doesn't dump information.",
        tools: ['Web search', 'File I/O'],
      },
      {
        slug:  'planning-life',
        icon:  '◷',
        name:  'Planning & Life',
        desc:  'Thinking partner for time, energy, and attention. Not a productivity coach selling a system.',
        tools: [],
      },
      {
        slug:  'relationship-counseling',
        icon:  '◌',
        name:  'Relationship Counseling',
        desc:  'Reflective space for thinking through interpersonal situations. Listens before advising. Not a therapist.',
        tools: [],
      },
    ],

    // Step 5 — execution environment
    execEnv: 'local',
    k8sMode: 'incluster',
    kubeconfig: '',
    k8sTesting: false,
    k8sTestResult: null,
    k8sTestError: '',

    // Step 6
    completing: false,
    completeError: null,

    // Server-instance timestamp — read once from <meta name="setup-ts"> so all
    // methods can use it without scope issues (set in init, used in _saveProgress)
    _serverTs: 0,

    // Compare event log
    compareLog: [],
    compareLogOpen: false,
    compareLogPinned: false,
    compareCopied: false,
    compareTtftWarnings: [],

    copyDebugLog() {
      const text = this.compareLog.join('\\n');
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => { this.compareCopied = true; setTimeout(() => this.compareCopied = false, 1500); });
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try { document.execCommand('copy'); } catch (_) {}
        document.body.removeChild(ta);
        this.compareCopied = true;
        setTimeout(() => this.compareCopied = false, 1500);
      }
    },

    init() {
      const ua = navigator.userAgent;
      if (ua.includes('Win')) this.osPlatform = 'windows';
      else if (ua.includes('Linux')) this.osPlatform = 'linux';
      else this.osPlatform = 'mac';

      // Restore full wizard progress so a refresh resumes from where the user left off
      // But only if the session belongs to this server instance — a pod restart (e.g.
      // HERMES_WIPE_ON_START) invalidates saved state so the intro always shows fresh.
      this._serverTs = parseInt(document.querySelector('meta[name="setup-ts"]')?.content || '0');
      let restoredFromProgress = false;
      try {
        const saved = localStorage.getItem('logos_setup_progress_v2');
        if (saved) {
          const s = JSON.parse(saved);
          const _svrMatch = this._serverTs > 0 && s.serverTs === this._serverTs;
          if (s?.ts && Date.now() - s.ts < 60 * 60 * 1000 && _svrMatch) {
            this.step           = s.step || 0;
            this.introConfirmed = s.introConfirmed ?? (this.step > 0);
            this.track          = s.track ?? null;
            if (s.foundServers?.length) {
              this.foundServers    = s.foundServers;
              this.selectedServers = s.selectedServers || [];
              this.activeServer    = s.activeServer    || null;
              this.autoScanDone    = true;
            }
            if (s.compareDone) {
              this.compareTargets         = s.compareTargets         || [];
              this.compareResults         = s.compareResults         || {};
              this.compareRecommended     = s.compareRecommended     || null;
              this.compareReason          = s.compareReason          || '';
              this.compareFastRecommended = s.compareFastRecommended || null;
              this.compareFastReason      = s.compareFastReason      || '';
              this.compareServerRecs      = s.compareServerRecs      || {};
              this.serverModelSelections  = s.serverModelSelections  || {};
              this.compareDone            = true;
            }
            if (s.selectedModel)     this.selectedModel     = s.selectedModel;
            if (s.execEnv)           this.execEnv           = s.execEnv;
            if (s.k8sMode)           this.k8sMode           = s.k8sMode;
            if (s.selectedSoul)      this.selectedSoul      = s.selectedSoul;
            if (s.selectedAgentType) this.selectedAgentType = s.selectedAgentType;
            if (s.setupEmail)        this.setupEmail        = s.setupEmail;
            if (s.setupUsername)     this.setupUsername     = s.setupUsername;
            restoredFromProgress = true;
          }
        }
      } catch {}

      // Fallback: restore scan-only cache
      if (!restoredFromProgress) {
        try {
          const cached = localStorage.getItem('logos_setup_scan');
          if (cached) {
            const { servers, ts } = JSON.parse(cached);
            if (servers?.length && Date.now() - ts < 10 * 60 * 1000) {
              this.foundServers    = servers;
              this.selectedServers = servers.filter(s => s.status === 'up');
              this.activeServer    = this.selectedServers[0] || null;
              this.autoScanDone    = true;
            }
          }
        } catch {}
      }

      // Watch fields set directly from templates
      this.$watch('selectedSoul',      () => this._saveProgress());
      this.$watch('execEnv',           () => this._saveProgress());
      this.$watch('k8sMode',           () => this._saveProgress());
      this.$watch('selectedAgentType', () => this._saveProgress());
    },

    _saveProgress() {
      try {
        localStorage.setItem('logos_setup_progress_v2', JSON.stringify({
          ts:                     Date.now(),
          serverTs:               this._serverTs,
          step:                   this.step,
          introConfirmed:         this.introConfirmed,
          track:                  this.track,
          foundServers:           this.foundServers,
          selectedServers:        this.selectedServers,
          activeServer:           this.activeServer,
          compareTargets:         this.compareTargets,
          compareResults:         this.compareResults,
          compareRecommended:     this.compareRecommended,
          compareReason:          this.compareReason,
          compareFastRecommended: this.compareFastRecommended,
          compareFastReason:      this.compareFastReason,
          compareServerRecs:      this.compareServerRecs,
          serverModelSelections:  this.serverModelSelections,
          compareDone:            this.compareDone,
          selectedModel:          this.selectedModel,
          execEnv:                this.execEnv,
          k8sMode:                this.k8sMode,
          selectedSoul:           this.selectedSoul,
          selectedAgentType:      this.selectedAgentType,
          setupEmail:             this.setupEmail,
          setupUsername:          this.setupUsername,
        }));
      } catch {}
    },

    async startConnectScan() {
      this.connectScanning = true;
      this.connectInstances = [];
      try {
        const r = await fetch('/api/setup/discover');
        const d = await r.json();
        this.connectInstances = (d.instances || []).filter(i => i.setup_completed);
        if (this.connectInstances.length === 1) this.connectUrl = this.connectInstances[0].url;
      } catch {}
      this.connectScanning = false;
    },

    async saveRemoteConnect() {
      if (!this.connectUrl) return;
      this.connectSaving = true;
      this.connectError = '';
      try {
        const r = await fetch('/api/setup/set-remote', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: this.connectUrl }),
        });
        const d = await r.json();
        if (!r.ok) {
          const msg = d.error === 'unreachable' ? "Can't reach that server — check the URL and try again."
                    : d.error === 'not_logos' ? "That URL doesn't look like a Logos server."
                    : (d.detail || d.error || "Connection failed.");
          this.connectError = msg;
        } else {
          // Redirect to the remote Logos instance
          window.location.href = d.url;
        }
      } catch (e) {
        this.connectError = "Network error — " + e.message;
      }
      this.connectSaving = false;
    },

    selectTrack(track) {
      this.track = track;
      this.step = 1;
      this._saveProgress();
      // Only scan if we don't already have cached results — avoids redundant scan on resume
      this.$nextTick(() => { if (!this.autoScanDone) this.autoDetect(); });
    },

    async autoDetect() {
      this.autoScanning = true;
      this.autoScanDone = false;
      this.foundServers = [];
      this.selectedServers = [];
      this.activeServer = null;
      this.showManualEntry = false;
      this.manualUrl = '';
      this.manualName = '';
      this.manualKey = '';
      this.manualError = '';
      try {
        const r = await fetch('/api/setup/scan', { credentials: 'include' });
        const d = await r.json();
        // Add _apiKey field for auth_required inline entry
        this.foundServers = (d.servers || [])
          .filter(s => s.status !== 'down')
          .map(s => ({ ...s, _apiKey: '' }));
        // Auto-select all 'up' servers
        this.selectedServers = this.foundServers.filter(s => s.status === 'up');
        this.activeServer = this.selectedServers[0] || null;
        // Cache results so a refresh skips the scan wait
        if (this.foundServers.length) {
          try { localStorage.setItem('logos_setup_scan', JSON.stringify({ servers: this.foundServers, ts: Date.now() })); } catch {}
        }
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
          const enriched = { ...server, _apiKey: this.manualKey, customName: this.manualName.trim() || '' };
          if (!this.foundServers.find(s => s.endpoint === server.endpoint)) this.foundServers.push(enriched);
          if (!this.selectedServers.find(s => s.endpoint === server.endpoint)) this.selectedServers.push(enriched);
          this.activeServer = this.selectedServers[0] || null;
          this.showManualEntry = false;
          this.manualUrl = '';
          this.manualName = '';
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

    async _goStep(n, afterFn) {
      this.stepFading = true;
      await new Promise(r => setTimeout(r, 180));
      this.step = n;
      this._saveProgress();
      if (afterFn) afterFn();
      await this.$nextTick();
      this.stepFading = false;
    },

    async goNext() {
      if (this.step === 1) { await this._goStep(2, () => { this.$nextTick(() => { if (!this.compareDone) this.startCompare(); }); }); return; }
      if (this.step === 2) { await this._goStep(3); return; }
      if (this.step === 3) { await this._goStep(4); return; }
      if (this.step === 4) { await this._goStep(5); return; }
      if (this.step === 5) { await this._goStep(6); return; }
      if (this.step === 6) { await this._goStep(7); return; }
    },
    async goTo(i) {
      if (i < this.step && i >= 1 && this.step <= 7) {
        await this._goStep(i, i === 2 ? () => { this.$nextTick(() => { if (!this.compareDone) this.startCompare(); }); } : null);
      }
    },

    startCompare() {
      const models = this.getModels();
      if (models.length === 0) return;
      // Single model — skip comparison, go straight to test
      if (models.length === 1) {
        this.pickModel(models[0]);
        this.compareTargets     = [models[0].id];
        this.compareResults     = {};
        this.compareRecommended = models[0].id;
        this.compareReason      = 'Only one model available — proceeding to full test.';
        this.compareDone        = true;
        return;
      }
      this.compareRunning     = true;
      this.compareDone        = false;
      this.compareTargets     = [];
      this.compareResults     = {};
      this.compareTesting     = null;
      this.compareLoadingFor  = null;
      this.compareRecommended     = null;
      this.compareReason          = '';
      this.compareFastRecommended = null;
      this.compareFastReason      = '';
      this.compareServerRecs      = {};
      this.compareExpanded        = null;
      this.compareTtftWarnings    = [];
      this.runCompare();
    },

    async runCompare() {
      const models = this.getModels();
      try {
        if (!this.activeServer || !this.activeServer.endpoint) {
          this.compareReason = 'No server selected — go back to step 1 and pick a model server.';
          return;
        }
        const r = await fetch('/api/setup/compare', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          body: JSON.stringify({
            endpoint:    this.activeServer.endpoint,
            api_key:     this.activeServer._apiKey || 'ollama',
            server_type: this.activeServer.type || 'unknown',
            models: models.map(m => {
              const srv = this.selectedServers
                ? this.selectedServers.find(s => s.endpoint === m._serverEndpoint)
                : null;
              return {
                id:          m.id,
                endpoint:    m._serverEndpoint || this.activeServer.endpoint,
                api_key:     (srv && srv._apiKey) || m._apiKey || this.activeServer._apiKey || 'ollama',
                server_type: m._serverType     || this.activeServer.type || 'unknown',
              };
            }),
          }),
        });
        if (!r.ok) {
          const errBody = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
          this.compareReason = errBody.error || `Server error: ${r.status}`;
          return;
        }
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
              if (ev.targets)       { this.compareTargets = ev.targets; }
              if (ev.testing)       { this.compareTesting = ev.testing; }
              if (ev.loading_model) { this.compareLoadingFor = ev.loading_model; }
              if (ev.log) {
                this.compareLog = [...this.compareLog, ev.log];
                if (!this.compareLogPinned) {
                  this.$nextTick(() => {
                    const el = this.$refs.compareLogEl;
                    if (el) el.scrollTop = el.scrollHeight;
                  });
                }
              }
              if (ev.result) {
                this.compareLoadingFor = null;
                this.compareResults = { ...this.compareResults, [ev.result.model]: ev.result };
              }
              if (ev.done) {
                this.compareRunning          = false;
                this.compareDone             = true;
                this.compareTesting          = null;
                this.compareLoadingFor       = null;
                this.compareRecommended      = ev.recommendation;
                this.compareReason           = ev.reason || '';
                this.compareFastRecommended  = ev.fast_recommendation || null;
                this.compareFastReason       = ev.fast_reason || '';
                this.compareServerRecs       = ev.per_server_recommendations || {};
                this.compareTtftWarnings     = ev.ttft_warnings || [];
                // Seed per-server selections from benchmark recommendations
                for (const [ep, rec] of Object.entries(this.compareServerRecs)) {
                  if (!this.serverModelSelections[ep] && rec && rec.model) {
                    this.serverModelSelections = { ...this.serverModelSelections, [ep]: rec.model };
                  }
                }
                if (ev.recommendation) {
                  const m = models.find(x => x.id === ev.recommendation);
                  if (m) this.pickModel(m);
                }
                this._saveProgress();
              }
            } catch {}
          }
        }
      } catch (e) {
        this.compareReason = 'Connection error: ' + e.message;
      } finally {
        // Always ensure we exit the "scanning" state — never leave the UI stuck
        if (this.compareRunning) {
          this.compareRunning    = false;
          this.compareDone       = true;
          this.compareTesting    = null;
          this.compareLoadingFor = null;
          if (!this.compareReason) this.compareReason = 'Stream closed before comparison completed.';
        }
        // Ensure every selected server has a model assigned so the Continue button
        // can appear even when the benchmark fails or the stream closes early.
        // Priority: benchmark result for that server → any result → first available model.
        for (const s of (this.selectedServers || [])) {
          if (this.serverModelSelections[s.endpoint]) continue;
          const resultForServer = Object.values(this.compareResults || {})
            .find(r => r.endpoint === s.endpoint && r.model && !r.error);
          const anyResult = Object.values(this.compareResults || {})
            .find(r => r.model && !r.error);
          const fallback = resultForServer || anyResult;
          if (fallback) {
            this.serverModelSelections = { ...this.serverModelSelections, [s.endpoint]: fallback.model };
          } else {
            const fm = (s.models || [])[0];
            if (fm) this.serverModelSelections = { ...this.serverModelSelections, [s.endpoint]: fm.id || fm.name };
          }
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
      this._saveProgress();
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

    serverDefaultName(s) {
      return s.type === 'lmstudio' ? 'LM Studio' : s.type === 'ollama' ? 'Ollama' : 'Server';
    },

    serverName(s) {
      return s.customName || this.serverDefaultName(s);
    },

    get localServers() {
      return this.foundServers.filter(s => {
        try { const h = new URL(s.endpoint).hostname; return h === 'localhost' || h === '127.0.0.1'; } catch { return false; }
      });
    },

    get remoteServers() {
      return this.foundServers.filter(s => {
        try { const h = new URL(s.endpoint).hostname; return h !== 'localhost' && h !== '127.0.0.1'; } catch { return false; }
      });
    },

    // Find the server a model belongs to
    modelServer(modelId) {
      const m = this.getModels().find(m => m.id === modelId);
      if (!m) return null;
      return this.selectedServers.find(s => s.endpoint === m._serverEndpoint) || null;
    },

    // Return the server name for which this model is the per-server best (or null)
    serverBestFor(modelId) {
      for (const [ep, rec] of Object.entries(this.compareServerRecs || {})) {
        if (rec.model === modelId) {
          const s = this.selectedServers.find(s => s.endpoint === ep);
          return s ? this.serverName(s) : ep;
        }
      }
      return null;
    },

    // Models NOT in compareTargets — available for delegation
    secondaryModels() {
      const targets = new Set(this.compareTargets);
      return this.getModels().filter(m => !targets.has(m.id));
    },

    // Models in compareTargets that belong to a given server endpoint
    compareTargetsForServer(ep) {
      const models = this.getModels();
      const byEp = new Set(models.filter(m => m._serverEndpoint === ep).map(m => m.id));
      return this.compareTargets.filter(mid => byEp.has(mid));
    },

    // User-selected (or benchmark-recommended) model for a given server endpoint
    serverModelForServer(ep) {
      return this.serverModelSelections[ep] || (this.compareServerRecs[ep] || {}).model || null;
    },

    // Override the default model for a specific server; also update selectedModel if it's the primary
    pickServerModel(ep, modelId) {
      this.serverModelSelections = { ...this.serverModelSelections, [ep]: modelId };
      const primaryEp = this.activeServer && this.activeServer.endpoint;
      if (ep === primaryEp || this.selectedServers.length === 1) {
        const m = this.getModels().find(x => x.id === modelId);
        if (m) this.pickModel(m);
      }
    },

    // True once every selected server has a model assigned
    allServersHaveModel() {
      if (!this.compareDone) return false;
      return this.selectedServers.length > 0 && this.selectedServers.every(s => !!this.serverModelForServer(s.endpoint));
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
                if (this.activeServer) this.activeServer.models = [{ id: modelName, name: modelName, size: 0 }];
                this.$nextTick(() => this.startCompare());
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
        if (found) {
          this.activeServer = { ...found, _apiKey: this.activeServer._apiKey || '' };
          if ((found.models || []).length > 0) this.$nextTick(() => this.startCompare());
        }
      } catch {}
    },

    async runTest() {
      this.testResponse = ''; this.testDone = false; this.testBenchmarking = false;
      this.testLoadingModel = false; this.testLog = [];
      this.testError = null; this.testLatency = null; this.testTtft = null;
      this.testTokS = null; this.testScoreLabel = ''; this.testQualityPass = null; this.testRuns = 0;
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
              if (ev.error)      { this.testError = ev.error; return; }
              if (ev.status === 'loading_model') { this.testLoadingModel = true; }
              if (ev.status === 'benchmarking') { this.testBenchmarking = true; this.testLoadingModel = false; }
              if (ev.log) {
                this.testLog = [...this.testLog, ev.log];
                this.$nextTick(() => { const el = this.$refs.testLogEl; if (el) el.scrollTop = el.scrollHeight; });
              }
              if (ev.token)      { this.testResponse += ev.token; this.testLoadingModel = false; }
              if (ev.done) {
                this.testDone        = true;
                this.testLatency     = ev.latency;
                this.testTtft        = ev.ttft;
                this.testTokS        = ev.tok_s;
                this.testScoreLabel  = ev.score_label;
                this.testScoreColour = ev.score_colour;
                this.testQualityPass = ev.quality_pass;
                this.testRuns        = ev.runs;
                this.testBenchmarking = false;
              }
            } catch {}
          }
        }
      } catch (e) {
        this.testError = 'Connection error \u2014 ' + e.message;
      }
    },

    benchRecommendation() {
      const s = this.testTokS;
      const q = this.testQualityPass;
      if (s === null) return { text: '', tone: 'good', icon: '' };
      if (s >= 30 && q)  return { tone: 'upgrade', icon: '⚡', text: 'Great speed — you have headroom. Consider a 14B model (e.g. Qwen2.5-14B Q4) for stronger reasoning if VRAM allows.' };
      if (s >= 30 && !q) return { tone: 'warn',    icon: '⚠', text: 'Fast, but reasoning looks weak — this model may not suit agent tasks. Try a general-purpose chat model like Qwen3-8B or Llama-3.1-8B.' };
      if (s >= 15 && q)  return { tone: 'good',    icon: '✓', text: 'Sweet spot — fast enough and reasoning checks out. This model is well-matched to your hardware.' };
      if (s >= 15 && !q) return { tone: 'warn',    icon: '⚠', text: 'Speed is fine but reasoning looks off. Try a different model in the same size class (7–9B), or a more instruction-tuned variant.' };
      if (s >= 6  && q)  return { tone: 'warn',    icon: '↓', text: 'Responses will feel sluggish at this speed. Consider a smaller quantised model — e.g. Qwen3-8B Q4_K_M (~5 GB VRAM) for snappier results.' };
      if (s >= 6  && !q) return { tone: 'bad',     icon: '↓', text: 'Slow and reasoning issues — try a 7B model like Qwen3-8B or Llama-3.1-8B at Q4 quantisation.' };
      if (q)             return { tone: 'bad',      icon: '↓', text: 'Too slow for comfortable use. Drop to a 3–4B model (e.g. Qwen3-4B) or connect a faster inference machine.' };
                         return { tone: 'bad',      icon: '✕', text: 'Too slow and reasoning issues. Drop to a 3–4B model like Qwen3-4B, or use a cloud endpoint.' };
    },

    async testK8s() {
      this.k8sTesting = true;
      this.k8sTestResult = null;
      this.k8sTestError = '';
      try {
        const r = await fetch('/api/setup/test-k8s', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          body: JSON.stringify({
            mode:       this.k8sMode,
            namespace:  this.selectedAgentType || 'hermes',
            kubeconfig: this.kubeconfig,
          }),
        });
        const d = await r.json();
        this.k8sTestResult = d.ok ? 'ok' : 'error';
        if (!d.ok) this.k8sTestError = d.error || 'Connection failed';
      } catch (e) {
        this.k8sTestResult = 'error';
        this.k8sTestError = e.message;
      }
      this.k8sTesting = false;
    },

    async _flyLogoToNav() {
      // Animate the setup logo from its current top-centre position to the
      // nav logo position on the main page (top-left, 32×32).
      // Nav logo centre: containerLeft + px-4(16) + half-logo(16), y=32.
      const logoEl = document.querySelector('.setup-logo');
      if (!logoEl) return;
      const r = logoEl.getBoundingClientRect();
      const fromCX = r.left + r.width / 2;
      const fromCY = r.top  + r.height / 2;
      const toCX   = Math.max(0, (window.innerWidth - 1280) / 2) + 32;
      const toCY   = 32;
      logoEl.style.transition = 'transform 0.9s cubic-bezier(0.4,0,0.2,1)';
      logoEl.style.transformOrigin = 'center center';
      logoEl.style.transform = `translate(${toCX - fromCX}px, ${toCY - fromCY}px) scale(${(32/56).toFixed(4)})`;
      await new Promise(r => setTimeout(r, 900));
    },

    async _fadeOutPage() {
      // Fade the entire setup page out before navigating — mirrors the page-fadein on load.
      const content = document.querySelector('.setup-content');
      if (content) {
        content.classList.add('setup-fadeout');
        await new Promise(r => setTimeout(r, 460));
      }
    },

    async complete() {
      if (this.completeError?.warning) { await this._flyLogoToNav(); await this._fadeOutPage(); window.location.href = '/login'; return; }
      this.completing = true;
      this.completeError = null;
      try {
        const r = await fetch('/api/setup/complete', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
          body: JSON.stringify({
            endpoint:      this.activeServer ? this.activeServer.endpoint : '',
            model:         this.selectedModel,
            server_type:   this.activeServer ? this.activeServer.type : '',
            agent_type:    this.selectedSoul || 'general',
            exec_env:      this.execEnv || 'local',
            k8s_namespace: this.selectedAgentType || 'hermes',
            kubeconfig:    this.execEnv === 'k8s' && this.k8sMode === 'kubeconfig' ? this.kubeconfig : '',
            setup_email:    this.setupEmail    || '',
            setup_username: this.setupUsername || '',
            setup_password: this.setupPassword || '',
            // All selected servers so the backend can register each as a machine
            servers: (this.selectedServers || []).map(s => ({
              endpoint:          s.endpoint,
              type:              s.type || 'unknown',
              api_key:           s._apiKey || '',
              name:              s.customName || s.name || '',
              recommended_model: this.serverModelSelections[s.endpoint] || (this.compareServerRecs[s.endpoint] || {}).model || null,
            })),
          }),
        });
        if (r.ok) {
          const d = await r.json().catch(() => ({}));
          if (d.warning) {
            // Endpoint unreachable but setup saved — show warning then proceed
            this.completeError = { warning: true, message: d.warning };
            this.completing = false;
            return;
          }
          try { localStorage.removeItem('logos_setup_scan'); localStorage.removeItem('logos_setup_progress'); localStorage.removeItem('logos_setup_progress_v2'); } catch {}
          try { sessionStorage.setItem('logos_fl', '1'); } catch {}
          await this._flyLogoToNav();
          await this._fadeOutPage();
          window.location.href = '/login';
          return;
        }
        const d = await r.json().catch(() => ({}));
        this.completeError = { warning: false, message: d.detail || d.error || `Setup failed (HTTP ${r.status}) — check server logs.` };
        this.completing = false;
      } catch (e) {
        this.completeError = { warning: false, message: 'Network error: ' + e.message };
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
  <!-- Version badge -->
  <div style="position:fixed;bottom:16px;right:18px;z-index:50;
              font-size:0.65rem;color:rgba(71,85,105,0.45);
              letter-spacing:0.04em;font-family:ui-monospace,monospace;pointer-events:none;">
    __VERSION_LABEL__
  </div>
</body>
</html>"""


async def _handle_setup_page(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if is_setup_completed():
        raise web.HTTPFound("/")
    html = _SETUP_HTML.replace("__VERSION_LABEL__", _VERSION_LABEL).replace("__SETUP_TS__", _SERVER_START_TS)
    return web.Response(text=html, content_type="text/html")


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
    inject = f'<script>window.__LOGOS__={{isCanary:{str(_IS_CANARY).lower()},runtimeMode:"{_RUNTIME_MODE}"}};window._hueEpochMs={_HUE_EPOCH_MS};</script>'
    html = _ADMIN_HTML.replace("</head>", inject + "</head>", 1)
    return web.Response(text=html, content_type="text/html")


async def _handle_login_page(request: web.Request) -> web.Response:
    html = _LOGIN_HTML.replace("__VERSION_LABEL__", _VERSION_LABEL)
    return web.Response(text=html, content_type="text/html")


async def _handle_log_tail(request: web.Request) -> web.Response:
    """Return the last N lines of the gateway log file.

    GET /api/logs?n=200&file=gateway   (file: gateway|errors)
    Requires view_audit_logs permission (admin/operator).
    """
    n = min(int(request.query.get("n", 200)), 2000)
    fname = request.query.get("file", "gateway")
    if fname not in ("gateway", "errors"):
        return web.json_response({"error": "invalid file"}, status=400)
    log_path = _hermes_home / "logs" / f"{fname}.log"
    try:
        if not log_path.exists():
            return web.json_response({"lines": [], "path": str(log_path), "exists": False})
        # Read last N lines efficiently without loading the whole file
        lines: list[str] = []
        with open(log_path, "rb") as fh:
            # Seek backwards in chunks to find the last N newlines
            chunk = 1024 * 32
            fh.seek(0, 2)  # end
            size = fh.tell()
            buf = b""
            pos = size
            while len(lines) < n + 1 and pos > 0:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
                lines = buf.split(b"\n")
        lines = [l.decode("utf-8", errors="replace") for l in lines[-n:] if l]
        return web.json_response({"lines": lines, "path": str(log_path), "exists": True, "total_bytes": log_path.stat().st_size})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


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
    executor = request.app["executor"]
    loop = asyncio.get_event_loop()
    try:
        res = await loop.run_in_executor(None, executor.get_resources)
    except Exception as e:
        res = {"_error": str(e)}
    try:
        inst = await loop.run_in_executor(None, executor.list_instances)
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
    # Normalise local-executor instances to the same shape the k8s executor returns
    # so the frontend can use a single template for both modes.
    if _RUNTIME_MODE == "local":
        registry = _get_soul_registry()
        normalized = []
        for i in inst:
            slug = i.get("soul_name", "")
            soul_obj = registry.get(slug)
            normalized.append({
                "name":          i.get("name", ""),
                "instance_name": f"Hermes for {i.get('requester') or i.get('name', '')}",
                "soul":          {"name": soul_obj.name, "slug": slug, "status": soul_obj.status}
                                 if soul_obj else {"name": slug or "default", "slug": slug, "status": "stable"},
                "model_alias":   i.get("model", ""),
                "machine_name":  None,
                "k8s_status":    "running" if i.get("healthy") else "starting",
                "status":        "running" if i.get("healthy") else "starting",
                "ready":         1 if i.get("healthy") else 0,
                "desired":       1,
                "node_port":     i.get("port"),
                "url":           i.get("url"),
                "pid":           i.get("pid"),
                "source":        "local",
                "cpu_percent":   i.get("cpu_percent"),
                "mem_mb":        i.get("mem_mb"),
            })
        inst = normalized
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
    executor = request.app["executor"]

    # Check resources via executor
    try:
        headroom = await loop.run_in_executor(None, executor.get_headroom)
        can_spawn_now = headroom.can_spawn
        headroom_reason = headroom.reason
    except Exception as e:
        can_spawn_now = False
        headroom_reason = f"executor unavailable: {e}"

    if not can_spawn_now:
        _instance_queue.append({"requester": requester, "soul_slug": soul_slug, "reason": headroom_reason, "requested_at": time.time()})
        logger.info("Instance request queued for %s: %s", requester, headroom_reason)
        return web.json_response({"status": "queued", "requester": requester, "reason": headroom_reason})

    # Spawn — k8s uses _spawn_instance directly (soul/toolset logic lives here);
    # local mode uses executor.spawn() with a lightweight InstanceConfig.
    try:
        if _RUNTIME_MODE == "local":
            from gateway.executors.base import InstanceConfig as _IC
            spawned = await loop.run_in_executor(
                None, executor.spawn,
                _IC(
                    name=_safe_k8s_name(requester),
                    soul_name=soul_slug,
                    model=model_alias,
                    requester=requester,
                ),
            )
            result = {
                "status": "created" if spawned.healthy else "starting",
                "name": spawned.name,
                "url": spawned.url,
                "instance_name": f"Hermes for {requester}",
            }
        else:
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

    # Try to resolve NodePort / URL (may take a moment to assign)
    await asyncio.sleep(1)
    try:
        instances = await loop.run_in_executor(None, executor.list_instances)
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
    executor = request.app["executor"]
    try:
        await loop.run_in_executor(None, executor.delete_instance, name)
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


async def _handle_hue(request: web.Request) -> web.Response:
    """Return the server hue epoch so the tray icon can phase-lock its cycle."""
    return web.json_response({"epoch_ms": _HUE_EPOCH_MS, "rate": 6})


async def _handle_favicon(request: web.Request) -> web.Response:
    """Serve logos.ico as /favicon.ico — public route so Edge --app shows the
    correct icon in the title bar and Windows taskbar without requiring auth."""
    import sys as _sys2
    import pathlib as _pl2
    candidates = []
    if getattr(_sys2, "frozen", False):
        candidates.append(_pl2.Path(_sys2._MEIPASS) / "launcher" / "logos.ico")
    candidates.append(_pl2.Path(__file__).parent.parent / "launcher" / "logos.ico")
    for p in candidates:
        if p.exists():
            data = p.read_bytes()
            return web.Response(
                body=data,
                content_type="image/x-icon",
                headers={"Cache-Control": "public, max-age=86400"},
            )
    raise web.HTTPNotFound()


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
    from gateway.auth.db import is_setup_completed as _isc
    return web.json_response({
        "status": "ok",
        "product": "logos",
        "setup_completed": _isc(),
        "sessions": len(sessions),
        "uptime_s": uptime,
        "platform_stats": getattr(runner, "_platform_stats", {}),
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

    # Initialise auth DB alongside existing Logos state
    global _hermes_home
    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".logos"))
    _hermes_home = hermes_home
    auth_db.init_db(hermes_home)

    # Ensure a stable JWT secret exists for local installs.
    # K8s sets HERMES_JWT_SECRET via a k8s Secret; local desktop/CLI installs
    # never set it.  Generate once, persist to ~/.logos/.jwt_secret so tokens
    # survive gateway restarts without forcing re-login every time.
    if not os.environ.get("HERMES_JWT_SECRET"):
        import secrets as _secrets
        _jwt_secret_path = hermes_home / ".jwt_secret"
        if _jwt_secret_path.exists():
            os.environ["HERMES_JWT_SECRET"] = _jwt_secret_path.read_text().strip()
        else:
            _jwt_secret_path.parent.mkdir(parents=True, exist_ok=True)
            _new_secret = _secrets.token_hex(32)
            _jwt_secret_path.write_text(_new_secret)
            _jwt_secret_path.chmod(0o600)
            os.environ["HERMES_JWT_SECRET"] = _new_secret
            logger.info("Generated new JWT secret at %s", _jwt_secret_path)
    # HERMES_WIPE_ON_START: wipe setup state so /setup always runs fresh (setup-test deployments)
    if os.environ.get("HERMES_WIPE_ON_START", "").lower() in ("1", "true", "yes"):
        try:
            auth_db.reset_setup_completed()
            for _m in auth_db.list_machines():
                auth_db.delete_machine(_m["id"])
            for _p in auth_db.list_policies():
                auth_db.delete_policy(_p["id"])
            logger.info("HERMES_WIPE_ON_START: wiped setup state, machines, and policies")
        except Exception as _wipe_err:
            logger.warning("HERMES_WIPE_ON_START: partial failure: %s", _wipe_err)
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

    # Executor — selects kubernetes or local-process backend based on runtime mode
    from gateway.executors import build_executor
    app["executor"] = build_executor(_RUNTIME_MODE)
    logger.info("Instance executor: %s (runtime_mode=%s)", type(app["executor"]).__name__, _RUNTIME_MODE)

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
    app.router.add_get("/favicon.ico",   _handle_favicon)      # public — Edge --app needs this before auth
    app.router.add_get("/api/hue",       _handle_hue)          # public — tray icon phase-lock
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
    app.router.add_post("/api/setup/pull",    _sh.handle_setup_pull)
    app.router.add_post("/api/setup/compare", _sh.handle_setup_compare)
    app.router.add_post("/api/setup/test-k8s", _sh.handle_setup_test_k8s)
    app.router.add_post("/api/setup/test",    _sh.handle_setup_test)
    app.router.add_post("/api/setup/complete",    _sh.handle_setup_complete)
    app.router.add_get("/api/setup/discover",     _sh.handle_setup_discover)
    app.router.add_post("/api/setup/set-remote",  _sh.handle_setup_set_remote)
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
    app.router.add_get(
        "/api/logs",
        require_permission("view_audit_logs")(_handle_log_tail),
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

    # ── Evolution ───────────────────────────────────────────────────────────
    from gateway import evolution_handlers as _eh
    _vev  = require_permission("view_evolution")
    _mev  = require_permission("manage_evolution")
    _dev  = require_permission("decide_evolution")
    app.router.add_get("/evolution/proposals",           _vev(_eh.handle_list_proposals))
    app.router.add_get("/evolution/proposals/{id}",      _vev(_eh.handle_get_proposal))
    app.router.add_post("/evolution/proposals",          _mev(require_csrf(_eh.handle_create_proposal)))
    app.router.add_post("/evolution/proposals/{id}/decide", _dev(require_csrf(_eh.handle_decide_proposal)))
    app.router.add_post("/evolution/proposals/{id}/answer", _mev(require_csrf(_eh.handle_answer_question)))
    app.router.add_post("/evolution/proposals/{id}/consult", _dev(require_csrf(_eh.handle_consult_frontier)))
    app.router.add_get("/evolution/settings",            _vev(_eh.handle_get_settings))
    app.router.add_patch("/evolution/settings",          _mev(require_csrf(_eh.handle_update_settings)))

    app.router.add_get("/admin/routing/resolve",  _vrd(admin_handlers.handle_routing_resolve))
    app.router.add_get("/admin/routing/log",      require_permission("view_audit_logs")(admin_handlers.handle_routing_log))
    app.router.add_post("/admin/setup",           _mm(require_csrf(admin_handlers.handle_setup_wizard)))
    app.router.add_get("/routing/preview",        admin_handlers.handle_routing_preview)

    # Serve static assets (logo, etc.)
    import pathlib as _pathlib
    import sys as _sys
    if getattr(_sys, "frozen", False):
        # PyInstaller bundle: __file__ doesn't resolve relative to source tree
        _static_dir = _pathlib.Path(_sys._MEIPASS) / "assets"
    else:
        _static_dir = _pathlib.Path(__file__).parent.parent / "assets"
    if _static_dir.exists():
        app.router.add_static("/static", str(_static_dir), show_index=False)

    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP API listening on port %d", port)

    async def _queue_retry_loop():
        """Retry queued instance requests when resources free up."""
        _executor = app["executor"]
        while True:
            await asyncio.sleep(60)
            if not _instance_queue:
                continue
            try:
                loop = asyncio.get_event_loop()
                headroom = await loop.run_in_executor(None, _executor.get_headroom)
                if headroom.can_spawn:
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
