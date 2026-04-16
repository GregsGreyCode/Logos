"""Regression test: dispatch_platform_message must enrich attachments.

When the legacy ``_handle_message`` was deleted in Phase 5.6
(commit ``9270201``), the inbound platform path moved to
``GatewayRunner.dispatch_platform_message``. The attachment-enrichment
step (audio→transcription, image→vision, document→context-note) was
dropped from the new path and tracked as a follow-up in the
``platforms-as-gateway-mediated`` migration doc.

The user-visible symptom: voice messages sent on Telegram / Discord /
Slack / WhatsApp / Signal arrived at the agent without being
transcribed — the agent saw an empty caption and could not respond
sensibly to the user's voice.

This test pins the wiring so the regression cannot recur silently.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.run import GatewayRunner
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.config import Platform


@pytest.fixture
def gateway(tmp_path):
    """Build a bare GatewayRunner with the bits dispatch_platform_message needs."""
    config = MagicMock()
    config.sessions_dir = tmp_path
    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    # Seeded by __init__; dispatch_platform_message writes to it to populate
    # the Live Executions panel, so the bypassed-__init__ fixture needs it too.
    gw._session_status = {}

    # session_store: returns a session entry with the right shape
    session_entry = MagicMock()
    session_entry.session_id = "test-session-id"
    session_entry.session_key = "test-session-key"
    gw.session_store = MagicMock()
    gw.session_store.get_or_create_session.return_value = session_entry

    # worker_registry: present, healthy worker, dispatch returns a fixed reply
    worker_entry = MagicMock()
    worker_entry.healthy = True
    worker_entry.ws = MagicMock()
    worker_entry.ws.closed = False
    worker_entry.toolsets = ["hermes-cli"]
    gw.worker_registry = MagicMock()
    gw.worker_registry.get.return_value = worker_entry
    gw.worker_registry.dispatch_task = AsyncMock(
        return_value={"status": "ok", "response": "agent reply"}
    )

    return gw


@pytest.mark.asyncio
async def test_dispatch_enriches_audio_attachments(gateway):
    """Voice messages must be transcribed before reaching the sandbox worker.

    Verifies the wiring fixed in the audio-to-text repair after the
    Phase 5.6 _handle_message deletion.
    """
    enriched_text = (
        '[The user sent a voice message~ '
        'Here\'s what they said: "hello world"]\n\nhi'
    )

    event = MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="100",
            user_id="42",
            user_name="alice",
        ),
        media_urls=["/tmp/voice_abc.ogg"],
        media_types=["audio/ogg"],
    )

    # Stub the auth DB lookups so dispatch_platform_message reaches the
    # enrichment + dispatch step. We don't care which agent it picks —
    # only that the enriched message ends up in the task payload.
    fake_agent = {"id": "a1", "name": "test-agent"}
    fake_db = MagicMock()
    fake_db.list_agents.return_value = [fake_agent]
    fake_db.resolve_platform_routing.return_value = None
    fake_db.get_agent.return_value = fake_agent

    # Patch the enrichment method on the runner; the real implementation
    # would need vision + transcription tool stacks loaded, which is
    # out of scope for this regression test. We only assert the call.
    gateway._enrich_message_with_attachments = AsyncMock(return_value=enriched_text)

    with patch("gateway.auth.db", fake_db), \
         patch("gateway.session.build_agent_system_prompt", return_value="system"), \
         patch("gateway.executors.openshell._sanitize_sandbox_name", return_value="hermes-test-agent"):
        result = await gateway.dispatch_platform_message(event)

    # 1. Enrichment was invoked with the audio attachment
    gateway._enrich_message_with_attachments.assert_awaited_once_with(
        "hi",
        ["/tmp/voice_abc.ogg"],
        ["audio/ogg"],
    )

    # 2. The dispatched task payload carries the enriched text, NOT the
    #    raw event.text. This is the load-bearing assertion — if the fix
    #    regresses, the worker will receive "hi" instead of the
    #    transcript-prefixed message.
    gateway.worker_registry.dispatch_task.assert_awaited_once()
    _args, kwargs = gateway.worker_registry.dispatch_task.await_args
    payload = _args[1] if len(_args) > 1 else kwargs.get("task_payload")
    # dispatch_task is called positionally as (worker_id, task_payload, ...)
    if payload is None:
        # second positional arg
        payload = _args[1]
    assert payload["message"] == enriched_text, (
        f"task_payload.message should be the enriched text, got {payload['message']!r}"
    )

    # 3. The dispatch returned a result (sanity)
    assert result is not None


@pytest.mark.asyncio
async def test_dispatch_skips_enrichment_when_no_media(gateway):
    """No media_urls → skip the enrichment call entirely (cheap path)."""
    event = MessageEvent(
        text="just text",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="100",
            user_id="42",
            user_name="alice",
        ),
        media_urls=[],
        media_types=[],
    )

    fake_agent = {"id": "a1", "name": "test-agent"}
    fake_db = MagicMock()
    fake_db.list_agents.return_value = [fake_agent]
    fake_db.resolve_platform_routing.return_value = None
    fake_db.get_agent.return_value = fake_agent

    gateway._enrich_message_with_attachments = AsyncMock()

    with patch("gateway.auth.db", fake_db), \
         patch("gateway.session.build_agent_system_prompt", return_value="system"), \
         patch("gateway.executors.openshell._sanitize_sandbox_name", return_value="hermes-test-agent"):
        await gateway.dispatch_platform_message(event)

    # Enrichment should NOT have been called (no media to enrich)
    gateway._enrich_message_with_attachments.assert_not_awaited()

    # And the raw text reaches the worker unchanged
    _args, _kwargs = gateway.worker_registry.dispatch_task.await_args
    payload = _args[1]
    assert payload["message"] == "just text"


@pytest.mark.asyncio
async def test_dispatch_continues_when_enrichment_fails(gateway):
    """If transcription/vision blows up, fall through with the raw text
    rather than dropping the dispatch entirely. The user still gets a
    reply (to the caption / surrounding text) instead of silent
    failure."""
    event = MessageEvent(
        text="caption",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="100",
            user_id="42",
            user_name="alice",
        ),
        media_urls=["/tmp/bad.ogg"],
        media_types=["audio/ogg"],
    )

    fake_agent = {"id": "a1", "name": "test-agent"}
    fake_db = MagicMock()
    fake_db.list_agents.return_value = [fake_agent]
    fake_db.resolve_platform_routing.return_value = None
    fake_db.get_agent.return_value = fake_agent

    gateway._enrich_message_with_attachments = AsyncMock(
        side_effect=RuntimeError("whisper crashed")
    )

    with patch("gateway.auth.db", fake_db), \
         patch("gateway.session.build_agent_system_prompt", return_value="system"), \
         patch("gateway.executors.openshell._sanitize_sandbox_name", return_value="hermes-test-agent"):
        result = await gateway.dispatch_platform_message(event)

    # Dispatch still happened with the raw caption
    gateway.worker_registry.dispatch_task.assert_awaited_once()
    _args, _kwargs = gateway.worker_registry.dispatch_task.await_args
    payload = _args[1]
    assert payload["message"] == "caption"
    assert result is not None
