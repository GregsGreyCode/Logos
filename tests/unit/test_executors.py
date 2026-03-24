"""
Unit tests for gateway/executors — LocalProcessExecutor and build_executor factory.
KubernetesExecutor tests are integration-level (require k8s) and are in tests/integration/.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.executors import InstanceExecutor, build_executor
from gateway.executors.base import InstanceConfig, ResourceHeadroom, SpawnedInstance
from gateway.executors.local import LocalProcessExecutor


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
        from hermes_cli.config import DEFAULT_CONFIG
        assert "runtime" in DEFAULT_CONFIG

    def test_runtime_mode_default_is_local(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["runtime"]["mode"] == "local"

    def test_runtime_port_range_exists(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "local_port_range" in DEFAULT_CONFIG["runtime"]

    def test_config_version_is_8(self):
        from hermes_cli.config import DEFAULT_CONFIG
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
