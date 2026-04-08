"""
Pydantic v2 argument schemas for Logos MCP tools.

Each tool registers its handler with a JSON Schema generated from the
corresponding model via ``model.model_json_schema()``. That way the
sandbox-side tool catalogue gets free validation, auto-complete and
type hints without us hand-writing JSON Schema.

Keeping the schemas in a single module makes it easy to cross-reference
what types each tool accepts and to share common field validators across
tools (e.g. both ``platform_send`` and future ``platform_edit`` accept a
``channel`` field).
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Platform tools — L.2
# ---------------------------------------------------------------------------

# The platform literal is intentionally restrictive. Adding a new platform
# needs a corresponding adapter in gateway/platforms/, so we fail fast at
# arg-validation time rather than letting the tool discover "unknown
# platform" at runtime.
PlatformLiteral = Literal[
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "signal",
    "email",
    "homeassistant",
]


class PlatformSendArgs(BaseModel):
    """Send a message to an arbitrary platform channel.

    ``channel`` is the platform-specific recipient identifier — a Telegram
    chat_id, a Discord channel snowflake, a Slack channel ID, a WhatsApp
    phone number, etc. The adapter layer knows how to interpret it.

    ``reply_to`` is the platform-native message ID to thread this reply
    beneath. Used for reply-in-thread auto-approval in the dispatcher.
    """

    platform: PlatformLiteral = Field(
        ...,
        description="Target platform. Must have an adapter running on the gateway.",
    )
    channel: str = Field(
        ...,
        min_length=1,
        description="Platform-specific channel/chat/user ID to send the message to.",
    )
    text: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description="Message text. Markdown may be honoured depending on the platform.",
    )
    media_urls: Optional[List[str]] = Field(
        default=None,
        description="Optional list of media URLs to attach (images, audio, etc). L.2 accepts but does not yet send media — media support lands alongside adapter.send_image.",
    )
    reply_to: Optional[str] = Field(
        default=None,
        description="Optional platform-native message ID to reply-in-thread. When set, the dispatcher auto-approves the send; otherwise outbound to a new channel may require user approval.",
    )


class HomeMessageArgs(BaseModel):
    """Send a message to the user's configured home channel.

    The home channel is picked from the gateway config's ``home_channel``
    entry for each platform. If ``platform`` is omitted, the first
    platform that has a home channel configured is used. This tool is
    the canonical way for an agent to reach its user without knowing any
    platform-specific IDs.
    """

    text: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description="Message text to deliver to the user's home channel.",
    )
    platform: Optional[PlatformLiteral] = Field(
        default=None,
        description="Which platform's home channel to target. If omitted, the gateway picks the first configured home channel.",
    )
    media_urls: Optional[List[str]] = Field(
        default=None,
        description="Optional media attachments. See PlatformSendArgs.media_urls note.",
    )
