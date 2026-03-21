"""Idempotent seed system for Hermes.

Rules:
- NEVER overwrites existing records.
- Each seed function is guarded by an emptiness check.
- All seed data is generic and example-friendly — no personal names or real IPs.
- run_seed() is safe to call on every startup; it is a no-op on established deployments.
"""

import logging

import gateway.auth.db as db
from gateway.auth.password import hash_password

logger = logging.getLogger(__name__)

# ── Seed data constants ────────────────────────────────────────────────────────

_MACHINES = [
    {
        "name":         "High Performance Node",
        "endpoint_url": "http://localhost:1234/v1",
        "description":  "Example GPU node — edit endpoint and capabilities to match your hardware.",
        "capabilities": ["lightweight", "coding", "general", "reasoning"],
    },
    {
        "name":         "Secondary Node",
        "endpoint_url": "http://localhost:8080/v1",
        "description":  "Example secondary node — lighter workloads and fallback.",
        "capabilities": ["lightweight", "general"],
    },
]

# Profile rules are expressed as (model_class, machine_name_key, rank) tuples.
# machine_name_key maps to _MACHINES[*]["name"].
_PROFILE_NAME = "default"
_PROFILE_DESCRIPTION = "Auto-generated default profile — edit or replace."
_PROFILE_RULES_SPEC = [
    # lightweight: Secondary first (lower cost), HP as fallback
    ("lightweight", "Secondary Node",          1),
    ("lightweight", "High Performance Node",   2),
    # general: same preference
    ("general",     "Secondary Node",          1),
    ("general",     "High Performance Node",   2),
    # wildcard catches coding / reasoning / anything else → HP only
    ("*",           "High Performance Node",   1),
]

_ADMIN_EMAIL    = "admin@example.com"
_ADMIN_USERNAME = "admin"
_ADMIN_PASSWORD = "admin"   # Must be changed after first login.
_ADMIN_DISPLAY  = "Admin"


# ── Individual seed functions ──────────────────────────────────────────────────

def seed_machines() -> dict[str, str]:
    """Insert example machines if the machines table is empty.

    Returns a mapping of machine name → machine id for downstream use.
    """
    existing = db.list_machines()
    if existing:
        logger.debug("seed_machines: skipped (%d machines already present)", len(existing))
        return {m["name"]: m["id"] for m in existing}

    name_to_id: dict[str, str] = {}
    for spec in _MACHINES:
        machine = db.create_machine(
            name=spec["name"],
            endpoint_url=spec["endpoint_url"],
            description=spec["description"],
        )
        db.set_machine_capabilities(machine["id"], spec["capabilities"])
        name_to_id[machine["name"]] = machine["id"]
        logger.info("seed_machines: created '%s' (%s)", machine["name"], machine["id"])

    return name_to_id


def seed_profile(name_to_id: dict[str, str]) -> str | None:
    """Insert the default profile + rules if no profiles exist.

    Returns the new profile id, or None if skipped.
    name_to_id: mapping of machine name → machine id (from seed_machines).
    """
    existing = db.list_policies()
    if existing:
        logger.debug("seed_profile: skipped (%d profiles already present)", len(existing))
        return None

    profile = db.create_policy(
        name=_PROFILE_NAME,
        description=_PROFILE_DESCRIPTION,
        fallback="any_available",
    )

    rules = []
    for rank_idx, (model_class, machine_name, rank) in enumerate(_PROFILE_RULES_SPEC):
        machine_id = name_to_id.get(machine_name)
        if not machine_id:
            logger.warning(
                "seed_profile: machine '%s' not found — skipping rule for class '%s'",
                machine_name, model_class,
            )
            continue
        rules.append({"model_class": model_class, "machine_id": machine_id, "rank": rank})

    db.set_policy_rules(profile["id"], rules)
    logger.info(
        "seed_profile: created '%s' (%s) with %d rules",
        profile["name"], profile["id"], len(rules),
    )
    return profile["id"]


def seed_admin_user(policy_id: str | None = None) -> None:
    """Insert a default admin user if no users exist.

    If policy_id is provided the new user is assigned that profile.
    The generated password is intentionally weak — it must be changed
    immediately after first login.
    """
    users, total = db.list_users(page=1, limit=1)
    if total > 0:
        logger.debug("seed_admin_user: skipped (%d users already present)", total)
        return

    user = db.create_user(
        email=_ADMIN_EMAIL,
        username=_ADMIN_USERNAME,
        password_hash=hash_password(_ADMIN_PASSWORD),
        role="admin",
        display_name=_ADMIN_DISPLAY,
    )
    if policy_id:
        db.assign_user_policy(user["id"], policy_id)

    logger.info(
        "seed_admin_user: created '%s' (id=%s)%s",
        user["email"], user["id"],
        f" with profile {policy_id}" if policy_id else "",
    )
    logger.warning(
        "SEED: default admin account created (admin@example.com / admin). "
        "Change this password immediately."
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def run_seed() -> None:
    """Run all seed steps in dependency order.

    Each step is independently guarded — already-populated tables are skipped.
    Safe to call on every startup.
    """
    name_to_id = seed_machines()
    policy_id  = seed_profile(name_to_id)
    seed_admin_user(policy_id)


# ── Setup wizard helpers (called from the first-run UI) ───────────────────────

def apply_single_machine_setup(endpoint_url: str = "http://localhost:1234/v1") -> dict:
    """Create a single 'Local Node' with all capabilities and a catch-all profile.

    Only runs if machines table is empty.  Returns {machine_id, policy_id}.
    """
    if db.list_machines():
        return {"error": "machines_already_exist"}

    machine = db.create_machine(
        name="Local Node",
        endpoint_url=endpoint_url,
        description="Single local inference node — edit to match your setup.",
    )
    db.set_machine_capabilities(
        machine["id"],
        ["lightweight", "general", "coding", "reasoning", "vision", "embedding"],
    )

    profile = db.create_policy(
        name="default",
        description="Auto-created by setup wizard.",
        fallback="any_available",
    )
    db.set_policy_rules(profile["id"], [
        {"model_class": "*", "machine_id": machine["id"], "rank": 1},
    ])

    logger.info("setup wizard: single-machine setup applied")
    return {"machine_id": machine["id"], "policy_id": profile["id"]}


def apply_multi_machine_setup() -> dict:
    """Create the two example nodes with the default profile rules.

    Only runs if machines table is empty.  Returns {machine_ids, policy_id}.
    """
    if db.list_machines():
        return {"error": "machines_already_exist"}

    name_to_id = seed_machines()
    policy_id  = seed_profile(name_to_id)
    return {"machine_ids": list(name_to_id.values()), "policy_id": policy_id}
