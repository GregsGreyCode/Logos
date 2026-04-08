"""
Platform tools for the Logos capabilities MCP server (Phase L.2).

Exposes two tools to sandboxed agents:

- ``platform_send``: Send a message to a specific platform/channel via a
  gateway-held adapter. The sandbox never touches the bot token — it
  just hands the gateway a ``(platform, channel, text)`` tuple and the
  gateway dispatches through the in-process adapter.
- ``home_message``: Send a message to the user's configured "home
  channel" without needing to know any platform-specific IDs. The
  gateway resolves the home channel from config and dispatches it.

Both tools return the standard Logos MCP envelope from
``InProcessMCPServer.call_tool``: ``{ok, data, error, tool, duration_ms}``.

Security posture:

- The bot token lives in the gateway process env and is only read by the
  platform adapter code. Tools here never see the token and have no
  mechanism to exfiltrate it — they only hold a reference to the adapter
  object and call ``adapter.send(...)``.
- ``calling_agent`` (if provided by L.3+ resolution) is threaded through
  for audit logging but not used for authorisation gating yet. The
  existing MCP approval-tier machinery (user_approve vs auto_approve) is
  the gate, and for platform tools the tier is picked by the dispatcher
  based on whether ``reply_to`` is set (reply-in-thread = auto, outbound
  to new channel = user approve — see design doc section 4.4).

Adapter-missing behaviour:

- Phase 5.1 of the platforms migration disabled adapter startup in
  ``GatewayRunner.start()``, so ``runner.adapters`` is currently empty
  for new deploys. Calls to ``platform_send`` will therefore return a
  structured ``AdapterUnavailable`` error until Phase 5.4 re-enables the
  adapters. The error envelope flows back through the standard return
  path so the sandbox sees a clean failure, not an exception trace.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import ValidationError

from gateway.mcp_logos.schemas import HomeMessageArgs, PlatformSendArgs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_platform_enum(runner: Any, platform_value: str) -> Optional[Any]:
    """Resolve a platform string ("telegram") to the runner's Platform enum."""
    try:
        from gateway.config import Platform
        return Platform(platform_value)
    except Exception:
        return None


def _lookup_adapter(runner: Any, platform_value: str) -> Any:
    """Return the adapter for the given platform, or None."""
    if runner is None or not hasattr(runner, "adapters"):
        return None
    enum = _get_platform_enum(runner, platform_value)
    if enum is None:
        return None
    return runner.adapters.get(enum)


def _adapter_unavailable(platform_value: str) -> dict:
    """Raise-like sentinel: returned as tool data but marks ok=False upstream."""
    raise _AdapterUnavailable(platform_value)


class _AdapterUnavailable(Exception):
    """Raised by tool bodies when no adapter is bound for the requested platform.

    Caught by :class:`InProcessMCPServer.call_tool` which converts the
    exception into a structured ``AdapterUnavailable`` error envelope so
    the sandbox sees a clean failure.
    """


# ---------------------------------------------------------------------------
# platform_send
# ---------------------------------------------------------------------------


def _make_platform_send_handler(server):
    """Build an async handler closed over the server (which holds runner ref)."""

    async def platform_send(calling_agent: Optional[dict], args: dict) -> dict:
        try:
            parsed = PlatformSendArgs.model_validate(args)
        except ValidationError as exc:
            raise ValueError(f"invalid args: {exc.errors()[0]['msg']}") from exc

        runner = server.runner
        adapter = _lookup_adapter(runner, parsed.platform)
        if adapter is None:
            raise _AdapterUnavailable(parsed.platform)

        # Call the adapter. BasePlatformAdapter.send signature is
        # ``async def send(chat_id, content, reply_to=None, metadata=None)``
        # so map our validated args to its kwargs.
        result = await adapter.send(
            chat_id=parsed.channel,
            content=parsed.text,
            reply_to=parsed.reply_to,
        )

        # SendResult is a dataclass from gateway.platforms.base.
        # Serialise to a plain dict for the envelope.
        if not getattr(result, "success", False):
            return {
                "sent":       False,
                "platform":   parsed.platform,
                "channel":    parsed.channel,
                "message_id": None,
                "error":      getattr(result, "error", None) or "send failed",
            }

        # Audit log: who called what. calling_agent may be None in L.2
        # (worker-id resolution is wired in when platforms start
        # dispatching via this path in Phase 5.3).
        agent_hint = (calling_agent or {}).get("name", "unknown")
        logger.info(
            "mcp_logos.platform_send: agent=%s platform=%s channel=%s message_id=%s",
            agent_hint, parsed.platform, parsed.channel, getattr(result, "message_id", None),
        )
        return {
            "sent":       True,
            "platform":   parsed.platform,
            "channel":    parsed.channel,
            "message_id": getattr(result, "message_id", None),
            "error":      None,
        }

    return platform_send


