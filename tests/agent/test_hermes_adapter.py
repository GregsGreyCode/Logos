"""Tests for Phase 1 agent abstractions: interface, runner, HermesAdapter."""

from unittest.mock import MagicMock, patch
import pytest

from logos.agent.interface import (
    AgentAdapter,
    AgentCapabilities,
    AgentContext,
    AgentResult,
)
from logos.agent.runner import AgentRunner
from logos.adapters.hermes.adapter import HermesAdapter


# ── AgentResult ──────────────────────────────────────────────────────────────

class TestAgentResult:
    def test_from_dict_minimal(self):
        d = {
            "final_response": "Hello!",
            "messages": [{"role": "assistant", "content": "Hello!"}],
            "api_calls": 1,
            "completed": True,
            "partial": False,
            "interrupted": False,
        }
        result = AgentResult.from_dict(d)
        assert result.final_response == "Hello!"
        assert result.completed is True
        assert result.api_calls == 1

    def test_from_dict_with_interrupt_message(self):
        d = {
            "final_response": None,
            "messages": [],
            "api_calls": 2,
            "completed": False,
            "partial": False,
            "interrupted": True,
            "interrupt_message": "User interrupted",
        }
        result = AgentResult.from_dict(d)
        assert result.interrupted is True
        assert result.interrupt_message == "User interrupted"

    def test_from_dict_extras_preserved(self):
        d = {
            "final_response": None,
            "messages": [],
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "interrupted": False,
            "error": "Model returned invalid JSON",
        }
        result = AgentResult.from_dict(d)
        assert result.extras["error"] == "Model returned invalid JSON"

    def test_to_dict_roundtrip(self):
        d = {
            "final_response": "Done",
            "last_reasoning": "I thought about it",
            "messages": [],
            "api_calls": 3,
            "completed": True,
            "partial": False,
            "interrupted": False,
            "response_previewed": False,
        }
        result = AgentResult.from_dict(d)
        out = result.to_dict()
        for key, val in d.items():
            assert out[key] == val

    def test_to_dict_interrupt_message_omitted_when_none(self):
        result = AgentResult(
            final_response="Hi",
            messages=[],
            api_calls=1,
            completed=True,
            interrupt_message=None,
        )
        assert "interrupt_message" not in result.to_dict()

    def test_to_dict_interrupt_message_included_when_set(self):
        result = AgentResult(
            final_response=None,
            messages=[],
            api_calls=1,
            completed=False,
            interrupted=True,
            interrupt_message="Stop",
        )
        assert result.to_dict()["interrupt_message"] == "Stop"

    def test_to_dict_extras_merged(self):
        d = {
            "final_response": None,
            "messages": [],
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "interrupted": False,
            "error": "oops",
        }
        out = AgentResult.from_dict(d).to_dict()
        assert out["error"] == "oops"


# ── AgentAdapter ABC ──────────────────────────────────────────────────────────

class TestAgentAdapterABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AgentAdapter()

    def test_concrete_subclass_must_implement_all_methods(self):
        class Incomplete(AgentAdapter):
            pass

        with pytest.raises(TypeError):
            Incomplete()


# ── AgentRunner ───────────────────────────────────────────────────────────────

class TestAgentRunner:
    def _make_adapter(self, return_result=None):
        adapter = MagicMock(spec=AgentAdapter)
        adapter.agent_id = "mock"
        if return_result is None:
            return_result = AgentResult(
                final_response="ok", messages=[], api_calls=1, completed=True
            )
        adapter.run.return_value = return_result
        return adapter

    def test_run_delegates_to_adapter(self):
        expected = AgentResult(
            final_response="response", messages=[], api_calls=2, completed=True
        )
        adapter = self._make_adapter(return_result=expected)
        runner = AgentRunner(adapter)
        ctx = AgentContext(user_message="hello")
        result = runner.run(ctx)
        adapter.run.assert_called_once_with(ctx)
        assert result is expected

    def test_rejects_non_adapter(self):
        with pytest.raises(TypeError, match="AgentAdapter"):
            AgentRunner("not an adapter")

    def test_adapter_property(self):
        adapter = self._make_adapter()
        runner = AgentRunner(adapter)
        assert runner.adapter is adapter


