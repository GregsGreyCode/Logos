"""Unit tests for ``worker_registry_v2.fetch_toolsets_from_sandbox``.

The helper curls hermes's ``GET /v1/toolsets`` (registered by the
monkeypatch at ``hermes_cancel_monkeypatch._apply_toolset_introspection_patch``)
through ``openshell sandbox exec`` and returns the parsed JSON. These
tests cover the state-file lookup + subprocess wiring with a mock; the
end-to-end path (real sandbox curl against a live hermes) is
validated separately by the LOG-64 smoke test.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest


def _fake_state_entry(sandbox_name="hermes-alice", with_setup=True,
                      openshell_name="cluster-a"):
    entry = {
        "sandbox_name": sandbox_name,
        "name": "alice",
        "openshell_name": openshell_name,
    }
    if with_setup:
        entry["hermes_server_setup"] = {
            "api_key": "test_key_xyz",
            "base_url": "http://127.0.0.1:8642",
            "hermes_home": "/tmp/hermes-srv-home",
        }
    return entry


class TestFetchToolsetsFromSandbox:
    def test_returns_none_when_no_setup(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [_fake_state_entry(with_setup=False)],
        )
        fake_run = MagicMock()
        monkeypatch.setattr("subprocess.run", fake_run)

        result = asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-alice"))

        assert result is None
        fake_run.assert_not_called()

    def test_returns_none_when_sandbox_not_in_state(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state", lambda: [],
        )
        result = asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-ghost"))
        assert result is None

    def test_returns_parsed_payload_on_success(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [_fake_state_entry()],
        )

        payload = {
            "toolsets": {
                "web": {"available": True, "tools": ["web_search"], "description": "Web search"},
            },
            "all_tool_names": ["web_search"],
            "availability": {"web": True},
            "source": "hermes.tools.registry",
        }
        mock_result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        fake_run = MagicMock(return_value=mock_result)
        monkeypatch.setattr("subprocess.run", fake_run)

        result = asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-alice"))
        assert result == payload

    def test_returns_none_on_curl_failure(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [_fake_state_entry()],
        )
        mock_result = MagicMock(returncode=22, stdout="", stderr="curl: (22) 404")
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=mock_result))

        result = asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-alice"))
        assert result is None

    def test_returns_none_on_malformed_json(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [_fake_state_entry()],
        )
        mock_result = MagicMock(returncode=0, stdout="not json at all", stderr="")
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=mock_result))

        result = asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-alice"))
        assert result is None

    def test_returns_none_when_response_is_not_dict(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [_fake_state_entry()],
        )
        mock_result = MagicMock(returncode=0, stdout='["not", "a", "dict"]', stderr="")
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=mock_result))

        result = asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-alice"))
        assert result is None

    def test_passes_gateway_and_auth_into_exec_command(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [_fake_state_entry(openshell_name="zonk-gateway")],
        )

        captured = {}

        def _capture_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout='{"toolsets": {}}', stderr="")

        monkeypatch.setattr("subprocess.run", _capture_run)

        asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-alice"))

        assert "-g" in captured["cmd"]
        gi = captured["cmd"].index("-g")
        assert captured["cmd"][gi + 1] == "zonk-gateway"
        sh_arg = captured["cmd"][-1]
        assert "Bearer test_key_xyz" in sh_arg
        assert "http://127.0.0.1:8642/v1/toolsets" in sh_arg

    def test_timeout_exception_returns_none(self, monkeypatch):
        from gateway import worker_registry_v2 as v2
        monkeypatch.setattr(
            "gateway.executors.openshell._load_state",
            lambda: [_fake_state_entry()],
        )

        import subprocess as _sp

        def _raise(*a, **k):
            raise _sp.TimeoutExpired(cmd="openshell", timeout=10)

        monkeypatch.setattr("subprocess.run", _raise)

        result = asyncio.run(v2.fetch_toolsets_from_sandbox("hermes-alice"))
        assert result is None
