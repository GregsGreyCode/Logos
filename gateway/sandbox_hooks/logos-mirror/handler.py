"""
logos-mirror handler — forwards each `agent:end` event from this
sandbox to the host Logos gateway so the web UI's Chats sidebar can
show Telegram / Discord / Slack conversations that otherwise live
entirely inside the sandbox.

Runs inside the sandbox as a hermes hook. Reads the gateway URL +
shared secret from env vars set by the gateway at spawn:

  LOGOS_GATEWAY_URL   — e.g. http://host.openshell.internal:8091
  LOGOS_MIRROR_TOKEN  — shared secret, matched by gateway's
                        mirror_receiver._expected_token

The agent:end payload shape (see opt/hermes/gateway/run.py:4562):
  platform, user_id, session_id, message, response

Additional context we derive locally:
  agent_name      — from HERMES_AGENT_NAME or hostname fallback
  chat_id         — source.chat_id from session_entry if reachable,
                    else user_id (correct for DMs, approximate for
                    groups until we surface source.chat_id in the
                    hook payload upstream)

Failure policy: swallow everything. A mirror dropout should never
affect the turn the user is actually waiting on.
"""

from __future__ import annotations

import logging
import os
import socket
import time

logger = logging.getLogger(__name__)

_GATEWAY_URL = (os.environ.get("LOGOS_GATEWAY_URL") or "").rstrip("/")
_MIRROR_TOKEN = (os.environ.get("LOGOS_MIRROR_TOKEN") or "").strip()


def _agent_name() -> str:
    # Explicit env wins; fall back to the pod hostname which is set
    # to `hermes-<sanitized_agent_name>` by the executor. Stripping
    # the prefix gives us the name the gateway knows about.
    explicit = (os.environ.get("HERMES_AGENT_NAME") or "").strip()
    if explicit:
        return explicit
    try:
        host = socket.gethostname() or ""
    except Exception:
        host = ""
    if host.startswith("hermes-"):
        return host[len("hermes-"):]
    return host


async def handle(event_type: str, context: dict) -> None:
    """Called by hermes' HookRegistry on `agent:end`. Async, fire-and-forget."""
    if event_type != "agent:end":
        return
    if not _GATEWAY_URL or not _MIRROR_TOKEN:
        # Gateway didn't provision us — pre-mirror hermes, dev mode,
        # or a stale sandbox. Skip silently.
        return

    platform = (context.get("platform") or "").strip().lower()
    if not platform or platform in ("local", "cli", "api_server", "cron"):
        # Only mirror "real" external platforms. Local web-UI chats
        # already flow through the gateway's own /chat path and would
        # double-record if we mirrored them here.
        return

    user_id  = str(context.get("user_id") or "").strip()
    user_msg = (context.get("message") or "").strip()
    reply    = (context.get("response") or "").strip()
    sess_id  = (context.get("session_id") or "").strip() or None

    # Shape payload for the gateway receiver. For DMs chat_id == user_id;
    # for groups hermes already knows source.chat_id but it isn't in
    # hook_ctx yet — until run.py is extended, DM-only is acceptable
    # since groups were never in the Chats sidebar either.
    payload = {
        "platform":    platform,
        "agent_name":  _agent_name(),
        "chat_id":     user_id,  # DM: user_id == chat_id
        "user_id":     user_id,
        "user_name":   context.get("user_name") or "",
        "user_msg":    user_msg,
        "agent_reply": reply,
        "session_id":  sess_id,
        "started_at":  float(context.get("started_at") or time.time()),
        "ended_at":    time.time(),
    }

    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=3)
        headers = {
            "Content-Type":   "application/json",
            "X-Mirror-Token": _MIRROR_TOKEN,
        }
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                f"{_GATEWAY_URL}/api/internal/mirror-turn",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    logger.debug(
                        "logos-mirror: gateway returned %d (%s)",
                        resp.status, await resp.text(),
                    )
    except Exception as exc:
        logger.debug("logos-mirror: POST failed: %s", exc)
