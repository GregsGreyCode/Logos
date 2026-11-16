"""Role-based access control: two roles (admin, user) and their permissions.

v2 has two people on it, so v1's four-role matrix collapsed to two in S1.6.
The permission names are the ones handlers in ``gateway/`` still check via
``require_permission`` / ``has_permission``; ``manage_machines`` is a v1
name that survives as the general "admin operation" gate on those handlers.
"""

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "spawn_instance",           # unrestricted soul spawn
        "delete_instance",
        "view_instances",
        "override_toolsets",
        "manage_machines",          # general admin gate (agents, costs, updates, ...)
        "manage_platform",
        "manage_users",
        "view_audit_logs",
        "view_approvals",           # view all pending approval requests
        "decide_approvals",         # approve or reject any pending request
        "view_souls",               # list + view soul manifests and soul.md content
        "manage_souls",             # create/edit/delete soul directories
        "view_runs",                # view dispatch records
        "view_evolution",           # view proposals and settings
        "manage_evolution",         # configure evolution settings / trigger runs
        "decide_evolution",         # accept / decline / question proposals
    },
    "user": {
        "spawn_instance_restricted",  # only souls with user_accessible: true
        "view_instances",
        "view_approvals",           # own pending approvals (filtered in handler)
        "view_runs",                # own dispatch records (filtered in handler)
        "view_evolution",
    },
}

ROLES: tuple[str, ...] = ("admin", "user")

# v1 roles that no longer exist. Both become plain users: operator loses
# its elevated rights rather than being promoted to admin. The
# collapse_roles_v1 migration in gateway/auth/db.py rewrites stored rows
# the same way; this mapping also covers role claims in tokens issued
# before that migration ran.
_LEGACY_ROLES: dict[str, str] = {
    "operator": "user",
    "viewer":   "user",
}


def normalize_role(role: str | None) -> str:
    """Map any role name to ``admin`` or ``user``; unknown names to ``""``."""
    role = (role or "").strip().lower()
    if role in ROLE_PERMISSIONS:
        return role
    return _LEGACY_ROLES.get(role, "")


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(normalize_role(role), set())


def can_spawn(role: str, soul_manifest_dict: dict) -> bool:
    """True if this role is allowed to spawn the given soul."""
    if has_permission(role, "spawn_instance"):
        return True
    if has_permission(role, "spawn_instance_restricted"):
        return bool(soul_manifest_dict.get("user_accessible", False))
    return False


def get_permissions(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(normalize_role(role), set()))
