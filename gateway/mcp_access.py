"""
Per-session MCP access grant registry.

Tracks which MCP servers each session has been approved to use.
Thread-safe. In-memory only (grants reset on gateway restart).

Usage::

    from gateway.mcp_access import grant_access, has_access, get_grants, clear_session

    # Grant a session access to a server (called after approval)
    grant_access(session_id, "filesystem")

    # Check before dispatching a tool call
    if has_access(session_id, "filesystem"):
        ...

    # Get full approved set for a session
    for server_name in get_grants(session_id):
        ...

    # Clean up when session ends
    clear_session(session_id)
"""

import logging
import threading
from typing import FrozenSet

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# session_id → set of approved server names
_grants: dict[str, set[str]] = {}


def grant_access(session_id: str, server_name: str) -> None:
    """Approve a session's access to a named MCP server."""
    with _lock:
        if session_id not in _grants:
            _grants[session_id] = set()
        _grants[session_id].add(server_name)
    logger.info("mcp_access: granted session=%s server=%s", session_id, server_name)


def revoke_access(session_id: str, server_name: str) -> None:
    """Revoke a session's access to a named MCP server."""
    with _lock:
        if session_id in _grants:
            _grants[session_id].discard(server_name)
    logger.info("mcp_access: revoked session=%s server=%s", session_id, server_name)


def has_access(session_id: str, server_name: str) -> bool:
    """Return True if the session has been granted access to the server."""
    with _lock:
        return server_name in _grants.get(session_id, set())


def get_grants(session_id: str) -> FrozenSet[str]:
    """Return the frozenset of server names approved for this session."""
    with _lock:
        return frozenset(_grants.get(session_id, set()))


def clear_session(session_id: str) -> None:
    """Remove all grants for a session (call on session end)."""
    with _lock:
        removed = _grants.pop(session_id, None)
    if removed:
        logger.debug("mcp_access: cleared %d grants for session=%s", len(removed), session_id)


def all_grants() -> dict[str, FrozenSet[str]]:
    """Return a snapshot of all active grants (for admin/status endpoints)."""
    with _lock:
        return {sid: frozenset(servers) for sid, servers in _grants.items()}
