"""Unit tests for v2 memory parity:
- ``gateway.worker_registry_v2.sync_memories_from_sandbox`` (download hook)
- ``gateway.executors.hermes_server_mode.deploy_agent_memories`` (upload helper)

These cover the plumbing that makes v2 durable across sandbox resets —
without it, flipping v2 as the default dispatch silently loses every
agent memory when the sandbox is bounced or resurrected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# deploy_agent_memories
# ---------------------------------------------------------------------------


class TestDeployAgentMemories:
    def test_missing_host_dir_is_noop(self, tmp_path, monkeypatch):
        from gateway.executors import hermes_server_mode as hm
        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        fake_upload = MagicMock()
        monkeypatch.setattr(hm, "_upload_file_via_openshell", fake_upload)

        n = hm.deploy_agent_memories("hermes-nobody", "nobody")
        assert n == 0
        fake_upload.assert_not_called()

    def test_empty_host_dir_is_noop(self, tmp_path, monkeypatch):
        from gateway.executors import hermes_server_mode as hm
        mem_dir = tmp_path / "agents" / "alice" / "memories"
        mem_dir.mkdir(parents=True)

        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        fake_upload = MagicMock()
        monkeypatch.setattr(hm, "_upload_file_via_openshell", fake_upload)

        n = hm.deploy_agent_memories("hermes-alice", "alice")
        assert n == 0
        fake_upload.assert_not_called()

    def test_uploads_each_file_to_correct_remote_dir(self, tmp_path, monkeypatch):
        from gateway.executors import hermes_server_mode as hm
        mem_dir = tmp_path / "agents" / "alice" / "memories"
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text("# alice memories\n")
        (mem_dir / "entity_bob.md").write_text("bob is a cat\n")
        # Stray directory — should be skipped.
        (mem_dir / "subdir").mkdir()

        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        fake_upload = MagicMock()
        monkeypatch.setattr(hm, "_upload_file_via_openshell", fake_upload)

        n = hm.deploy_agent_memories(
            "hermes-alice", "alice", gateway="cluster-a",
        )
        assert n == 2
        assert fake_upload.call_count == 2
        for call in fake_upload.call_args_list:
            args, kwargs = call
            assert args[0] == "hermes-alice"
            assert isinstance(args[1], Path)
            assert args[2] == "/tmp/hermes-srv-home/memories"
            assert kwargs["gateway"] == "cluster-a"

    def test_empty_agent_name_is_noop(self, tmp_path, monkeypatch):
        from gateway.executors import hermes_server_mode as hm
        fake_upload = MagicMock()
        monkeypatch.setattr(hm, "_upload_file_via_openshell", fake_upload)

        n = hm.deploy_agent_memories("hermes-nobody", "")
        assert n == 0
        fake_upload.assert_not_called()


# ---------------------------------------------------------------------------
# sync_memories_from_sandbox
# ---------------------------------------------------------------------------


def _stat_ok(mtime: float) -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = f"{mtime}\n"
    r.stderr = ""
    return r


def _stat_fail() -> MagicMock:
    r = MagicMock()
    r.returncode = 1
    r.stdout = ""
    r.stderr = "stat: no such file"
    return r


def _download_ok() -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


class TestSyncMemoriesFromSandbox:
    def _state_with(self, sandbox_name: str, agent_name: str, gateway: str = "cluster-a"):
        return [
            {
                "sandbox_name": sandbox_name,
                "name": agent_name,
                "openshell_name": gateway,
            }
        ]

    def test_unknown_sandbox_is_noop(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state", lambda: [],
        )
        fake_run = MagicMock()
        monkeypatch.setattr("subprocess.run", fake_run)

        asyncio.run(v2.sync_memories_from_sandbox("hermes-ghost"))

        fake_run.assert_not_called()

    def test_stat_failure_skips_download(self, tmp_path, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: self._state_with("hermes-alice", "alice"),
        )
        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        v2._MEMORY_MTIMES.pop("hermes-alice", None)

        fake_run = MagicMock(return_value=_stat_fail())
        monkeypatch.setattr("subprocess.run", fake_run)

        asyncio.run(v2.sync_memories_from_sandbox("hermes-alice"))

        # Only stat was attempted; no download follow-up.
        assert fake_run.call_count == 1
        cmd = fake_run.call_args_list[0][0][0]
        assert "stat" in cmd

    def test_unchanged_mtime_skips_download(self, tmp_path, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: self._state_with("hermes-alice", "alice"),
        )
        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        v2._MEMORY_MTIMES["hermes-alice"] = 1000.0

        fake_run = MagicMock(return_value=_stat_ok(1000.0))
        monkeypatch.setattr("subprocess.run", fake_run)

        asyncio.run(v2.sync_memories_from_sandbox("hermes-alice"))

        # Stat ran, then bailed on mtime check before the download.
        assert fake_run.call_count == 1

    def test_advanced_mtime_triggers_download(self, tmp_path, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: self._state_with("hermes-alice", "alice"),
        )
        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        v2._MEMORY_MTIMES["hermes-alice"] = 1000.0

        fake_run = MagicMock(side_effect=[_stat_ok(2000.0), _download_ok()])
        monkeypatch.setattr("subprocess.run", fake_run)

        asyncio.run(v2.sync_memories_from_sandbox("hermes-alice"))

        assert fake_run.call_count == 2
        # Second call is the download: pull from sandbox path to host dir.
        dl_cmd = fake_run.call_args_list[1][0][0]
        assert "download" in dl_cmd
        assert "/tmp/hermes-srv-home/memories/" in dl_cmd
        # The cached mtime advanced.
        assert v2._MEMORY_MTIMES["hermes-alice"] == 2000.0

    def test_download_failure_does_not_update_mtime_cache(self, tmp_path, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: self._state_with("hermes-alice", "alice"),
        )
        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        v2._MEMORY_MTIMES["hermes-alice"] = 1000.0

        bad_dl = MagicMock()
        bad_dl.returncode = 1
        bad_dl.stdout = ""
        bad_dl.stderr = "permission denied"
        fake_run = MagicMock(side_effect=[_stat_ok(2000.0), bad_dl])
        monkeypatch.setattr("subprocess.run", fake_run)

        asyncio.run(v2.sync_memories_from_sandbox("hermes-alice"))

        assert fake_run.call_count == 2
        # Cache unchanged — next dispatch will retry.
        assert v2._MEMORY_MTIMES["hermes-alice"] == 1000.0

    def test_gateway_flag_passes_through(self, tmp_path, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: self._state_with(
                "hermes-alice", "alice", gateway="cluster-zonk",
            ),
        )
        monkeypatch.setattr(
            "gateway.executors.openshell._HERMES_HOME", tmp_path,
        )
        v2._MEMORY_MTIMES.pop("hermes-alice", None)

        fake_run = MagicMock(side_effect=[_stat_ok(3000.0), _download_ok()])
        monkeypatch.setattr("subprocess.run", fake_run)

        asyncio.run(v2.sync_memories_from_sandbox("hermes-alice"))

        stat_cmd = fake_run.call_args_list[0][0][0]
        dl_cmd = fake_run.call_args_list[1][0][0]
        assert "-g" in stat_cmd and "cluster-zonk" in stat_cmd
        assert "-g" in dl_cmd and "cluster-zonk" in dl_cmd


# ---------------------------------------------------------------------------
# _resolve_agent_info
# ---------------------------------------------------------------------------


class TestResolveAgentInfo:
    def test_unknown_returns_none(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state", lambda: [],
        )
        assert v2._resolve_agent_info("hermes-ghost") == (None, "")

    def test_known_returns_name_and_gateway(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [
                {
                    "sandbox_name": "hermes-alice",
                    "name": "alice",
                    "openshell_name": "cluster-a",
                },
            ],
        )
        assert v2._resolve_agent_info("hermes-alice") == ("alice", "cluster-a")

    def test_missing_openshell_name_returns_empty_string(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [{"sandbox_name": "hermes-alice", "name": "alice"}],
        )
        assert v2._resolve_agent_info("hermes-alice") == ("alice", "")
