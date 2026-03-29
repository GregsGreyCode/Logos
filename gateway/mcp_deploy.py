"""K8s lifecycle for MCP tool servers.

Creates Deployment + ClusterIP Service + Secret in the hermes namespace.
Uses the same k8s_clients() auth chain as the agent instance deployer.
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MCP_NAMESPACE = "hermes"
MCP_LABEL_KEY = "logos.io/mcp-server"
MCP_NAME_LABEL = "logos.io/mcp-name"


def deploy_mcp_server(
    name: str,
    image: str,
    port: int,
    env_vars: Dict[str, str],
    secret_vars: Dict[str, str],
    resources: Dict[str, str] | None = None,
    mcp_path: str = "/mcp",
    namespace: str = MCP_NAMESPACE,
) -> Dict[str, Any]:
    """Deploy an MCP server to k8s.

    Args:
        name: Server name (used for k8s resource names: mcp-{name})
        image: Container image (e.g. ghcr.io/gregsgreycode/inspector-mcp:latest)
        port: Container port the MCP server listens on
        env_vars: Non-secret environment variables
        secret_vars: Sensitive env vars (stored in a k8s Secret)
        resources: CPU/memory requests and limits
        mcp_path: URL path suffix for the MCP endpoint
        namespace: K8s namespace to deploy into

    Returns:
        Dict with url, deployment_name, service_name, secret_name, status
    """
    from kubernetes import client as k8s
    from gateway.executors.k8s_helpers import k8s_clients

    core, apps = k8s_clients()

    dep_name = f"mcp-{name}"
    svc_name = dep_name
    secret_name = f"{dep_name}-env"
    labels = {
        "app": dep_name,
        MCP_LABEL_KEY: "true",
        MCP_NAME_LABEL: name,
    }

    res = resources or {}
    cpu_req = res.get("cpu_request", "50m")
    mem_req = res.get("mem_request", "128Mi")
    cpu_lim = res.get("cpu_limit", "500m")
    mem_lim = res.get("mem_limit", "256Mi")

    # ── 1. Secret ──────────────────────────────────────────────────────
    if secret_vars:
        secret_body = k8s.V1Secret(
            metadata=k8s.V1ObjectMeta(name=secret_name, namespace=namespace, labels=labels),
            string_data=secret_vars,
        )
        try:
            core.create_namespaced_secret(namespace, secret_body)
        except k8s.exceptions.ApiException as exc:
            if exc.status == 409:
                core.replace_namespaced_secret(secret_name, namespace, secret_body)
            else:
                raise

    # ── 2. Deployment ──────────────────────────────────────────────────
    env_list = [k8s.V1EnvVar(name=k, value=v) for k, v in env_vars.items()]
    for sk in secret_vars:
        env_list.append(k8s.V1EnvVar(
            name=sk,
            value_from=k8s.V1EnvVarSource(
                secret_key_ref=k8s.V1SecretKeySelector(name=secret_name, key=sk),
            ),
        ))

    container = k8s.V1Container(
        name="mcp-server",
        image=image,
        ports=[k8s.V1ContainerPort(container_port=port)],
        env=env_list,
        resources=k8s.V1ResourceRequirements(
            requests={"cpu": cpu_req, "memory": mem_req},
            limits={"cpu": cpu_lim, "memory": mem_lim},
        ),
        readiness_probe=k8s.V1Probe(
            http_get=k8s.V1HTTPGetAction(path="/health", port=port),
            initial_delay_seconds=5,
            period_seconds=10,
        ),
        security_context=k8s.V1SecurityContext(
            allow_privilege_escalation=False,
            run_as_non_root=True,
            run_as_user=1001,
            capabilities=k8s.V1Capabilities(drop=["ALL"]),
        ),
    )

    deployment = k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(name=dep_name, namespace=namespace, labels=labels),
        spec=k8s.V1DeploymentSpec(
            replicas=1,
            selector=k8s.V1LabelSelector(match_labels={"app": dep_name}),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(labels=labels),
                spec=k8s.V1PodSpec(
                    containers=[container],
                    security_context=k8s.V1PodSecurityContext(
                        run_as_non_root=True,
                        seccomp_profile=k8s.V1SeccompProfile(type="RuntimeDefault"),
                    ),
                ),
            ),
        ),
    )

    try:
        apps.create_namespaced_deployment(namespace, deployment)
    except k8s.exceptions.ApiException as exc:
        if exc.status == 409:
            apps.replace_namespaced_deployment(dep_name, namespace, deployment)
        else:
            raise

    # ── 3. ClusterIP Service ───────────────────────────────────────────
    service = k8s.V1Service(
        metadata=k8s.V1ObjectMeta(name=svc_name, namespace=namespace, labels=labels),
        spec=k8s.V1ServiceSpec(
            selector={"app": dep_name},
            ports=[k8s.V1ServicePort(port=port, target_port=port)],
            type="ClusterIP",
        ),
    )

    try:
        core.create_namespaced_service(namespace, service)
    except k8s.exceptions.ApiException as exc:
        if exc.status == 409:
            core.replace_namespaced_service(svc_name, namespace, service)
        else:
            raise

    url = f"http://{svc_name}.{namespace}.svc.cluster.local:{port}{mcp_path}"
    logger.info("Deployed MCP server %s → %s", name, url)

    return {
        "url": url,
        "deployment_name": dep_name,
        "service_name": svc_name,
        "secret_name": secret_name if secret_vars else None,
        "namespace": namespace,
        "status": "deploying",
    }


def undeploy_mcp_server(name: str, namespace: str = MCP_NAMESPACE) -> bool:
    """Remove all k8s resources for an MCP server. Returns True on success."""
    from kubernetes import client as k8s
    from gateway.executors.k8s_helpers import k8s_clients

    core, apps = k8s_clients()
    dep_name = f"mcp-{name}"
    secret_name = f"{dep_name}-env"
    ok = True

    for resource, fn in [
        ("Deployment", lambda: apps.delete_namespaced_deployment(dep_name, namespace)),
        ("Service", lambda: core.delete_namespaced_service(dep_name, namespace)),
        ("Secret", lambda: core.delete_namespaced_secret(secret_name, namespace)),
    ]:
        try:
            fn()
        except k8s.exceptions.ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete %s %s: %s", resource, dep_name, exc.reason)
                ok = False
        except Exception as exc:
            logger.warning("Failed to delete %s %s: %s", resource, dep_name, exc)
            ok = False

    logger.info("Undeployed MCP server %s (ok=%s)", name, ok)
    return ok


def get_mcp_deploy_status(name: str, namespace: str = MCP_NAMESPACE) -> Dict[str, Any]:
    """Query k8s for the pod status of a deployed MCP server."""
    from kubernetes import client as k8s
    from gateway.executors.k8s_helpers import k8s_clients

    core, _ = k8s_clients()
    dep_name = f"mcp-{name}"

    try:
        pods = core.list_namespaced_pod(
            namespace,
            label_selector=f"app={dep_name}",
            limit=5,
        )
    except Exception as exc:
        return {"status": "unknown", "error": str(exc)}

    if not pods.items:
        return {"status": "not_found", "pods": 0}

    pod = pods.items[0]
    phase = (pod.status.phase or "Unknown").lower()
    ready = False
    restarts = 0
    if pod.status.container_statuses:
        cs = pod.status.container_statuses[0]
        ready = cs.ready or False
        restarts = cs.restart_count or 0

    if phase == "running" and ready:
        status = "running"
    elif phase == "running":
        status = "starting"
    elif phase in ("pending", "containercreating"):
        status = "deploying"
    else:
        status = "error"

    return {
        "status": status,
        "phase": phase,
        "ready": ready,
        "restarts": restarts,
        "pods": len(pods.items),
    }
