"""
Unit tests for gateway/executors — DockerSandboxExecutor and build_executor
factory. The legacy KubernetesExecutor and LocalProcessExecutor were removed
when OpenShell became the canonical sandbox runtime.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from gateway.executors import InstanceExecutor, build_executor
from gateway.executors.base import InstanceConfig, ResourceHeadroom, SpawnedInstance
from gateway.executors.docker import DockerSandboxExecutor


# ---------------------------------------------------------------------------
# TestBuildExecutor
# ---------------------------------------------------------------------------

class TestBuildExecutor:
    def test_docker_mode_returns_docker_executor(self):
        executor = build_executor("docker")
        assert isinstance(executor, DockerSandboxExecutor)

    def test_docker_executor_satisfies_protocol(self):
        executor = build_executor("docker")
        assert isinstance(executor, InstanceExecutor)

    def test_unknown_mode_falls_back_to_openshell(self):
        # Anything that isn't "docker" (including the legacy "local") should
        # return the OpenShellExecutor.
        from gateway.executors.openshell import OpenShellExecutor
        for mode in ("local", "desktop", "openshell", "", "anything"):
            assert isinstance(build_executor(mode), OpenShellExecutor)



# ---------------------------------------------------------------------------
# TestConfigRuntimeSection
# ---------------------------------------------------------------------------

class TestConfigRuntimeSection:
    def test_runtime_section_exists(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert "runtime" in DEFAULT_CONFIG

    def test_runtime_mode_default_is_openshell(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["runtime"]["mode"] == "openshell"

    def test_config_version_is_8(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["_config_version"] == 8


# ---------------------------------------------------------------------------
# TestRuntimeModeInjectedToWindow
# ---------------------------------------------------------------------------

class TestRuntimeModeInjectedToWindow:
    def test_runtime_mode_env_var_read_from_environ(self, monkeypatch):
        """LOGOS_RUNTIME_MODE env var drives runtime mode; defaults to 'openshell'."""
        monkeypatch.delenv("LOGOS_RUNTIME_MODE", raising=False)
        monkeypatch.delenv("HERMES_RUNTIME_MODE", raising=False)
        value = (
            os.environ.get("LOGOS_RUNTIME_MODE")
            or os.environ.get("HERMES_RUNTIME_MODE")
            or "openshell"
        )
        assert value == "openshell"

    def test_runtime_mode_env_var_override(self, monkeypatch):
        monkeypatch.setenv("LOGOS_RUNTIME_MODE", "local")
        value = (
            os.environ.get("LOGOS_RUNTIME_MODE")
            or os.environ.get("HERMES_RUNTIME_MODE")
            or "openshell"
        )
        assert value == "local"

    def test_runtime_mode_legacy_hermes_var_still_honored(self, monkeypatch):
        monkeypatch.delenv("LOGOS_RUNTIME_MODE", raising=False)
        monkeypatch.setenv("HERMES_RUNTIME_MODE", "docker")
        value = (
            os.environ.get("LOGOS_RUNTIME_MODE")
            or os.environ.get("HERMES_RUNTIME_MODE")
            or "openshell"
        )
        assert value == "docker"

    def test_http_api_runtime_mode_constant_matches_env(self, monkeypatch):
        """The _RUNTIME_MODE constant in http_api is derived from the env var."""
        monkeypatch.setenv("LOGOS_RUNTIME_MODE", "local")
        derived = (
            os.environ.get("LOGOS_RUNTIME_MODE")
            or os.environ.get("HERMES_RUNTIME_MODE")
            or "openshell"
        )
        assert derived == "local"


# ===========================================================================
# DockerSandboxExecutor tests
# ===========================================================================

# Shared fixture: redirect state + lock files to tmp_path and bypass file lock
# so tests don't interact with the real filesystem or block on fcntl.

@pytest.fixture()
def docker_env(tmp_path, monkeypatch):
    """Set up isolated state/lock files and patch _file_lock to a no-op."""
    state_file = tmp_path / "docker_instances.json"
    lock_file = tmp_path / "docker_instances.lock"
    monkeypatch.setattr("gateway.executors.docker._STATE_FILE", state_file)
    monkeypatch.setattr("gateway.executors.docker._LOCK_FILE", lock_file)

    import contextlib

    @contextlib.contextmanager
    def _noop_lock():
        yield

    monkeypatch.setattr("gateway.executors.docker._file_lock", _noop_lock)
    return state_file


# ---------------------------------------------------------------------------
# TestDockerPortAllocation
# ---------------------------------------------------------------------------

class TestDockerPortAllocation:
    @pytest.fixture(autouse=True)
    def _mock_socket(self, monkeypatch):
        """Prevent real socket bind so tests don't depend on host port state."""
        import socket as _socket
        orig_socket = _socket.socket

        class FakeSocket:
            def __init__(self, *a, **kw):
                pass
            def setsockopt(self, *a):
                pass
            def bind(self, addr):
                pass
            def close(self):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        monkeypatch.setattr("socket.socket", FakeSocket)

    def test_allocates_first_free_port(self):
        from gateway.executors.docker import _allocate_port
        port = _allocate_port([], port_min=8200, port_max=8205)
        assert port == 8200

    def test_skips_used_ports(self):
        from gateway.executors.docker import _allocate_port
        instances = [{"port": 8200}, {"port": 8201}]
        port = _allocate_port(instances, port_min=8200, port_max=8205)
        assert port == 8202

    def test_raises_when_pool_exhausted(self):
        from gateway.executors.docker import _allocate_port
        instances = [{"port": p} for p in range(8200, 8203)]
        with pytest.raises(RuntimeError, match="No free ports"):
            _allocate_port(instances, port_min=8200, port_max=8202)


