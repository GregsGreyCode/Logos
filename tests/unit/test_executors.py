"""
Unit tests for gateway/executors — OpenShell is now the only supported
sandbox runtime. The legacy KubernetesExecutor, LocalProcessExecutor and
DockerSandboxExecutor have been removed.
"""

from __future__ import annotations

import pytest

from gateway.executors import InstanceExecutor, build_executor
from gateway.executors.base import (
    InstanceConfig,
    ResourceHeadroom,
    SpawnedInstance,
    safe_k8s_name,
)


# ---------------------------------------------------------------------------
# TestBuildExecutor
# ---------------------------------------------------------------------------


class TestBuildExecutor:
    def test_returns_openshell_executor(self):
        from gateway.executors.openshell import OpenShellExecutor
        executor = build_executor()
        assert isinstance(executor, OpenShellExecutor)

    def test_openshell_executor_satisfies_protocol(self):
        executor = build_executor()
        assert isinstance(executor, InstanceExecutor)


# ---------------------------------------------------------------------------
# Multi-instance naming tests
# ---------------------------------------------------------------------------


class TestSafeK8sNameMultiInstance:
    """Verify safe_k8s_name supports instance labels for multi-instance."""

    def test_with_label(self):
        result = safe_k8s_name("greg", "researcher")
        assert result == "hermes-greg-researcher"

    def test_without_label(self):
        result = safe_k8s_name("greg")
        assert result == "hermes-greg"

    def test_empty_label(self):
        result = safe_k8s_name("greg", "")
        assert result == "hermes-greg"

    def test_label_sanitised(self):
        result = safe_k8s_name("Greg Palos", "My Researcher!")
        assert result == "hermes-greg-palos-my-researcher"
        # Should only contain valid k8s chars
        import re
        assert re.match(r"^hermes-[a-z0-9-]+$", result)

    def test_same_requester_different_labels_distinct(self):
        a = safe_k8s_name("alice", "coder")
        b = safe_k8s_name("alice", "researcher")
        c = safe_k8s_name("alice", "sysadmin")
        assert len({a, b, c}) == 3

    def test_truncation_at_52(self):
        result = safe_k8s_name("a" * 30, "b" * 30)
        assert len(result) <= 52


class TestInstanceConfigLabel:
    """InstanceConfig carries instance_label field."""

    def test_default_empty(self):
        ic = InstanceConfig(name="test")
        assert ic.instance_label == ""

    def test_explicit_label(self):
        ic = InstanceConfig(name="test", instance_label="researcher")
        assert ic.instance_label == "researcher"
