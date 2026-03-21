"""Role-based access control: roles, permissions, and enforcement helpers."""

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "spawn_instance",           # unrestricted soul spawn
        "spawn_instance_any_soul",  # alias: all souls, including non-user-accessible
        "delete_instance",
        "view_instances",
        "manage_souls",
        "manage_users",
        "manage_machines",
        "manage_profiles",          # create/edit/delete routing profiles (policies)
        "assign_profile",           # assign a routing profile to a user
        "override_routing",         # override machine selection at spawn time
        "override_toolsets",
        "view_routing_debug",       # access routing debug/resolve tools
        "view_audit_logs",
        "view_settings",
        "manage_settings",
        "promote_canary",
        "manage_platform",
        "claim_machine",            # stake ownership/priority on a compute machine
        # Policy & Trust (v1)
        "manage_action_policies",   # create/edit/delete action policies
        "assign_action_policy",     # assign action policy to a user
        "view_approvals",           # view all pending approval requests
        "decide_approvals",         # approve or reject any pending request
        # Workflow execution layer (v1)
        "manage_workflows",         # create/edit/delete workflow definitions
        "trigger_workflow",         # start a workflow run
        "view_workflows",           # view definitions, runs, and step state
        "decide_workflow_approvals", # approve/reject workflow approval steps
        # Agent run records
        "view_runs",                # view agent run audit records
    },
    "operator": {
        "spawn_instance",
        "delete_instance",
        "view_instances",
        "assign_profile",           # operators can assign routing profiles to users
        "override_routing",         # operators can override machine at spawn time
        "override_toolsets",
        "view_routing_debug",       # operators can use the routing debug/resolve tool
        "view_audit_logs",
        "view_settings",
        "claim_machine",            # operators can stake machine claims
        # Policy & Trust (v1)
        "manage_action_policies",
        "assign_action_policy",
        "view_approvals",
        "decide_approvals",
        # Workflow execution layer (v1)
        "manage_workflows",
        "trigger_workflow",
        "view_workflows",
        "decide_workflow_approvals",
        # Agent run records
        "view_runs",
    },
    "user": {
        "spawn_instance_restricted",  # only souls with user_accessible: true
        "view_instances",
        "view_settings",
        "claim_machine",            # users can claim machines assigned to them
        # Users can view their own pending approvals (filtered by session in handler)
        "view_approvals",
        "view_workflows",           # users can view workflow runs (read-only)
        "view_runs",                # users can view their own agent run records
    },
    "viewer": {
        "view_instances",
        "view_settings",
        "view_runs",                # viewers can see their own run records
    },
}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())


def can_spawn(role: str, soul_manifest_dict: dict) -> bool:
    """True if this role is allowed to spawn the given soul."""
    if has_permission(role, "spawn_instance"):
        return True
    if has_permission(role, "spawn_instance_restricted"):
        return bool(soul_manifest_dict.get("user_accessible", False))
    return False


def get_permissions(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(role, set()))
