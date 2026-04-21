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

# TOOL_INTEGRATIONS dict removed 2026-04-21. It catalogued cloud-tool
# API keys (Firecrawl / fal.ai / OpenRouter / Browserbase / ElevenLabs /
# Anthropic) but the keys only landed in Logos's os.environ — they were
# never forwarded into sandboxes where hermes actually reads env. Per-
# agent cloud-tool keys now live in the `agent_env_credentials` table
# and flow into each sandbox's own .env at spawn time (and on hot-
# refresh) via `build_channel_extra_env`. See:
#   - gateway/auth/db.py: agent_env_credentials table + CRUD
#   - gateway/admin_handlers.py: handle_agent_env_credentials_*
#   - gateway/http_api.py: /setup slash-command handler
#   - gateway/executors/hermes_server_mode.py: build_channel_extra_env
#
# MESSAGING_INTEGRATIONS below is intentionally preserved — messaging
# adapters inside the sandbox still read those env var names, and the
# Messaging tab uses this table for per-platform validation logic.

# ---------------------------------------------------------------------------
# Messaging platform integrations — maps env var → metadata for Channels UI.
# Uses the same credential storage as tool integrations above.
# ---------------------------------------------------------------------------

MESSAGING_INTEGRATIONS = {
    "TELEGRAM_BOT_TOKEN": {
        "label": "Telegram",
        "description": "Bot token from @BotFather",
        "icon": "telegram",
        "env_var": "TELEGRAM_BOT_TOKEN",
        "validate_url": "https://api.telegram.org/bot{value}/getMe",
        "help_text": "Search @BotFather on Telegram → /newbot → copy the token.",
    },
    "DISCORD_BOT_TOKEN": {
        "label": "Discord",
        "description": "Bot token from Discord Developer Portal",
        "icon": "discord",
        "env_var": "DISCORD_BOT_TOKEN",
        "validate_url": "https://discord.com/api/v10/users/@me",
        "validate_headers": lambda key: {"Authorization": f"Bot {key}"},
        "help_text": "discord.com/developers → New Application → Bot → Copy Token.",
    },
    "SLACK_BOT_TOKEN": {
        "label": "Slack",
        "description": "Bot User OAuth Token (xoxb-...)",
        "icon": "slack",
        "env_var": "SLACK_BOT_TOKEN",
        "validate_url": "https://slack.com/api/auth.test",
        "validate_headers": lambda key: {"Authorization": f"Bearer {key}"},
        "help_text": "api.slack.com → Create App → OAuth → Bot Token (xoxb-...).",
    },
    "WHATSAPP_TOKEN": {
        "label": "WhatsApp",
        "description": "WhatsApp Business API token",
        "icon": "whatsapp",
        "env_var": "WHATSAPP_TOKEN",
        "help_text": "developers.facebook.com → WhatsApp → API Setup → copy token.",
    },
}


# ---------------------------------------------------------------------------
# DB credential helpers
# ---------------------------------------------------------------------------

def _get_credentials() -> dict:
    """Read stored credentials from the auth DB.

    Resilient to the DB not being initialised yet — ``load_gateway_config``
    runs before ``auth_db.init_db`` in the normal startup path, so the
    happy-path import would raise "Auth DB not initialised". In that
    case we open SQLite directly at the well-known ``~/.logos/auth.db``
    path so inject_credentials can still populate os.environ before
    ``_apply_env_overrides`` decides which platforms to enable.
    """
    import json
    try:
        from gateway.auth.db import get_platform_feature_flags
        flags = get_platform_feature_flags()
        return flags.get("credentials") or {}
    except Exception:
        pass
    # Fallback: direct SQLite read bypassing the init_db requirement.
    # Matches the path auth_db would resolve via HERMES_HOME/LOGOS_HOME.
    try:
        import sqlite3
        from pathlib import Path
        home = os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") \
            or str(Path.home() / ".logos")
        db_path = Path(home) / "auth.db"
        if not db_path.exists():
            return {}
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT feature_flags FROM platform_settings WHERE id=1"
            ).fetchone()
        if not row or not row["feature_flags"]:
            return {}
        flags = json.loads(row["feature_flags"]) or {}
        return flags.get("credentials") or {}
    except Exception as exc:
        logger.debug("services: fallback credential read failed: %s", exc)
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


# ---------------------------------------------------------------------------
# Per-agent messaging credentials — overrides for sandbox env injection
# ---------------------------------------------------------------------------

# Bot-token-shaped platforms only: these have a single token env var that
# send_message_tool + the Telegram/Discord/Slack/WhatsApp libraries both read.
# Signal / Email / HomeAssistant use multi-var configs and need separate
# handling when we broaden per-agent credentials beyond bot tokens.
_AGENT_CHANNEL_ENV_BY_PLATFORM = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord":  "DISCORD_BOT_TOKEN",
    "slack":    "SLACK_BOT_TOKEN",
    "whatsapp": "WHATSAPP_TOKEN",
}


