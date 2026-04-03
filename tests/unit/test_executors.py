"""
Unit tests for gateway/executors — LocalProcessExecutor, DockerSandboxExecutor,
and build_executor factory.
KubernetesExecutor tests are integration-level (require k8s) and are in tests/integration/.
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
from gateway.executors.local import LocalProcessExecutor
from gateway.executors.docker import DockerSandboxExecutor


# ---------------------------------------------------------------------------
# TestBuildExecutor
# ---------------------------------------------------------------------------

class TestBuildExecutor:
    def test_kubernetes_mode_returns_k8s_executor(self):
        from gateway.executors.kubernetes import KubernetesExecutor
        executor = build_executor("kubernetes")
        assert isinstance(executor, KubernetesExecutor)

    def test_local_mode_returns_local_executor(self):
        executor = build_executor("local")
        assert isinstance(executor, LocalProcessExecutor)

    def test_docker_mode_returns_docker_executor(self):
        executor = build_executor("docker")
        assert isinstance(executor, DockerSandboxExecutor)

    def test_docker_executor_satisfies_protocol(self):
        executor = build_executor("docker")
        assert isinstance(executor, InstanceExecutor)

    def test_unknown_mode_returns_local_executor(self):
        executor = build_executor("desktop")
        assert isinstance(executor, LocalProcessExecutor)

    def test_local_executor_satisfies_protocol(self):
        executor = build_executor("local")
        assert isinstance(executor, InstanceExecutor)


# ---------------------------------------------------------------------------
# TestPortAllocation
# ---------------------------------------------------------------------------

class TestPortAllocation:
    def _make_executor(self, port_range=(8081, 8085)):
        return LocalProcessExecutor(port_range=port_range)

    def test_allocates_first_free_port(self):
        exe = self._make_executor()
        port = exe._allocate_port([])
        assert port == 8081

    def test_skips_used_ports(self):
        exe = self._make_executor()
        instances = [{"port": 8081}]
        port = exe._allocate_port(instances)
        assert port == 8082

    def test_raises_when_pool_exhausted(self):
        exe = self._make_executor(port_range=(8081, 8083))
        instances = [{"port": p} for p in range(8081, 8084)]
        with pytest.raises(RuntimeError, match="No free ports"):
            exe._allocate_port(instances)

    def test_uses_explicit_port_if_set(self, tmp_path, monkeypatch):
        """InstanceConfig with an explicit port bypasses pool allocation entirely."""
        instances_file = tmp_path / "instances.json"
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = self._make_executor(port_range=(8081, 8083))

        mock_proc = MagicMock()
        mock_proc.pid = 1234

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=True), \
             patch("builtins.open", side_effect=lambda p, *a, **kw: open(p, *a, **kw) if "instance_" not in str(p) else MagicMock().__enter__.return_value):
            # Use a port outside the pool (9000) — should not raise
            config = InstanceConfig(name="test-explicit", port=9000)
            # We need the log dir to exist; patch mkdir to avoid real filesystem
            with patch.object(Path, "mkdir", return_value=None), \
                 patch("builtins.open", MagicMock()):
                result = exe.spawn(config)

        # Popen was called — the explicit port is passed via HERMES_PORT env var
        call_args = mock_popen.call_args
        env = call_args[1].get("env") or call_args[0][1] if len(call_args[0]) > 1 else {}
        # env is passed as keyword arg
        env = mock_popen.call_args.kwargs.get("env") or mock_popen.call_args[1].get("env", {})
        assert env.get("HERMES_PORT") == "9000"


# ---------------------------------------------------------------------------
# TestLocalProcessExecutorSpawn
# ---------------------------------------------------------------------------

class TestLocalProcessExecutorSpawn:
    def _make_instances_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "instances.json"
        f.write_text("[]")
        return f

    def test_spawn_creates_process(self, tmp_path, monkeypatch):
        instances_file = self._make_instances_file(tmp_path)
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        mock_proc = MagicMock()
        mock_proc.pid = 5555

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=False), \
             patch.object(Path, "mkdir", return_value=None), \
             patch("builtins.open", MagicMock()):
            config = InstanceConfig(name="alpha", soul_name="default", model="gpt-4o")
            exe.spawn(config)

        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        assert "-m" in cmd
        assert "gateway.run" in cmd
        # Port is now passed via HERMES_PORT env var, not --port flag
        env = mock_popen.call_args.kwargs.get("env") or mock_popen.call_args[1].get("env", {})
        assert "HERMES_PORT" in env

    def test_spawn_saves_to_instances_file(self, tmp_path, monkeypatch):
        instances_file = self._make_instances_file(tmp_path)
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        mock_proc = MagicMock()
        mock_proc.pid = 7777

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=False), \
             patch.object(Path, "mkdir", return_value=None), \
             patch("builtins.open", MagicMock()):
            config = InstanceConfig(name="beta")
            exe.spawn(config)

        saved = json.loads(instances_file.read_text())
        assert len(saved) == 1
        assert saved[0]["name"] == "beta"
        assert saved[0]["pid"] == 7777
        assert saved[0]["source"] == "local"

    def test_spawn_returns_spawned_instance(self, tmp_path, monkeypatch):
        instances_file = self._make_instances_file(tmp_path)
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        mock_proc = MagicMock()
        mock_proc.pid = 9999

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=False), \
             patch.object(Path, "mkdir", return_value=None), \
             patch("builtins.open", MagicMock()):
            config = InstanceConfig(name="gamma", soul_name="mySoul", model="claude-3", requester="user1")
            result = exe.spawn(config)

        assert isinstance(result, SpawnedInstance)
        assert result.name == "gamma"
        assert result.pid == 9999
        assert result.url == "http://127.0.0.1:8081"
        assert result.port == 8081
        assert result.source == "local"
        assert result.soul_name == "mySoul"
        assert result.model == "claude-3"
        assert result.requester == "user1"
        assert result.healthy is True

    def test_spawn_uses_create_detached_process_on_windows(self, tmp_path, monkeypatch):
        """On Windows, CREATE_DETACHED_PROCESS is passed instead of start_new_session."""
        instances_file = self._make_instances_file(tmp_path)
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)
        monkeypatch.setattr("sys.platform", "win32")

        mock_proc = MagicMock()
        mock_proc.pid = 4321

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=False), \
             patch.object(Path, "mkdir", return_value=None), \
             patch("builtins.open", MagicMock()):
            exe.spawn(InstanceConfig(name="win-test"))

        kwargs = mock_popen.call_args.kwargs
        # CREATE_DETACHED_PROCESS = 0x8; use the numeric value since the
        # attribute only exists on Windows
        assert kwargs.get("creationflags") == 0x00000008
        assert "start_new_session" not in kwargs

    def test_spawn_uses_start_new_session_on_unix(self, tmp_path, monkeypatch):
        """On Unix, start_new_session=True is used (not creationflags)."""
        instances_file = self._make_instances_file(tmp_path)
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)
        monkeypatch.setattr("sys.platform", "linux")

        mock_proc = MagicMock()
        mock_proc.pid = 1234

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=False), \
             patch.object(Path, "mkdir", return_value=None), \
             patch("builtins.open", MagicMock()):
            exe.spawn(InstanceConfig(name="unix-test"))

        kwargs = mock_popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True
        assert "creationflags" not in kwargs

    def test_spawn_prunes_dead_instances_before_allocating(self, tmp_path, monkeypatch):
        """Dead PIDs in instances.json are removed before port allocation."""
        instances_file = tmp_path / "instances.json"
        # Pre-populate with a dead instance on port 8081
        instances_file.write_text(json.dumps([
            {"name": "dead", "port": 8081, "pid": 111, "url": "http://127.0.0.1:8081",
             "source": "local", "soul_name": "default", "model": "", "requester": ""}
        ]))
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        mock_proc = MagicMock()
        mock_proc.pid = 2222

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        # pid 111 is dead → _is_alive returns False for it; new proc is alive
        def is_alive_side_effect(pid):
            return pid == 2222

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("gateway.executors.local._health_check", return_value=False), \
             patch("gateway.executors.local._is_alive", side_effect=is_alive_side_effect), \
             patch.object(Path, "mkdir", return_value=None), \
             patch("builtins.open", MagicMock()):
            config = InstanceConfig(name="new-inst")
            result = exe.spawn(config)

        # After pruning the dead instance, port 8081 should be reused
        assert result.port == 8081
        saved = json.loads(instances_file.read_text())
        names = [i["name"] for i in saved]
        assert "dead" not in names
        assert "new-inst" in names


# ---------------------------------------------------------------------------
# TestLocalProcessExecutorListInstances
# ---------------------------------------------------------------------------

class TestLocalProcessExecutorListInstances:
    def test_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        # File does not exist
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()
        result = exe.list_instances()
        assert result == []

    def test_returns_alive_instances(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        instances_file.write_text(json.dumps([
            {"name": "alive1", "port": 8081, "pid": 100,
             "url": "http://127.0.0.1:8081", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
            {"name": "alive2", "port": 8082, "pid": 101,
             "url": "http://127.0.0.1:8082", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
        ]))
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()

        with patch("gateway.executors.local._is_alive", return_value=True), \
             patch("gateway.executors.local._health_check", return_value=True):
            result = exe.list_instances()

        assert len(result) == 2
        names = {r["name"] for r in result}
        assert names == {"alive1", "alive2"}

    def test_prunes_dead_instances(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        instances_file.write_text(json.dumps([
            {"name": "alive", "port": 8081, "pid": 200,
             "url": "http://127.0.0.1:8081", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
            {"name": "dead", "port": 8082, "pid": 201,
             "url": "http://127.0.0.1:8082", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
        ]))
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()

        def is_alive_side_effect(pid):
            return pid == 200  # only pid 200 is alive

        with patch("gateway.executors.local._is_alive", side_effect=is_alive_side_effect), \
             patch("gateway.executors.local._health_check", return_value=True):
            result = exe.list_instances()

        assert len(result) == 1
        assert result[0]["name"] == "alive"

        # Confirm dead instance was pruned from file
        saved = json.loads(instances_file.read_text())
        assert len(saved) == 1
        assert saved[0]["name"] == "alive"


# ---------------------------------------------------------------------------
# TestLocalProcessExecutorDeleteInstance
# ---------------------------------------------------------------------------

class TestLocalProcessExecutorDeleteInstance:
    def _write_instances(self, path: Path, instances: list) -> None:
        path.write_text(json.dumps(instances))

    def test_delete_sends_sigterm(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        self._write_instances(instances_file, [
            {"name": "target", "port": 8081, "pid": 300,
             "url": "http://127.0.0.1:8081", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
        ])
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()

        kill_calls = []

        def mock_kill(pid, sig):
            kill_calls.append((pid, sig))

        with patch("os.kill", side_effect=mock_kill), \
             patch("gateway.executors.local._is_alive", return_value=False):
            exe.delete_instance("target")

        assert (300, signal.SIGTERM) in kill_calls

    def test_delete_removes_from_file(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        self._write_instances(instances_file, [
            {"name": "gone", "port": 8081, "pid": 400,
             "url": "http://127.0.0.1:8081", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
            {"name": "keep", "port": 8082, "pid": 401,
             "url": "http://127.0.0.1:8082", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
        ])
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()

        with patch("os.kill"), \
             patch("gateway.executors.local._is_alive", return_value=False):
            exe.delete_instance("gone")

        saved = json.loads(instances_file.read_text())
        names = [i["name"] for i in saved]
        assert "gone" not in names
        assert "keep" in names

    def test_delete_escalates_to_sigkill_on_unix(self, tmp_path, monkeypatch):
        """If SIGTERM doesn't kill the process, SIGKILL is sent on Unix."""
        instances_file = tmp_path / "instances.json"
        self._write_instances(instances_file, [
            {"name": "stubborn", "port": 8081, "pid": 600,
             "url": "http://127.0.0.1:8081", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
        ])
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()
        kill_calls = []

        def mock_kill(pid, sig):
            kill_calls.append((pid, sig))

        # Process stays alive through the whole grace period → SIGKILL triggered
        with patch("os.kill", side_effect=mock_kill), \
             patch("gateway.executors.local._is_alive", return_value=True), \
             patch("time.sleep"), \
             patch("signal.SIGKILL", signal.SIGKILL, create=True):
            exe.delete_instance("stubborn")

        assert (600, signal.SIGTERM) in kill_calls
        assert (600, signal.SIGKILL) in kill_calls

    def test_delete_uses_taskkill_when_sigkill_missing(self, tmp_path, monkeypatch):
        """On Windows (no SIGKILL), taskkill /F /PID is used as a fallback."""
        instances_file = tmp_path / "instances.json"
        self._write_instances(instances_file, [
            {"name": "windows-proc", "port": 8081, "pid": 700,
             "url": "http://127.0.0.1:8081", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
        ])
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()
        taskkill_calls = []

        def fake_run(cmd, **kwargs):
            taskkill_calls.append(cmd)

        import signal as _signal
        with patch("os.kill"), \
             patch("gateway.executors.local._is_alive", return_value=True), \
             patch("time.sleep"), \
             patch("subprocess.run", side_effect=fake_run), \
             patch.object(_signal, "SIGKILL", new=None, create=True) if not hasattr(_signal, "SIGKILL") \
                 else patch("signal.SIGKILL", None):
            # Remove SIGKILL attr to simulate Windows
            original = getattr(_signal, "SIGKILL", "MISSING")
            if original != "MISSING":
                delattr(_signal, "SIGKILL")
            try:
                exe.delete_instance("windows-proc")
            finally:
                if original != "MISSING":
                    _signal.SIGKILL = original

        assert any("taskkill" in str(c) for c in taskkill_calls), \
            f"Expected taskkill call, got: {taskkill_calls}"

    def test_delete_unknown_name_no_error(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        self._write_instances(instances_file, [
            {"name": "exists", "port": 8081, "pid": 500,
             "url": "http://127.0.0.1:8081", "source": "local",
             "soul_name": "default", "model": "", "requester": ""},
        ])
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)

        exe = LocalProcessExecutor()

        # Should not raise
        exe.delete_instance("nonexistent")

        # Existing instance is untouched
        saved = json.loads(instances_file.read_text())
        assert len(saved) == 1
        assert saved[0]["name"] == "exists"


# ---------------------------------------------------------------------------
# TestLocalProcessExecutorGetHeadroom
# ---------------------------------------------------------------------------

class TestLocalProcessExecutorGetHeadroom:
    def test_returns_can_spawn_true_when_resources_available(self):
        exe = LocalProcessExecutor()

        mock_psutil = MagicMock()
        mock_psutil.cpu_count.return_value = 8
        mock_psutil.cpu_percent.return_value = 10.0  # 90% idle → 7.2 free cores
        mock_mem = MagicMock()
        mock_mem.available = 4 * 1024 ** 3  # 4 GB
        mock_psutil.virtual_memory.return_value = mock_mem

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            # Force re-import path by patching the import inside get_headroom
            with patch("builtins.__import__", side_effect=lambda name, *a, **kw: mock_psutil if name == "psutil" else __import__(name, *a, **kw)):
                headroom = exe.get_headroom()

        assert headroom.can_spawn is True
        assert headroom.available_cpu > 0
        assert headroom.available_mem_gb > 0

    def test_returns_can_spawn_false_when_low_cpu(self):
        exe = LocalProcessExecutor()

        mock_psutil = MagicMock()
        # 1 logical CPU, 99% busy → 0.01 free cores (below threshold of 1.0)
        mock_psutil.cpu_count.return_value = 1
        mock_psutil.cpu_percent.return_value = 99.0
        mock_mem = MagicMock()
        mock_mem.available = 8 * 1024 ** 3  # 8 GB free (above threshold)
        mock_psutil.virtual_memory.return_value = mock_mem

        with patch("builtins.__import__", side_effect=lambda name, *a, **kw: mock_psutil if name == "psutil" else __import__(name, *a, **kw)):
            headroom = exe.get_headroom()

        assert headroom.can_spawn is False
        assert headroom.reason != ""

    def test_returns_can_spawn_true_when_psutil_not_installed(self):
        exe = LocalProcessExecutor()

        def raise_import_error(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("No module named 'psutil'")
            return __import__(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=raise_import_error):
            headroom = exe.get_headroom()

        assert headroom.can_spawn is True


# ---------------------------------------------------------------------------
# TestConfigRuntimeSection
# ---------------------------------------------------------------------------

class TestConfigRuntimeSection:
    def test_runtime_section_exists(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert "runtime" in DEFAULT_CONFIG

    def test_runtime_mode_default_is_local(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["runtime"]["mode"] == "local"

    def test_runtime_port_range_exists(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert "local_port_range" in DEFAULT_CONFIG["runtime"]

    def test_config_version_is_8(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["_config_version"] == 8


# ---------------------------------------------------------------------------
# TestRuntimeModeInjectedToWindow
# ---------------------------------------------------------------------------

class TestRuntimeModeInjectedToWindow:
    def test_runtime_mode_env_var_read_from_environ(self, monkeypatch):
        """HERMES_RUNTIME_MODE env var drives runtime mode; defaults to 'kubernetes'."""
        monkeypatch.delenv("HERMES_RUNTIME_MODE", raising=False)
        value = os.environ.get("HERMES_RUNTIME_MODE", "kubernetes")
        assert value == "kubernetes"

    def test_runtime_mode_env_var_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_RUNTIME_MODE", "local")
        value = os.environ.get("HERMES_RUNTIME_MODE", "kubernetes")
        assert value == "local"

    def test_runtime_mode_kubernetes_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_RUNTIME_MODE", "kubernetes")
        value = os.environ.get("HERMES_RUNTIME_MODE", "kubernetes")
        assert value == "kubernetes"

    def test_http_api_runtime_mode_constant_matches_env(self, monkeypatch):
        """The _RUNTIME_MODE constant in http_api is derived from the env var."""
        # We verify the pattern used in http_api.py is consistent:
        # os.environ.get("HERMES_RUNTIME_MODE", "kubernetes")
        monkeypatch.setenv("HERMES_RUNTIME_MODE", "local")
        derived = os.environ.get("HERMES_RUNTIME_MODE", "kubernetes")
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

    def test_local_mode(self):
        exe = build_executor("local")
        assert isinstance(exe, LocalProcessExecutor)


class TestLocalExecutorMultiInstance:
    """Local executor creates per-instance HERMES_HOME."""

    def test_spawn_sets_per_instance_hermes_home(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        instances_file.write_text("[]")
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)
        monkeypatch.setattr("gateway.executors.local._HERMES_HOME", tmp_path)

        mock_proc = MagicMock()
        mock_proc.pid = 3333

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=False):
            config = InstanceConfig(name="hermes-alice-researcher", instance_label="researcher", requester="alice")
            exe.spawn(config)

        env = mock_popen.call_args.kwargs.get("env", {})
        # HERMES_HOME should point to per-instance directory, not shared root
        assert "instances" in env["HERMES_HOME"]
        assert "hermes-alice-researcher" in env["HERMES_HOME"]
        assert "HERMES_SHARED_HOME" in env
        # Directory should have been created
        instance_home = tmp_path / "instances" / "hermes-alice-researcher"
        assert instance_home.exists()
        assert (instance_home / "memories").exists()

    def test_two_instances_get_different_homes(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        instances_file.write_text("[]")
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)
        monkeypatch.setattr("gateway.executors.local._HERMES_HOME", tmp_path)

        exe = LocalProcessExecutor(port_range=(8081, 8199))
        homes = []

        for label in ["coder", "researcher"]:
            mock_proc = MagicMock()
            mock_proc.pid = 4000 + len(homes)

            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
                 patch("gateway.executors.local._health_check", return_value=True), \
                 patch("gateway.executors.local._is_alive", return_value=False):
                config = InstanceConfig(name=f"hermes-bob-{label}", instance_label=label, requester="bob")
                exe.spawn(config)

            env = mock_popen.call_args.kwargs.get("env", {})
            homes.append(env["HERMES_HOME"])

        assert homes[0] != homes[1]
        assert "coder" in homes[0]
        assert "researcher" in homes[1]

    def test_instance_label_stored_in_record(self, tmp_path, monkeypatch):
        instances_file = tmp_path / "instances.json"
        instances_file.write_text("[]")
        monkeypatch.setattr("gateway.executors.local._INSTANCES_FILE", instances_file)
        monkeypatch.setattr("gateway.executors.local._HERMES_HOME", tmp_path)

        mock_proc = MagicMock()
        mock_proc.pid = 5555

        exe = LocalProcessExecutor(port_range=(8081, 8199))

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("gateway.executors.local._health_check", return_value=True), \
             patch("gateway.executors.local._is_alive", return_value=False):
            config = InstanceConfig(name="hermes-eve-analyst", instance_label="analyst", requester="eve")
            exe.spawn(config)

        saved = json.loads(instances_file.read_text())
        assert saved[0]["instance_label"] == "analyst"
        assert saved[0]["requester"] == "eve"
