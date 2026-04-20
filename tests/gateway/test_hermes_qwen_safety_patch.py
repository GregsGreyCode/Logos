"""Unit tests for the Qwen OpenAI-SDK safety net in
``gateway.executors.hermes_cancel_monkeypatch``.

The monkeypatch file is uploaded into v2 sandboxes and executed before
hermes boots so the patches apply in the sandbox's Python process. These
tests cover the pure helper functions directly against the module —
they do not exercise the actual openai SDK wrap (that's validated in
the live end-to-end test that runs against a real sandbox).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway.executors.hermes_cancel_monkeypatch import (
    _apply_qwen_safety_to_choice,
    _normalize_finish_reason,
    _parse_qwen_xml_tools,
    _strip_qwen_think_leak,
)


# ---------------------------------------------------------------------------
# _parse_qwen_xml_tools
# ---------------------------------------------------------------------------


class TestParseQwenXmlTools:
    def test_empty_text_returns_empty_list(self):
        assert _parse_qwen_xml_tools("") == []

    def test_text_without_xml_returns_empty_list(self):
        assert _parse_qwen_xml_tools("hello world") == []

    def test_extracts_single_tool_call(self):
        text = (
            "Sure, let me do that. "
            "<function=write_file>"
            "<parameter=path>/tmp/x.md</parameter>"
            "<parameter=content>hello</parameter>"
            "</function>"
        )
        tools = _parse_qwen_xml_tools(text)
        assert len(tools) == 1
        t = tools[0]
        assert t["type"] == "function"
        assert t["function"]["name"] == "write_file"
        args = json.loads(t["function"]["arguments"])
        assert args == {"path": "/tmp/x.md", "content": "hello"}
        assert t["id"].startswith("call_")

    def test_extracts_multiple_tool_calls(self):
        text = (
            "<function=list_files>"
            "<parameter=path>/</parameter>"
            "</function>"
            "<function=read_file>"
            "<parameter=path>/README.md</parameter>"
            "</function>"
        )
        tools = _parse_qwen_xml_tools(text)
        assert [t["function"]["name"] for t in tools] == ["list_files", "read_file"]

    def test_parses_json_valued_parameters(self):
        text = (
            "<function=web_search>"
            "<parameter=options>{\"limit\": 5}</parameter>"
            "</function>"
        )
        tools = _parse_qwen_xml_tools(text)
        args = json.loads(tools[0]["function"]["arguments"])
        assert args == {"options": {"limit": 5}}

    def test_fast_path_rejects_uppercase_tag(self):
        """Sanity check: the lowercase fast-path check short-circuits before
        the case-insensitive regex, so uppercase variants are ignored.
        qwen always emits lowercase, so this is fine in practice; locking
        the behaviour in so nobody "fixes" it and opens a regex-DoS vector.
        """
        tools = _parse_qwen_xml_tools("<FUNCTION=foo></FUNCTION>")
        assert tools == []


# ---------------------------------------------------------------------------
# _strip_qwen_think_leak
# ---------------------------------------------------------------------------


class TestStripQwenThinkLeak:
    def test_empty_string(self):
        assert _strip_qwen_think_leak("") == ""

    def test_whole_think_block_stripped(self):
        text = "<think>long internal reasoning...</think>Final answer: 42."
        assert _strip_qwen_think_leak(text) == "Final answer: 42."

    def test_orphan_closing_tag_stripped(self):
        text = "</think>Final answer: 42."
        assert _strip_qwen_think_leak(text) == "Final answer: 42."

    def test_no_think_tags_preserved(self):
        assert _strip_qwen_think_leak("just a plain answer") == "just a plain answer"

    def test_multiple_think_blocks(self):
        text = "<think>a</think>X<think>b</think>Y"
        assert _strip_qwen_think_leak(text) == "XY"

    def test_strips_leading_whitespace_after_removal(self):
        text = "<think>thoughts</think>\n\n  Final answer"
        # The regex drops the think block; .strip() cleans surrounding blank.
        out = _strip_qwen_think_leak(text)
        assert out == "Final answer"


# ---------------------------------------------------------------------------
# _normalize_finish_reason
# ---------------------------------------------------------------------------


class TestNormalizeFinishReason:
    @pytest.mark.parametrize("bad", ["stop", "error", "eos_token", "", None])
    def test_flips_bad_reason_when_tools_present(self, bad):
        assert _normalize_finish_reason(bad, has_tool_calls=True) == "tool_calls"

    def test_does_not_flip_when_no_tools(self):
        # No tool calls → 'stop' is legitimate, leave it.
        assert _normalize_finish_reason("stop", has_tool_calls=False) == "stop"

    def test_does_not_flip_already_good_reason(self):
        assert _normalize_finish_reason("tool_calls", has_tool_calls=True) == "tool_calls"

    def test_length_finish_reason_preserved(self):
        # 'length' isn't in the bad set — models may legit stop from length
        # even mid tool call; don't second-guess.
        assert _normalize_finish_reason("length", has_tool_calls=True) == "length"


# ---------------------------------------------------------------------------
# _apply_qwen_safety_to_choice
# ---------------------------------------------------------------------------


def _choice_dict(content="", tool_calls=None, finish_reason="stop"):
    return {
        "message": {"content": content, "tool_calls": tool_calls or []},
        "finish_reason": finish_reason,
    }


class TestApplyQwenSafetyToChoice:
    def test_no_message_no_op(self):
        got = _apply_qwen_safety_to_choice({})
        assert got == (False, False, False)

    def test_clean_content_no_op(self):
        choice = _choice_dict(content="Just a plain answer.")
        got = _apply_qwen_safety_to_choice(choice)
        assert got == (False, False, False)
        assert choice["message"]["content"] == "Just a plain answer."

    def test_think_leak_stripped(self):
        choice = _choice_dict(content="<think>x</think>Hello")
        fired_xml, fired_think, fired_finish = _apply_qwen_safety_to_choice(choice)
        assert fired_think is True
        assert choice["message"]["content"] == "Hello"

    def test_xml_recovered_and_content_blanked(self):
        choice = _choice_dict(
            content="<function=foo><parameter=x>1</parameter></function>",
        )
        fired_xml, _, fired_finish = _apply_qwen_safety_to_choice(choice)
        assert fired_xml is True
        assert choice["message"]["tool_calls"]
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "foo"
        # Content had ONLY XML so it's blanked (or None).
        assert not choice["message"]["content"]
        # finish_reason flipped because tools are now present.
        assert fired_finish is True
        assert choice["finish_reason"] == "tool_calls"

    def test_xml_recovery_leaves_surrounding_text(self):
        choice = _choice_dict(
            content="Sure! <function=foo><parameter=x>1</parameter></function> Done.",
        )
        _apply_qwen_safety_to_choice(choice)
        # Non-XML bits survive.
        assert "Sure!" in choice["message"]["content"]
        assert "Done" in choice["message"]["content"]

    def test_xml_recovery_skipped_when_tools_already_present(self):
        """If the SDK already parsed tool_calls, the text XML is ignored —
        we don't want to duplicate tool calls."""
        existing = [{"id": "call_x", "type": "function",
                     "function": {"name": "bar", "arguments": "{}"}}]
        choice = _choice_dict(
            content="<function=foo><parameter=x>1</parameter></function>",
            tool_calls=existing,
        )
        fired_xml, _, _ = _apply_qwen_safety_to_choice(choice)
        assert fired_xml is False
        # No duplicate.
        assert len(choice["message"]["tool_calls"]) == 1
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "bar"

    def test_attribute_style_choice(self):
        """Works on openai SDK response objects that use attributes, not dicts."""
        msg = SimpleNamespace(content="<think>hmm</think>Actual answer", tool_calls=[])
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        fired_xml, fired_think, _ = _apply_qwen_safety_to_choice(choice)
        assert fired_think is True
        assert msg.content == "Actual answer"

    def test_finish_reason_fixed_with_existing_tools(self):
        existing = [{"id": "call_x", "type": "function",
                     "function": {"name": "bar", "arguments": "{}"}}]
        choice = _choice_dict(tool_calls=existing, finish_reason="stop")
        fired_xml, fired_think, fired_finish = _apply_qwen_safety_to_choice(choice)
        assert fired_finish is True
        assert choice["finish_reason"] == "tool_calls"
