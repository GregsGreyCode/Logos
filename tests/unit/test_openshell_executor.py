"""
Unit tests for gateway/executors/openshell.py — OpenShellExecutor.

Mirrors the structure of test_docker_executor.py.
All OpenShell CLI calls are mocked — no openshell binary required.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.executors import build_executor
from gateway.executors.base import InstanceConfig, InstanceExecutor, ResourceHeadroom, SpawnedInstance
from gateway.executors.openshell import (
    OpenShellExecutor,
    _allocate_port,
    _health_check,
    _load_state,
    _parse_ssh_config,
    _sandbox_exists,
    _save_state,
)


# ---------------------------------------------------------------------------
# TestBuildExecutor
# ---------------------------------------------------------------------------

class TestBuildExecutorOpenShell:
    def test_openshell_mode_returns_openshell_executor(self):
        executor = build_executor("openshell")
        assert isinstance(executor, OpenShellExecutor)

    def test_satisfies_instance_executor_protocol(self):
        executor = OpenShellExecutor()
        assert isinstance(executor, InstanceExecutor)

    def test_default_image(self):
        executor = OpenShellExecutor()
        assert executor.sandbox_image == "logos-hermes-sandbox"

    def test_custom_image(self):
        executor = OpenShellExecutor(sandbox_image="custom:v1")
        assert executor.sandbox_image == "custom:v1"

    def test_default_policy_file(self):
        executor = OpenShellExecutor()
        if executor.policy_file:
            assert "openshell_default.yaml" in executor.policy_file

    def test_custom_policy_file(self):
        executor = OpenShellExecutor(policy_file="/tmp/custom.yaml")
        assert executor.policy_file == "/tmp/custom.yaml"


# ---------------------------------------------------------------------------
# TestPortAllocation
# ---------------------------------------------------------------------------

class TestOpenShellPortAllocation:
    def test_allocates_first_free_port(self):
        """_allocate_port does `import socket as _socket` locally, so we
        patch the stdlib socket module that it will import."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.socket", return_value=mock_sock):
            port = _allocate_port([])
            assert port == 8200

    def test_skips_used_ports(self):
        instances = [{"local_port": 8200}, {"local_port": 8201}]
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.socket", return_value=mock_sock):
            port = _allocate_port(instances)
            assert port == 8202


# ---------------------------------------------------------------------------
# TestSandboxExists
# ---------------------------------------------------------------------------

class TestSandboxExists:
    @patch("gateway.executors.openshell._openshell")
    def test_returns_true_when_sandbox_found(self, mock_os):
        mock_os.return_value = MagicMock(
            stdout=json.dumps([{"name": "hermes-test"}])
        )
        assert _sandbox_exists("hermes-test") is True

    @patch("gateway.executors.openshell._openshell")
    def test_returns_false_when_not_found(self, mock_os):
        mock_os.return_value = MagicMock(
            stdout=json.dumps([{"name": "hermes-other"}])
        )
        assert _sandbox_exists("hermes-test") is False

    @patch("gateway.executors.openshell._openshell")
    def test_returns_false_on_empty_list(self, mock_os):
        mock_os.return_value = MagicMock(stdout="[]")
        assert _sandbox_exists("hermes-test") is False

    @patch("gateway.executors.openshell._openshell")
    def test_returns_false_on_exception(self, mock_os):
        mock_os.side_effect = Exception("CLI not found")
        assert _sandbox_exists("hermes-test") is False


# ---------------------------------------------------------------------------
# TestParseSSHConfig
# ---------------------------------------------------------------------------

class TestParseSSHConfig:
    def test_parses_standard_config(self):
        config_text = (
            "Host hermes-test\n"
            "  HostName 172.17.0.2\n"
            "  Port 2222\n"
            "  IdentityFile /tmp/openshell/keys/hermes-test\n"
            "  User root\n"
        )
        result = _parse_ssh_config(config_text)
        assert result["HostName"] == "172.17.0.2"
        assert result["Port"] == "2222"
        assert result["IdentityFile"] == "/tmp/openshell/keys/hermes-test"
        assert result["User"] == "root"

    def test_parses_empty_string(self):
        assert _parse_ssh_config("") == {}

    def test_handles_no_whitespace_prefix(self):
        result = _parse_ssh_config("HostName 10.0.0.1\nPort 22")
        assert result["HostName"] == "10.0.0.1"
        assert result["Port"] == "22"


# ---------------------------------------------------------------------------
# TestStateFile
# ---------------------------------------------------------------------------

class TestOpenShellStateFile:
    def test_load_returns_empty_when_no_file(self, tmp_path):
        with patch("gateway.executors.openshell._STATE_FILE", tmp_path / "nonexistent.json"):
            assert _load_state() == []

    def test_save_and_load_roundtrip(self, tmp_path):
        state_file = tmp_path / "openshell_instances.json"
        instances = [{"name": "test-1", "sandbox_name": "hermes-test-1", "local_port": 8200}]
        with patch("gateway.executors.openshell._STATE_FILE", state_file):
            _save_state(instances)
            loaded = _load_state()
            assert loaded == instances

    def test_load_handles_corrupt_json(self, tmp_path):
        state_file = tmp_path / "openshell_instances.json"
        state_file.write_text("not json{{{")
        with patch("gateway.executors.openshell._STATE_FILE", state_file):
            assert _load_state() == []


