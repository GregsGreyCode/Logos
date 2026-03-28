"""
Unified Services Registry — tool credentials and integration catalogue.

Manages API keys for tool integrations (Firecrawl, fal.ai, Browserbase, etc.)
stored in the auth DB. Keys are injected into os.environ so existing tool code
continues to work unchanged (tools read from os.getenv).

Resolution priority: os.environ (k8s secrets, Docker env) > .env > DB credentials.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known tool integrations — maps env var → metadata for the UI catalogue.
# Tools self-register with requires_env in the tool registry; this table
# adds human-readable labels, help URLs, and validation endpoints.
# ---------------------------------------------------------------------------

TOOL_INTEGRATIONS = {
    "FIRECRAWL_API_KEY": {
        "label": "Firecrawl",
        "description": "Web search and content extraction",
        "tools": ["web_search", "web_extract"],
        "toolset": "web",
        "help_url": "https://firecrawl.dev/",
        "validate_url": "https://api.firecrawl.dev/v1/scrape",
        "validate_method": "POST",
        "validate_headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "validate_body": {"url": "https://example.com", "formats": ["markdown"], "limit": 1},
    },
    "FAL_KEY": {
        "label": "fal.ai",
        "description": "Image generation (Flux, SDXL, etc.)",
        "tools": ["image_generate"],
        "toolset": "image",
        "help_url": "https://fal.ai/",
        "validate_url": "https://queue.fal.run/fal-ai/flux/schnell",
        "validate_method": "HEAD",
        "validate_headers": lambda key: {"Authorization": f"Key {key}"},
    },
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter",
        "description": "Multi-model inference routing (200+ models)",
        "tools": ["mixture_of_agents"],
        "toolset": "moa",
        "help_url": "https://openrouter.ai/",
        "validate_url": "https://openrouter.ai/api/v1/models",
        "validate_method": "GET",
        "validate_headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "BROWSERBASE_API_KEY": {
        "label": "Browserbase",
        "description": "Cloud browser automation with stealth and proxies",
        "tools": ["browser_navigate", "browser_click", "browser_type", "browser_snapshot"],
        "toolset": "browser",
        "help_url": "https://browserbase.com/",
        "validate_url": "https://www.browserbase.com/v1/sessions",
        "validate_method": "GET",
        "validate_headers": lambda key: {"x-bb-api-key": key},
    },
    "ELEVENLABS_API_KEY": {
        "label": "ElevenLabs",
        "description": "Premium text-to-speech voices",
        "tools": ["text_to_speech"],
        "toolset": "tts-premium",
        "help_url": "https://elevenlabs.io/",
        "validate_url": "https://api.elevenlabs.io/v1/voices",
        "validate_method": "GET",
        "validate_headers": lambda key: {"xi-api-key": key},
    },
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic",
        "description": "Claude models (direct API, not via OpenRouter)",
        "tools": [],
        "toolset": "inference",
        "help_url": "https://console.anthropic.com/",
        "validate_url": "https://api.anthropic.com/v1/messages",
        "validate_method": "POST",
        "validate_headers": lambda key: {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        "validate_body": {"model": "claude-haiku-4-5-20251001", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
    },
}


# ---------------------------------------------------------------------------
# DB credential helpers
# ---------------------------------------------------------------------------

def _get_credentials() -> dict:
    """Read stored credentials from the auth DB."""
    try:
        from gateway.auth.db import get_platform_feature_flags
        flags = get_platform_feature_flags()
        return flags.get("credentials") or {}
    except Exception:
        return {}


def _set_credentials(creds: dict) -> None:
    """Write credentials dict to the auth DB."""
    from gateway.auth.db import get_platform_feature_flags, set_platform_feature_flag
    flags = get_platform_feature_flags()
    flags["credentials"] = creds
    # Write the entire flags dict back
    import time
    from gateway.auth.db import _conn
    with _conn() as conn:
        conn.execute(
            "UPDATE platform_settings SET feature_flags=?, updated_at=? WHERE id=1",
            (json.dumps(flags), int(time.time() * 1000)),
        )


def get_credential(env_var: str) -> Optional[str]:
    """Get a single credential from DB."""
    return _get_credentials().get(env_var)


def set_credential(env_var: str, value: str) -> None:
    """Store a credential and inject into os.environ."""
    creds = _get_credentials()
    creds[env_var] = value
    _set_credentials(creds)
    # Inject immediately so tools pick it up
    os.environ[env_var] = value
    logger.info("services: credential set for %s", env_var)


def delete_credential(env_var: str) -> None:
    """Remove a credential from DB and os.environ."""
    creds = _get_credentials()
    creds.pop(env_var, None)
    _set_credentials(creds)
    os.environ.pop(env_var, None)
    logger.info("services: credential removed for %s", env_var)


def inject_credentials() -> int:
    """Load all DB credentials into os.environ (called at startup + before each agent run).

    Only sets keys NOT already in os.environ, so .env and k8s secrets take priority.
    Returns the number of credentials injected.
    """
    creds = _get_credentials()
    injected = 0
    for env_var, value in creds.items():
        if env_var not in os.environ and value:
            os.environ[env_var] = value
            injected += 1
    if injected:
        logger.debug("services: injected %d credential(s) from DB", injected)
    return injected


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def get_tool_integrations() -> list[dict]:
    """Build the tool integrations catalogue for the UI.

    Merges TOOL_INTEGRATIONS metadata with live availability status.
    """
    creds = _get_credentials()
    result = []
    for env_var, meta in TOOL_INTEGRATIONS.items():
        has_key = bool(os.environ.get(env_var) or creds.get(env_var))
        # Check if the toolset is actually available (tool check_fn passes)
        available = False
        if has_key:
            try:
                from tools.registry import registry
                available = registry.is_toolset_available(meta["toolset"])
            except Exception:
                available = has_key  # assume available if we can't check
        result.append({
            "env_var": env_var,
            "label": meta["label"],
            "description": meta["description"],
            "tools": meta["tools"],
            "toolset": meta["toolset"],
            "has_key": has_key,
            "available": available,
            "help_url": meta["help_url"],
            "source": "env" if os.environ.get(env_var) and env_var not in creds else "db" if env_var in creds else None,
        })
    return result


async def validate_credential(env_var: str, value: str) -> dict:
    """Test a credential with a real API call. Returns {ok: bool, message: str}."""
    meta = TOOL_INTEGRATIONS.get(env_var)
    if not meta or "validate_url" not in meta:
        return {"ok": True, "message": "No validation available — key saved on trust."}

    import aiohttp
    try:
        headers = meta["validate_headers"](value)
        method = meta.get("validate_method", "GET")
        body = meta.get("validate_body")

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            kwargs = {"headers": headers}
            if body and method == "POST":
                kwargs["json"] = body

            if method == "GET":
                async with session.get(meta["validate_url"], **kwargs) as resp:
                    if resp.status in (200, 201):
                        return {"ok": True, "message": f"Validated ({resp.status})"}
                    text = (await resp.text())[:200]
                    return {"ok": False, "message": f"HTTP {resp.status}: {text}"}
            elif method == "POST":
                async with session.post(meta["validate_url"], **kwargs) as resp:
                    if resp.status in (200, 201):
                        return {"ok": True, "message": f"Validated ({resp.status})"}
                    text = (await resp.text())[:200]
                    return {"ok": False, "message": f"HTTP {resp.status}: {text}"}
            elif method == "HEAD":
                async with session.head(meta["validate_url"], **kwargs) as resp:
                    if resp.status in (200, 201, 405):
                        return {"ok": True, "message": f"Validated ({resp.status})"}
                    return {"ok": False, "message": f"HTTP {resp.status}"}
    except aiohttp.ClientError as e:
        return {"ok": False, "message": f"Connection error: {e}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}

    return {"ok": True, "message": "Saved"}
