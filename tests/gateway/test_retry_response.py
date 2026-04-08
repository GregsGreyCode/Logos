"""Regression test: /retry must return the agent response, not None.

Originally tracked PR #441 where _handle_retry_command() called
_handle_message(retry_event) and discarded the return value. Phase 5.6
deleted _handle_message and re-pointed _handle_retry_command at
dispatch_platform_message; the same return-the-inner-result invariant
applies to the new code path.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from gateway.run import GatewayRunner
from gateway.platforms.base import MessageEvent, MessageType


@pytest.fixture
def gateway(tmp_path):
    config = MagicMock()
    config.sessions_dir = tmp_path
    config.max_context_messages = 20
    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    gw.session_store = MagicMock()
    return gw


@pytest.mark.asyncio
async def test_retry_returns_response_not_none(gateway):
    """_handle_retry_command must return the inner handler response, not None."""
    gateway.session_store.get_or_create_session.return_value = MagicMock(
        session_id="test-session"
    )
    gateway.session_store.load_transcript.return_value = [
        {"role": "user", "content": "Hello Hermes"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    gateway.session_store.rewrite_transcript = MagicMock()
    expected_response = "Hi there! (retried)"
    gateway.dispatch_platform_message = AsyncMock(return_value=expected_response)
    event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=MagicMock(),
    )
    result = await gateway._handle_retry_command(event)
    assert result is not None, "/retry must not return None"
    assert result == expected_response


@pytest.mark.asyncio
async def test_retry_no_previous_message(gateway):
    """If there is no previous user message, return early with a message."""
    gateway.session_store.get_or_create_session.return_value = MagicMock(
        session_id="test-session"
    )
    gateway.session_store.load_transcript.return_value = []
    event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=MagicMock(),
    )
    result = await gateway._handle_retry_command(event)
    assert result == "No previous message to retry."
