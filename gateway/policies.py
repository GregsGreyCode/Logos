"""Per-agent network policy preset management.

Python port of the relevant half of NemoClaw's
``knowledge-repos/NemoClaw/src/lib/policies.ts``. This module owns the
life cycle of network policy presets for Logos agents — discovery,
loading, merging into the baseline, persisting per-agent selections
to the DB, and pushing the merged effective policy to a running
OpenShell sandbox via ``openshell policy set``.

The baseline policy lives at
``gateway/policies/openshell_default.yaml`` and contains only what
every Logos sandbox needs (inference.local + DNS). Presets live in
``gateway/policies/presets/*.yaml`` and are opt-in per-agent
additions that widen the network policy for specific integrations
(github, slack, telegram, discord, huggingface, pypi, etc.).

Usage
─────

    from gateway import policies as gp

    # Discover presets for the Tools editor UI
    for p in gp.list_presets():
        print(p.name, p.description)

    # Apply a preset to an agent — writes the DB AND pushes the
    # merged effective policy to the running sandbox so it takes
    # effect without waiting for a respawn.
    gp.apply_preset(agent_id, "github")

    # List what's currently applied to an agent
    applied = gp.get_applied_presets(agent_id)  # -> ["github", "slack"]

    # Compute the merged effective policy (baseline + applied
    # presets). Used by OpenShellExecutor.spawn() to write the
    # initial policy file, and by the policy-set path to re-push
    # at runtime after a preset change.
    effective = gp.compute_effective_policy(agent_id)  # dict

Design notes
────────────

* **DB is the source of truth for applied presets** — the list is
  stored as a JSON array in ``agents.applied_presets`` (added in
  db.py v10 migration). ``openshell policy set`` is a side effect;
  if pushing to the live sandbox fails (e.g. sandbox not yet
  spawned), the DB still reflects user intent and ``spawn()`` will
  pick it up at next sandbox (re-)creation.

* **Merge semantics**: preset entries override baseline entries on
  name collision. This matches NemoClaw's ``mergePresetIntoPolicy``
  at ``src/lib/policies.ts:166-221``. Presets SHOULD use unique
  network_policy names to avoid stomping the baseline — by
  convention the preset file's top-level key matches the preset
  name, so ``github.yaml`` contributes a ``network_policies.github``
  entry, not ``network_policies.inference_local``.

* **Failing to push is non-fatal**. If the sandbox doesn't exist
  yet, or openshell is unreachable, the DB change still happens and
  a warning is logged. The next spawn picks up the effective
  policy from the DB. Callers that need strict push semantics can
  inspect the log or call ``push_effective_policy(agent_id)``
  directly and wrap in their own error handling.

* **This module does NOT touch approval_requests or action_policies**.
  Those are the *application*-layer policy surface (per-tool-call
  gating) and live in ``gateway/auth/db.py``. Network policy
  presets are the *infrastructure*-layer surface (per-host egress).
  See MISSING.md M10's two-layer STAMP model for the division.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Canonical locations — computed once at import. ``gateway/policies/``
# is the directory (this file is ``gateway/policies.py`` and the
# baseline YAML is ``gateway/policies/openshell_default.yaml``).
_POLICIES_DIR = Path(__file__).parent / "policies"
_PRESETS_DIR = _POLICIES_DIR / "presets"
_BASELINE_PATH = _POLICIES_DIR / "openshell_default.yaml"


# ── Public data types ──────────────────────────────────────────────────────

@dataclass
class PresetInfo:
    """Metadata for a single preset file.

    Returned by :func:`list_presets` for use in the Tools editor UI
    and CLI listings. The ``path`` is informational — most callers
    should use :func:`load_preset` to get parsed contents rather
    than reading the file themselves.
    """

    name: str
    description: str
    path: Path


class PresetNotFound(Exception):
    """Raised when a preset name doesn't match any file in
    ``gateway/policies/presets/``."""


class PolicyMergeError(Exception):
    """Raised when the baseline policy can't be loaded or a preset
    can't be merged into it. Callers should treat this as a config
    bug — the baseline should always parse, and presets should always
    conform to the expected YAML shape."""


# ── Tool → preset mapping ─────────────────────────────────────────────────
#
# Maps tool names to the network presets and environment variables they
# need. Used by:
#   - tool-readiness endpoint (Phase 2) to show which tools need config
#   - auto-apply logic (Phase 1c) to apply presets when API keys are saved
#   - /setup tools command (Phase 3) to present configuration options
#
# Tools not listed here run entirely inside the sandbox and need no
# network preset (terminal, memory, file ops, todo, delegate, etc.).

TOOL_PRESET_MAP: Dict[str, Dict[str, Any]] = {
    # Web search & extraction — Firecrawl cloud API
    "web_search": {
        "presets": ["firecrawl"],
        "env": ["FIRECRAWL_API_KEY"],
        "toolset": "web",
        "description": "Web search and content extraction",
        "setup_url": "https://firecrawl.dev",
    },
    "web_extract": {
        "presets": ["firecrawl"],
        "env": ["FIRECRAWL_API_KEY"],
        "toolset": "web",
        "description": "Web page content extraction",
        "setup_url": "https://firecrawl.dev",
    },
    # Browser automation — Browserbase (cloud) or local agent-browser
    # Chromium (no preset needed for local).
    "browser_navigate": {
        "presets": ["browserbase"],
        "env": ["BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"],
        "toolset": "browser",
        "description": "Cloud browser automation",
        "setup_url": "https://browserbase.com",
        "optional": True,  # local browser works without this
    },
    # Vision — OpenRouter multi-model API
    "vision_analyze": {
        "presets": ["openrouter"],
        "env": ["OPENROUTER_API_KEY"],
        "toolset": "vision",
        "description": "Image analysis via OpenRouter",
        "setup_url": "https://openrouter.ai",
    },
    # Image generation — fal.ai
    "image_generate": {
        "presets": ["fal"],
        "env": ["FAL_KEY"],
        "toolset": "image_gen",
        "description": "Image generation via fal.ai",
        "setup_url": "https://fal.ai",
    },
    # Text-to-speech — ElevenLabs (premium) or Edge TTS (free, no preset)
    "text_to_speech": {
        "presets": ["elevenlabs"],
        "env": ["ELEVENLABS_API_KEY"],
        "toolset": "tts",
        "description": "Text-to-speech via ElevenLabs",
        "setup_url": "https://elevenlabs.io",
        "optional": True,  # Edge TTS works without this
    },
    # Mixture of agents — OpenRouter
    "mixture_of_agents": {
        "presets": ["openrouter"],
        "env": ["OPENROUTER_API_KEY"],
        "toolset": "moa",
        "description": "Multi-model reasoning via OpenRouter",
        "setup_url": "https://openrouter.ai",
    },
    # Messaging — the actual tool the agent calls is `send_message`
    # (unified, takes a `target` param like "telegram:123" or
    # "slack:#eng"). This row drives the T pill's readiness badge:
    # ready when any supported platform has both a token and the
    # matching preset applied.
    "send_message": {
        # any one of these presets being applied is enough — the
        # readiness helper short-circuits on the first match.
        "presets": ["telegram", "slack", "discord", "whatsapp"],
        "env": ["TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN", "DISCORD_BOT_TOKEN", "WHATSAPP_TOKEN"],
        "toolset": "messaging",
        "description": "Post messages on connected platforms (Telegram, Slack, Discord, WhatsApp)",
        "setup_url": "https://core.telegram.org/bots",
        "any_preset": True,   # reads as "at least one preset satisfies"
        "any_env": True,      # reads as "at least one env var satisfies"
    },
    # Per-platform auto-apply stubs. NOT real tools — the agent never
    # calls a function literally named send_telegram/send_slack/etc.
    # These live here solely so `auto_apply_presets_for_env` can map
    # "TELEGRAM_BOT_TOKEN was saved" → "apply the telegram preset to
    # every agent with the messaging toolset". Filtered out of
    # get_tool_readiness via auto_apply_only so they don't surface as
    # ghost tools in the T pill UI.
    "send_telegram": {
        "presets": ["telegram"],
        "env": ["TELEGRAM_BOT_TOKEN"],
        "toolset": "messaging",
        "description": "Post messages to Telegram chats",
        "setup_url": "https://core.telegram.org/bots",
        "auto_apply_only": True,
    },
    "send_slack": {
        "presets": ["slack"],
        "env": ["SLACK_BOT_TOKEN"],
        "toolset": "messaging",
        "description": "Post messages to Slack channels",
        "setup_url": "https://api.slack.com/apps",
        "auto_apply_only": True,
    },
    "send_discord": {
        "presets": ["discord"],
        "env": ["DISCORD_BOT_TOKEN"],
        "toolset": "messaging",
        "description": "Post messages to Discord channels",
        "setup_url": "https://discord.com/developers/applications",
        "auto_apply_only": True,
    },
    # GitHub — the repo-management surface is MCP-server-driven rather
    # than a first-class tool (an agent mounts the github MCP server
    # and gets mcp_github_* functions at runtime). Kept here so saving
    # GITHUB_TOKEN still triggers the github preset auto-apply. Marked
    # auto_apply_only so the T pill doesn't advertise a non-existent
    # `manage_github` function name.
    "manage_github": {
        "presets": ["github"],
        "env": ["GITHUB_TOKEN"],
        "toolset": "github",
        "description": "Open issues, comment on PRs, push commits",
        "setup_url": "https://github.com/settings/tokens",
        "auto_apply_only": True,
    },
    # HuggingFace — same story as github: no first-class `huggingface`
    # tool, but saving HUGGINGFACE_API_KEY should auto-apply the hf
    # preset to agents configured to use it.
    "huggingface": {
        "presets": ["huggingface"],
        "env": ["HUGGINGFACE_API_KEY"],
        "toolset": "vision",
        "description": "Pull models, run hosted inference",
        "setup_url": "https://huggingface.co/settings/tokens",
        "optional": True,
        "auto_apply_only": True,
    },
}


def get_tool_readiness(agent_id: str) -> List[Dict[str, Any]]:
    """Return per-tool readiness status for an agent.

    Checks three things per tool:
    1. Is the toolset enabled for this agent?
    2. Is the required env var set?
    3. Is the required network preset applied?

    Returns a list of dicts with: name, toolset, status, reason, preset,
    setup_url. Status is one of: "ready", "needs_config", "not_enabled".
    """
    from gateway.auth import db as auth_db

    agent = auth_db.get_agent(agent_id)
    if not agent:
        return []

    # Resolve enabled toolsets from the agent record
    raw_toolsets = agent.get("toolsets")
    if isinstance(raw_toolsets, str):
        try:
            enabled_toolsets = set(json.loads(raw_toolsets))
        except (ValueError, TypeError):
            enabled_toolsets = set()
    elif isinstance(raw_toolsets, list):
        enabled_toolsets = set(raw_toolsets)
    else:
        enabled_toolsets = set()

    # Resolve applied presets
    applied = set(get_applied_presets(agent_id))

    results = []

    # First: tools that need external config
    for tool_name, info in TOOL_PRESET_MAP.items():
        # Skip per-platform stubs like send_telegram / send_slack that
        # exist solely so auto_apply_presets_for_env can map an env
        # var to a preset. They don't correspond to functions the
        # agent can actually call (the real tool is `send_message`)
        # and listing them in the readiness UI produced phantom rows
        # the user couldn't act on.
        if info.get("auto_apply_only"):
            continue
        toolset = info["toolset"]
        if toolset not in enabled_toolsets:
            continue  # toolset not enabled, skip

        # Check env vars. "any_env" / "any_preset" mean "satisfied when
        # at least one is set" (used by send_message where any one of
        # TELEGRAM/SLACK/DISCORD/WHATSAPP tokens + the matching preset
        # is enough to be ready).
        env_keys = info.get("env", [])
        any_env = info.get("any_env", False)
        if any_env:
            has_env = bool(env_keys) and any(os.environ.get(k) for k in env_keys)
        else:
            has_env = bool(env_keys) and all(os.environ.get(k) for k in env_keys)

        # Check presets
        needed_presets = info.get("presets", [])
        any_preset = info.get("any_preset", False)
        if any_preset:
            has_presets = bool(needed_presets) and any(p in applied for p in needed_presets)
        else:
            has_presets = all(p in applied for p in needed_presets)

        if has_env and has_presets:
            status = "ready"
            reason = ""
        elif not has_env:
            missing = [k for k in env_keys if not os.environ.get(k)]
            status = "needs_config"
            reason = f"{', '.join(missing)} not set"
        else:
            status = "needs_preset"
            reason = f"preset '{needed_presets[0]}' not applied"

        missing_preset = next(
            (p for p in needed_presets if p not in applied),
            needed_presets[0] if needed_presets else None,
        )
        results.append({
            "name": tool_name,
            "toolset": toolset,
            "status": status,
            "reason": reason,
            "preset": missing_preset,
            "setup_url": info.get("setup_url", ""),
            "description": info.get("description", ""),
            "optional": info.get("optional", False),
        })

    # Second: tools that work locally (no config needed)
    local_toolsets = {
        "terminal": "Command execution",
        "memory": "Persistent memory",
        "file": "File operations",
        "todo": "Task planning",
        "delegation": "Subagent delegation",
        "clarify": "Clarifying questions",
        "session_search": "Session history search",
        "code_execution": "Python sandbox",
        "skills": "Skill management",
        "homeassistant": "Home Assistant control",
    }
    for toolset, desc in local_toolsets.items():
        if toolset in enabled_toolsets:
            results.append({
                "name": toolset,
                "toolset": toolset,
                "status": "ready",
                "reason": "",
                "preset": None,
                "setup_url": "",
                "description": desc,
                "optional": False,
            })

    return results


def auto_apply_presets_for_env(env_key: str) -> List[str]:
    """When an API key env var is saved, auto-apply matching presets.

    Finds all tools that need ``env_key``, looks up their required
    presets, and applies those presets to every agent that has the
    corresponding toolset enabled. Returns the list of preset names
    that were applied.

    Called by the ``POST /tools/configure`` endpoint after saving
    a credential to ``~/.hermes/.env``.
    """
    from gateway.auth import db as auth_db

    # Find which presets this env key unlocks.
    presets_to_apply: Dict[str, str] = {}  # preset_name -> toolset
    for tool_name, info in TOOL_PRESET_MAP.items():
        if env_key not in info.get("env", []):
            continue
        for preset in info.get("presets", []):
            presets_to_apply[preset] = info["toolset"]

    if not presets_to_apply:
        return []

    applied_names = []
    agents = auth_db.list_agents()

    for preset_name, toolset in presets_to_apply.items():
        for agent in agents:
            # Check if the agent has the relevant toolset enabled
            raw_ts = agent.get("toolsets")
            if isinstance(raw_ts, str):
                try:
                    agent_toolsets = json.loads(raw_ts)
                except (ValueError, TypeError):
                    agent_toolsets = []
            elif isinstance(raw_ts, list):
                agent_toolsets = raw_ts
            else:
                agent_toolsets = []

            if toolset in agent_toolsets:
                current = get_applied_presets(agent["id"])
                if preset_name not in current:
                    try:
                        apply_preset(agent["id"], preset_name)
                        logger.info(
                            "auto_apply: applied '%s' to agent '%s' (toolset '%s' enabled, %s configured)",
                            preset_name, agent.get("name", agent["id"]), toolset, env_key,
                        )
                    except Exception as exc:
                        logger.warning(
                            "auto_apply: failed to apply '%s' to '%s': %s",
                            preset_name, agent.get("name", agent["id"]), exc,
                        )

        if preset_name not in applied_names:
            applied_names.append(preset_name)

    return applied_names


# ── Preset discovery ──────────────────────────────────────────────────────


def list_presets() -> List[PresetInfo]:
    """Return metadata for every preset in ``gateway/policies/presets/``.

    Sorted alphabetically by filename for UI stability. Files that
    fail to parse or lack the ``preset:`` metadata header are logged
    and skipped — a single broken preset should never hide all the
    others.
    """
    if not _PRESETS_DIR.exists():
        logger.debug("list_presets: %s does not exist", _PRESETS_DIR)
        return []
    out: List[PresetInfo] = []
    for path in sorted(_PRESETS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("list_presets: failed to parse %s: %s", path.name, exc)
            continue
        header = (data or {}).get("preset") or {}
        name = header.get("name")
        if not name:
            logger.warning(
                "list_presets: %s has no preset.name header — skipping", path.name,
            )
            continue
        out.append(PresetInfo(
            name=str(name),
            description=str(header.get("description", "")),
            path=path,
        ))
    return out


def load_preset(name: str) -> Dict[str, Any]:
    """Return the parsed YAML for preset ``name``.

    Raises :class:`PresetNotFound` if no matching ``.yaml`` file exists
    OR if the name is malformed (directory traversal guard).
    """
    # Directory traversal / injection guard. Preset names come from
    # the UI or from DB-stored applied lists, so treating them as
    # untrusted is the right default even though the attack surface
    # is narrow today.
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise PresetNotFound(f"Invalid preset name: {name!r}")
    path = _PRESETS_DIR / f"{name}.yaml"
    if not path.exists():
        raise PresetNotFound(f"Preset not found: {name}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PresetNotFound(f"Preset {name} failed to parse: {exc}") from exc
    return data or {}


def extract_preset_network_policies(preset_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return just the ``network_policies`` dict from a parsed preset.

    Presets have two top-level sections: ``preset:`` (metadata) and
    ``network_policies:`` (the actual rules). For merging we only
    care about the latter — the header is used by the UI for
    labels/descriptions but is not part of the effective policy.
    """
    np = preset_data.get("network_policies")
    if not isinstance(np, dict):
        return {}
    return dict(np)


