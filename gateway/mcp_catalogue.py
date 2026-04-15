"""Built-in catalogue of known MCP server types for the Tools tab.

Each entry defines the server's image, port, config form schema, and
default resource limits.  The catalogue is shipped with Logos and can
be extended at runtime via a remote catalogue URL.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Config field types ───────────────────────────────────────────────
# Used by the UI to render the correct input widget.
#   string  — plain text input
#   url     — text input with URL validation hint
#   secret  — password-masked input, stored in k8s Secret
#   number  — numeric input
#   json    — textarea for JSON content
#   boolean — toggle switch

BUILTIN_CATALOGUE: Dict[str, Dict[str, Any]] = {
    "homelab-inspector": {
        "name": "Homelab Inspector",
        "description": "Docker containers, Prometheus metrics, Proxmox VMs, network probes",
        "category": "infrastructure",
        "image": "ghcr.io/gregsgreycode/inspector-mcp:latest",
        "port": 8000,
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "config_schema": [
            {
                "key": "INSPECTOR_URL",
                "label": "Inspector API URL",
                "type": "url",
                "required": True,
                "description": "URL of the homelab-inspector HTTP API",
                "placeholder": "http://homelab-inspector:8000",
            },
            {
                "key": "INSPECTOR_TOKEN",
                "label": "Inspector API Token",
                "type": "secret",
                "required": True,
                "description": "Bearer token for Inspector API authentication",
            },
            {
                "key": "MCP_CLIENT_TOKEN",
                "label": "MCP Client Token",
                "type": "secret",
                "required": False,
                "description": "Token that Logos sends when connecting to this server",
            },
        ],
        "default_tools": [
            "docker_list_containers", "docker_get_logs",
            "prom_query", "proxmox_vms",
            "files_tree", "files_read",
            "network_probe", "network_probe_many",
        ],
        "resources": {
            "cpu_request": "50m", "mem_request": "128Mi",
            "cpu_limit": "500m", "mem_limit": "256Mi",
        },
    },

    "homelab-lgtm": {
        "name": "LGTM Observability",
        "description": "Prometheus, Grafana dashboards, Loki logs, Alertmanager",
        "category": "observability",
        "image": "ghcr.io/gregsgreycode/lgtm-mcp:latest",
        "port": 8000,
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "config_schema": [
            {
                "key": "GRAFANA_URL",
                "label": "Grafana URL",
                "type": "url",
                "required": False,
                "default": "http://grafana:3000",
                "description": "Grafana endpoint",
            },
            {
                "key": "GRAFANA_READ_TOKEN",
                "label": "Grafana Read Token",
                "type": "secret",
                "required": True,
                "description": "Grafana service account token with Viewer role",
            },
            {
                "key": "PROMETHEUS_URL",
                "label": "Prometheus URL",
                "type": "url",
                "required": False,
                "default": "http://prometheus-operated:9090",
                "description": "Prometheus query endpoint",
            },
            {
                "key": "LOKI_URL",
                "label": "Loki URL",
                "type": "url",
                "required": False,
                "default": "http://loki:3100",
                "description": "Loki logs endpoint",
            },
            {
                "key": "ALERTMANAGER_URL",
                "label": "Alertmanager URL",
                "type": "url",
                "required": False,
                "default": "http://alertmanager:9093",
                "description": "Alertmanager endpoint",
            },
            {
                "key": "MCP_CLIENT_TOKEN",
                "label": "MCP Client Token",
                "type": "secret",
                "required": False,
                "description": "Token that Logos sends when connecting",
            },
        ],
        "default_tools": [
            "prometheus_list_targets", "prometheus_list_alerts", "prometheus_list_rules",
            "loki_query", "loki_label_names", "loki_label_values",
            "grafana_list_dashboards", "grafana_get_dashboard",
            "grafana_list_datasources", "grafana_list_folders",
            "alertmanager_list_alerts", "alertmanager_list_silences",
            "alertmanager_get_config",
        ],
        "resources": {
            "cpu_request": "50m", "mem_request": "128Mi",
            "cpu_limit": "500m", "mem_limit": "256Mi",
        },
    },

    "homelab-ssh": {
        "name": "SSH Access",
        "description": "Remote shell execution and file reading on configured hosts",
        "category": "infrastructure",
        "image": "ghcr.io/gregsgreycode/ssh-mcp:latest",
        "port": 8000,
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "config_schema": [
            {
                "key": "SSH_HOSTS_JSON",
                "label": "Hosts Configuration",
                "type": "json",
                "required": True,
                "description": "JSON dict of host aliases to connection details",
                "placeholder": '{"myhost": {"host": "192.168.1.100", "port": 22, "username": "admin"}}',
            },
            {
                "key": "SSH_PRIVATE_KEY",
                "label": "SSH Private Key",
                "type": "secret",
                "required": False,
                "description": "PEM content of the SSH private key (alternative to mounted key file)",
            },
            {
                "key": "SSH_COMMAND_TIMEOUT",
                "label": "Command Timeout (seconds)",
                "type": "number",
                "required": False,
                "default": "60",
                "description": "Default timeout for SSH commands (1-300)",
            },
            {
                "key": "MCP_CLIENT_TOKEN",
                "label": "MCP Client Token",
                "type": "secret",
                "required": False,
                "description": "Token that Logos sends when connecting",
            },
        ],
        "default_tools": ["ssh_list_hosts", "ssh_exec", "ssh_read_file"],
        "resources": {
            "cpu_request": "50m", "mem_request": "128Mi",
            "cpu_limit": "250m", "mem_limit": "256Mi",
        },
    },

    "homelab-socraticode": {
        "name": "SocratiCode",
        "description": "Semantic and lexical code search, dependency graphs, artifact discovery",
        "category": "development",
        "image": "ghcr.io/gregsgreycode/socraticode-mcp:latest",
        "port": 8500,
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "config_schema": [
            {
                "key": "QDRANT_URL",
                "label": "Qdrant URL",
                "type": "url",
                "required": False,
                "default": "http://qdrant:6333",
                "description": "Qdrant vector store endpoint",
            },
            {
                "key": "OPENAI_BASE_URL",
                "label": "Embedding API URL",
                "type": "url",
                "required": False,
                "description": "Base URL for the embedding model API",
            },
            {
                "key": "OPENAI_API_KEY",
                "label": "Embedding API Key",
                "type": "secret",
                "required": False,
                "default": "lm-studio",
                "description": "API key for the embedding provider",
            },
            {
                "key": "EMBEDDING_MODEL",
                "label": "Embedding Model",
                "type": "string",
                "required": False,
                "description": "Model name for embeddings (e.g. text-embedding-3-small)",
            },
        ],
        "default_tools": [
            "codebase_search", "codebase_graph_query",
            "codebase_context", "codebase_context_search", "codebase_status",
        ],
        "resources": {
            "cpu_request": "100m", "mem_request": "256Mi",
            "cpu_limit": "1000m", "mem_limit": "512Mi",
        },
    },

    "homelab-operator": {
        "name": "Operator",
        "description": "Git, Docker build/push, kubectl apply -- write access to infrastructure",
        "category": "infrastructure",
        "image": "ghcr.io/gregsgreycode/operator-mcp:latest",
        "port": 8000,
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "config_schema": [
            {
                "key": "OPERATOR_TOKEN",
                "label": "Operator Token",
                "type": "secret",
                "required": True,
                "description": "Authentication token for operator requests",
            },
            {
                "key": "OPERATOR_ALLOWED_REPO_ROOT",
                "label": "Repo Root",
                "type": "string",
                "required": False,
                "default": "/repo",
                "description": "Base directory for git and file write operations",
            },
            {
                "key": "OPERATOR_ALLOWED_IMAGE_PREFIX",
                "label": "Allowed Image Prefix",
                "type": "string",
                "required": False,
                "default": "ghcr.io/gregsgreycode/",
                "description": "Required prefix for Docker images that can be built/pushed",
            },
            {
                "key": "DOCKER_HOST",
                "label": "Docker Host",
                "type": "url",
                "required": False,
                "description": "Docker daemon URL for builds (e.g. tcp://192.168.1.198:2375)",
            },
            {
                "key": "MCP_CLIENT_TOKEN",
                "label": "MCP Client Token",
                "type": "secret",
                "required": False,
                "description": "Token that Logos sends when connecting",
            },
        ],
        "default_tools": [
            "files_write",
            "git_status_tool", "git_diff_tool", "git_commit_tool", "git_push_tool",
            "docker_build_tool", "docker_push_tool",
            "kubectl_apply_tool", "kubectl_rollout_restart_tool",
            "kubectl_rollout_status_tool", "kubectl_delete_deployment_tool",
        ],
        "resources": {
            "cpu_request": "100m", "mem_request": "256Mi",
            "cpu_limit": "1000m", "mem_limit": "512Mi",
        },
    },

    "homelab-claude": {
        "name": "Claude Specialist",
        "description": "Delegates complex reasoning tasks to Anthropic Claude API",
        "category": "ai",
        "image": "ghcr.io/gregsgreycode/claude-mcp:latest",
        "port": 8000,
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "config_schema": [
            {
                "key": "ANTHROPIC_API_KEY",
                "label": "Anthropic API Key",
                "type": "secret",
                "required": True,
                "description": "API key for the Anthropic Claude API",
            },
            {
                "key": "CLAUDE_MODEL",
                "label": "Model",
                "type": "string",
                "required": False,
                "default": "claude-sonnet-4-6",
                "description": "Claude model to use for delegated tasks",
            },
            {
                "key": "CLAUDE_MAX_TOKENS",
                "label": "Max Tokens",
                "type": "number",
                "required": False,
                "default": "8192",
                "description": "Maximum tokens per response",
            },
            {
                "key": "MCP_CLIENT_TOKEN",
                "label": "MCP Client Token",
                "type": "secret",
                "required": False,
                "description": "Token that Logos sends when connecting",
            },
        ],
        "default_tools": ["claude_task", "claude_review"],
        "resources": {
            "cpu_request": "50m", "mem_request": "128Mi",
            "cpu_limit": "250m", "mem_limit": "256Mi",
        },
    },

    # ── Verification-only entry — not for production use ──────────────
    # Points at a locally-built test image (see docker/testing/mcp-echo/)
    # that speaks the MCP streamable-HTTP transport and exposes a single
    # ``echo`` tool. Used to smoke-test the Docker-container deploy
    # pipeline end-to-end without needing a real registry image or
    # backing infrastructure. Deploying this should always work (no
    # config required); if it fails, the MCP deploy flow has a bug.
    "echo-test": {
        "name": "Echo (test server)",
        "description": (
            "Zero-config smoke-test MCP server. The ``echo`` tool wraps its "
            "input in a per-container marker (``echo-test[<container-id> "
            "pid=<pid> call=<n>]: <text>``) the model cannot invent without "
            "actually reaching the container — so a real call is "
            "distinguishable from a hallucinated response. NOT FOR PRODUCTION "
            "USE. Build the image first: ``docker build -f docker/testing/"
            "mcp-echo/Dockerfile -t logos-mcp-echo-test:local docker/testing/"
            "mcp-echo``."
        ),
        "category": "testing",
        "image": "logos-mcp-echo-test:local",
        "port": 8000,
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "config_schema": [],   # intentionally empty — zero config
        "default_tools": ["echo"],
        "resources": {
            "cpu_request": "20m", "mem_request": "32Mi",
            "cpu_limit": "100m", "mem_limit": "64Mi",
        },
    },
}


def get_catalogue(remote_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the merged catalogue (built-in + legacy remote URL).

    Kept for back-compat with callers that only know about the single
    ``mcp_catalogue_url`` flag. Prefer ``get_catalogue_merged`` for new
    code — it takes a list of source dicts and supports Docker Hub
    namespaces.
    """
    sources: List[Dict[str, Any]] = []
    if remote_url:
        sources.append({"type": "http_json", "url": remote_url, "enabled": True})
    return get_catalogue_merged(sources)


