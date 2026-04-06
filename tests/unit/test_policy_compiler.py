"""
Unit tests for gateway/policies/compiler.py — OpenShell policy compilation.

Tests that ActionPolicy dimensions are correctly translated into OpenShell
YAML policy files, with dynamic injection of model endpoints, MCP servers,
and network allowlists.
"""

from __future__ import annotations

import yaml
import pytest

from gateway.auth.policy import (
    ActionPolicy,
    FilesystemPolicy,
    NetworkPolicy,
)
from gateway.policies.compiler import (
    compile_openshell_policy,
    policy_hash,
    validate_openshell_policy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(yaml_str: str) -> dict:
    return yaml.safe_load(yaml_str)


def _network_destinations(yaml_str: str) -> list[str]:
    """Extract all destination values from network rules."""
    doc = _parse(yaml_str)
    return [r.get("destination", "") for r in doc.get("network", [])]


def _find_rule(yaml_str: str, destination: str) -> dict | None:
    """Find the first network rule matching a destination."""
    doc = _parse(yaml_str)
    for r in doc.get("network", []):
        if r.get("destination") == destination:
            return r
    return None


# ---------------------------------------------------------------------------
# TestPresetSelection
# ---------------------------------------------------------------------------

class TestPresetSelection:
    """Each FilesystemPolicy maps to the correct preset YAML."""

    def test_full_selects_full_access(self):
        p = ActionPolicy(filesystem_policy=FilesystemPolicy.FULL)
        result = _parse(compile_openshell_policy(p))
        # full_access allows /sandbox, /tmp, /home as read_write
        rw = result.get("filesystem", {}).get("read_write", [])
        assert "/home" in rw
        assert "/sandbox" in rw

    def test_workspace_only_selects_workspace_preset(self):
        p = ActionPolicy(filesystem_policy=FilesystemPolicy.WORKSPACE_ONLY)
        result = _parse(compile_openshell_policy(p))
        rw = result.get("filesystem", {}).get("read_write", [])
        assert "/sandbox" in rw
        assert "/tmp" in rw
        assert "/home" not in rw  # /home is read_only in this preset

    def test_repo_scoped_selects_repo_preset(self):
        p = ActionPolicy(filesystem_policy=FilesystemPolicy.REPO_SCOPED)
        result = _parse(compile_openshell_policy(p))
        rw = result.get("filesystem", {}).get("read_write", [])
        assert "/sandbox/repo" in rw
        assert "/sandbox" not in rw  # /sandbox is read_only, only /sandbox/repo writable

    def test_read_only_selects_read_only_preset(self):
        p = ActionPolicy(filesystem_policy=FilesystemPolicy.READ_ONLY)
        result = _parse(compile_openshell_policy(p))
        rw = result.get("filesystem", {}).get("read_write", [])
        assert rw == ["/tmp"]  # only /tmp is writable

    def test_unknown_falls_back_to_workspace_only(self):
        p = ActionPolicy(filesystem_policy="nonexistent_level")
        result = _parse(compile_openshell_policy(p))
        rw = result.get("filesystem", {}).get("read_write", [])
        assert "/sandbox" in rw  # workspace_only default


# ---------------------------------------------------------------------------
# TestModelEndpointInjection
# ---------------------------------------------------------------------------

class TestModelEndpointInjection:
    def test_adds_model_endpoint(self):
        p = ActionPolicy()
        result = compile_openshell_policy(
            p, model_endpoints=[{"host": "api.openai.com", "port": 443}]
        )
        rule = _find_rule(result, "api.openai.com")
        assert rule is not None
        assert 443 in rule["ports"]
        assert rule["action"] == "allow"

    def test_multiple_model_endpoints(self):
        p = ActionPolicy()
        result = compile_openshell_policy(p, model_endpoints=[
            {"host": "api.openai.com", "port": 443},
            {"host": "api.anthropic.com", "port": 443},
        ])
        dests = _network_destinations(result)
        assert "api.openai.com" in dests
        assert "api.anthropic.com" in dests

    def test_empty_model_endpoints_no_change(self):
        p = ActionPolicy()
        without = compile_openshell_policy(p)
        with_empty = compile_openshell_policy(p, model_endpoints=[])
        assert without == with_empty

    def test_skips_empty_host(self):
        p = ActionPolicy()
        result = compile_openshell_policy(
            p, model_endpoints=[{"host": "", "port": 443}]
        )
        # Should not add a rule with empty destination
        dests = _network_destinations(result)
        assert "" not in dests


# ---------------------------------------------------------------------------
# TestMCPInjection
# ---------------------------------------------------------------------------

class TestMCPInjection:
    def test_adds_mcp_rules(self):
        p = ActionPolicy(filesystem_policy=FilesystemPolicy.WORKSPACE_ONLY)
        result = compile_openshell_policy(p, mcp_servers=["filesystem", "github"])
        doc = _parse(result)
        # Find the MCP rule
        mcp_rules = [r for r in doc["network"]
                     if r.get("paths") and any("/mcp/" in path for path in r["paths"])]
        assert len(mcp_rules) == 1
        rule = mcp_rules[0]
        assert "/mcp/filesystem" in rule["paths"]
        assert "/mcp/github" in rule["paths"]
        assert rule["methods"] == ["POST"]

    def test_mcp_uses_configured_port(self):
        p = ActionPolicy()
        result = compile_openshell_policy(p, mcp_servers=["fs"], mcp_port=9090)
        doc = _parse(result)
        mcp_rules = [r for r in doc["network"] if r.get("paths")]
        assert mcp_rules[0]["ports"] == [9090]

    def test_mcp_uses_configured_gateway_host(self):
        p = ActionPolicy()
        result = compile_openshell_policy(
            p, mcp_servers=["fs"], gateway_host="gateway.local"
        )
        doc = _parse(result)
        mcp_rules = [r for r in doc["network"] if r.get("paths")]
        assert mcp_rules[0]["destination"] == "gateway.local"

    def test_read_only_omits_mcp(self):
        """READ_ONLY policy should not include MCP rules even when servers are specified."""
        p = ActionPolicy(filesystem_policy=FilesystemPolicy.READ_ONLY)
        result = compile_openshell_policy(p, mcp_servers=["filesystem", "github"])
        doc = _parse(result)
        mcp_rules = [r for r in doc["network"]
                     if r.get("paths") and any("/mcp/" in path for path in r["paths"])]
        assert len(mcp_rules) == 0

    def test_no_mcp_servers_no_rule(self):
        p = ActionPolicy()
        result = compile_openshell_policy(p, mcp_servers=[])
        doc = _parse(result)
        mcp_rules = [r for r in doc["network"] if r.get("paths")]
        assert len(mcp_rules) == 0


# ---------------------------------------------------------------------------
# TestNetworkPolicyFiltering
# ---------------------------------------------------------------------------

class TestNetworkPolicyFiltering:
    def test_local_only_strips_external(self):
        """LOCAL_ONLY should remove non-local destinations."""
        p = ActionPolicy(
            filesystem_policy=FilesystemPolicy.FULL,
            network_policy=NetworkPolicy.LOCAL_ONLY,
        )
        result = compile_openshell_policy(p)
        dests = _network_destinations(result)
        # pypi.org, github.com etc should be stripped
        assert "pypi.org" not in dests
        assert "github.com" not in dests
        assert "registry.npmjs.org" not in dests
        # local/inference should remain
        assert "host.docker.internal" in dests or "inference.local" in dests

    def test_local_only_preserves_dns(self):
        p = ActionPolicy(network_policy=NetworkPolicy.LOCAL_ONLY)
        result = compile_openshell_policy(p)
        doc = _parse(result)
        dns_rules = [r for r in doc["network"]
                     if r.get("protocol") == "udp" and 53 in r.get("ports", [])]
        assert len(dns_rules) >= 1

    def test_local_only_preserves_deny_all(self):
        p = ActionPolicy(network_policy=NetworkPolicy.LOCAL_ONLY)
        result = compile_openshell_policy(p)
        doc = _parse(result)
        last = doc["network"][-1]
        assert last["action"] == "deny"
        assert last["destination"] == "*"

    def test_internet_enabled_keeps_everything(self):
        """INTERNET_ENABLED should not filter anything."""
        p = ActionPolicy(
            filesystem_policy=FilesystemPolicy.FULL,
            network_policy=NetworkPolicy.INTERNET_ENABLED,
        )
        result = compile_openshell_policy(p)
        dests = _network_destinations(result)
        assert "pypi.org" in dests
        assert "github.com" in dests


# ---------------------------------------------------------------------------
# TestNetworkAllowlist
# ---------------------------------------------------------------------------

class TestNetworkAllowlist:
    def test_adds_allowlist_domains(self):
        p = ActionPolicy(network_allowlist=["custom-api.example.com", "data.internal"])
        result = compile_openshell_policy(p)
        dests = _network_destinations(result)
        assert "custom-api.example.com" in dests
        assert "data.internal" in dests

    def test_strips_whitespace_from_domains(self):
        p = ActionPolicy(network_allowlist=["  example.com  "])
        result = compile_openshell_policy(p)
        dests = _network_destinations(result)
        assert "example.com" in dests
        assert "  example.com  " not in dests

    def test_skips_empty_strings(self):
        p = ActionPolicy(network_allowlist=["", "  ", "valid.com"])
        result = compile_openshell_policy(p)
        dests = _network_destinations(result)
        assert "valid.com" in dests
        assert "" not in dests


# ---------------------------------------------------------------------------
# TestValidation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_all_presets_valid(self):
        for fs in [FilesystemPolicy.FULL, FilesystemPolicy.WORKSPACE_ONLY,
                   FilesystemPolicy.REPO_SCOPED, FilesystemPolicy.READ_ONLY]:
            p = ActionPolicy(filesystem_policy=fs)
            result = compile_openshell_policy(p)
            valid, err = validate_openshell_policy(result)
            assert valid, f"Preset {fs} invalid: {err}"

    def test_rejects_invalid_yaml(self):
        valid, err = validate_openshell_policy("{{invalid yaml")
        assert not valid
        assert "Invalid YAML" in err

    def test_rejects_missing_version(self):
        valid, err = validate_openshell_policy("network:\n- action: deny\n  destination: '*'\n")
        assert not valid
        assert "version" in err

    def test_rejects_missing_network(self):
        valid, err = validate_openshell_policy("version: '1'\n")
        assert not valid
        assert "network" in err

    def test_rejects_rule_without_action(self):
        doc = "version: '1'\nnetwork:\n- destination: '*'\n"
        valid, err = validate_openshell_policy(doc)
        assert not valid
        assert "action" in err

    def test_rejects_rule_without_destination(self):
        doc = "version: '1'\nnetwork:\n- action: deny\n"
        valid, err = validate_openshell_policy(doc)
        assert not valid
        assert "destination" in err

    def test_rejects_no_deny_all_at_end(self):
        doc = "version: '1'\nnetwork:\n- action: allow\n  destination: example.com\n  ports: [443]\n"
        valid, err = validate_openshell_policy(doc)
        assert not valid
        assert "deny-all" in err


# ---------------------------------------------------------------------------
# TestDeterminism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_inputs_same_hash(self):
        p = ActionPolicy(filesystem_policy=FilesystemPolicy.WORKSPACE_ONLY)
        r1 = compile_openshell_policy(p, mcp_servers=["fs"])
        r2 = compile_openshell_policy(p, mcp_servers=["fs"])
        assert policy_hash(r1) == policy_hash(r2)

    def test_different_inputs_different_hash(self):
        p1 = ActionPolicy(filesystem_policy=FilesystemPolicy.FULL)
        p2 = ActionPolicy(filesystem_policy=FilesystemPolicy.READ_ONLY)
        r1 = compile_openshell_policy(p1)
        r2 = compile_openshell_policy(p2)
        assert policy_hash(r1) != policy_hash(r2)

    def test_hash_is_16_hex_chars(self):
        p = ActionPolicy()
        h = policy_hash(compile_openshell_policy(p))
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# TestSoulMdProtection
# ---------------------------------------------------------------------------

class TestSoulMdProtection:
    """All presets should have /hermes/SOUL.md in read_only paths."""

    @pytest.mark.parametrize("fs_policy", [
        FilesystemPolicy.FULL,
        FilesystemPolicy.WORKSPACE_ONLY,
        FilesystemPolicy.REPO_SCOPED,
        FilesystemPolicy.READ_ONLY,
    ])
    def test_soul_md_read_only(self, fs_policy):
        p = ActionPolicy(filesystem_policy=fs_policy)
        result = _parse(compile_openshell_policy(p))
        ro = result.get("filesystem", {}).get("read_only", [])
        assert "/hermes/SOUL.md" in ro


# ---------------------------------------------------------------------------
# TestProcessUser
# ---------------------------------------------------------------------------

class TestProcessUser:
    """All presets should enforce non-root execution."""

    @pytest.mark.parametrize("fs_policy", [
        FilesystemPolicy.FULL,
        FilesystemPolicy.WORKSPACE_ONLY,
        FilesystemPolicy.REPO_SCOPED,
        FilesystemPolicy.READ_ONLY,
    ])
    def test_runs_as_hermes(self, fs_policy):
        p = ActionPolicy(filesystem_policy=fs_policy)
        result = _parse(compile_openshell_policy(p))
        assert result.get("process", {}).get("run_as_user") == "hermes"
