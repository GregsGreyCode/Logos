"""
Unit tests for gateway/executors/docker.py — DockerSandboxExecutor.

Mirrors the structure of test_executors.py (LocalProcessExecutor tests).
All Docker CLI calls are mocked — no Docker daemon required.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

from gateway.executors import build_executor
from gateway.executors.base import InstanceConfig, ResourceHeadroom, SpawnedInstance
from gateway.executors.docker import (
    DockerSandboxExecutor,
    _allocate_port,
    _container_running,
    _health_check,
    _load_state,
    _save_state,
)


# ---------------------------------------------------------------------------
# TestBuildExecutor
# ---------------------------------------------------------------------------

class TestBuildExecutorDocker:
    def test_docker_mode_returns_docker_executor(self):
        executor = build_executor("docker")
        assert isinstance(executor, DockerSandboxExecutor)

    def test_docker_executor_default_image(self):
        executor = DockerSandboxExecutor()
        assert executor.sandbox_image == "logos-hermes-sandbox"

    def test_docker_executor_custom_image(self):
        executor = DockerSandboxExecutor(sandbox_image="custom:latest")
        assert executor.sandbox_image == "custom:latest"


# ---------------------------------------------------------------------------
# TestPortAllocation
# ---------------------------------------------------------------------------

class TestDockerPortAllocation:
    def test_allocates_first_free_port(self):
        with patch("socket.socket") as mock_sock:
            mock_sock.return_value.__enter__ = MagicMock()
            mock_sock.return_value.__exit__ = MagicMock()
            port = _allocate_port([], port_min=8200, port_max=8205)
            assert port == 8200

    def test_skips_used_ports(self):
        instances = [{"port": 8200}, {"port": 8201}]
        with patch("socket.socket") as mock_sock:
            mock_sock.return_value.__enter__ = MagicMock()
            mock_sock.return_value.__exit__ = MagicMock()
            port = _allocate_port(instances, port_min=8200, port_max=8205)
            assert port == 8202

    def test_raises_when_no_ports_free(self):
        instances = [{"port": p} for p in range(8200, 8203)]
        with patch("socket.socket") as mock_sock:
            mock_sock.return_value.__enter__ = MagicMock(side_effect=OSError)
            mock_sock.return_value.__exit__ = MagicMock()
            with pytest.raises(RuntimeError, match="No free ports"):
                _allocate_port(instances, port_min=8200, port_max=8202)


# ---------------------------------------------------------------------------
# TestContainerRunning
# ---------------------------------------------------------------------------

class TestContainerRunning:
    @patch("gateway.executors.docker._docker")
    def test_returns_true_when_running(self, mock_docker):
        mock_docker.return_value = MagicMock(returncode=0, stdout="true\n")
        assert _container_running("hermes-test") is True

    @patch("gateway.executors.docker._docker")
    def test_returns_false_when_stopped(self, mock_docker):
        mock_docker.return_value = MagicMock(returncode=0, stdout="false\n")
        assert _container_running("hermes-test") is False

    @patch("gateway.executors.docker._docker")
    def test_returns_false_when_not_found(self, mock_docker):
        mock_docker.return_value = MagicMock(returncode=1, stdout="")
        assert _container_running("hermes-nonexistent") is False

    @patch("gateway.executors.docker._docker")
    def test_returns_false_on_exception(self, mock_docker):
        mock_docker.side_effect = Exception("Docker daemon not running")
        assert _container_running("hermes-test") is False


# ---------------------------------------------------------------------------
# TestStateFile
# ---------------------------------------------------------------------------

class TestDockerStateFile:
    def test_load_returns_empty_when_no_file(self, tmp_path):
        with patch("gateway.executors.docker._STATE_FILE", tmp_path / "nonexistent.json"):
            assert _load_state() == []

    def test_save_and_load_roundtrip(self, tmp_path):
        state_file = tmp_path / "docker_instances.json"
        instances = [{"name": "test-1", "port": 8200, "container_name": "hermes-test-1"}]
        with patch("gateway.executors.docker._STATE_FILE", state_file):
            _save_state(instances)
            loaded = _load_state()
            assert loaded == instances

    def test_load_handles_corrupt_json(self, tmp_path):
        state_file = tmp_path / "docker_instances.json"
        state_file.write_text("not json{{{")
        with patch("gateway.executors.docker._STATE_FILE", state_file):
            assert _load_state() == []


# ---------------------------------------------------------------------------
# TestSpawn
# ---------------------------------------------------------------------------

class TestDockerSpawn:
    @patch("gateway.executors.docker._health_check", return_value=True)
    @patch("gateway.executors.docker._docker")
    @patch("gateway.executors.docker._container_running", return_value=False)
    @patch("gateway.executors.docker._load_state", return_value=[])
    @patch("gateway.executors.docker._save_state")
    @patch("gateway.executors.docker._allocate_port", return_value=8200)
    def test_spawn_creates_container(self, mock_port, mock_save, mock_load,
                                      mock_running, mock_docker, mock_health):
        mock_docker.return_value = MagicMock(stdout="abc123def456\n")
        executor = DockerSandboxExecutor()
        config = InstanceConfig(name="test-agent", soul_name="general")

        result = executor.spawn(config)

        assert isinstance(result, SpawnedInstance)
        assert result.name == "test-agent"
        assert result.port == 8200
        assert result.source == "docker"
        assert result.healthy is True
        # Verify docker run was called
        mock_docker.assert_called_once()
        call_args = mock_docker.call_args[0]
        assert "run" in call_args
        assert "--rm" in call_args
        assert "--cap-drop=ALL" in call_args
        assert "--security-opt=no-new-privileges" in call_args

    @patch("gateway.executors.docker._health_check", return_value=False)
    @patch("gateway.executors.docker._docker")
    @patch("gateway.executors.docker._container_running", return_value=False)
    @patch("gateway.executors.docker._load_state", return_value=[])
    @patch("gateway.executors.docker._save_state")
    @patch("gateway.executors.docker._allocate_port", return_value=8200)
    def test_spawn_reports_unhealthy(self, mock_port, mock_save, mock_load,
                                      mock_running, mock_docker, mock_health):
        mock_docker.return_value = MagicMock(stdout="abc123\n")
        executor = DockerSandboxExecutor()
        config = InstanceConfig(name="test-agent")

        result = executor.spawn(config)
        assert result.healthy is False

    @patch("gateway.executors.docker._docker")
    @patch("gateway.executors.docker._container_running", return_value=False)
    @patch("gateway.executors.docker._load_state", return_value=[])
    @patch("gateway.executors.docker._allocate_port", return_value=8200)
    def test_spawn_raises_on_docker_failure(self, mock_port, mock_load,
                                             mock_running, mock_docker):
        mock_docker.side_effect = subprocess.CalledProcessError(
            1, "docker run", stderr="no such image"
        )
        executor = DockerSandboxExecutor()
        config = InstanceConfig(name="test-agent")

        with pytest.raises(RuntimeError, match="Failed to create Docker sandbox"):
            executor.spawn(config)

    @patch("gateway.executors.docker._health_check", return_value=True)
    @patch("gateway.executors.docker._docker")
    @patch("gateway.executors.docker._container_running", return_value=False)
    @patch("gateway.executors.docker._load_state", return_value=[])
    @patch("gateway.executors.docker._save_state")
    @patch("gateway.executors.docker._allocate_port", return_value=8200)
    def test_spawn_passes_env_vars(self, mock_port, mock_save, mock_load,
                                    mock_running, mock_docker, mock_health):
        mock_docker.return_value = MagicMock(stdout="abc123\n")
        executor = DockerSandboxExecutor()
        config = InstanceConfig(name="test-agent", soul_name="atlas", toolsets=["hermes-cli"], policy="WORKSPACE_ONLY")

        executor.spawn(config)

        call_args = mock_docker.call_args[0]
        args_str = " ".join(call_args)
        assert "HERMES_SOUL=atlas" in args_str
        assert "HERMES_TOOLSETS=hermes-cli" in args_str
        assert "HERMES_POLICY_LEVEL=WORKSPACE_ONLY" in args_str


# ---------------------------------------------------------------------------
# TestDeleteInstance
# ---------------------------------------------------------------------------

class TestDockerDeleteInstance:
    @patch("gateway.executors.docker._docker")
    @patch("gateway.executors.docker._save_state")
    @patch("gateway.executors.docker._load_state")
    def test_delete_stops_container(self, mock_load, mock_save, mock_docker):
        mock_load.return_value = [
            {"name": "test-agent", "container_name": "hermes-test-agent", "port": 8200},
        ]
        executor = DockerSandboxExecutor()
        executor.delete_instance("test-agent")

        mock_docker.assert_called_once_with("stop", "hermes-test-agent", check=False)
        mock_save.assert_called_once_with([])

    @patch("gateway.executors.docker._docker")
    @patch("gateway.executors.docker._save_state")
    @patch("gateway.executors.docker._load_state")
    def test_delete_unknown_name_no_error(self, mock_load, mock_save, mock_docker):
        mock_load.return_value = [
            {"name": "other-agent", "container_name": "hermes-other", "port": 8200},
        ]
        executor = DockerSandboxExecutor()
        executor.delete_instance("nonexistent")

        mock_docker.assert_not_called()
        mock_save.assert_called_once_with([
            {"name": "other-agent", "container_name": "hermes-other", "port": 8200},
        ])


# ---------------------------------------------------------------------------
# TestGetHeadroom
# ---------------------------------------------------------------------------

class TestDockerGetHeadroom:
    @patch("gateway.executors.docker._docker")
    def test_returns_can_spawn_when_docker_available(self, mock_docker):
        mock_docker.return_value = MagicMock(returncode=0)
        executor = DockerSandboxExecutor()
        headroom = executor.get_headroom()
        assert headroom.can_spawn is True

    @patch("gateway.executors.docker._docker")
    def test_returns_cannot_spawn_when_docker_unreachable(self, mock_docker):
        mock_docker.side_effect = Exception("Docker daemon not running")
        executor = DockerSandboxExecutor()
        headroom = executor.get_headroom()
        assert headroom.can_spawn is False
        assert "Docker" in headroom.reason


# ---------------------------------------------------------------------------
# TestListInstances
# ---------------------------------------------------------------------------

class TestDockerListInstances:
    @patch("gateway.executors.docker._health_check", return_value=True)
    @patch("gateway.executors.docker._container_running")
    @patch("gateway.executors.docker._save_state")
    @patch("gateway.executors.docker._load_state")
    def test_prunes_dead_containers(self, mock_load, mock_save, mock_running, mock_health):
        mock_load.return_value = [
            {"name": "alive", "container_name": "hermes-alive", "port": 8200},
            {"name": "dead", "container_name": "hermes-dead", "port": 8201},
        ]
        mock_running.side_effect = lambda cn: cn == "hermes-alive"

        executor = DockerSandboxExecutor()
        instances = executor.list_instances()

        assert len(instances) == 1
        assert instances[0]["name"] == "alive"
        mock_save.assert_called_once()

    @patch("gateway.executors.docker._container_running", return_value=False)
    @patch("gateway.executors.docker._save_state")
    @patch("gateway.executors.docker._load_state")
    def test_returns_empty_when_all_dead(self, mock_load, mock_save, mock_running):
        mock_load.return_value = [
            {"name": "dead", "container_name": "hermes-dead", "port": 8200},
        ]
        executor = DockerSandboxExecutor()
        instances = executor.list_instances()
        assert instances == []
