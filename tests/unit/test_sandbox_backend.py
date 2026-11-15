"""Unit tests for gateway/sandbox_backend.py (NullBackend + naming helper)."""

from __future__ import annotations

import re

import pytest

from gateway.sandbox_backend import (
    NO_BACKEND_MESSAGE,
    NullBackend,
    SandboxBackend,
    SandboxStatus,
    get_backend,
    sanitize_sandbox_name,
)


class TestNullBackend:
    def test_satisfies_protocol(self):
        assert isinstance(NullBackend(), SandboxBackend)

    def test_default_backend_is_null(self):
        assert isinstance(get_backend(), NullBackend)

    def test_list_is_empty(self):
        assert NullBackend().list() == []

    def test_status_is_missing(self):
        st = NullBackend().status("alpha")
        assert isinstance(st, SandboxStatus)
        assert st.name == "alpha"
        assert st.state == "missing"
        assert not st.running
        assert st.url is None
        assert st.to_dict()["state"] == "missing"

    @pytest.mark.parametrize("call", [
        lambda b: b.create({"id": "agent_1", "name": "alpha"}),
        lambda b: b.start("alpha"),
        lambda b: b.stop("alpha"),
        lambda b: b.delete("alpha"),
    ])
    def test_mutating_calls_raise(self, call):
        with pytest.raises(NotImplementedError, match=re.escape(NO_BACKEND_MESSAGE)):
            call(NullBackend())

    def test_message_points_at_e2(self):
        assert "(E2)" in NO_BACKEND_MESSAGE


class TestSanitizeSandboxName:
    def test_lowercases_and_dashes(self):
        assert sanitize_sandbox_name("My Agent_1") == "my-agent-1"

    def test_empty_falls_back(self):
        assert sanitize_sandbox_name("!!!") == "agent"

    def test_truncates_to_63(self):
        assert len(sanitize_sandbox_name("a" * 100)) == 63

