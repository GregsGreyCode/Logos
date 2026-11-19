"""Role-based access control: two roles (admin, user) and their permissions.

v2 has two people on it, so v1's four-role matrix collapsed to two in S1.6.
The permission names are the ones handlers in ``gateway/`` still check via
``require_permission`` / ``has_permission``. Everything else an admin may do
(updates, all agents' model figures) is checked as ``role == "admin"``.
"""

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "manage_platform",          # wipe and re-run setup
        "manage_users",             # add people
    },
    "user": set(),
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


def get_permissions(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(normalize_role(role), set()))
