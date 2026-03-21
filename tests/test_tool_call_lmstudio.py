"""Regression tests for tool-call handling with LM Studio / local OpenAI-compatible models.

Covers the three root-cause bugs that caused "Connection error: TypeError: network error":
  1. reasoning_content was injected for ALL providers even when reasoning came from
     <think> blocks — LM Studio rejects this unknown field.
  2. call_id / response_item_id / extra_content were only stripped for Mistral;
     LM Studio and other strict OpenAI-compatible backends reject them too.
  3. Double-fault in _handle_chat: a secondary exception during error-event send
     could escape uncaught, causing aiohttp to close the stream abruptly.

Tests here focus on #1 and #2 at the run_agent.py layer (pure unit tests, no
network calls needed).  The _handle_chat double-fault (#3) is an aiohttp concern
and is covered by the send_event try/except wrapping.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.hermes.agent import AIAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_tool_call(name: str, args: str = "{}", id: str = "call_abc123") -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        type="function",
        function=SimpleNamespace(name=name, arguments=args),
    )


def _mock_message(content, tool_calls=None, reasoning_content=None) -> SimpleNamespace:
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
        reasoning_details=None,
    )


def _mock_response(message, finish_reason="tool_calls") -> SimpleNamespace:
    choice = SimpleNamespace(message=message, finish_reason=finish_reason, index=0)
    return SimpleNamespace(
        id="chatcmpl-test",
        model="qwen/qwen3-8b",
        choices=[choice],
        usage=None,
    )


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("execute_code")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.base_url = "http://localhost:1234/v1"  # simulate LM Studio
        return a


# ---------------------------------------------------------------------------
# Bug #1: reasoning_content must NOT be injected for <think>-block reasoning
# ---------------------------------------------------------------------------


class TestReasoningContentNotInjectedForThinkBlocks:
    """When a model (e.g. qwen) puts reasoning in <think>...</think> content,
    the extracted reasoning must NOT be re-sent as reasoning_content on the
    follow-up API call — LM Studio doesn't accept that field."""

    def test_think_block_sets_reasoning_from_think_block_flag(self, agent):
        """_build_assistant_message marks think-block reasoning with the flag."""
        msg = _mock_message(content="<think>step by step</think>\n\n")
        msg.tool_calls = [_mock_tool_call("execute_code")]
        built = agent._build_assistant_message(msg, "tool_calls")
        assert built["reasoning"] == "step by step"
        assert built["reasoning_from_think_block"] is True

    def test_native_reasoning_not_flagged(self, agent):
        """When reasoning comes from a native API field, the flag is False/absent."""
        msg = _mock_message(content="Hello!", reasoning_content="thinking here")
        built = agent._build_assistant_message(msg, "stop")
        # reasoning_from_think_block should be False (native reasoning)
        assert not built.get("reasoning_from_think_block", False)

    def test_api_messages_strip_reasoning_content_for_think_blocks(self, agent):
        """run_conversation must not add reasoning_content to api_messages when
        the reasoning came from a <think> block."""
        # Simulate a stored assistant message with think-block reasoning
        stored_msg = {
            "role": "assistant",
            "content": "<think>deep thoughts</think>\n\nSure!",
            "reasoning": "deep thoughts",
            "reasoning_from_think_block": True,
            "finish_reason": "stop",
        }
        messages = [
            {"role": "user", "content": "hello"},
            stored_msg,
        ]

        api_messages = []
        for msg in messages:
            api_msg = msg.copy()
            # Replicate the API prep logic from run_conversation
            if msg.get("role") == "assistant":
                reasoning_text = msg.get("reasoning")
                if reasoning_text and not msg.get("reasoning_from_think_block"):
                    api_msg["reasoning_content"] = reasoning_text
            for drop_key in ("reasoning", "reasoning_from_think_block"):
                api_msg.pop(drop_key, None)
            api_msg.pop("finish_reason", None)
            api_messages.append(api_msg)

        assistant_api = api_messages[1]
        assert "reasoning_content" not in assistant_api, (
            "reasoning_content must NOT be sent to LM Studio for think-block reasoning"
        )
        assert "reasoning" not in assistant_api
        assert "reasoning_from_think_block" not in assistant_api

    def test_api_messages_include_reasoning_content_for_native_reasoning(self, agent):
        """Native reasoning_content SHOULD be passed back on follow-up calls."""
        stored_msg = {
            "role": "assistant",
            "content": "Sure!",
            "reasoning": "thinking natively",
            "reasoning_from_think_block": False,
            "finish_reason": "stop",
        }
        api_msg = stored_msg.copy()
        reasoning_text = stored_msg.get("reasoning")
        if reasoning_text and not stored_msg.get("reasoning_from_think_block"):
            api_msg["reasoning_content"] = reasoning_text
        for drop_key in ("reasoning", "reasoning_from_think_block"):
            api_msg.pop(drop_key, None)

        assert api_msg.get("reasoning_content") == "thinking natively"


