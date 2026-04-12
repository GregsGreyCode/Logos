"""Regression test for the memory_write preview payload.

Guards against the bug where the previous signature-refactor of
``on_tool_complete`` left an inverted ``if not success`` branch that
caused every successful memory write to emit ``"preview": ""``. The
chat UI then fell back to the literal string 'saved a memory' and the
actual memory content was never shown. See docker/sandbox_worker.py
build_memory_write_event and agents/hermes/agent.py for the contract.
"""
from docker.sandbox_worker import build_memory_write_event


def test_memory_write_preview_carries_content_on_success():
    evt = build_memory_write_event(
        tool_name="memory",
        success=True,
        result="User prefers concise answers; dislikes emoji.",
        task_id="t-1",
    )
    assert evt is not None
    assert evt["type"] == "memory_write"
    assert evt["preview"] == "User prefers concise answers; dislikes emoji."
    assert evt["task_id"] == "t-1"


def test_memory_write_event_not_emitted_on_failure():
    assert build_memory_write_event("memory", False, "irrelevant") is None


def test_memory_write_event_not_emitted_for_other_tools():
    assert build_memory_write_event("terminal", True, "ls output") is None
    assert build_memory_write_event("file", True, "content") is None


def test_memory_write_preview_truncated_to_200_chars():
    long = "x" * 500
    evt = build_memory_write_event("memory", True, long)
    assert evt is not None
    assert len(evt["preview"]) == 200


def test_memory_write_preview_handles_none_result_gracefully():
    evt = build_memory_write_event("memory", True, None)
    assert evt is not None
    assert evt["preview"] == ""