# ---------------------------------------------------------------------------
# home_message
# ---------------------------------------------------------------------------


def _make_home_message_handler(server):

    async def home_message(calling_agent: Optional[dict], args: dict) -> dict:
        try:
            parsed = HomeMessageArgs.model_validate(args)
        except ValidationError as exc:
            raise ValueError(f"invalid args: {exc.errors()[0]['msg']}") from exc

        runner = server.runner
        if runner is None or not hasattr(runner, "config"):
            raise _AdapterUnavailable("any")

        # Pick the platform: caller-specified, else the first platform that
        # has a home channel configured.
        from gateway.config import Platform
        candidates: list
        if parsed.platform:
            try:
                candidates = [Platform(parsed.platform)]
            except ValueError:
                raise ValueError(f"unknown platform '{parsed.platform}'")
        else:
            # Iterate in stable order so tests are deterministic
            candidates = list(Platform)

        home_channel = None
        chosen_platform = None
        for p in candidates:
            hc = runner.config.get_home_channel(p) if hasattr(runner.config, "get_home_channel") else None
            if hc is not None:
                home_channel = hc
                chosen_platform = p
                break

        if home_channel is None or chosen_platform is None:
            return {
                "sent":     False,
                "platform": None,
                "channel":  None,
                "error":    "no home channel configured",
            }

        adapter = runner.adapters.get(chosen_platform) if hasattr(runner, "adapters") else None
        if adapter is None:
            raise _AdapterUnavailable(chosen_platform.value)

        result = await adapter.send(
            chat_id=home_channel.chat_id,
            content=parsed.text,
        )

        if not getattr(result, "success", False):
            return {
                "sent":     False,
                "platform": chosen_platform.value,
                "channel":  home_channel.chat_id,
                "error":    getattr(result, "error", None) or "send failed",
            }

        agent_hint = (calling_agent or {}).get("name", "unknown")
        logger.info(
            "mcp_logos.home_message: agent=%s platform=%s channel=%s",
            agent_hint, chosen_platform.value, home_channel.chat_id,
        )
        return {
            "sent":       True,
            "platform":   chosen_platform.value,
            "channel":    home_channel.chat_id,
            "message_id": getattr(result, "message_id", None),
        }

    return home_message


# ---------------------------------------------------------------------------
# Public registration entry point
# ---------------------------------------------------------------------------


def register(server) -> None:
    """Register both platform tools on the given InProcessMCPServer."""
    server.register_tool(
        name="platform_send",
        description=(
            "Send a text message to a specific channel on a messaging platform "
            "(telegram, discord, slack, etc.) using the gateway's bot token. "
            "Use this when you need to reach a particular channel by its ID."
        ),
        input_schema=PlatformSendArgs.model_json_schema(),
        handler=_make_platform_send_handler(server),
        # Default to user_approve; the dispatcher overrides to auto_approve
        # when reply_to is set (reply-in-thread). Phase 4.4 of the design.
        tier="user_approve",
        timeout_s=30.0,
    )
    server.register_tool(
        name="home_message",
        description=(
            "Send a text message to the user's configured home channel without "
            "needing to know any platform-specific IDs. Use this for proactive "
            "updates, cron results, or anywhere you want to reach the user "
            "through whichever platform they've set as home."
        ),
        input_schema=HomeMessageArgs.model_json_schema(),
        handler=_make_home_message_handler(server),
        tier="auto_approve",
        timeout_s=30.0,
    )
