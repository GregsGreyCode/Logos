"""Unit tests for ``gateway.worker_registry_v2._CircuitBreaker``.

The breaker watches the translated SSE frame stream and trips when a
sandbox agent is stuck looping on the same failing tool call. The real
integration (``cancel_task`` + synthetic ``task_result``) is exercised
in the live dispatch test; these tests cover the state machine.
"""

from __future__ import annotations

import pytest

from gateway.worker_registry_v2 import _CircuitBreaker


def _start(tool: str, preview: str) -> dict:
    return {"type": "tool_start", "tool": tool, "preview": preview}


def _end(tool: str, *, error=False) -> dict:
    return {"type": "tool_end", "tool": tool, "error": error}


class TestCircuitBreaker:
    def test_no_trip_on_success(self):
        b = _CircuitBreaker(threshold=3)
        for _ in range(10):
            assert b.observe(_start("write_file", "write_file(path='/a')")) is False
            assert b.observe(_end("write_file", error=False)) is False

    def test_no_trip_before_threshold(self):
        b = _CircuitBreaker(threshold=5)
        for _ in range(4):
            b.observe(_start("write_file", "write_file(path='/a')"))
            assert b.observe(_end("write_file", error="boom")) is False

    def test_trips_at_threshold(self):
        b = _CircuitBreaker(threshold=5)
        tripped_on = None
        for i in range(5):
            b.observe(_start("write_file", "write_file(path='/a')"))
            if b.observe(_end("write_file", error="boom")):
                tripped_on = i + 1
                break
        assert tripped_on == 5
        assert b.tripped_tool == "write_file"
        assert b.tripped_preview == "write_file(path='/a')"
        assert b.count == 5

    def test_success_resets_counter(self):
        b = _CircuitBreaker(threshold=5)
        # 4 failures, then a success, then 4 more failures — should not trip.
        for _ in range(4):
            b.observe(_start("write_file", "write_file(path='/a')"))
            b.observe(_end("write_file", error="boom"))
        assert b.count == 4
        b.observe(_start("write_file", "write_file(path='/a')"))
        assert b.observe(_end("write_file", error=False)) is False
        assert b.count == 0
        for _ in range(4):
            b.observe(_start("write_file", "write_file(path='/a')"))
            assert b.observe(_end("write_file", error="boom")) is False

    def test_different_tool_resets_counter(self):
        b = _CircuitBreaker(threshold=5)
        for _ in range(4):
            b.observe(_start("write_file", "write_file(path='/a')"))
            b.observe(_end("write_file", error="boom"))
        assert b.count == 4
        # Different tool failing → reset, first entry of new window.
        b.observe(_start("read_file", "read_file(path='/b')"))
        assert b.observe(_end("read_file", error="boom")) is False
        assert b.count == 1

    def test_different_preview_same_tool_resets(self):
        b = _CircuitBreaker(threshold=5)
        for _ in range(4):
            b.observe(_start("write_file", "write_file(path='/a')"))
            b.observe(_end("write_file", error="boom"))
        # Same tool, DIFFERENT preview — shouldn't contribute to the
        # existing window. Agent actually tried a different call.
        b.observe(_start("write_file", "write_file(path='/b')"))
        assert b.observe(_end("write_file", error="boom")) is False
        assert b.count == 1

    def test_trips_exactly_at_fifth_identical_failure(self):
        b = _CircuitBreaker(threshold=5)
        b.observe(_start("patch", "patch(file='x')"))
        for i in range(4):
            assert b.observe(_end("patch", error="hunk mismatch")) is False
            b.observe(_start("patch", "patch(file='x')"))
        assert b.observe(_end("patch", error="hunk mismatch")) is True

    def test_non_tool_frames_ignored(self):
        b = _CircuitBreaker(threshold=3)
        for ftype in ("ready", "token", "thinking", "task_result"):
            assert b.observe({"type": ftype, "content": "noise"}) is False
        # State unaffected by noise frames.
        b.observe(_start("write_file", "write_file(path='/a')"))
        assert b.observe(_end("write_file", error="boom")) is False
        assert b.count == 1

    def test_missing_error_field_is_success(self):
        b = _CircuitBreaker(threshold=3)
        for _ in range(2):
            b.observe(_start("write_file", "write_file(path='/a')"))
            b.observe(_end("write_file", error="boom"))
        # tool_end without an ``error`` key is treated as success.
        assert (
            b.observe({"type": "tool_end", "tool": "write_file"}) is False
        )
        assert b.count == 0

    def test_missing_preview_treated_as_empty(self):
        b = _CircuitBreaker(threshold=3)
        # No tool_start before tool_end → preview defaults to "".
        # Still catches the loop: (tool, "") repeats.
        for i in range(3):
            if b.observe(_end("write_file", error="boom")):
                assert i == 2
                return
        pytest.fail("expected breaker to trip without preview context")

    def test_custom_threshold(self):
        b = _CircuitBreaker(threshold=2)
        b.observe(_start("write_file", "write_file(path='/a')"))
        assert b.observe(_end("write_file", error="boom")) is False
        b.observe(_start("write_file", "write_file(path='/a')"))
        assert b.observe(_end("write_file", error="boom")) is True

    def test_post_trip_properties_reflect_last_failure(self):
        b = _CircuitBreaker(threshold=3)
        for _ in range(3):
            b.observe(_start("write_file", "write_file(path='/loop')"))
            b.observe(_end("write_file", error="boom"))
        assert b.tripped_tool == "write_file"
        assert b.tripped_preview == "write_file(path='/loop')"
        assert b.count == 3
