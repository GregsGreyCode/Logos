"""
request_mcp_access — agent tool for requesting access to a gateway-managed MCP server.

Self-registers with the tool registry at import time so it is available in all
agent toolsets that include "mcp-gateway" (added to hermes-* toolsets by
model_tools._discover_tools when HERMES_GATEWAY_MCP=1).

Flow:
  1. Agent calls request_mcp_access(server_name, reason).
  2. Tool looks up the server in the gateway catalogue.
  3. Checks the policy tier for that server's category:
       auto_approve  → grants immediately, injects tools, returns "approved"
       user_approve  → creates an approval request, returns "pending"
       admin_approve → creates an approval request (admin-only), returns "pending"
       deny          → returns "denied"
  4. On "pending": the user approves or denies via Telegram / web UI.
     The approval webhook in http_api.py calls grant_access() + inject tools.

When running outside the gateway (HERMES_GATEWAY_MCP not set), the tool
returns a clear error rather than crashing.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_TOOL_NAME    = "request_mcp_access"
_TOOLSET_NAME = "mcp-gateway"


def _handler(args: dict, **kwargs) -> str:
    server_name: str          = args.get("server_name", "").strip()
    reason:      str          = args.get("reason", "").strip()
    session_id:  Optional[str] = kwargs.get("session_id")
    policy:      object        = kwargs.get("policy")
    auth_user_id: Optional[str] = kwargs.get("auth_user_id")

    if not server_name:
        return json.dumps({"error": "server_name is required"})

    # ── Gateway mode check ─────────────────────────────────────────────────
    if os.getenv("HERMES_GATEWAY_MCP") != "1":
        return json.dumps({
            "status": "error",
            "message": (
                "Gateway MCP service is not active. "
                "MCP servers are configured directly in ~/.logos/config.yaml "
                "and loaded at agent startup."
            ),
        })

    if not session_id:
        return json.dumps({"error": "No session_id available — cannot track MCP grant"})

    # ── Import service ─────────────────────────────────────────────────────
    try:
        from gateway import mcp_service as _svc_mod
        svc = _get_gateway_service()
    except Exception as exc:
        return json.dumps({"error": f"MCP gateway service unavailable: {exc}"})

    if svc is None:
        return json.dumps({
            "status": "error",
            "message": "Gateway MCP service is not running. Ask the administrator to restart the gateway.",
        })

    # ── Validate server name ───────────────────────────────────────────────
    catalogue = svc.get_catalogue()
    server_entry = next((s for s in catalogue if s["name"] == server_name), None)
    if server_entry is None:
        available = [s["name"] for s in catalogue]
        return json.dumps({
            "status": "error",
            "message": f"Server '{server_name}' not found. Available: {available}",
        })

    if not server_entry["connected"]:
        return json.dumps({
            "status": "error",
            "message": f"Server '{server_name}' is configured but currently not connected.",
        })

    # ── Already granted? ───────────────────────────────────────────────────
    from gateway.mcp_access import has_access, grant_access
    if has_access(session_id, server_name):
        return json.dumps({
            "status": "already_granted",
            "message": f"You already have access to '{server_name}'. The tools are available.",
            "toolset": f"mcp-{server_name}",
        })

    # ── Policy tier ────────────────────────────────────────────────────────
    tier = svc.get_server_policy_tier(server_name)
    category = server_entry.get("category", "general")

    if tier == _svc_mod.TIER_DENY:
        return json.dumps({
            "status": "denied",
            "message": f"Access to '{server_name}' (category: {category}) is not permitted.",
        })

    if tier == _svc_mod.TIER_AUTO:
        # Auto-approve: grant immediately and inject tools
        grant_access(session_id, server_name)
        _inject_tools(session_id, server_name, svc)
        logger.info("mcp_access_tool: auto-granted session=%s server=%s", session_id, server_name)
        return json.dumps({
            "status":  "approved",
            "message": (
                f"Access to '{server_name}' granted automatically. "
                f"The tools will be available from your next message."
            ),
            "toolset": f"mcp-{server_name}",
        })

    # ── Approval request (user or admin tier) ─────────────────────────────
    try:
        approval_id = _create_approval_request(
            server_name=server_name,
            reason=reason,
            session_id=session_id,
            category=category,
            tier=tier,
            policy=policy,
            auth_user_id=auth_user_id,
        )
        tier_label = "an administrator" if tier == _svc_mod.TIER_ADMIN else "you"
        return json.dumps({
            "status":      "pending",
            "approval_id": approval_id,
            "message": (
                f"Access to '{server_name}' requires approval from {tier_label}. "
                f"An approval request has been sent. Once approved, the tools will "
                f"be available from your next message."
            ),
        })
    except Exception as exc:
        logger.error("mcp_access_tool: failed to create approval request: %s", exc)
        return json.dumps({"error": f"Failed to create approval request: {exc}"})


def _get_gateway_service():
    """Retrieve the MCPGatewayService from the gateway app context."""
    # The service is stored in the aiohttp app dict; access it via a module-level
    # reference set by http_api.py when the service starts.
    try:
        from gateway import _mcp_service_ref  # set by http_api.py at startup
        return _mcp_service_ref
    except (ImportError, AttributeError):
        return None


def _inject_tools(session_id: str, server_name: str, svc) -> None:
    """Register tools from a newly approved gateway server into the toolset registry.

    The toolset name mcp-{server_name} is already registered globally by
    _discover_and_register_server when the gateway started the server.
    This is a no-op for tool registration — the grant stored in mcp_access
    is what gates access. On the next _run_agent call, the approved toolset
    is added to enabled_toolsets automatically.
    """
    logger.debug("mcp_access_tool: tools for '%s' will be available next turn", server_name)


def _create_approval_request(
    server_name: str,
    reason: str,
    session_id: str,
    category: str,
    tier: str,
    policy,
    auth_user_id: Optional[str],
) -> str:
    """Create a policy approval request for MCP server access.

    Returns the approval ID string.
    """
    from tools.approval import create_policy_approval_request
    from gateway.auth.policy import ACTION_MCP_ACCESS

    tool_args = {
        "server_name": server_name,
        "category":    category,
        "tier":        tier,
        "reason":      reason,
    }

    # For admin-tier requests, find a policy that requires admin approval.
    # For user-tier, use the session's current policy.
    policy_id = getattr(policy, "id", None) if policy else None

    return create_policy_approval_request(
        tool_name=_TOOL_NAME,
        tool_args=tool_args,
        session_id=session_id,
        policy_id=policy_id,
        expires_in=3600,
        action_type_override=ACTION_MCP_ACCESS,
    )


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

def _register():
    try:
        from tools.registry import registry
        from core.toolsets import create_custom_toolset

        schema = {
            "name": _TOOL_NAME,
            "description": (
                "Request access to a centralized MCP server by name. "
                "The gateway manages a set of MCP servers (filesystem, web search, APIs, etc.). "
                "Use this tool to request access to one — you will be notified once approved. "
                "Call get_mcp_catalogue first if you are unsure which servers are available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type":        "string",
                        "description": "The name of the MCP server to request access to (from the catalogue).",
                    },
                    "reason": {
                        "type":        "string",
                        "description": "Brief explanation of why you need access to this server.",
                    },
                },
                "required": ["server_name", "reason"],
            },
        }

        catalogue_schema = {
            "name":        "get_mcp_catalogue",
            "description": "List all available MCP servers on this gateway, including their names, categories, and approval tiers.",
            "parameters":  {"type": "object", "properties": {}},
        }

        registry.register(
            name=_TOOL_NAME,
            toolset=_TOOLSET_NAME,
            schema=schema,
            handler=_handler,
            is_async=False,
            description=schema["description"],
        )

        registry.register(
            name="get_mcp_catalogue",
            toolset=_TOOLSET_NAME,
            schema=catalogue_schema,
            handler=_catalogue_handler,
            is_async=False,
            description=catalogue_schema["description"],
        )

        create_custom_toolset(
            name=_TOOLSET_NAME,
            description="Gateway MCP access management tools",
            tools=[_TOOL_NAME, "get_mcp_catalogue"],
        )

        logger.debug("mcp_access_tool: registered %s and get_mcp_catalogue", _TOOL_NAME)
    except Exception as exc:
        logger.debug("mcp_access_tool: registration failed: %s", exc)


def _catalogue_handler(args: dict, **kwargs) -> str:
    """Return the MCP catalogue as a formatted list for the agent."""
    if os.getenv("HERMES_GATEWAY_MCP") != "1":
        return json.dumps({
            "status": "error",
            "message": "Gateway MCP service is not active.",
        })
    svc = _get_gateway_service()
    if svc is None:
        return json.dumps({"servers": [], "message": "MCP gateway service not running"})
    catalogue = svc.get_catalogue()
    return json.dumps({
        "servers": catalogue,
        "message": (
            f"{len(catalogue)} server(s) available. "
            "Use request_mcp_access(server_name, reason) to request access to one."
        ),
    })


_register()
