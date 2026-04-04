"""
logos.adapters.claude_direct — Claude Direct agent runtime.

Uses the Anthropic Python SDK directly with native tool use.
No Hermes iteration loop, no context compression, no checkpoint manager.
A thin, clean runtime for frontier model comparison and A/B testing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from logos.agent.interface import (
    AgentAdapter,
    AgentCapabilities,
    AgentContext,
    AgentResult,
)

logger = logging.getLogger(__name__)


def _openai_tool_to_anthropic(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an OpenAI function-calling tool definition to Anthropic format.

    OpenAI:  {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    Anthropic: {"name": ..., "description": ..., "input_schema": {...}}
    """
    fn = tool.get("function", {})
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


class ClaudeDirectAdapter(AgentAdapter):
    """
    Agent runtime using the Anthropic SDK directly.

    Implements a simple tool loop: send messages → if tool_use, execute tools
    → append results → repeat. No context compression, no memory flush,
    no reasoning config — just Claude + tools.
    """

    AGENT_ID = "claude-direct"

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: Optional[str] = None,
        system_prompt: Optional[str] = None,
        enabled_toolsets: Optional[List[str]] = None,
        tool_progress_callback: Optional[Callable] = None,
        tool_complete_callback: Optional[Callable] = None,
        max_iterations: int = 20,
        max_tokens: int = 8192,
        session_id: Optional[str] = None,
    ) -> None:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic package required for Claude Direct runtime. "
                "Install with: pip install anthropic"
            )

        self._model = model
        self._client = anthropic.Anthropic(api_key=api_key)
        self._system_prompt = system_prompt or ""
        self._enabled_toolsets = enabled_toolsets
        self._tool_progress_callback = tool_progress_callback
        self._tool_complete_callback = tool_complete_callback
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        self._session_id = session_id
        self._interrupted = False
        self._call_counter = 0

    # ── AgentAdapter interface ────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self.AGENT_ID

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            agent_id=self.AGENT_ID,
            supports_streaming=False,  # TODO: add streaming in follow-up
            supports_reasoning=False,
            supports_parallel_tools=True,
            available_toolsets=self._enabled_toolsets or [],
        )

    def interrupt(self) -> None:
        """Signal the tool loop to stop after the current tool completes."""
        self._interrupted = True

    def run(self, context: AgentContext) -> AgentResult:
        """Execute a conversation turn with Claude using native tool use."""
        # Use callbacks from context if not set at construction time
        progress_cb = self._tool_progress_callback or context.tool_progress_callback
        complete_cb = self._tool_complete_callback or context.tool_complete_callback

        # Build Anthropic messages from conversation history
        messages = self._convert_history(context.conversation_history or [])
        messages.append({"role": "user", "content": context.user_message})

        # Build tool definitions
        tools = self._build_tool_definitions()

        api_calls = 0
        all_messages = list(messages)

        for iteration in range(self._max_iterations):
            if self._interrupted:
                return AgentResult(
                    final_response="[interrupted]",
                    messages=all_messages,
                    api_calls=api_calls,
                    completed=False,
                    interrupted=True,
                )

            try:
                response = self._client.messages.create(
                    model=self._model,
                    system=self._system_prompt,
                    messages=all_messages,
                    tools=tools if tools else [],
                    max_tokens=self._max_tokens,
                )
                api_calls += 1
            except Exception as e:
                logger.error("Claude Direct API error: %s", e)
                return AgentResult(
                    final_response=None,
                    messages=all_messages,
                    api_calls=api_calls,
                    completed=False,
                    extras={"error": str(e), "failed": True},
                )

            # If no tool use, extract text and return
            if response.stop_reason != "tool_use":
                text = "".join(
                    b.text for b in response.content if hasattr(b, "text")
                )
                all_messages.append({
                    "role": "assistant",
                    "content": response.content,
                })
                return AgentResult(
                    final_response=text,
                    messages=all_messages,
                    api_calls=api_calls,
                    completed=True,
                )

            # Handle tool calls
            all_messages.append({
                "role": "assistant",
                "content": response.content,
            })
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                self._call_counter += 1
                call_id = self._call_counter
                tool_name = block.name
                tool_input = dict(block.input) if block.input else {}

                # Notify tool start
                preview = self._build_preview(tool_name, tool_input)
                if progress_cb:
                    try:
                        progress_cb(tool_name, preview, tool_input)
                    except Exception:
                        pass

                # Execute tool
                t_start = time.time()
                try:
                    from core.model_tools import handle_function_call
                    result_str = handle_function_call(
                        tool_name, tool_input,
                        task_id=context.task_id or self._session_id,
                    )
                    is_error = result_str.startswith('{"error"')
                except Exception as tool_err:
                    result_str = json.dumps({"error": str(tool_err)})
                    is_error = True
                duration_ms = (time.time() - t_start) * 1000

                # Notify tool complete
                if complete_cb:
                    try:
                        complete_cb(
                            tool_name, call_id,
                            not is_error, duration_ms,
                            error=result_str[:200] if is_error else None,
                        )
                    except Exception:
                        pass

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                    "is_error": is_error,
                })

            all_messages.append({"role": "user", "content": tool_results})

        # Exhausted iterations
        return AgentResult(
            final_response="[Claude Direct: max iterations reached]",
            messages=all_messages,
            api_calls=api_calls,
            completed=False,
        )

    # ── Internals ────────────────────────────────────────────────────────

    def _convert_history(
        self, history: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI-format conversation history to Anthropic format.

        Strips system messages (handled separately) and converts
        assistant tool_calls / tool results to Anthropic's content block format.
        Simple text messages pass through as-is.
        """
        messages = []
        for msg in history:
            role = msg.get("role", "")
            if role == "system":
                continue  # system prompt handled separately
            content = msg.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str):
                if content:  # skip empty messages
                    messages.append({"role": role, "content": content})
        return messages

    def _build_tool_definitions(self) -> List[Dict[str, Any]]:
        """Get Logos tool definitions and convert to Anthropic format."""
        try:
            from core.model_tools import get_tool_definitions
            openai_tools = get_tool_definitions(
                enabled_toolsets=self._enabled_toolsets,
                quiet_mode=True,
                session_id=self._session_id,
            )
            return [_openai_tool_to_anthropic(t) for t in openai_tools]
        except Exception as e:
            logger.warning("Failed to load tool definitions: %s", e)
            return []

    @staticmethod
    def _build_preview(tool_name: str, args: Dict[str, Any]) -> str:
        """Build a short preview string for tool progress display."""
        if not args:
            return ""
        # Use the first argument value as preview
        first_val = str(next(iter(args.values()), ""))
        if len(first_val) > 60:
            first_val = first_val[:57] + "..."
        return first_val
