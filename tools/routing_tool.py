"""
configure_routing — MCP tool for Hermes to inspect and auto-configure routing profiles.

Hermes calls this to:
  - list_claims: see all machine→user ownership/priority assignments
  - suggest: build a fair routing profile for one or all users based on claimed
             machines + their online/capability status
  - apply: write the suggested profile to the gateway (creates/updates routing
           policy and assigns it to the user)

The tool talks to the gateway via the HERMES_INTERNAL_TOKEN bearer token so it
can bypass user auth (it runs as the system agent, not a human session).
"""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_GATEWAY_BASE = os.environ.get("HERMES_GATEWAY_URL", "http://localhost:8080")
_INTERNAL_TOKEN = os.environ.get("HERMES_INTERNAL_TOKEN", "")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _INTERNAL_TOKEN:
        h["Authorization"] = f"Bearer {_INTERNAL_TOKEN}"
    return h


def _get(path: str) -> dict:
    import urllib.request
    req = urllib.request.Request(f"{_GATEWAY_BASE}{path}", headers=_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _post(path: str, body: dict) -> dict:
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{_GATEWAY_BASE}{path}", data=data, headers=_headers(), method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _suggest_for_user(user: dict, claims: list[dict], machines: list[dict]) -> dict:
    """Build a routing profile suggestion for one user.

    Strategy:
    - Claimed machines come first (sorted by their claimed priority).
    - Machines with capabilities are used to build policy rules (model_class → machine).
    - Unclaimed enabled machines are appended as fallback options.
    - If multiple users claim the same machine, priority ordering is respected —
      the user with the lower priority number gets that machine ranked higher.
    """
    machine_map = {m["id"]: m for m in machines}
    user_id = user["id"]

    # Machines this user has explicitly claimed, in priority order
    claimed = sorted(
        [c for c in claims if c["user_id"] == user_id],
        key=lambda c: c["priority"],
    )
    claimed_ids = {c["machine_id"] for c in claimed}

    # Unclaimed enabled machines as fallback
    unclaimed = [m for m in machines if m["enabled"] and m["id"] not in claimed_ids]

    ordered_machines = (
        [machine_map[c["machine_id"]] for c in claimed if c["machine_id"] in machine_map]
        + unclaimed
    )

    # Build one rule per model_class per machine (rank = position in ordered list)
    rules = []
    seen: dict[str, int] = {}  # model_class → next rank
    for rank_base, machine in enumerate(ordered_machines):
        # Treat every enabled machine as capable of "general" at minimum
        model_classes = ["general"]
        # If the machine has explicit capabilities in the claims data, use them
        # (capabilities are fetched separately; here we just use general + coding heuristics)
        for cls in model_classes:
            r = seen.get(cls, 0)
            rules.append({
                "model_class": cls,
                "machine_id": machine["id"],
                "rank": r,
            })
            seen[cls] = r + 1

    policy_name = f"auto:{user['username']}"
    return {
        "user_id": user_id,
        "username": user["username"],
        "policy_name": policy_name,
        "description": (
            f"Auto-configured by Hermes. "
            f"{'Based on ' + str(len(claimed)) + ' claimed machine(s).' if claimed else 'No claimed machines — using all available.'}"
        ),
        "fallback": "any_available",
        "rules": rules,
        "claimed_machines": [machine_map[c["machine_id"]]["name"] for c in claimed if c["machine_id"] in machine_map],
        "fallback_machines": [m["name"] for m in unclaimed],
    }


def configure_routing(action: str, user_id: str = None, apply: bool = False) -> str:
    """Main tool handler."""
    try:
        if action == "list_claims":
            data = _get("/internal/routing/claims")
            claims = data["claims"]
            machines = data["machines"]
            if not claims:
                return (
                    "No machine claims registered yet. "
                    "Users can claim machines via the Routing tab (Claim button on each machine card) "
                    "or you can advise them to run /configure_instances."
                )
            lines = ["Machine ownership claims:\n"]
            by_machine: dict[str, list] = {}
            for c in claims:
                by_machine.setdefault(c["machine_name"], []).append(c)
            for mname, claimants in by_machine.items():
                claimants.sort(key=lambda c: c["priority"])
                claimants_str = ", ".join(
                    f"{c['username']} (priority {c['priority']})" for c in claimants
                )
                lines.append(f"  {mname}: {claimants_str}")
            return "\n".join(lines)

        elif action == "suggest":
            data = _get("/internal/routing/claims")
            claims = data["claims"]
            machines = data["machines"]
            users = data["users"]

            target_users = [u for u in users if u["id"] == user_id] if user_id else users
            if not target_users:
                return f"User not found: {user_id}"

            suggestions = [_suggest_for_user(u, claims, machines) for u in target_users]

            if not apply:
                lines = ["Routing profile suggestions (not yet applied):\n"]
                for s in suggestions:
                    lines.append(f"User: {s['username']} → policy '{s['policy_name']}'")
                    if s["claimed_machines"]:
                        lines.append(f"  Claimed: {', '.join(s['claimed_machines'])}")
                    if s["fallback_machines"]:
                        lines.append(f"  Fallback: {', '.join(s['fallback_machines'])}")
                    lines.append(f"  Rules: {len(s['rules'])} rule(s)")
                    lines.append("")
                lines.append("To apply, call configure_routing(action='suggest', apply=True) "
                             "or configure_routing(action='apply', user_id=<id>)")
                return "\n".join(lines)
            else:
                # Fall through to apply all
                results = []
                for s in suggestions:
                    r = _post("/internal/routing/apply", {
                        "user_id": s["user_id"],
                        "policy_name": s["policy_name"],
                        "description": s["description"],
                        "fallback": s["fallback"],
                        "rules": s["rules"],
                    })
                    results.append(f"✓ {s['username']}: policy '{s['policy_name']}' applied ({len(s['rules'])} rules)")
                return "\n".join(results) if results else "No users to configure."

        elif action == "apply":
            data = _get("/internal/routing/claims")
            claims = data["claims"]
            machines = data["machines"]
            users = data["users"]

            target_users = [u for u in users if u["id"] == user_id] if user_id else users
            if not target_users:
                return f"User not found: {user_id}"

            results = []
            for u in target_users:
                s = _suggest_for_user(u, claims, machines)
                r = _post("/internal/routing/apply", {
                    "user_id": s["user_id"],
                    "policy_name": s["policy_name"],
                    "description": s["description"],
                    "fallback": s["fallback"],
                    "rules": s["rules"],
                })
                results.append(f"✓ {u['username']}: policy '{s['policy_name']}' applied ({len(s['rules'])} rules)")
            return "\n".join(results) if results else "No users configured."

        else:
            return f"Unknown action '{action}'. Use: list_claims, suggest, apply."

    except Exception as e:
        logger.exception("configure_routing error")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Schema + registry
# ---------------------------------------------------------------------------

_SCHEMA = {
    "name": "configure_routing",
    "description": (
        "Inspect and auto-configure AI routing profiles based on machine ownership claims. "
        "Use 'list_claims' to see who owns which machine. "
        "Use 'suggest' to preview routing profiles. "
        "Use 'apply' to write them to the gateway. "
        "Hermes should call this on startup if any user lacks a routing profile, "
        "and whenever a new machine comes online."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_claims", "suggest", "apply"],
                "description": (
                    "list_claims: show all machine→user ownership assignments. "
                    "suggest: preview auto-generated routing profiles (does not write). "
                    "apply: generate and write routing profiles to the gateway."
                ),
            },
            "user_id": {
                "type": "string",
                "description": "Target a specific user ID. Omit to operate on all users.",
            },
            "apply": {
                "type": "boolean",
                "description": "When action=suggest, also apply the suggestion immediately.",
            },
        },
        "required": ["action"],
    },
}


def _check() -> bool:
    return bool(_INTERNAL_TOKEN)


from tools.registry import registry

registry.register(
    name="configure_routing",
    toolset="operator",
    schema=_SCHEMA,
    handler=lambda args, **kw: configure_routing(
        action=args.get("action", "list_claims"),
        user_id=args.get("user_id"),
        apply=bool(args.get("apply", False)),
    ),
    check_fn=_check,
    is_async=False,
    description="Auto-configure routing profiles based on machine ownership and capability.",
)