# ---------------------------------------------------------------------------
# TestSpawn
# ---------------------------------------------------------------------------

class TestOpenShellSpawn:
    @patch("gateway.executors.openshell._health_check", return_value=True)
    @patch("gateway.executors.openshell._start_port_forward", return_value=12345)
    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._sandbox_exists", return_value=False)
    @patch("gateway.executors.openshell._load_state", return_value=[])
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._allocate_port", return_value=8200)
    def test_spawn_creates_sandbox(self, mock_port, mock_save, mock_load,
                                    mock_exists, mock_os, mock_fwd, mock_health):
        mock_os.return_value = MagicMock(stdout="sandbox created\n")
        executor = OpenShellExecutor()
        config = InstanceConfig(name="test-agent", soul_name="general")

        result = executor.spawn(config)

        assert isinstance(result, SpawnedInstance)
        assert result.name == "test-agent"
        assert result.port == 8200
        assert result.source == "openshell"
        assert result.healthy is True
        # Verify openshell sandbox create was called
        mock_os.assert_called()
        create_call = mock_os.call_args_list[0]
        args = create_call[0]
        assert "sandbox" in args
        assert "create" in args
        assert "--name" in args
        assert "hermes-test-agent" in args
        assert "--from" in args
        assert "--detach" in args

    @patch("gateway.executors.openshell._health_check", return_value=True)
    @patch("gateway.executors.openshell._start_port_forward", return_value=12345)
    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._sandbox_exists", return_value=False)
    @patch("gateway.executors.openshell._load_state", return_value=[])
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._allocate_port", return_value=8200)
    def test_spawn_passes_env_vars(self, mock_port, mock_save, mock_load,
                                    mock_exists, mock_os, mock_fwd, mock_health):
        mock_os.return_value = MagicMock(stdout="ok\n")
        executor = OpenShellExecutor()
        config = InstanceConfig(
            name="test-agent",
            soul_name="atlas",
            toolsets=["hermes-cli", "web"],
            policy="WORKSPACE_ONLY",
        )

        executor.spawn(config)

        create_call = mock_os.call_args_list[0]
        args_str = " ".join(create_call[0])
        assert "HERMES_SOUL=atlas" in args_str
        assert "HERMES_TOOLSETS=hermes-cli,web" in args_str
        assert "HERMES_POLICY_LEVEL=WORKSPACE_ONLY" in args_str

    @patch("gateway.executors.openshell._health_check", return_value=True)
    @patch("gateway.executors.openshell._start_port_forward", return_value=12345)
    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._sandbox_exists", return_value=False)
    @patch("gateway.executors.openshell._load_state", return_value=[])
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._allocate_port", return_value=8200)
    def test_spawn_applies_policy(self, mock_port, mock_save, mock_load,
                                   mock_exists, mock_os, mock_fwd, mock_health):
        mock_os.return_value = MagicMock(stdout="ok\n")
        executor = OpenShellExecutor(policy_file="/tmp/test-policy.yaml")
        config = InstanceConfig(name="test-agent")

        with patch("pathlib.Path.exists", return_value=True):
            executor.spawn(config)

        # Second call should be policy set
        assert len(mock_os.call_args_list) >= 2
        policy_call = mock_os.call_args_list[1]
        args = policy_call[0]
        assert "policy" in args
        assert "set" in args
        assert "/tmp/test-policy.yaml" in args

    @patch("gateway.executors.openshell._health_check", return_value=False)
    @patch("gateway.executors.openshell._start_port_forward", return_value=12345)
    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._sandbox_exists", return_value=False)
    @patch("gateway.executors.openshell._load_state", return_value=[])
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._allocate_port", return_value=8200)
    def test_spawn_reports_unhealthy(self, mock_port, mock_save, mock_load,
                                      mock_exists, mock_os, mock_fwd, mock_health):
        mock_os.return_value = MagicMock(stdout="ok\n")
        executor = OpenShellExecutor()
        config = InstanceConfig(name="test-agent")

        result = executor.spawn(config)
        assert result.healthy is False

    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._sandbox_exists", return_value=False)
    @patch("gateway.executors.openshell._load_state", return_value=[])
    @patch("gateway.executors.openshell._allocate_port", return_value=8200)
    def test_spawn_raises_on_cli_failure(self, mock_port, mock_load,
                                          mock_exists, mock_os):
        mock_os.side_effect = subprocess.CalledProcessError(
            1, "openshell sandbox create", stderr="image not found"
        )
        executor = OpenShellExecutor()
        config = InstanceConfig(name="test-agent")

        with pytest.raises(RuntimeError, match="Failed to create OpenShell sandbox"):
            executor.spawn(config)

    @patch("gateway.executors.openshell._health_check", return_value=True)
    @patch("gateway.executors.openshell._start_port_forward", return_value=None)
    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._sandbox_exists", return_value=False)
    @patch("gateway.executors.openshell._load_state", return_value=[])
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._allocate_port", return_value=8200)
    def test_spawn_continues_when_tunnel_fails(self, mock_port, mock_save, mock_load,
                                                mock_exists, mock_os, mock_fwd, mock_health):
        """Sandbox is created even if SSH tunnel fails — health check may still pass."""
        mock_os.return_value = MagicMock(stdout="ok\n")
        executor = OpenShellExecutor()
        config = InstanceConfig(name="test-agent")

        result = executor.spawn(config)
        assert result.name == "test-agent"
        # tunnel_pid=None should be recorded in state
        saved = mock_save.call_args[0][0]
        assert saved[0]["tunnel_pid"] is None


