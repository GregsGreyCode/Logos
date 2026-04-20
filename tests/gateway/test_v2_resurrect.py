"""Unit tests for LOG-61 / LOG-62 resurrect-path parity.

Covers:
- ``persist_hermes_server_setup`` — the state-file write helper.
- ``OpenShellExecutor.resurrect_hermes_server_mode`` — the restore-v2
  wrapper used by ``_resurrect_missing_sandboxes`` for both reconciled
  sandboxes and as a defensive backstop for spawn races.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_setup(key="abc123", base="http://127.0.0.1:8642",
                home="/tmp/hermes-srv-home"):
    return SimpleNamespace(api_key=key, base_url=base, hermes_home=home)


# ---------------------------------------------------------------------------
# persist_hermes_server_setup
# ---------------------------------------------------------------------------


class TestPersistHermesServerSetup:
    def test_writes_setup_into_matching_entry(self, tmp_path, monkeypatch):
        from gateway.executors import openshell as osh
        state_file = tmp_path / "state.json"
        lock_file = tmp_path / "state.lock"
        monkeypatch.setattr(osh, "_STATE_FILE", state_file)
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", lock_file)
        state_file.write_text(json.dumps([
            {"sandbox_name": "hermes-alice", "name": "alice", "phase": "ready"},
            {"sandbox_name": "hermes-bob", "name": "bob", "phase": "ready"},
        ]))
        setup = _make_setup(key="alice_key")

        ok = osh.persist_hermes_server_setup("hermes-alice", setup)

        assert ok is True
        saved = json.loads(state_file.read_text())
        by_name = {s["sandbox_name"]: s for s in saved}
        assert by_name["hermes-alice"]["hermes_server_setup"] == {
            "api_key": "alice_key",
            "base_url": "http://127.0.0.1:8642",
            "hermes_home": "/tmp/hermes-srv-home",
        }
        # Sibling entry untouched.
        assert "hermes_server_setup" not in by_name["hermes-bob"]

    def test_returns_false_when_no_matching_entry(self, tmp_path, monkeypatch):
        from gateway.executors import openshell as osh
        state_file = tmp_path / "state.json"
        lock_file = tmp_path / "state.lock"
        monkeypatch.setattr(osh, "_STATE_FILE", state_file)
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", lock_file)
        state_file.write_text(json.dumps([
            {"sandbox_name": "hermes-alice", "name": "alice"},
        ]))

        ok = osh.persist_hermes_server_setup("hermes-ghost", _make_setup())

        assert ok is False
        # State file left as-is.
        saved = json.loads(state_file.read_text())
        assert saved[0]["sandbox_name"] == "hermes-alice"
        assert "hermes_server_setup" not in saved[0]

    def test_overwrites_existing_setup(self, tmp_path, monkeypatch):
        from gateway.executors import openshell as osh
        state_file = tmp_path / "state.json"
        lock_file = tmp_path / "state.lock"
        monkeypatch.setattr(osh, "_STATE_FILE", state_file)
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", lock_file)
        state_file.write_text(json.dumps([
            {
                "sandbox_name": "hermes-alice",
                "name": "alice",
                "hermes_server_setup": {
                    "api_key": "old",
                    "base_url": "http://127.0.0.1:8642",
                    "hermes_home": "/tmp/hermes-srv-home",
                },
            },
        ]))

        ok = osh.persist_hermes_server_setup(
            "hermes-alice", _make_setup(key="new"),
        )

        assert ok is True
        saved = json.loads(state_file.read_text())
        assert saved[0]["hermes_server_setup"]["api_key"] == "new"


# ---------------------------------------------------------------------------
# resurrect_hermes_server_mode
# ---------------------------------------------------------------------------


def _bound_resurrect_call(sandbox_name: str, cfg) -> bool:
    """Call the unbound method against a MagicMock self.

    The method doesn't use ``self`` — it's a cohesion-driven method
    rather than stateful — so MagicMock works for wiring.
    """
    from gateway.executors.openshell import OpenShellExecutor
    return OpenShellExecutor.resurrect_hermes_server_mode(
        MagicMock(), sandbox_name, cfg,
    )


class TestResurrectHermesServerMode:
    def _base_patches(self, monkeypatch, *, enabled=True,
                      enable_returns=None, restart_raises=None,
                      health_raises=None):
        from gateway.executors import openshell as osh
        from gateway.executors import hermes_server_mode as hm
        from gateway.auth import db as adb

        monkeypatch.setattr(hm, "is_enabled", lambda: enabled)
        monkeypatch.setattr(
            hm, "build_channel_extra_env",
            lambda aid, sandbox_name_for_log=None: {"TELEGRAM_BOT_TOKEN": "x"},
        )
        mock_enable = MagicMock(return_value=enable_returns)
        mock_restart = MagicMock()
        mock_health = MagicMock()
        if restart_raises:
            mock_restart.side_effect = restart_raises
        if health_raises:
            mock_health.side_effect = health_raises
        monkeypatch.setattr(hm, "enable_hermes_server_mode", mock_enable)
        monkeypatch.setattr(hm, "restart_hermes_in_sandbox", mock_restart)
        monkeypatch.setattr(hm, "wait_for_hermes_health", mock_health)

        # State lookup used to resolve the gateway.
        monkeypatch.setattr(
            osh, "_load_state",
            lambda: [{"sandbox_name": "hermes-alice", "openshell_name": "cluster-a"}],
        )
        monkeypatch.setattr(
            adb, "get_agent_by_name",
            lambda name: {"id": f"agent-{name}"},
        )
        return {
            "enable": mock_enable,
            "restart": mock_restart,
            "health": mock_health,
        }

    def _cfg(self):
        from gateway.executors.base import InstanceConfig
        return InstanceConfig(name="alice", soul_name="general", model="test")

    def test_flag_off_is_noop(self, monkeypatch):
        self._base_patches(monkeypatch, enabled=False)
        result = _bound_resurrect_call("hermes-alice", self._cfg())
        assert result is False

    def test_end_to_end_happy_path(self, monkeypatch, tmp_path):
        setup = _make_setup(key="fresh_key")
        mocks = self._base_patches(monkeypatch, enable_returns=setup)

        # persist helper needs a writable state file.
        from gateway.executors import openshell as osh
        state_file = tmp_path / "state.json"
        lock_file = tmp_path / "state.lock"
        monkeypatch.setattr(osh, "_STATE_FILE", state_file)
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", lock_file)
        state_file.write_text(json.dumps([
            {"sandbox_name": "hermes-alice", "name": "alice"},
        ]))

        ok = _bound_resurrect_call("hermes-alice", self._cfg())

        assert ok is True
        mocks["enable"].assert_called_once()
        mocks["restart"].assert_called_once_with(
            "hermes-alice", gateway="cluster-a",
        )
        mocks["health"].assert_called_once_with(
            "hermes-alice", gateway="cluster-a",
        )
        saved = json.loads(state_file.read_text())
        assert saved[0]["hermes_server_setup"]["api_key"] == "fresh_key"

    def test_enable_failure_returns_false_without_restart(self, monkeypatch):
        mocks = self._base_patches(
            monkeypatch, enable_returns=None,
        )
        mocks["enable"].side_effect = RuntimeError("deploy failed")

        ok = _bound_resurrect_call("hermes-alice", self._cfg())

        assert ok is False
        mocks["restart"].assert_not_called()
        mocks["health"].assert_not_called()

    def test_restart_failure_returns_false(self, monkeypatch, tmp_path):
        setup = _make_setup()
        mocks = self._base_patches(
            monkeypatch,
            enable_returns=setup,
            restart_raises=RuntimeError("pkill denied"),
        )
        from gateway.executors import openshell as osh
        monkeypatch.setattr(osh, "_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", tmp_path / "state.lock")

        ok = _bound_resurrect_call("hermes-alice", self._cfg())

        assert ok is False
        mocks["restart"].assert_called_once()
        mocks["health"].assert_not_called()

    def test_health_failure_returns_false(self, monkeypatch, tmp_path):
        setup = _make_setup()
        mocks = self._base_patches(
            monkeypatch,
            enable_returns=setup,
            health_raises=TimeoutError("no /health after 30s"),
        )
        from gateway.executors import openshell as osh
        monkeypatch.setattr(osh, "_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", tmp_path / "state.lock")

        ok = _bound_resurrect_call("hermes-alice", self._cfg())

        assert ok is False
        mocks["health"].assert_called_once()

    def test_persist_miss_returns_false(self, monkeypatch, tmp_path):
        setup = _make_setup()
        self._base_patches(monkeypatch, enable_returns=setup)

        from gateway.executors import openshell as osh
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(osh, "_STATE_FILE", state_file)
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", tmp_path / "state.lock")
        # Make the persist helper see an empty state file — its
        # _load_state is the module-level one, which reads _STATE_FILE.
        # Override the monkey-patched _load_state back to real behaviour
        # for the persist phase, but the gateway-resolution phase earlier
        # already grabbed ``cluster-a`` from the fake list.
        state_file.write_text(json.dumps([]))
        monkeypatch.setattr(osh, "_load_state", lambda: [])

        ok = _bound_resurrect_call("hermes-alice", self._cfg())

        assert ok is False

    def test_uses_configured_gateway(self, monkeypatch, tmp_path):
        setup = _make_setup()
        mocks = self._base_patches(monkeypatch, enable_returns=setup)

        from gateway.executors import openshell as osh
        monkeypatch.setattr(
            osh, "_load_state",
            lambda: [{"sandbox_name": "hermes-alice", "openshell_name": "zonk-gateway"}],
        )
        state_file = tmp_path / "state.json"
        lock_file = tmp_path / "state.lock"
        monkeypatch.setattr(osh, "_STATE_FILE", state_file)
        monkeypatch.setattr(osh, "_STATE_LOCK_FILE", lock_file)
        state_file.write_text(json.dumps([
            {"sandbox_name": "hermes-alice", "name": "alice"},
        ]))

        _bound_resurrect_call("hermes-alice", self._cfg())

        # enable_hermes_server_mode should have been called with the
        # resolved gateway.
        assert mocks["enable"].call_args.kwargs.get("gateway") == "zonk-gateway"
