"""
Kubernetes utilities extracted from gateway/http_api.py.

Imported by both KubernetesExecutor (executors/kubernetes.py) and
the spawn function that still lives in http_api.py (_spawn_instance).

Nothing in this module imports from gateway.http_api — that would be circular.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# ── Namespace + resource constants ──────────────────────────────────────────

HERMES_NAMESPACE = "hermes"
INSTANCE_CPU_REQUEST = "500m"
INSTANCE_MEM_REQUEST = "2Gi"
INSTANCE_CPU_LIMIT = "4000m"
INSTANCE_MEM_LIMIT = "6Gi"
SPAWN_CPU_THRESHOLD = 4.0           # cores — minimum free before auto-spawn
SPAWN_MEM_THRESHOLD = 6 * 1024**3  # bytes (6 GiB)


# ── Low-level helpers ────────────────────────────────────────────────────────

def k8s_clients():
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


def parse_cpu(s: str) -> float:
    """Parse k8s CPU string to float cores ('500m' → 0.5)."""
    if not s:
        return 0.0
    if s.endswith("m"):
        return int(s[:-1]) / 1000
    return float(s)


def parse_mem(s: str) -> int:
    """Parse k8s memory string to bytes ('2Gi' → 2147483648)."""
    if not s:
        return 0
    for suffix, mult in [
        ("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3), ("Ti", 1024**4),
        ("K", 1000),  ("M", 1000**2),  ("G", 1000**3),
    ]:
        if s.endswith(suffix):
            return int(s[:-len(suffix)]) * mult
    return int(s)


def safe_k8s_name(requester: str) -> str:
    """Convert a requester string to a valid k8s Deployment name."""
    name = re.sub(r"[^a-z0-9]+", "-", requester.lower()).strip("-")
    return f"hermes-{name}"[:52]  # k8s name limit is 63 chars


# ── Cluster resource query ────────────────────────────────────────────────────

def cluster_resources() -> dict:
    """Return total allocatable and currently requested CPU/RAM across the cluster."""
    core, _ = k8s_clients()

    total_cpu = total_mem = 0.0
    for node in core.list_node().items:
        alloc = node.status.allocatable or {}
        total_cpu += parse_cpu(alloc.get("cpu", "0"))
        total_mem += parse_mem(alloc.get("memory", "0"))

    used_cpu = used_mem = 0.0
    for pod in core.list_pod_for_all_namespaces().items:
        if (pod.status.phase or "") not in ("Running", "Pending"):
            continue
        for c in (pod.spec.containers or []):
            req = (c.resources and c.resources.requests) or {}
            used_cpu += parse_cpu(req.get("cpu", "0"))
            used_mem += parse_mem(req.get("memory", "0"))

    return {
        "total_cpu": round(total_cpu, 2),
        "total_mem": int(total_mem),
        "used_cpu": round(used_cpu, 2),
        "used_mem": int(used_mem),
        "free_cpu": round(total_cpu - used_cpu, 2),
        "free_mem": int(total_mem - used_mem),
    }


# ── Instance list ─────────────────────────────────────────────────────────────

def list_hermes_instances() -> list[dict]:
    """List Deployments in the hermes namespace that are Hermes instances."""
    _, apps = k8s_clients()
    core, _ = k8s_clients()
    result = []
    for dep in apps.list_namespaced_deployment(HERMES_NAMESPACE).items:
        name = dep.metadata.name
        if not name.startswith("hermes"):
            continue
        port = None
        try:
            svcs = core.list_namespaced_service(
                HERMES_NAMESPACE, label_selector=f"app={name}",
            ).items
            for svc in svcs:
                for p in (svc.spec.ports or []):
                    if p.node_port:
                        port = p.node_port
        except Exception:
            pass

        env_map = {}
        containers = dep.spec.template.spec.containers or []
        if containers:
            for e in (containers[0].env or []):
                if e.value:
                    env_map[e.name] = e.value

        ready = dep.status.ready_replicas or 0
        desired = dep.spec.replicas or 1
        annotations = dep.metadata.annotations or {}
        soul_meta = None
        if annotations.get("hermes.io/soul-slug"):
            try:
                ets = json.loads(annotations.get("hermes.io/effective-toolsets", "[]"))
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
            "source": "k8s",
        })
    return result


# ── Instance delete ───────────────────────────────────────────────────────────

def delete_hermes_instance(name: str) -> None:
    """Delete a named Hermes k8s instance (Deployment + Service + PVC + ConfigMap)."""
    core, apps = k8s_clients()
    from kubernetes.client.rest import ApiException
    for fn in [
        lambda: apps.delete_namespaced_deployment(name, HERMES_NAMESPACE),
        lambda: core.delete_namespaced_service(name, HERMES_NAMESPACE),
        lambda: core.delete_namespaced_persistent_volume_claim(f"{name}-pvc", HERMES_NAMESPACE),
        lambda: core.delete_namespaced_config_map(f"{name}-soul-snap", HERMES_NAMESPACE),
    ]:
        try:
            fn()
        except ApiException as e:
            if e.status != 404:
                raise