# ── HermesAdapter ─────────────────────────────────────────────────────────────

class _FakeAIAgent:
    """Minimal stand-in for AIAgent — does not import run_agent."""

    def __init__(self, return_dict=None):
        self.enabled_toolsets = ["core", "files"]
        self._return = return_dict or {
            "final_response": "hermes says hi",
            "last_reasoning": None,
            "messages": [],
            "api_calls": 1,
            "completed": True,
            "partial": False,
            "interrupted": False,
            "response_previewed": False,
        }
        self._calls = []

    def run_conversation(self, user_message, system_message=None,
                         conversation_history=None, task_id=None,
                         stream_callback=None, persist_user_message=None):
        self._calls.append({
            "user_message": user_message,
            "system_message": system_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "stream_callback": stream_callback,
            "persist_user_message": persist_user_message,
        })
        return self._return


class TestHermesAdapter:
    def test_implements_agent_adapter(self):
        fake = _FakeAIAgent()
        adapter = HermesAdapter(fake)
        assert isinstance(adapter, AgentAdapter)

    def test_agent_id(self):
        adapter = HermesAdapter(_FakeAIAgent())
        assert adapter.agent_id == "hermes"

    def test_capabilities(self):
        adapter = HermesAdapter(_FakeAIAgent())
        caps = adapter.capabilities()
        assert isinstance(caps, AgentCapabilities)
        assert caps.agent_id == "hermes"
        assert caps.supports_streaming is True
        assert caps.supports_reasoning is True
        assert "core" in caps.available_toolsets

    def test_run_delegates_to_run_conversation(self):
        fake = _FakeAIAgent()
        adapter = HermesAdapter(fake)
        ctx = AgentContext(
            user_message="hello",
            system_message="be concise",
            conversation_history=[{"role": "user", "content": "hi"}],
            task_id="t1",
            persist_user_message="hello",
        )
        result = adapter.run(ctx)

        assert len(fake._calls) == 1
        call = fake._calls[0]
        assert call["user_message"] == "hello"
        assert call["system_message"] == "be concise"
        assert call["conversation_history"] == [{"role": "user", "content": "hi"}]
        assert call["task_id"] == "t1"
        assert call["persist_user_message"] == "hello"

    def test_run_returns_agent_result(self):
        adapter = HermesAdapter(_FakeAIAgent())
        result = adapter.run(AgentContext(user_message="hi"))
        assert isinstance(result, AgentResult)
        assert result.final_response == "hermes says hi"
        assert result.completed is True

    def test_run_result_to_dict_matches_original(self):
        original = {
            "final_response": "done",
            "last_reasoning": None,
            "messages": [{"role": "assistant", "content": "done"}],
            "api_calls": 2,
            "completed": True,
            "partial": False,
            "interrupted": False,
            "response_previewed": False,
        }
        fake = _FakeAIAgent(return_dict=original)
        adapter = HermesAdapter(fake)
        result_dict = adapter.run(AgentContext(user_message="q")).to_dict()
        for key, val in original.items():
            assert result_dict[key] == val, f"mismatch on key {key!r}"

    def test_agent_property_exposes_inner_agent(self):
        fake = _FakeAIAgent()
        adapter = HermesAdapter(fake)
        assert adapter.agent is fake

    def test_runner_wrapping_hermes_adapter(self):
        fake = _FakeAIAgent()
        runner = AgentRunner(HermesAdapter(fake))
        result = runner.run(AgentContext(user_message="test via runner"))
        assert result.completed is True
        assert fake._calls[0]["user_message"] == "test via runner"