def get_catalogue_entry(catalogue_id: str) -> Optional[Dict[str, Any]]:
    """Look up a single catalogue entry by ID."""
    entry = BUILTIN_CATALOGUE.get(catalogue_id)
    if entry:
        return {"catalogue_id": catalogue_id, **entry, "source": "builtin"}
    return None


# ── Pluggable sources ───────────────────────────────────────────────────────
#
# A source is a small config dict:
#   {"type": "builtin"}
#   {"type": "http_json", "url": "https://example.com/catalogue.json"}
#   {"type": "docker_hub_namespace", "namespace": "mcp"}
#
# Each fetcher returns a list of catalogue entries (same shape as
# BUILTIN_CATALOGUE values, with ``catalogue_id`` + ``source`` fields
# added). Failures are logged and treated as empty — one bad source
# doesn't break the browser.
#
# Caching: a tiny in-process dict keyed on ``(type, url/namespace)`` with
# a 6-hour TTL. No thread lock — occasional duplicate fetches on cache
# miss are harmless.

import time

_CACHE_TTL_SECONDS = 6 * 3600
_cache: Dict[str, Dict[str, Any]] = {}  # key -> {"ts": float, "entries": [...]}


def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    hit = _cache.get(key)
    if not hit:
        return None
    if time.time() - hit["ts"] > _CACHE_TTL_SECONDS:
        return None
    return hit["entries"]


