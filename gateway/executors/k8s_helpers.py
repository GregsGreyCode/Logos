"""
Kubernetes utilities for the gateway.

After the legacy KubernetesExecutor was removed, only two helpers remain:

* ``k8s_clients()`` — used by ``gateway.mcp_deploy`` to deploy MCP tool
  servers into a k8s cluster (independent of the agent sandbox runtime).
* ``safe_k8s_name()`` — used universally by every executor to coerce a
  requester string into a valid RFC 1123 subdomain for instance naming.

Nothing in this module imports from ``gateway.http_api`` — that would be
circular.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def k8s_clients():
    """Return (CoreV1Api, AppsV1Api).

    Auth priority:
    1. In-cluster service account (when Logos runs as a k8s pod)
    2. KUBECONFIG env var or ~/.kube/config (standard kubeconfig lookup)
    3. Kubeconfig stored in the auth DB (pasted during setup wizard)
    """
    try:
        from kubernetes import client as k8s_client, config as k8s_config
    except ImportError:
        raise RuntimeError("kubernetes package not installed")

    # 1. In-cluster
    try:
        k8s_config.load_incluster_config()
        return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()
    except Exception:
        pass

    # 2. KUBECONFIG env var or ~/.kube/config
    try:
        k8s_config.load_kube_config()
        return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()
    except Exception:
        pass

    # 3. Kubeconfig stored in auth DB (written by setup wizard)
    try:
        import gateway.auth.db as _auth_db
        flags = _auth_db.get_platform_feature_flags()
        stored_kube = flags.get("k8s_kubeconfig", "")
        if stored_kube:
            import os, tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, prefix="logos_kube_"
            ) as _tf:
                _tf.write(stored_kube)
                _tmp = _tf.name
            try:
                k8s_config.load_kube_config(config_file=_tmp)
                core = k8s_client.CoreV1Api()
                apps = k8s_client.AppsV1Api()
                return core, apps
            finally:
                try:
                    os.unlink(_tmp)
                except OSError:
                    pass
    except Exception as _db_err:
        logger.debug("k8s_clients: DB kubeconfig fallback failed: %s", _db_err)

    raise RuntimeError(
        "No Kubernetes credentials found. "
        "Provide KUBECONFIG env var, ~/.kube/config, or paste a kubeconfig in the setup wizard."
    )


def safe_k8s_name(requester: str, label: str = "") -> str:
    """Convert a requester string (and optional instance label) to a valid k8s name.

    When *label* is provided the result is ``hermes-{requester}-{label}``
    (sanitised, max 52 chars).  This allows multiple instances per user.

    Used universally by every executor for instance naming, not just the
    (now-removed) Kubernetes one — the function name is historical.
    """
    raw = f"{requester}-{label}" if label else requester
    name = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return f"hermes-{name}"[:52]  # k8s name limit is 63 chars