# ---------------------------------------------------------------------------
# TestDockerContainerRunning
# ---------------------------------------------------------------------------

class TestDockerContainerRunning:
    def test_returns_true_when_running(self):
        from gateway.executors.docker import _container_running
        mock_result = MagicMock(returncode=0, stdout="true\n")
        with patch("gateway.executors.docker._docker", return_value=mock_result):
            assert _container_running("hermes-test") is True

    def test_returns_false_when_stopped(self):
        from gateway.executors.docker import _container_running
        mock_result = MagicMock(returncode=0, stdout="false\n")
        with patch("gateway.executors.docker._docker", return_value=mock_result):
            assert _container_running("hermes-test") is False

    def test_returns_false_when_not_found(self):
        from gateway.executors.docker import _container_running
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("gateway.executors.docker._docker", return_value=mock_result):
            assert _container_running("hermes-gone") is False

    def test_returns_false_on_docker_error(self):
        from gateway.executors.docker import _container_running
        with patch("gateway.executors.docker._docker", side_effect=RuntimeError("docker not found")):
            assert _container_running("hermes-err") is False


# ---------------------------------------------------------------------------
# TestDockerSpawn
# ---------------------------------------------------------------------------

class TestDockerSpawn:
    def _mock_docker_run(self, stdout="abc123def456\n", returncode=0, check=True):
        """Return a side_effect that handles both 'run' and 'inspect' docker calls."""
        def side_effect(*args, **kwargs):
            if args and args[0] == "inspect":
                # _container_running check during prune
                return MagicMock(returncode=1, stdout="")
            result = MagicMock(returncode=returncode, stdout=stdout, stderr="")
            if check and kwargs.get("check", True) and returncode != 0:
                raise subprocess.CalledProcessError(returncode, "docker", stderr="error")
            return result
        return side_effect

    def test_spawn_calls_docker_run(self, docker_env):
        docker_calls = []

        def capture_docker(*args, **kwargs):
            docker_calls.append(args)
            if args and args[0] == "inspect":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="abc123\n", stderr="")

        exe = DockerSandboxExecutor(sandbox_image="test-image")
        with patch("gateway.executors.docker._docker", side_effect=capture_docker), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.spawn(InstanceConfig(name="test-agent"))

        # Find the 'run' call
        run_calls = [c for c in docker_calls if c[0] == "run"]
        assert len(run_calls) == 1
        run_args = run_calls[0]
        assert "-d" in run_args
        assert "--rm" in run_args
        assert "--cap-drop=ALL" in run_args
        assert "--security-opt=no-new-privileges" in run_args
        assert "test-image" in run_args

    def test_spawn_publishes_port_to_localhost_only(self, docker_env):
        docker_calls = []

        def capture_docker(*args, **kwargs):
            docker_calls.append(args)
            if args and args[0] == "inspect":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="abc123\n", stderr="")

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=capture_docker), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.spawn(InstanceConfig(name="port-test"))

        run_calls = [c for c in docker_calls if c[0] == "run"]
        run_args = run_calls[0]
        # Port mapping should bind to 127.0.0.1 only
        port_flag_idx = run_args.index("-p")
        port_mapping = run_args[port_flag_idx + 1]
        assert port_mapping.startswith("127.0.0.1:")
        assert ":8080" in port_mapping

    def test_spawn_passes_env_vars(self, docker_env):
        docker_calls = []

        def capture_docker(*args, **kwargs):
            docker_calls.append(args)
            if args and args[0] == "inspect":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="abc123\n", stderr="")

        exe = DockerSandboxExecutor()
        config = InstanceConfig(
            name="env-test",
            soul_name="philosopher",
            toolsets=["web", "code"],
            policy="WORKSPACE_ONLY",
        )
        with patch("gateway.executors.docker._docker", side_effect=capture_docker), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.spawn(config)

        run_calls = [c for c in docker_calls if c[0] == "run"]
        run_args = run_calls[0]
        # Collect all -e arguments
        env_pairs = []
        for i, arg in enumerate(run_args):
            if arg == "-e" and i + 1 < len(run_args):
                env_pairs.append(run_args[i + 1])
        env_dict = dict(p.split("=", 1) for p in env_pairs)
        assert env_dict["HERMES_INSTANCE_NAME"] == "env-test"
        assert env_dict["LOGOS_PORT"] == "8080"
        assert env_dict["HERMES_PORT"] == "8080"
        assert env_dict["HERMES_SOUL"] == "philosopher"
        assert env_dict["HERMES_TOOLSETS"] == "web,code"
        assert env_dict["HERMES_POLICY_LEVEL"] == "WORKSPACE_ONLY"
        assert "HERMES_GATEWAY_URL" in env_dict

    def test_spawn_does_not_pass_default_soul(self, docker_env):
        """soul_name='default' should NOT set HERMES_SOUL env var."""
        docker_calls = []

        def capture_docker(*args, **kwargs):
            docker_calls.append(args)
            if args and args[0] == "inspect":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="abc123\n", stderr="")

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=capture_docker), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.spawn(InstanceConfig(name="default-soul", soul_name="default"))

        run_calls = [c for c in docker_calls if c[0] == "run"]
        run_args = run_calls[0]
        env_pairs = []
        for i, arg in enumerate(run_args):
            if arg == "-e" and i + 1 < len(run_args):
                env_pairs.append(run_args[i + 1])
        env_keys = [p.split("=", 1)[0] for p in env_pairs]
        assert "HERMES_SOUL" not in env_keys

    def test_spawn_saves_to_state_file(self, docker_env):
        state_file = docker_env
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=self._mock_docker_run()), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.spawn(InstanceConfig(name="persist-test", soul_name="default", model="gpt-4o"))

        saved = json.loads(state_file.read_text())
        assert len(saved) == 1
        assert saved[0]["name"] == "persist-test"
        assert saved[0]["source"] == "docker"
        assert saved[0]["container_name"] == "hermes-persist-test"
        assert saved[0]["model"] == "gpt-4o"

    def test_spawn_returns_spawned_instance(self, docker_env):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=self._mock_docker_run()), \
             patch("gateway.executors.docker._health_check", return_value=True):
            result = exe.spawn(InstanceConfig(
                name="ret-test", soul_name="sage", model="claude-3", requester="alice",
            ))

        assert isinstance(result, SpawnedInstance)
        assert result.name == "ret-test"
        assert result.source == "docker"
        assert result.soul_name == "sage"
        assert result.model == "claude-3"
        assert result.requester == "alice"
        assert result.healthy is True
        assert result.url.startswith("http://127.0.0.1:")

    def test_spawn_uses_explicit_port(self, docker_env):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=self._mock_docker_run()), \
             patch("gateway.executors.docker._health_check", return_value=True):
            result = exe.spawn(InstanceConfig(name="explicit-port", port=9500))

        assert result.port == 9500
        assert result.url == "http://127.0.0.1:9500"

    def test_spawn_prunes_stopped_containers(self, docker_env):
        state_file = docker_env
        # Pre-populate with a "dead" container
        state_file.write_text(json.dumps([
            {"name": "dead", "container_name": "hermes-dead", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": "",
             "toolsets": [], "policy": "", "sandbox_image": "img",
             "container_id": "dead123"}
        ]))

        def docker_side(*args, **kwargs):
            if args and args[0] == "inspect":
                # hermes-dead is not running
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="new123\n", stderr="")

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=docker_side), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.spawn(InstanceConfig(name="fresh"))

        saved = json.loads(state_file.read_text())
        names = [i["name"] for i in saved]
        assert "dead" not in names
        assert "fresh" in names

    def test_spawn_reports_unhealthy(self, docker_env, caplog):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=self._mock_docker_run()), \
             patch("gateway.executors.docker._health_check", return_value=False):
            result = exe.spawn(InstanceConfig(name="sick"))

        assert result.healthy is False
        assert any("did not become healthy" in r.message for r in caplog.records)

    def test_spawn_raises_on_docker_failure(self, docker_env):
        def docker_fail(*args, **kwargs):
            if args and args[0] == "inspect":
                return MagicMock(returncode=1, stdout="")
            raise subprocess.CalledProcessError(1, "docker", stderr="image not found")

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=docker_fail):
            with pytest.raises(RuntimeError, match="Failed to create Docker sandbox"):
                exe.spawn(InstanceConfig(name="fail"))