# ---------------------------------------------------------------------------
# TestDeleteInstance
# ---------------------------------------------------------------------------

class TestOpenShellDeleteInstance:
    @patch("gateway.executors.openshell._kill_pid")
    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._load_state")
    def test_delete_destroys_sandbox_and_kills_tunnel(self, mock_load, mock_save,
                                                       mock_os, mock_kill):
        mock_load.return_value = [
            {"name": "test-agent", "sandbox_name": "hermes-test-agent",
             "local_port": 8200, "tunnel_pid": 12345},
        ]
        executor = OpenShellExecutor()
        executor.delete_instance("test-agent")

        mock_kill.assert_called_once_with(12345)
        mock_os.assert_called_once_with("sandbox", "delete", "hermes-test-agent", check=False)
        mock_save.assert_called_once_with([])

    @patch("gateway.executors.openshell._kill_pid")
    @patch("gateway.executors.openshell._openshell")
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._load_state")
    def test_delete_unknown_name_no_error(self, mock_load, mock_save,
                                           mock_os, mock_kill):
        mock_load.return_value = [
            {"name": "other-agent", "sandbox_name": "hermes-other", "local_port": 8200},
        ]
        executor = OpenShellExecutor()
        executor.delete_instance("nonexistent")

        mock_os.assert_not_called()
        mock_kill.assert_not_called()
        mock_save.assert_called_once_with([
            {"name": "other-agent", "sandbox_name": "hermes-other", "local_port": 8200},
        ])


# ---------------------------------------------------------------------------
# TestListInstances
# ---------------------------------------------------------------------------

class TestOpenShellListInstances:
    @patch("gateway.executors.openshell._health_check", return_value=True)
    @patch("gateway.executors.openshell._sandbox_exists")
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._load_state")
    def test_prunes_dead_sandboxes(self, mock_load, mock_save, mock_exists, mock_health):
        mock_load.return_value = [
            {"name": "alive", "sandbox_name": "hermes-alive", "local_port": 8200},
            {"name": "dead", "sandbox_name": "hermes-dead", "local_port": 8201},
        ]
        mock_exists.side_effect = lambda sn: sn == "hermes-alive"

        executor = OpenShellExecutor()
        instances = executor.list_instances()

        assert len(instances) == 1
        assert instances[0]["name"] == "alive"
        mock_save.assert_called_once()

    @patch("gateway.executors.openshell._sandbox_exists", return_value=False)
    @patch("gateway.executors.openshell._save_state")
    @patch("gateway.executors.openshell._load_state")
    def test_returns_empty_when_all_dead(self, mock_load, mock_save, mock_exists):
        mock_load.return_value = [
            {"name": "dead", "sandbox_name": "hermes-dead", "local_port": 8200},
        ]
        executor = OpenShellExecutor()
        instances = executor.list_instances()
        assert instances == []


# ---------------------------------------------------------------------------
# TestGetHeadroom
# ---------------------------------------------------------------------------

class TestOpenShellGetHeadroom:
    @patch("gateway.executors.openshell.OpenShellExecutor.list_instances", return_value=[])
    def test_falls_back_to_psutil(self, mock_list):
        executor = OpenShellExecutor()
        headroom = executor.get_headroom()
        assert isinstance(headroom, ResourceHeadroom)
        # Should return something sensible from psutil fallback or last-resort default
        assert headroom.can_spawn is True or isinstance(headroom.reason, str)

    @patch("gateway.executors.openshell.OpenShellExecutor.list_instances", return_value=[])
    def test_get_resources_returns_dict(self, mock_list):
        executor = OpenShellExecutor()
        resources = executor.get_resources()
        assert isinstance(resources, dict)
        assert resources["executor"] == "openshell"
        assert "can_spawn" in resources


# ---------------------------------------------------------------------------
# TestCLINotFound
# ---------------------------------------------------------------------------

class TestOpenShellCLINotFound:
    @patch("shutil.which", return_value=None)
    def test_openshell_not_on_path_raises(self, mock_which):
        from gateway.executors.openshell import _openshell
        with pytest.raises(FileNotFoundError, match="openshell CLI not found"):
            _openshell("sandbox", "list")