# ── Baseline + merge ──────────────────────────────────────────────────────


def load_baseline() -> Dict[str, Any]:
    """Return the parsed baseline ``openshell_default.yaml``.

    Raises :class:`PolicyMergeError` if the file is missing or
    malformed. This is a hard failure because every effective
    policy depends on the baseline; a broken baseline breaks all
    sandbox spawns.
    """
    try:
        text = _BASELINE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyMergeError(
            f"Baseline policy missing at {_BASELINE_PATH}: {exc}"
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyMergeError(
            f"Baseline policy {_BASELINE_PATH} failed to parse: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PolicyMergeError(
            f"Baseline policy {_BASELINE_PATH} is not a YAML mapping"
        )
    return data


def merge_presets_into_baseline(preset_names: List[str]) -> Dict[str, Any]:
    """Merge the named presets into the baseline and return the result.

    Preset ``network_policies`` entries are added to the baseline's
    ``network_policies``. On name collision the preset overrides the
    baseline entry — same semantics as NemoClaw's
    ``mergePresetIntoPolicy`` at ``src/lib/policies.ts:166-221``.
    Non-network sections (``filesystem_policy``, ``process``,
    ``landlock``) are preserved from the baseline as-is.

    Unknown preset names in the list are logged and silently skipped
    so a stale DB entry for a removed preset doesn't break the
    merge. :func:`set_applied_presets` validates at write time so
    this shouldn't normally happen.
    """
    merged = load_baseline()
    base_np = merged.setdefault("network_policies", {})
    if not isinstance(base_np, dict):
        raise PolicyMergeError(
            "Baseline policy network_policies is not a YAML mapping"
        )

    for preset_name in preset_names:
        try:
            preset_data = load_preset(preset_name)
        except PresetNotFound as exc:
            logger.warning(
                "merge_presets_into_baseline: skipping unknown preset %r: %s",
                preset_name, exc,
            )
            continue
        preset_np = extract_preset_network_policies(preset_data)
        for key, value in preset_np.items():
            if key in base_np:
                logger.info(
                    "merge_presets_into_baseline: preset %r overrides "
                    "baseline network_policies[%r]",
                    preset_name, key,
                )
            base_np[key] = value

    merged["network_policies"] = base_np
    return merged


def validate_preset_content(data: Dict[str, Any]) -> None:
    """Raise PolicyMergeError if a preset dict is structurally invalid.

    Guards the subset that matters for safe write + merge: requires
    ``preset.name`` (non-empty, no path chars) and a ``network_policies``
    mapping. Deeper schema checks (valid host globs, port types, L7
    rule shapes) are left to OpenShell itself — it rejects bad
    policies at ``policy set`` time with a clear error.
    """
    if not isinstance(data, dict):
        raise PolicyMergeError("Preset must be a YAML mapping at the top level")
    header = data.get("preset")
    if not isinstance(header, dict):
        raise PolicyMergeError("Preset must have a 'preset:' header section")
    name = header.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PolicyMergeError("preset.name is required and must be a non-empty string")
    if "/" in name or "\\" in name or name.startswith("."):
        raise PolicyMergeError(f"preset.name contains unsafe characters: {name!r}")
    np = data.get("network_policies")
    if np is not None and not isinstance(np, dict):
        raise PolicyMergeError("network_policies must be a YAML mapping when present")


def write_preset(name: str, yaml_text: str) -> Dict[str, Any]:
    """Parse, validate, and write a preset YAML to disk.

    ``name`` is the filename stem (so ``github`` writes to
    ``presets/github.yaml``). It must match ``preset.name`` inside the
    YAML to keep filename ↔ identity in sync. Returns the parsed dict
    on success; raises ``PolicyMergeError`` or ``yaml.YAMLError`` on
    failure, in which case the file on disk is not touched.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise PolicyMergeError(f"Invalid preset filename: {name!r}")
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise PolicyMergeError(f"YAML parse error: {exc}") from exc
    validate_preset_content(data)
    header_name = data["preset"]["name"]
    if header_name != name:
        raise PolicyMergeError(
            f"preset.name ({header_name!r}) does not match filename ({name!r})"
        )
    _PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    path = _PRESETS_DIR / f"{name}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return data


def delete_preset(name: str) -> bool:
    """Remove a preset file from disk. Returns True if it existed."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise PolicyMergeError(f"Invalid preset filename: {name!r}")
    path = _PRESETS_DIR / f"{name}.yaml"
    if not path.exists():
        return False
    path.unlink()
    return True


def get_agents_using_preset(name: str) -> List[str]:
    """Return the ids of agents whose ``applied_presets`` contains ``name``.

    Used by the Sandbox Policy editor to warn the admin how many live
    agents will be affected by a save (hot-reload target set).
    """
    from gateway.auth import db as auth_db
    agents = auth_db.list_agents() if hasattr(auth_db, "list_agents") else []
    hits: List[str] = []
    for agent in agents:
        applied = agent.get("applied_presets") or []
        if isinstance(applied, str):
            try:
                applied = json.loads(applied)
            except Exception:
                applied = []
        if name in (applied or []):
            hits.append(str(agent.get("id") or agent.get("name")))
    return hits


def compute_effective_policy(agent_id: str) -> Dict[str, Any]:
    """Return the merged baseline + applied presets for an agent.

    Reads the applied preset list from the DB and merges everything
    into a single policy dict suitable for writing to a temp file
    and passing to ``openshell policy set --policy <file>``.
    """
    presets = get_applied_presets(agent_id)
    return merge_presets_into_baseline(presets)


# ── Applied presets (DB-backed, JSON-aware wrappers) ──────────────────────


def get_applied_presets(agent_id: str) -> List[str]:
    """Return the list of preset names currently applied to an agent.

    Thin wrapper over ``gateway.auth.db.get_agent_applied_presets``
    that lives here so UI code can import everything it needs from
    ``gateway.policies`` without also reaching into the auth module.
    """
    from gateway.auth import db as auth_db
    return auth_db.get_agent_applied_presets(agent_id)


def set_applied_presets(agent_id: str, presets: List[str]) -> List[str]:
    """Replace the applied preset list for an agent.

    Validates every entry exists in the presets directory before
    writing — unknown names are dropped with a warning. Returns the
    cleaned list actually written to the DB, so callers can tell
    which entries were accepted and which were silently dropped.
    """
    known = {p.name for p in list_presets()}
    clean: List[str] = []
    seen: set[str] = set()
    for name in presets or []:
        s = str(name)
        if s in seen:
            continue
        seen.add(s)
        if s not in known:
            logger.warning(
                "set_applied_presets(%s): dropping unknown preset %r "
                "(not in %s)", agent_id, s, _PRESETS_DIR,
            )
            continue
        clean.append(s)

    from gateway.auth import db as auth_db
    auth_db.set_agent_applied_presets(agent_id, clean)
    return clean


# ── Apply / remove (the main entry points for the UI) ────────────────────


def apply_preset(agent_id: str, preset_name: str) -> Dict[str, Any]:
    """Add ``preset_name`` to an agent's applied set and push the
    merged effective policy to the running sandbox.

    Idempotent — applying an already-applied preset is a no-op on
    the DB but still re-pushes to openshell (in case the running
    sandbox's policy drifted from the DB state).

    Returns the merged effective policy dict that was pushed.

    Raises:
        PresetNotFound: if ``preset_name`` is not a known preset
        PolicyMergeError: if the baseline policy can't be loaded
    """
    # Validate the preset exists before touching the DB
    load_preset(preset_name)

    current = get_applied_presets(agent_id)
    if preset_name not in current:
        current.append(preset_name)
        set_applied_presets(agent_id, current)
        logger.info(
            "apply_preset(%s, %s): added to applied list (now %s)",
            agent_id, preset_name, current,
        )
    else:
        logger.info(
            "apply_preset(%s, %s): already applied, re-pushing",
            agent_id, preset_name,
        )

    effective = compute_effective_policy(agent_id)
    push_effective_policy(agent_id, effective=effective)
    return effective


def ensure_channel_access(agent_id: str, platform: str) -> Dict[str, Any]:
    """Give an agent the tools + network policy to send on a platform.

    Called when a per-agent channel credential is added so the agent
    doesn't sit on a bot token it can't actually use. Two pieces:

      1. Toolset: the ``messaging`` toolset (contains ``send_message``)
         is added to ``agents.toolsets`` if missing. Without this the
         agent has the token in its env but no tool to call.
      2. Network preset: the matching platform YAML
         (``telegram`` / ``discord`` / ``slack`` / ``whatsapp``) is
         applied so the sandbox's L7 proxy permits egress to the
         platform's API host. Without this the tool would fire and
         get a 403 from the proxy.

    Both operations are idempotent. Returns the effective applied
    preset list + updated toolset list so the caller can surface the
    change in UI ("enabled messaging toolset + telegram preset").
    """
    import json as _json
    from gateway.auth import db as _auth_db

    added_toolset = False
    agent = _auth_db.get_agent(agent_id)
    if agent:
        try:
            toolsets = _json.loads(agent.get("toolsets") or "[]")
            if not isinstance(toolsets, list):
                toolsets = []
        except Exception:
            toolsets = []
        if "messaging" not in toolsets:
            toolsets.append("messaging")
            try:
                from gateway.auth.db import _conn
                import time as _time
                with _conn() as conn:
                    conn.execute(
                        "UPDATE agents SET toolsets=?, updated_at=? WHERE id=?",
                        (_json.dumps(toolsets), int(_time.time() * 1000), agent_id),
                    )
                added_toolset = True
                logger.info(
                    "ensure_channel_access(%s, %s): added 'messaging' toolset",
                    agent_id, platform,
                )
            except Exception:
                logger.exception("ensure_channel_access: toolset write failed")

    # Apply the platform network preset if one exists. Not all
    # platforms have a preset in gateway/policies/presets/ — treat a
    # missing preset as "no network override needed" rather than a
    # hard error (e.g. HomeAssistant reaches a local URL, no preset).
    preset_applied = False
    try:
        load_preset(platform)
        apply_preset(agent_id, platform)
        preset_applied = True
    except PresetNotFound:
        logger.debug("ensure_channel_access: no preset named %s (skipping)", platform)
    except Exception:
        logger.exception("ensure_channel_access: apply_preset(%s) failed", platform)

    # Refresh the sandbox's instance-config so the new toolset + policy
    # take effect immediately without waiting for a respawn.
    try:
        from gateway.executors.openshell import OpenShellExecutor
        agent_name = (agent or {}).get("name") or ""
        if agent_name:
            OpenShellExecutor().refresh_instance_config(agent_name)
    except Exception:
        logger.exception("ensure_channel_access: sandbox refresh failed (non-fatal)")

    return {
        "toolset_added": added_toolset,
        "preset_applied": preset_applied,
        "applied_presets": get_applied_presets(agent_id),
    }


def remove_preset(agent_id: str, preset_name: str) -> Dict[str, Any]:
    """Remove ``preset_name`` from an agent's applied set and push
    the updated effective policy to the running sandbox.

    Idempotent — removing a preset that isn't applied is a no-op
    for the DB but still re-pushes the baseline to openshell.

    Returns the merged effective policy dict that was pushed.
    """
    current = get_applied_presets(agent_id)
    if preset_name in current:
        current.remove(preset_name)
        set_applied_presets(agent_id, current)
        logger.info(
            "remove_preset(%s, %s): removed (now %s)",
            agent_id, preset_name, current,
        )
    else:
        logger.info(
            "remove_preset(%s, %s): not applied, re-pushing baseline",
            agent_id, preset_name,
        )

    effective = compute_effective_policy(agent_id)
    push_effective_policy(agent_id, effective=effective)
    return effective


# ── Push to the running sandbox ───────────────────────────────────────────


def push_effective_policy(
    agent_id: str,
    effective: Optional[Dict[str, Any]] = None,
) -> bool:
    """Write the effective policy to a tempfile and call
    ``openshell policy set --policy <tempfile> --wait <sandbox>``.

    Best-effort: returns True on success, False (with a warning log)
    on any failure including "sandbox doesn't exist yet" and
    "openshell CLI unreachable". Callers should NOT treat a False
    return as a hard error — the DB is the source of truth for
    applied presets, and :func:`gateway.executors.openshell.
    OpenShellExecutor.spawn` re-reads the DB at next spawn so
    a missed runtime push is recovered automatically.

    Args:
        agent_id: the agent whose policy we're pushing
        effective: optional pre-computed effective policy (to avoid
            recomputing if the caller already has it). When None,
            computed from the DB state.

    Returns:
        True if the openshell CLI accepted the policy, False
        otherwise. False does NOT mean the DB state is wrong —
        only that the live sandbox didn't get the update this time.
    """
    # Import inside the function to avoid a module-level import cycle:
    # gateway.executors.openshell → gateway.auth.db → (would eventually
    # pull in gateway.policies for the spawn flow) → gateway.executors.
    from gateway.auth import db as auth_db
    from gateway.executors.openshell import (
        _openshell,
        _sanitize_sandbox_name,
        _load_state,
    )

    agent = auth_db.get_agent(agent_id)
    if not agent:
        logger.warning(
            "push_effective_policy(%s): agent not found in DB — skipping",
            agent_id,
        )
        return False

    sandbox_name = _sanitize_sandbox_name(f"hermes-{agent.get('name', '')}")
    if effective is None:
        effective = compute_effective_policy(agent_id)

    # Resolve which OpenShell gateway hosts this sandbox. Same lookup
    # spawn() uses at ``openshell.py:_resolve_sandbox_gateway``, but
    # inlined here to avoid pulling in the entire resolver chain for
    # a simple "which -g flag do I pass" question.
    target_gw: Optional[str] = None
    try:
        for inst in _load_state():
            if (inst.get("sandbox_name") == sandbox_name
                    or inst.get("worker_id") == sandbox_name):
                target_gw = inst.get("openshell_name")
                break
    except Exception as exc:
        logger.warning(
            "push_effective_policy(%s): state file lookup failed: %s",
            sandbox_name, exc,
        )

    if not target_gw:
        logger.info(
            "push_effective_policy(%s): no live sandbox in state file — "
            "policy will apply on next spawn",
            sandbox_name,
        )
        return False

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="logos-policy-", delete=False,
        encoding="utf-8",
    )
    try:
        # ``sort_keys=False`` preserves the baseline's section order
        # (filesystem_policy → landlock → process → network_policies)
        # for readable debug output. ``default_flow_style=False``
        # forces block style so the diff against the on-disk baseline
        # is visually comparable.
        yaml.safe_dump(
            effective, tmp,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        tmp.close()

        try:
            _openshell(
                "policy", "set",
                "--policy", tmp.name,
                "--wait", sandbox_name,
                gateway=target_gw,
                check=True,
                timeout=60.0,
            )
            logger.info(
                "push_effective_policy(%s): applied %d presets in gateway %s",
                sandbox_name, len(get_applied_presets(agent_id)), target_gw,
            )
            return True
        except Exception as exc:
            logger.warning(
                "push_effective_policy(%s): `openshell policy set` failed "
                "(non-fatal, will apply on next spawn): %s",
                sandbox_name, exc,
            )
            return False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ── Utility: preset-aware baseline path (for spawn-time merge) ────────────


def get_allowed_hosts_for_agent(agent_id: str) -> List[str]:
    """Return a deduplicated, sorted list of hosts the agent's effective
    network policy permits.

    Used to inject "you can navigate these hosts: ..." into the agent's
    system prompt so it doesn't trial-and-error against the firewall.
    Pulls from `network_policies.<bucket>.endpoints[].host` across every
    bucket in the merged baseline+presets policy. Drops bare wildcards
    ("*") since they're not actionable for an LLM. Keeps narrow
    wildcards ("*.example.com") so the model can still infer subdomain
    coverage.

    Returns [] on any error — empty list is safe (worker just skips
    the injection and behaviour matches the pre-injection baseline).
    """
    try:
        effective = compute_effective_policy(agent_id)
    except Exception:
        return []
    hosts: set = set()
    for bucket in (effective.get("network_policies") or {}).values():
        for ep in (bucket.get("endpoints") or []):
            h = ep.get("host")
            if not h or h == "*":
                continue
            hosts.add(h)
    return sorted(hosts)


def write_effective_policy_to_tempfile(agent_id: str) -> Path:
    """Compute the effective policy for an agent, serialize it, and
    return the tempfile path.

    Used by :class:`gateway.executors.openshell.OpenShellExecutor` at
    spawn time when applying the initial policy — the executor reads
    ``agents.applied_presets`` and writes a merged baseline+presets
    policy for ``openshell sandbox create --policy <file>``.

    The caller is responsible for unlinking the returned path after
    ``openshell sandbox create`` consumes it. Use a try/finally so
    the cleanup happens on error paths too.
    """
    effective = compute_effective_policy(agent_id)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="logos-effective-", delete=False,
        encoding="utf-8",
    )
    try:
        yaml.safe_dump(
            effective, tmp,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        tmp.close()
        return Path(tmp.name)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