# ---------------------------------------------------------------------------
# TestDockerListInstances
# ---------------------------------------------------------------------------

class TestDockerListInstances:
    def test_returns_empty_when_no_file(self, docker_env):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._container_running", return_value=False):
            assert exe.list_instances() == []

    def test_returns_running_containers(self, docker_env):
        state_file = docker_env
        state_file.write_text(json.dumps([
            {"name": "a", "container_name": "hermes-a", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
            {"name": "b", "container_name": "hermes-b", "port": 8201,
             "url": "http://127.0.0.1:8201", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
        ]))

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._container_running", return_value=True), \
             patch("gateway.executors.docker._health_check", return_value=True):
            result = exe.list_instances()

        assert len(result) == 2
        assert {r["name"] for r in result} == {"a", "b"}
        assert all(r["healthy"] for r in result)

    def test_prunes_stopped_containers(self, docker_env):
        state_file = docker_env
        state_file.write_text(json.dumps([
            {"name": "alive", "container_name": "hermes-alive", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
            {"name": "dead", "container_name": "hermes-dead", "port": 8201,
             "url": "http://127.0.0.1:8201", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
        ]))

        def running_side(cn):
            return cn == "hermes-alive"

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._container_running", side_effect=running_side), \
             patch("gateway.executors.docker._health_check", return_value=True):
            result = exe.list_instances()

        assert len(result) == 1
        assert result[0]["name"] == "alive"
        # Confirm pruned from file
        saved = json.loads(state_file.read_text())
        assert len(saved) == 1
        assert saved[0]["name"] == "alive"

    def test_does_not_rewrite_when_no_change(self, docker_env):
        state_file = docker_env
        data = [{"name": "ok", "container_name": "hermes-ok", "port": 8200,
                 "url": "http://127.0.0.1:8200", "source": "docker",
                 "soul_name": "default", "model": "", "requester": ""}]
        state_file.write_text(json.dumps(data))
        mtime_before = state_file.stat().st_mtime_ns

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._container_running", return_value=True), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.list_instances()

        # File should not have been rewritten (no pruning needed)
        assert state_file.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# TestDockerDeleteInstance
# ---------------------------------------------------------------------------

class TestDockerDeleteInstance:
    def test_delete_calls_docker_stop(self, docker_env):
        state_file = docker_env
        state_file.write_text(json.dumps([
            {"name": "target", "container_name": "hermes-target", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
        ]))

        docker_calls = []

        def capture_docker(*args, **kwargs):
            docker_calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=capture_docker):
            exe.delete_instance("target")

        stop_calls = [c for c in docker_calls if c[0] == "stop"]
        assert len(stop_calls) == 1
        assert stop_calls[0] == ("stop", "hermes-target")

    def test_delete_removes_from_state_file(self, docker_env):
        state_file = docker_env
        state_file.write_text(json.dumps([
            {"name": "gone", "container_name": "hermes-gone", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
            {"name": "keep", "container_name": "hermes-keep", "port": 8201,
             "url": "http://127.0.0.1:8201", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
        ]))

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", return_value=MagicMock()):
            exe.delete_instance("gone")

        saved = json.loads(state_file.read_text())
        names = [i["name"] for i in saved]
        assert "gone" not in names
        assert "keep" in names

    def test_delete_unknown_name_no_error(self, docker_env):
        state_file = docker_env
        state_file.write_text(json.dumps([
            {"name": "exists", "container_name": "hermes-exists", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
        ]))

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", return_value=MagicMock()):
            exe.delete_instance("nonexistent")

        saved = json.loads(state_file.read_text())
        assert len(saved) == 1
        assert saved[0]["name"] == "exists"

    def test_delete_tolerates_docker_stop_failure(self, docker_env):
        state_file = docker_env
        state_file.write_text(json.dumps([
            {"name": "stubborn", "container_name": "hermes-stubborn", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
        ]))

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=RuntimeError("timeout")):
            # Should not raise — error is logged and instance is still removed from state
            exe.delete_instance("stubborn")

        saved = json.loads(state_file.read_text())
        assert len(saved) == 0


# ---------------------------------------------------------------------------
# TestDockerGetHeadroom
# ---------------------------------------------------------------------------

class TestDockerGetHeadroom:
    def test_can_spawn_when_daemon_reachable(self):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", return_value=MagicMock()):
            headroom = exe.get_headroom()

        assert headroom.can_spawn is True
        assert headroom.reason == ""

    def test_cannot_spawn_when_daemon_unreachable(self):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=RuntimeError("no docker")):
            headroom = exe.get_headroom()

        assert headroom.can_spawn is False
        assert "not reachable" in headroom.reason


# ---------------------------------------------------------------------------
# TestDockerGetResources
# ---------------------------------------------------------------------------

class TestDockerGetResources:
    def test_returns_executor_field(self):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", return_value=MagicMock()):
            res = exe.get_resources()

        assert res["executor"] == "docker"
        assert res["can_spawn"] is True

    def test_reports_cannot_spawn_when_daemon_down(self):
        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", side_effect=RuntimeError("nope")):
            res = exe.get_resources()

        assert res["can_spawn"] is False


# ---------------------------------------------------------------------------
# TestDockerFileLock
# ---------------------------------------------------------------------------

class TestDockerFileLock:
    def test_lock_is_acquired_during_spawn(self, tmp_path, monkeypatch):
        """Verify that spawn() calls _file_lock (not the no-op fixture version)."""
        state_file = tmp_path / "docker_instances.json"
        lock_file = tmp_path / "docker_instances.lock"
        monkeypatch.setattr("gateway.executors.docker._STATE_FILE", state_file)
        monkeypatch.setattr("gateway.executors.docker._LOCK_FILE", lock_file)

        lock_acquired = []

        import contextlib
        from gateway.executors import docker as docker_mod

        original_lock = docker_mod._file_lock

        @contextlib.contextmanager
        def tracking_lock():
            lock_acquired.append("spawn")
            with original_lock():
                yield

        monkeypatch.setattr("gateway.executors.docker._file_lock", tracking_lock)

        exe = DockerSandboxExecutor()

        def docker_side(*args, **kwargs):
            if args and args[0] == "inspect":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="abc123\n", stderr="")

        with patch("gateway.executors.docker._docker", side_effect=docker_side), \
             patch("gateway.executors.docker._health_check", return_value=True):
            exe.spawn(InstanceConfig(name="lock-test"))

        assert "spawn" in lock_acquired

    def test_lock_is_acquired_during_delete(self, tmp_path, monkeypatch):
        state_file = tmp_path / "docker_instances.json"
        lock_file = tmp_path / "docker_instances.lock"
        state_file.write_text(json.dumps([
            {"name": "x", "container_name": "hermes-x", "port": 8200,
             "url": "http://127.0.0.1:8200", "source": "docker",
             "soul_name": "default", "model": "", "requester": ""},
        ]))
        monkeypatch.setattr("gateway.executors.docker._STATE_FILE", state_file)
        monkeypatch.setattr("gateway.executors.docker._LOCK_FILE", lock_file)

        lock_acquired = []

        import contextlib
        from gateway.executors import docker as docker_mod

        original_lock = docker_mod._file_lock

        @contextlib.contextmanager
        def tracking_lock():
            lock_acquired.append("delete")
            with original_lock():
                yield

        monkeypatch.setattr("gateway.executors.docker._file_lock", tracking_lock)

        exe = DockerSandboxExecutor()
        with patch("gateway.executors.docker._docker", return_value=MagicMock()):
            exe.delete_instance("x")

        assert "delete" in lock_acquired


# ===========================================================================
# Multi-instance naming tests
# ===========================================================================

class TestSafeK8sNameMultiInstance:
    """Verify safe_k8s_name supports instance labels for multi-instance."""

    def test_with_label(self):
        from gateway.executors.k8s_helpers import safe_k8s_name
        result = safe_k8s_name("greg", "researcher")
        assert result == "hermes-greg-researcher"

    def test_without_label(self):
        from gateway.executors.k8s_helpers import safe_k8s_name
        result = safe_k8s_name("greg")
        assert result == "hermes-greg"

    def test_empty_label(self):
        from gateway.executors.k8s_helpers import safe_k8s_name
        result = safe_k8s_name("greg", "")
        assert result == "hermes-greg"

    def test_label_sanitised(self):
        from gateway.executors.k8s_helpers import safe_k8s_name
        result = safe_k8s_name("Greg Palos", "My Researcher!")
        assert result == "hermes-greg-palos-my-researcher"
        # Should only contain valid k8s chars
        import re
        assert re.match(r"^hermes-[a-z0-9-]+$", result)

    def test_same_requester_different_labels_distinct(self):
        from gateway.executors.k8s_helpers import safe_k8s_name
        a = safe_k8s_name("alice", "coder")
        b = safe_k8s_name("alice", "researcher")
        c = safe_k8s_name("alice", "sysadmin")
        assert len({a, b, c}) == 3

    def test_truncation_at_52(self):
        from gateway.executors.k8s_helpers import safe_k8s_name
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


class TestBuildExecutorMultiInstance:
    """build_executor still works for all modes after InstanceConfig changes."""

    def test_docker_mode(self):
        exe = build_executor("docker")
        assert isinstance(exe, DockerSandboxExecutor)
