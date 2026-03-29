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
}


def get_catalogue(remote_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the merged catalogue (built-in + remote if configured).

    Each entry includes the catalogue_id (dict key) for reference.
    """
    entries = []
    for cid, entry in BUILTIN_CATALOGUE.items():
        entries.append({"catalogue_id": cid, **entry})

    if remote_url:
        try:
            remote = _fetch_remote_catalogue(remote_url)
            # Remote entries use their own IDs; skip duplicates
            known_ids = {e["catalogue_id"] for e in entries}
            for re_entry in remote:
                if re_entry.get("catalogue_id") not in known_ids:
                    entries.append(re_entry)
        except Exception as exc:
            logger.warning("Failed to fetch remote MCP catalogue from %s: %s", remote_url, exc)

    return entries


def get_catalogue_entry(catalogue_id: str) -> Optional[Dict[str, Any]]:
    """Look up a single catalogue entry by ID."""
    entry = BUILTIN_CATALOGUE.get(catalogue_id)
    if entry:
        return {"catalogue_id": catalogue_id, **entry}
    return None


def _fetch_remote_catalogue(url: str) -> List[Dict[str, Any]]:
    """Fetch and parse a remote catalogue JSON."""
    import httpx
    resp = httpx.get(url, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "servers" in data:
        return data["servers"]
    return []