def get_agent_channel_env(agent_id: str) -> dict:
    """Return `{env_var: token}` for an agent's enabled credential rows.

    Used by the sandbox executor to inject agent-scoped messaging
    tokens into the sandbox env — so send_message_tool (running inside
    the sandbox, which reads from os.getenv) uses THIS agent's bot,
    not whatever global value happens to be set on the gateway
    process. Rows on platforms outside the bot-token shape
    (signal/email/homeassistant) are ignored for now; they'll need
    per-platform multi-var plumbing when we generalise.

    Multi-label selection: when the agent has several rows for the
    same platform (e.g. prod + staging), the ``default`` label wins.
    If no row is labelled ``default``, the first row by label sort
    order is used. The tool itself can't yet pick between labels at
    call time; that's a future enhancement.
    """
    try:
        from gateway.auth.db import list_agent_channel_credentials
    except Exception as exc:
        logger.debug("get_agent_channel_env: db import failed: %s", exc)
        return {}
    try:
        rows = list_agent_channel_credentials(agent_id=agent_id, enabled_only=True)
    except Exception:
        logger.exception("get_agent_channel_env: list failed for agent_id=%s", agent_id)
        return {}

    # Group rows by platform and pick the preferred row per platform.
    by_platform: dict[str, list[dict]] = {}
    for row in rows:
        by_platform.setdefault(row["platform"], []).append(row)

    env: dict[str, str] = {}
    for platform, platform_rows in by_platform.items():
        env_var = _AGENT_CHANNEL_ENV_BY_PLATFORM.get(platform)
        if not env_var:
            continue
        # Prefer label='default'; fall back to alphabetical first.
        chosen = next(
            (r for r in platform_rows if r["label"] == "default"),
            sorted(platform_rows, key=lambda r: r["label"])[0],
        )
        if chosen.get("token"):
            env[env_var] = chosen["token"]
    return env


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
# Messaging integrations catalogue + validation
#
# (The tool integrations catalogue + `validate_credential` were removed
# 2026-04-21 when per-agent cloud-tool keys moved to the sandbox .env
# via agent_env_credentials. Messaging stays here because the shape is
# genuinely different — one credential per platform, centralised at the
# Logos layer rather than per-agent. Per-agent messaging tokens live in
# agent_channel_credentials and feed via the same .env injection path.)
# ---------------------------------------------------------------------------

def get_messaging_integrations() -> list[dict]:
    """Build the messaging integrations catalogue for the Channels UI."""
    creds = _get_credentials()
    result = []
    for env_var, meta in MESSAGING_INTEGRATIONS.items():
        has_key = bool(os.environ.get(env_var) or creds.get(env_var))
        result.append({
            "env_var": env_var,
            "label": meta["label"],
            "description": meta["description"],
            "icon": meta["icon"],
            "has_key": has_key,
            "connected": has_key,  # TODO: check adapter is actually running
            "help_text": meta["help_text"],
            "source": "env" if os.environ.get(env_var) and env_var not in creds else "db" if env_var in creds else None,
        })
    return result


async def validate_messaging_credential(env_var: str, value: str) -> dict:
    """Test a messaging token with the platform API. Returns {ok, message, details}."""
    meta = MESSAGING_INTEGRATIONS.get(env_var)
    if not meta:
        return {"ok": False, "message": f"Unknown messaging platform: {env_var}"}

    if "validate_url" not in meta:
        return {"ok": True, "message": "No validation available — token saved on trust."}

    import aiohttp
    try:
        url = meta["validate_url"].format(value=value)
        headers_fn = meta.get("validate_headers")
        headers = headers_fn(value) if headers_fn else {}

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200:
                    # Extract useful details per platform
                    details = {}
                    if env_var == "TELEGRAM_BOT_TOKEN":
                        bot = body.get("result", {})
                        details = {"username": bot.get("username"), "name": bot.get("first_name")}
                    elif env_var == "DISCORD_BOT_TOKEN":
                        details = {"username": body.get("username"), "id": body.get("id")}
                    elif env_var == "SLACK_BOT_TOKEN":
                        if body.get("ok"):
                            details = {"team": body.get("team"), "user": body.get("user")}
                        else:
                            return {"ok": False, "message": f"Slack error: {body.get('error', 'unknown')}"}
                    return {"ok": True, "message": "Connected", "details": details}
                else:
                    text = json.dumps(body)[:200] if body else str(resp.status)
                    return {"ok": False, "message": f"HTTP {resp.status}: {text}"}
    except aiohttp.ClientError as e:
        return {"ok": False, "message": f"Connection error: {e}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
