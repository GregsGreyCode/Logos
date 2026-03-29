"""
request_tools — lazy tool loading meta-tool.

Agents start with a small set of core tools to keep token usage low.
When they need additional capabilities (web search, browser, image gen,
etc.), they call request_tools(categories) to inject those tool schemas
into the next API call.

This follows the same pattern as request_mcp_access — the agent asks
for what it needs, the gateway provides it.

Core tools (~2-3K tokens, always loaded):
  terminal, read_file, write_file, patch, search_files,
  memory, todo, clarify, request_tools

Extended tools (~6-7K tokens, loaded on demand):
  web, browser, image, vision, tts, delegation, code,
  workflows, cron, messaging, logs, skills, process, bugs

The split point is: can the agent have a useful conversation with just
the core tools? Yes — it can read/write files, run commands, search
code, and remember things. The extended tools are for specific tasks.
"""

import json
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool categories → toolset names (maps user-friendly names to registry IDs)
# ---------------------------------------------------------------------------

TOOL_CATEGORIES = {
    "web":        {"tools": ["web_search", "web_extract"], "description": "Web search and content extraction (Firecrawl)"},
    "browser":    {"tools": ["browser_navigate", "browser_click", "browser_type", "browser_snapshot", "browser_scroll", "browser_press", "browser_back", "browser_close", "browser_get_images", "browser_vision", "browser_console"], "description": "Browser automation"},
    "image":      {"tools": ["image_generate"], "description": "Image generation (fal.ai)"},
    "vision":     {"tools": ["vision_analyze"], "description": "Image analysis using AI vision"},
    "tts":        {"tools": ["text_to_speech"], "description": "Text-to-speech audio generation"},
    "delegation": {"tools": ["delegate_task", "execute_code", "mixture_of_agents"], "description": "Subagent spawning and programmatic tool calling"},
    "workflows":  {"tools": ["workflow"], "description": "Multi-step DAG task workflows"},
    "cron":       {"tools": ["schedule_cronjob", "list_cronjobs", "remove_cronjob"], "description": "Scheduled task management"},
    "messaging":  {"tools": ["send_message"], "description": "Cross-platform message delivery"},
    "logs":       {"tools": ["log_inspector"], "description": "Runtime log analysis"},
    "skills":     {"tools": ["skill_manage", "skill_view", "skills_list"], "description": "Skill management and browsing"},
    "process":    {"tools": ["process"], "description": "Background process management"},
    "bugs":       {"tools": ["bug_notes"], "description": "Self-reported bug tracking"},
    "session":    {"tools": ["session_search"], "description": "Long-term conversation memory search"},
}

# Core tools — always loaded regardless of lazy mode
CORE_TOOLS = frozenset({
    "terminal",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "memory",
    "todo",
    "clarify",
})

# ---------------------------------------------------------------------------
# Session-level tool grants (same pattern as mcp_access)
# ---------------------------------------------------------------------------

import threading

_lock = threading.Lock()
_session_tools: dict[str, set[str]] = {}  # session_id → set of granted tool names


def grant_tools(session_id: str, tool_names: list[str]) -> None:
    """Grant additional tools to a session."""
    with _lock:
        if session_id not in _session_tools:
            _session_tools[session_id] = set()
        _session_tools[session_id].update(tool_names)


def get_granted_tools(session_id: str) -> frozenset[str]:
    """Return the set of tools granted to this session beyond core."""
    with _lock:
        return frozenset(_session_tools.get(session_id, set()))


def clear_session(session_id: str) -> None:
    """Clean up when session ends."""
    with _lock:
        _session_tools.pop(session_id, None)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

_TOOL_NAME = "request_tools"


def _handler(args: dict, **kwargs) -> str:
    categories = args.get("categories") or []
    session_id = kwargs.get("session_id")

    if not categories:
        # List available categories
        cat_list = []
        for cat, info in TOOL_CATEGORIES.items():
            cat_list.append(f"  {cat}: {info['description']} ({len(info['tools'])} tools)")
        return json.dumps({
            "available_categories": list(TOOL_CATEGORIES.keys()),
            "details": "\n".join(cat_list),
            "message": "Call request_tools with the categories you need.",
        })

    granted = []
    not_found = []
    for cat in categories:
        cat = cat.strip().lower()
        if cat in TOOL_CATEGORIES:
            tools = TOOL_CATEGORIES[cat]["tools"]
            if session_id:
                grant_tools(session_id, tools)
            granted.extend(tools)
            logger.info("request_tools: granted %s tools to session %s: %s", cat, session_id, tools)
        else:
            not_found.append(cat)

    result = {
        "status": "granted",
        "tools_added": granted,
        "message": f"Added {len(granted)} tools. They will be available from your next message.",
    }
    if not_found:
        result["not_found"] = not_found
        result["available_categories"] = list(TOOL_CATEGORIES.keys())

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

def _register():
    try:
        from tools.registry import registry

        cat_names = ", ".join(TOOL_CATEGORIES.keys())
        schema = {
            "name": _TOOL_NAME,
            "description": (
                "Request additional tool capabilities beyond the core set. "
                f"Available categories: {cat_names}. "
                "Call with no arguments to see descriptions. "
                "Tools are added to your session and available from the next message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"Tool categories to load. Available: {cat_names}",
                    },
                },
                "required": [],
            },
        }

        registry.register(
            name=_TOOL_NAME,
            toolset="core",
            schema=schema,
            handler=_handler,
            is_async=False,
            description=schema["description"],
        )
        logger.debug("request_tools: registered")
    except Exception as exc:
        logger.debug("request_tools: registration failed: %s", exc)


_register()