def _cache_put(key: str, entries: List[Dict[str, Any]]) -> None:
    _cache[key] = {"ts": time.time(), "entries": entries}


def clear_catalogue_cache() -> None:
    """Drop all cached remote-source results. Next fetch will refresh."""
    _cache.clear()


def get_catalogue_merged(sources: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Return a merged catalogue across all enabled sources.

    Built-in entries always come first. Remote entries are deduped by
    ``catalogue_id`` — the first source wins for a given ID, so user-
    configured sources can't silently shadow a builtin.

    Each returned entry has a ``source`` field naming the source type
    (``builtin``, ``http_json``, ``docker_hub_namespace``) so the UI
    can badge or filter them.
    """
    entries: List[Dict[str, Any]] = []
    known_ids: set = set()

    for cid, entry in BUILTIN_CATALOGUE.items():
        entries.append({"catalogue_id": cid, "source": "builtin", **entry})
        known_ids.add(cid)

    for src in (sources or []):
        if not src.get("enabled", True):
            continue
        src_type = src.get("type")
        try:
            if src_type == "http_json":
                fetched = _fetch_http_json(src.get("url", ""))
            elif src_type == "docker_hub_namespace":
                fetched = _fetch_docker_hub_namespace(src.get("namespace", ""))
            elif src_type == "builtin":
                continue  # already added
            else:
                logger.warning("Unknown catalogue source type: %r", src_type)
                continue
        except Exception as exc:
            logger.warning("Catalogue source %r failed: %s", src, exc)
            continue

        for e in fetched:
            cid = e.get("catalogue_id")
            if not cid or cid in known_ids:
                continue
            e.setdefault("source", src_type)
            entries.append(e)
            known_ids.add(cid)

    return entries


def _fetch_http_json(url: str) -> List[Dict[str, Any]]:
    """Fetch a JSON catalogue. Cached for 6 h on success."""
    if not url:
        return []
    cache_key = f"http_json::{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    import httpx
    resp = httpx.get(url, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and "servers" in data:
        entries = data["servers"]
    else:
        entries = []
    _cache_put(cache_key, entries)
    return entries


def _fetch_docker_hub_namespace(namespace: str) -> List[Dict[str, Any]]:
    """List public repos in a Docker Hub namespace as catalogue entries.

    Hits ``hub.docker.com/v2/repositories/<namespace>/?page_size=100``
    unauthenticated. Each repo becomes a catalogue entry with a
    best-effort image ref of ``<namespace>/<name>:latest`` — users can
    override the tag when deploying. Description comes from the repo's
    ``short_description``; category is always ``docker-hub``.

    Cached for 6 h on success. On failure, returns []; the UI still
    renders builtin + other sources.
    """
    if not namespace:
        return []
    cache_key = f"docker_hub::{namespace}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    import httpx
    url = f"https://hub.docker.com/v2/repositories/{namespace}/?page_size=100"
    resp = httpx.get(url, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json() or {}
    repos = data.get("results", []) or []

    entries: List[Dict[str, Any]] = []
    for repo in repos:
        name = repo.get("name")
        if not name:
            continue
        entries.append({
            "catalogue_id": f"dockerhub-{namespace}-{name}",
            "name": name,
            "description": (repo.get("short_description") or "").strip() or f"Docker Hub: {namespace}/{name}",
            "category": "docker-hub",
            "image": f"{namespace}/{name}:latest",
            "port": 8000,
            "transport": "streamable-http",
            "mcp_path": "/mcp",
            # We don't know env vars from the v2 API — user fills them
            # in on the deploy form or overrides the image tag.
            "config_schema": [],
            "default_tools": [],
            "resources": {
                "cpu_request": "50m", "mem_request": "128Mi",
                "cpu_limit": "500m", "mem_limit": "256Mi",
            },
            "source": "docker_hub_namespace",
            "source_detail": {"namespace": namespace, "pull_count": repo.get("pull_count")},
        })

    _cache_put(cache_key, entries)
    return entries
