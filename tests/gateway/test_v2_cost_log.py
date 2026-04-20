"""Unit tests for ``gateway.worker_registry_v2.record_cost_entry`` — the
shared v1/v2 cost-log writer. Without it, v2 dispatches would silently
stop attributing token cost to agents, breaking LOG-25.6's per-agent
daily budget gate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _build_result(
    model="gpt-4o",
    input_tokens=100,
    output_tokens=50,
    cache_read=0,
    cache_write=0,
    extra=None,
):
    usage = {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }
    if extra:
        usage.update(extra)
    return {"status": "ok", "usage": usage}


class TestRecordCostEntry:
    def _patch_db(self, monkeypatch, *, agent_by_name=None, route=None,
                  dispatch=None):
        from gateway.auth import db as adb
        calls = {}
        insert_mock = MagicMock()
        monkeypatch.setattr(adb, "insert_cost_entry", insert_mock)
        monkeypatch.setattr(
            adb, "get_agent_by_name",
            lambda n: agent_by_name or {},
        )
        monkeypatch.setattr(
            adb, "get_model_route", lambda rid: route or {},
        )
        monkeypatch.setattr(
            adb, "get_dispatch_by_task_id",
            lambda tid: dispatch or {},
        )
        calls["insert"] = insert_mock
        return calls

    def _patch_pricing(self, monkeypatch, *, cost=0.0005, raises=False):
        from gateway import pricing
        cost_mock = MagicMock(
            return_value=None if raises else cost,
            side_effect=RuntimeError("boom") if raises else None,
        )
        monkeypatch.setattr(pricing, "cost_for_usage", cost_mock)
        return cost_mock

    def test_no_op_on_none_result(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(monkeypatch)
        record_cost_entry(None, "hermes-alice", "t1", "s1")
        calls["insert"].assert_not_called()

    def test_no_op_on_non_dict_result(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(monkeypatch)
        record_cost_entry("not a dict", "hermes-alice", "t1", "s1")
        calls["insert"].assert_not_called()

    def test_no_op_when_model_missing(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(monkeypatch)
        record_cost_entry(
            {"usage": {"input_tokens": 10}}, "hermes-alice", "t1", "s1",
        )
        calls["insert"].assert_not_called()

    def test_inserts_row_with_resolved_agent_and_provider(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(
            monkeypatch,
            agent_by_name={"id": "a1", "model_route_id": "r1"},
            route={"provider": "openai"},
            dispatch={"user_id": "u42"},
        )
        self._patch_pricing(monkeypatch, cost=0.001234)

        record_cost_entry(
            _build_result(model="gpt-4o", input_tokens=1000, output_tokens=500),
            "hermes-alice", "task-xyz", "session-1",
        )

        calls["insert"].assert_called_once()
        kwargs = calls["insert"].call_args.kwargs
        assert kwargs["agent_id"] == "a1"
        assert kwargs["agent_name"] == "alice"
        assert kwargs["provider"] == "openai"
        assert kwargs["model"] == "gpt-4o"
        assert kwargs["input_tokens"] == 1000
        assert kwargs["output_tokens"] == 500
        assert kwargs["cost_usd"] == pytest.approx(0.001234)
        assert kwargs["pricing_known"] is True
        assert kwargs["user_id"] == "u42"
        assert kwargs["task_id"] == "task-xyz"
        assert kwargs["session_id"] == "session-1"

    def test_cache_tokens_are_passed_through(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(
            monkeypatch,
            agent_by_name={"id": "a1"},
        )
        self._patch_pricing(monkeypatch, cost=0.0002)

        record_cost_entry(
            _build_result(
                model="claude-opus-4-7",
                input_tokens=10, output_tokens=5,
                cache_read=200, cache_write=100,
            ),
            "hermes-alice", "t", "s",
        )
        kwargs = calls["insert"].call_args.kwargs
        assert kwargs["cache_read_tokens"] == 200
        assert kwargs["cache_write_tokens"] == 100

    def test_zero_tokens_skips_pricing_call(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(
            monkeypatch, agent_by_name={"id": "a1"},
        )
        cost_mock = self._patch_pricing(monkeypatch)

        record_cost_entry(
            _build_result(model="gpt-4o", input_tokens=0, output_tokens=0),
            "hermes-alice", "t", "s",
        )

        cost_mock.assert_not_called()
        # Row is still inserted (with cost=0) so activity volume shows.
        calls["insert"].assert_called_once()
        kwargs = calls["insert"].call_args.kwargs
        assert kwargs["cost_usd"] == 0.0
        assert kwargs["pricing_known"] is False

    def test_pricing_exception_does_not_raise(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(monkeypatch, agent_by_name={"id": "a1"})
        self._patch_pricing(monkeypatch, raises=True)

        # Should complete without raising.
        record_cost_entry(
            _build_result(model="gpt-4o"),
            "hermes-alice", "t", "s",
        )

        # Row still inserted with cost=0, pricing_known=False.
        calls["insert"].assert_called_once()
        kwargs = calls["insert"].call_args.kwargs
        assert kwargs["cost_usd"] == 0.0
        assert kwargs["pricing_known"] is False

    def test_sandbox_name_without_hermes_prefix_has_no_agent_name(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        calls = self._patch_db(monkeypatch)
        self._patch_pricing(monkeypatch)
        record_cost_entry(
            _build_result(),
            "some-other-sandbox", "t", "s",
        )
        kwargs = calls["insert"].call_args.kwargs
        assert kwargs["agent_name"] is None
        assert kwargs["agent_id"] is None

    def test_missing_get_dispatch_by_task_id_uses_none(self, monkeypatch):
        from gateway.worker_registry_v2 import record_cost_entry
        from gateway.auth import db as adb
        calls = self._patch_db(monkeypatch, agent_by_name={"id": "a1"})
        self._patch_pricing(monkeypatch)
        # Remove the helper entirely to simulate an older db layer.
        if hasattr(adb, "get_dispatch_by_task_id"):
            monkeypatch.delattr(adb, "get_dispatch_by_task_id")

        record_cost_entry(
            _build_result(),
            "hermes-alice", "t", "s",
        )

        kwargs = calls["insert"].call_args.kwargs
        assert kwargs["user_id"] is None