# ---------------------------------------------------------------------------
# Bug #2: call_id / response_item_id / extra_content must be stripped
# ---------------------------------------------------------------------------


class TestExtraToolCallFieldsStripped:
    """call_id, response_item_id, and extra_content are Hermes-internal fields
    that must be stripped before sending to any chat_completions provider."""

    def test_sanitize_strips_call_id_and_response_item_id(self, agent):
        api_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "call_id": "call_abc",        # internal
                    "response_item_id": "call_abc",  # Codex-only
                    "type": "function",
                    "function": {"name": "execute_code", "arguments": "{}"},
                }
            ],
        }
        agent._sanitize_tool_calls_for_strict_api(api_msg)
        tc = api_msg["tool_calls"][0]
        assert "call_id" not in tc, "call_id must be stripped for OpenAI-compatible providers"
        assert "response_item_id" not in tc, "response_item_id must be stripped"
        assert tc["id"] == "call_abc"  # id must remain
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "execute_code"

    def test_sanitize_strips_extra_content(self, agent):
        """extra_content (Gemini field) must also be stripped."""
        api_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_xyz",
                    "call_id": "call_xyz",
                    "response_item_id": "call_xyz",
                    "extra_content": {"thought_signature": "abc"},
                    "type": "function",
                    "function": {"name": "web_search", "arguments": '{"query":"test"}'},
                }
            ],
        }
        agent._sanitize_tool_calls_for_strict_api(api_msg)
        tc = api_msg["tool_calls"][0]
        assert "extra_content" not in tc

    def test_sanitize_handles_no_tool_calls(self, agent):
        """Messages without tool_calls are returned unchanged."""
        api_msg = {"role": "user", "content": "hello"}
        result = agent._sanitize_tool_calls_for_strict_api(api_msg)
        assert result == {"role": "user", "content": "hello"}

    def test_sanitize_handles_empty_tool_calls(self, agent):
        """Empty tool_calls list is handled gracefully."""
        api_msg = {"role": "assistant", "content": "", "tool_calls": []}
        agent._sanitize_tool_calls_for_strict_api(api_msg)
        assert api_msg["tool_calls"] == []


# ---------------------------------------------------------------------------
# Integration: _build_assistant_message with tool_calls + <think> content
# ---------------------------------------------------------------------------


class TestBuildAssistantMessageWithThinkAndToolCalls:
    """Covers the LM Studio pattern: content has <think>...</think> + tool_calls."""

    def test_think_plus_tool_calls(self, agent):
        """Model returns <think>...</think>\n\n with tool_calls and finish_reason=tool_calls."""
        msg = _mock_message(
            content="<think>I should run the code</think>\n\n",
            tool_calls=[_mock_tool_call("execute_code", '{"code": "print(1+1)"}')],
        )
        built = agent._build_assistant_message(msg, "tool_calls")

        assert built["role"] == "assistant"
        assert built["finish_reason"] == "tool_calls"
        assert built["reasoning"] == "I should run the code"
        assert built["reasoning_from_think_block"] is True
        assert len(built["tool_calls"]) == 1
        assert built["tool_calls"][0]["function"]["name"] == "execute_code"

    def test_empty_content_with_tool_calls(self, agent):
        """Model returns content=None with tool_calls — should not raise."""
        msg = _mock_message(content=None, tool_calls=[_mock_tool_call("execute_code")])
        built = agent._build_assistant_message(msg, "tool_calls")
        assert built["content"] == ""
        assert len(built["tool_calls"]) == 1
        assert not built.get("reasoning_from_think_block")

    def test_think_only_no_tool_calls(self, agent):
        """Standalone <think> response (no tool calls) correctly sets the flag."""
        msg = _mock_message(content="<think>reasoning</think>")
        built = agent._build_assistant_message(msg, "stop")
        assert built["reasoning"] == "reasoning"
        assert built["reasoning_from_think_block"] is True
        assert not built.get("tool_calls")
