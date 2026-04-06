"""
Policy compiler — translates a Logos ActionPolicy into OpenShell YAML.

The compiler selects a base preset (one of the four YAML templates in this
directory) based on the ``filesystem_policy`` dimension, then injects
dynamic network rules for:

  * configured model endpoints (Ollama, LM Studio, cloud providers)
  * MCP gateway endpoints (per-server, scoped to POST /mcp/*)
  * user-specified network allowlist domains

The output is a complete, self-contained OpenShell policy YAML string
that can be passed to ``openshell policy set --policy <file>``.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from pathlib import Path
from typing import Optional

import yaml

from gateway.auth.policy import (
    ActionPolicy,
    FilesystemPolicy,
    NetworkPolicy,
)

logger = logging.getLogger(__name__)

_PRESETS_DIR = Path(__file__).parent

# Map FilesystemPolicy values to preset YAML filenames.
_FS_PRESET_MAP: dict[str, str] = {
    FilesystemPolicy.FULL:           "full_access.yaml",
    FilesystemPolicy.WORKSPACE_ONLY: "workspace_only.yaml",
    FilesystemPolicy.REPO_SCOPED:    "repo_scoped.yaml",
    FilesystemPolicy.READ_ONLY:      "read_only.yaml",
}

# Fallback preset when the filesystem_policy value is unknown.
_DEFAULT_PRESET = "workspace_only.yaml"


def _load_preset(filename: str) -> dict:
    """Load and parse a preset YAML file, returning the parsed dict."""
    path = _PRESETS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Policy preset not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _make_network_rule(
    destination: str,
    ports: list[int],
    *,
    methods: Optional[list[str]] = None,
    protocol: Optional[str] = None,
    paths: Optional[list[str]] = None,
    action: str = "allow",
) -> dict:
    """Build a single network rule dict."""
    rule: dict = {
        "action": action,
        "destination": destination,
        "ports": ports,
    }
    if methods:
        rule["methods"] = methods
    if protocol:
        rule["protocol"] = protocol
    if paths:
        rule["paths"] = paths
    return rule


def compile_openshell_policy(
    action_policy: ActionPolicy,
    config: Optional[dict] = None,
    mcp_servers: Optional[list[str]] = None,
    model_endpoints: Optional[list[dict]] = None,
    mcp_port: int = 8081,
    gateway_host: str = "host.docker.internal",
) -> str:
    """Generate a complete OpenShell policy YAML from an ActionPolicy.

    Parameters
    ----------
    action_policy:
        The Logos ActionPolicy to translate.
    config:
        Optional runtime config dict (from config.yaml).  Currently unused
        but reserved for future dynamic config resolution.
    mcp_servers:
        List of MCP server names to generate network rules for.
        Omitted for READ_ONLY policies (agent can't use tools).
    model_endpoints:
        List of dicts with ``host`` and ``port`` keys for additional model
        endpoints beyond the defaults baked into the preset.
    mcp_port:
        The port the MCP gateway listens on (default 8081).
    gateway_host:
        The hostname the sandbox uses to reach the Logos gateway
        (default ``host.docker.internal``).

    Returns
    -------
    str
        A complete OpenShell policy YAML string ready for
        ``openshell policy set --policy <file>``.
    """
    # 1. Select base preset from filesystem_policy dimension
    preset_file = _FS_PRESET_MAP.get(
        action_policy.filesystem_policy, _DEFAULT_PRESET
    )
    policy = _load_preset(preset_file)

    # 2. Find the insertion point — just before the final deny rule
    network_rules = policy.get("network", [])
    deny_idx = None
    for i, rule in enumerate(network_rules):
        if rule.get("action") == "deny":
            deny_idx = i
            break

    def _insert_rule(rule: dict) -> None:
        nonlocal deny_idx
        if deny_idx is not None:
            network_rules.insert(deny_idx, rule)
            deny_idx += 1
        else:
            network_rules.append(rule)

    # 3. Inject additional model endpoints
    if model_endpoints:
        for ep in model_endpoints:
            host = ep.get("host", "")
            port = ep.get("port", 443)
            if host:
                _insert_rule(_make_network_rule(
                    host, [port], methods=["GET", "POST"],
                ))

    # 4. Inject MCP gateway rules (unless READ_ONLY — no tool access)
    if mcp_servers and action_policy.filesystem_policy != FilesystemPolicy.READ_ONLY:
        # One rule for all MCP servers — scoped to POST on /mcp/* paths
        mcp_paths = []
        for server_name in mcp_servers:
            mcp_paths.append(f"/mcp/{server_name}")
            mcp_paths.append(f"/mcp/{server_name}/*")

        _insert_rule(_make_network_rule(
            gateway_host,
            [mcp_port],
            methods=["POST"],
            paths=mcp_paths,
        ))

    # 5. Inject network allowlist domains (for ALLOWLISTED or INTERNET_ENABLED)
    if action_policy.network_allowlist:
        for domain in action_policy.network_allowlist:
            if isinstance(domain, str) and domain.strip():
                _insert_rule(_make_network_rule(
                    domain.strip(), [443], methods=["GET", "POST"],
                ))

    # 6. For LOCAL_ONLY network policy, strip all non-local/non-DNS rules
    if action_policy.network_policy == NetworkPolicy.LOCAL_ONLY:
        local_destinations = {
            "host.docker.internal", "127.0.0.1", "localhost",
            "inference.local",
        }
        filtered = []
        for rule in network_rules:
            dest = rule.get("destination", "")
            is_dns = rule.get("protocol") == "udp" and 53 in rule.get("ports", [])
            is_deny = rule.get("action") == "deny"
            is_local = dest in local_destinations
            if is_dns or is_deny or is_local:
                filtered.append(rule)
        policy["network"] = filtered
    else:
        policy["network"] = network_rules

    return yaml.dump(policy, default_flow_style=False, sort_keys=False)


def policy_hash(yaml_str: str) -> str:
    """Return a short SHA-256 hash of a policy YAML for STAMP recording.

    This allows exact reproducibility verification: if two runs have the
    same policy hash, they ran under identical sandbox security contexts.
    """
    return hashlib.sha256(yaml_str.encode("utf-8")).hexdigest()[:16]


def validate_openshell_policy(yaml_str: str) -> tuple[bool, str]:
    """Basic structural validation of generated policy YAML.

    Checks:
      - Valid YAML
      - Has ``version`` key
      - Has ``network`` key with at least one rule
      - All network rules have ``action`` and ``destination``
      - Ends with a deny-all rule

    Returns
    -------
    tuple[bool, str]
        (is_valid, error_message).  error_message is empty on success.
    """
    try:
        doc = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        return False, f"Invalid YAML: {e}"

    if not isinstance(doc, dict):
        return False, "Policy must be a YAML mapping"

    if "version" not in doc:
        return False, "Missing 'version' key"

    network = doc.get("network")
    if not network or not isinstance(network, list):
        return False, "Missing or empty 'network' key"

    for i, rule in enumerate(network):
        if not isinstance(rule, dict):
            return False, f"Network rule {i} is not a mapping"
        if "action" not in rule:
            return False, f"Network rule {i} missing 'action'"
        if "destination" not in rule:
            return False, f"Network rule {i} missing 'destination'"

    # Check last rule is deny-all
    last = network[-1]
    if last.get("action") != "deny" or last.get("destination") != "*":
        return False, "Last network rule must be deny-all (action: deny, destination: '*')"

    return True, ""
