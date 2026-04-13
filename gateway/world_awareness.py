"""Agent World awareness — turns DB records into strings the LLM can use.

Two surfaces:

1. ``describe_character(char_index)`` — decodes the 8×4×5 sprite-sheet
   index into a one-line natural-language appearance, so an agent can
   answer "what do you look like?".

2. ``build_world_snapshot()`` — returns a structured snapshot of every
   named agent on the system (name, soul, model, appearance, status,
   busyness). Used both by the system-prompt injector (passive
   awareness) and by the ``get_agent_world`` tool (active query).

The two surfaces are intentionally simple and read-only. The eventual
agent-to-agent messaging feature will layer on top — agents will use
this snapshot to choose who to delegate to or address.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Per-body description matches the eight ai-town source characters in
# assets/world/characters.png. Order is the source layout: top row 0..3
# are the dark-haired/varied bodies, bottom row 4..7. Worded as static
# noun phrases so the system-prompt template can wrap them naturally.
_BODY_DESCRIPTIONS = [
    "short dark hair and a violet shirt",
    "short dark hair and a teal shirt",
    "long dark hair and a grey dress",
    "white hair and a heavy dark coat",
    "spiky blond hair and dark clothes",
    "long pink hair and a violet outfit",
    "shaggy light brown hair and a white tank top",
    "long blond hair and a soft pink shirt",
]

_SKIN_NAMES = ["fair", "light", "medium", "dark"]

# hair index 0 = original palette; 1..4 = Logos theme tints applied to
# the outfit/hair colours via HSV hue shift in the sprite generator.
_THEME_NAMES = ["original", "midnight blue", "crimson red", "terminal green", "dusk purple"]


def describe_character(char_index: Optional[int]) -> str:
    """Return a one-line appearance description for an agent.

    ``char_index`` follows the 8×4×5 encoding ``body*20 + skin*5 + theme``
    described in ``gateway/world/SpriteData.js``. ``None`` means the
    sprite was auto-assigned from the agent name (the ``?`` slot in the
    picker) — we still produce a generic but evocative description.
    """
    if char_index is None:
        return "a pixel-art character from the Agent World, auto-assigned from your name"

    try:
        ci = int(char_index)
    except (TypeError, ValueError):
        return "a pixel-art character from the Agent World"

    if not 0 <= ci <= 159:
        return "a pixel-art character from the Agent World"

    body = ci // 20
    skin = (ci % 20) // 5
    theme = ci % 5

    body_desc = _BODY_DESCRIPTIONS[body]
    skin_name = _SKIN_NAMES[skin]
    theme_clause = "" if theme == 0 else f", recoloured in {_THEME_NAMES[theme]} tones"

    return f"a pixel-art character with {skin_name} skin, {body_desc}{theme_clause}"


def build_world_snapshot(
    *,
    self_agent_id: Optional[str] = None,
    include_self: bool = False,
) -> dict:
    """Return the current world snapshot as a JSON-able dict.

    Shape::

        {
          "agents": [{"name", "soul", "model", "appearance",
                      "status", "busy", "is_self"} ...],
          "total_agents": int,
        }

    ``self_agent_id`` lets callers mark which agent is "you" in the
    snapshot (the system-prompt injector flips ``include_self=False``
    so the agent doesn't see itself listed alongside its peers; the
    tool flips it ``True`` so the snapshot is complete for inspection).

    Failures are logged and an empty snapshot is returned — the world
    snapshot is observability, never on the critical path.
    """
    try:
        from gateway.auth import db as auth_db
    except Exception as exc:
        logger.warning("world_snapshot: auth db import failed: %s", exc)
        return {"agents": [], "total_agents": 0}

    try:
        agents = auth_db.list_agents()
    except Exception as exc:
        logger.warning("world_snapshot: list_agents failed: %s", exc)
        return {"agents": [], "total_agents": 0}

    # Worker registry tells us which agents have a live sandbox connected.
    # Failure here is non-fatal — we just report status="unknown".
    registry = None
    try:
        from gateway.worker_registry import get_registry
        registry = get_registry()
    except Exception:
        registry = None

    out = []
    for a in agents:
        if not include_self and self_agent_id and a.get("id") == self_agent_id:
            continue

        status = "unknown"
        active_tasks = 0
        if registry is not None:
            try:
                if hasattr(registry, "is_running") and registry.is_running(a["id"]):
                    status = "running"
                elif hasattr(registry, "worker_connected") and registry.worker_connected(a["id"]):
                    status = "running"
                else:
                    status = "offline"
                if hasattr(registry, "active_task_count"):
                    active_tasks = int(registry.active_task_count(a["id"]) or 0)
            except Exception:
                pass

        out.append({
            "name": a.get("name") or "Unnamed",
            "soul": a.get("soul_slug") or "general",
            "model": a.get("model") or "default",
            "appearance": describe_character(a.get("char_index")),
            "status": status,
            "busy": active_tasks > 0,
            "is_self": bool(self_agent_id and a.get("id") == self_agent_id),
        })

    return {"agents": out, "total_agents": len(out)}


def render_self_and_peers_prompt(
    self_agent_record: Optional[dict],
) -> str:
    """Build the awareness paragraph that gets prepended to every system
    prompt. Self-description first, then a compact roster of other
    agents on the system. Empty string if there's nothing useful to say
    (single-agent install, or the record is missing).
    """
    if not self_agent_record:
        return ""

    name = self_agent_record.get("name") or "Agent"
    appearance = describe_character(self_agent_record.get("char_index"))

    snapshot = build_world_snapshot(
        self_agent_id=self_agent_record.get("id"),
        include_self=False,
    )
    peers = snapshot.get("agents", [])

    parts = [
        f"You appear as {appearance}, and you live in the Agent World "
        f"— a shared, tamagotchi-like environment where named agents "
        f"coexist, can see each other, and may eventually collaborate."
    ]

    if peers:
        roster = "\n".join(
            f"  - {p['name']} ({p['soul']}, {p['status']}{', busy' if p['busy'] else ''})"
            for p in peers[:12]
        )
        suffix = "" if len(peers) <= 12 else f"\n  ... and {len(peers) - 12} more"
        parts.append(
            f"Other agents currently in the world (you can refer to them by name):\n"
            f"{roster}{suffix}"
        )
        parts.append(
            "When relevant, you can mention these peers, suggest delegating "
            f"to one whose soul fits the task, or note that you are working alongside them. "
            f"Use the get_agent_world tool to refresh this list mid-session."
        )

    return "\n\n".join(parts)
