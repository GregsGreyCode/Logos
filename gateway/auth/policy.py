"""Action Policy model — what a user/session/agent is allowed to do.

Six orthogonal policy dimensions, each with an ordered set of values from
most-restrictive to least-restrictive.  Policies compose by always choosing
the *stricter* value across dimensions; this means a session policy can only
tighten, never loosen, the user's baseline policy.

Resolution order (strictest wins):
    resolved = merge(user_policy, session_policy)

If neither is set, DEFAULT_POLICY is used (fully permissive — preserves
existing behaviour for all current users and sessions).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Optional

# ---------------------------------------------------------------------------
# Policy dimension constants
# ---------------------------------------------------------------------------

class NetworkPolicy:
    LOCAL_ONLY        = "local_only"        # in-cluster / LAN only
    ALLOWLISTED       = "allowlisted"       # explicit domain/IP allowlist
    INTERNET_ENABLED  = "internet_enabled"  # unrestricted outbound


class FilesystemPolicy:
    READ_ONLY         = "read_only"         # no writes at all
    WORKSPACE_ONLY    = "workspace_only"    # writes to /work only
    REPO_SCOPED       = "repo_scoped"       # writes to /repo and /work
    FULL              = "full"              # any path


class ExecPolicy:
    NONE              = "none"              # no shell/SSH execution
    RESTRICTED        = "restricted"        # restricted commands only
    FULL              = "full"              # unrestricted exec


class WritePolicy:
    DRY_RUN           = "dry_run"           # all mutations blocked
    APPROVAL_REQUIRED = "approval_required" # must be approved before executing
    AUTO_APPLY        = "auto_apply"        # execute without approval gate


class ProviderPolicy:
    LOCAL_ONLY        = "local_only"        # only providers with trust=local
    TRUSTED_EXTERNAL  = "trusted_external"  # local + trusted_external
    ANY               = "any"              # all providers including untrusted


class SecretPolicy:
    REDACTED          = "redacted"          # secrets masked in output
    TOOL_ONLY         = "tool_only"         # secrets usable in tool calls only
    UNRESTRICTED      = "unrestricted"      # no masking


# ---------------------------------------------------------------------------
# Ordinal rankings (lower = more restrictive)
# ---------------------------------------------------------------------------

_NETWORK_RANK: dict[str, int] = {
    NetworkPolicy.LOCAL_ONLY:       0,
    NetworkPolicy.ALLOWLISTED:      1,
    NetworkPolicy.INTERNET_ENABLED: 2,
}

_FS_RANK: dict[str, int] = {
    FilesystemPolicy.READ_ONLY:       0,
    FilesystemPolicy.WORKSPACE_ONLY:  1,
    FilesystemPolicy.REPO_SCOPED:     2,
    FilesystemPolicy.FULL:            3,
}

_EXEC_RANK: dict[str, int] = {
    ExecPolicy.NONE:       0,
    ExecPolicy.RESTRICTED: 1,
    ExecPolicy.FULL:       2,
}

_WRITE_RANK: dict[str, int] = {
    WritePolicy.DRY_RUN:           0,
    WritePolicy.APPROVAL_REQUIRED: 1,
    WritePolicy.AUTO_APPLY:        2,
}

_PROVIDER_RANK: dict[str, int] = {
    ProviderPolicy.LOCAL_ONLY:       0,
    ProviderPolicy.TRUSTED_EXTERNAL: 1,
    ProviderPolicy.ANY:              2,
}

_SECRET_RANK: dict[str, int] = {
    SecretPolicy.REDACTED:      0,
    SecretPolicy.TOOL_ONLY:     1,
    SecretPolicy.UNRESTRICTED:  2,
}


def _stricter(rank_map: dict[str, int], a: str, b: str) -> str:
    """Return the more restrictive of two values (lower rank wins)."""
    return a if rank_map.get(a, 99) <= rank_map.get(b, 99) else b


# ---------------------------------------------------------------------------
# ActionPolicy dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ActionPolicy:
    id:               str  = "default"
    name:             str  = "default"
    description:      str  = ""
    network_policy:   str  = NetworkPolicy.INTERNET_ENABLED
    network_allowlist: list = dataclasses.field(default_factory=list)
    filesystem_policy: str = FilesystemPolicy.WORKSPACE_ONLY
    exec_policy:      str  = ExecPolicy.RESTRICTED
    write_policy:     str  = WritePolicy.AUTO_APPLY
    provider_policy:  str  = ProviderPolicy.ANY
    secret_policy:    str  = SecretPolicy.TOOL_ONLY

    @classmethod
    def from_row(cls, row: dict) -> "ActionPolicy":
        allowlist = []
        raw = row.get("network_allowlist") or "[]"
        try:
            allowlist = json.loads(raw)
        except (ValueError, TypeError):
            pass
        return cls(
            id=row["id"],
            name=row["name"],
            description=row.get("description") or "",
            network_policy=row.get("network_policy",   NetworkPolicy.INTERNET_ENABLED),
            network_allowlist=allowlist,
            filesystem_policy=row.get("filesystem_policy", FilesystemPolicy.WORKSPACE_ONLY),
            exec_policy=row.get("exec_policy",         ExecPolicy.RESTRICTED),
            write_policy=row.get("write_policy",       WritePolicy.AUTO_APPLY),
            provider_policy=row.get("provider_policy", ProviderPolicy.ANY),
            secret_policy=row.get("secret_policy",     SecretPolicy.TOOL_ONLY),
        )

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return d

    def to_db_dict(self) -> dict:
        """Serialise for INSERT/UPDATE into action_policies table."""
        return {
            "id":               self.id,
            "name":             self.name,
            "description":      self.description,
            "network_policy":   self.network_policy,
            "network_allowlist": json.dumps(self.network_allowlist),
            "filesystem_policy": self.filesystem_policy,
            "exec_policy":      self.exec_policy,
            "write_policy":     self.write_policy,
            "provider_policy":  self.provider_policy,
            "secret_policy":    self.secret_policy,
        }


# The permissive default preserves all existing behaviour for users/sessions
# that have no policy assigned.
DEFAULT_POLICY = ActionPolicy()


# ---------------------------------------------------------------------------
# Policy merge (strictest wins across all dimensions)
# ---------------------------------------------------------------------------

def merge_policies(*policies: Optional[ActionPolicy]) -> ActionPolicy:
    """Merge multiple policies — stricter value wins on every dimension.

    Accepts None entries (treated as DEFAULT_POLICY).
    Typical call: merge_policies(user_policy, session_policy)
    """
    result = ActionPolicy()
    for p in policies:
        if p is None:
            continue
        result.network_policy   = _stricter(_NETWORK_RANK,   result.network_policy,   p.network_policy)
        result.filesystem_policy = _stricter(_FS_RANK,       result.filesystem_policy, p.filesystem_policy)
        result.exec_policy      = _stricter(_EXEC_RANK,      result.exec_policy,      p.exec_policy)
        result.write_policy     = _stricter(_WRITE_RANK,     result.write_policy,     p.write_policy)
        result.provider_policy  = _stricter(_PROVIDER_RANK,  result.provider_policy,  p.provider_policy)
        result.secret_policy    = _stricter(_SECRET_RANK,    result.secret_policy,    p.secret_policy)
        # Allowlist: intersection when both non-empty; otherwise union
        if p.network_allowlist:
            if not result.network_allowlist:
                result.network_allowlist = list(p.network_allowlist)
            else:
                result.network_allowlist = list(
                    set(result.network_allowlist) & set(p.network_allowlist)
                )
    return result


# ---------------------------------------------------------------------------
# Tool categorisation
# ---------------------------------------------------------------------------

# Canonical action type strings used in approval_requests.action_type
ACTION_FILE_WRITE   = "file_write"
ACTION_GIT_MUTATION = "git_mutation"
ACTION_KUBECTL      = "kubectl_mutation"
ACTION_DOCKER       = "docker_mutation"
ACTION_SSH_EXEC     = "ssh_exec"
ACTION_EXEC         = "exec"
ACTION_EXTERNAL_API = "external_api"
ACTION_MCP_ACCESS   = "mcp_access"   # gateway MCP server access request
ACTION_OTHER        = "other"

_TOOL_ACTION_MAP: dict[str, str] = {
    # File writes
    "files_write":                                       ACTION_FILE_WRITE,
    "mcp_homelab-operator_files_write":                  ACTION_FILE_WRITE,
    "write_file":                                        ACTION_FILE_WRITE,
    # Patch / edit tools also write files and must go through filesystem policy
    "patch_replace":                                     ACTION_FILE_WRITE,
    "patch_v4a":                                         ACTION_FILE_WRITE,
    "patch":                                             ACTION_FILE_WRITE,
    # Git mutations
    "git_commit":                                        ACTION_GIT_MUTATION,
    "git_push":                                          ACTION_GIT_MUTATION,
    "mcp_homelab-operator_git_commit_tool":              ACTION_GIT_MUTATION,
    "mcp_homelab-operator_git_push_tool":                ACTION_GIT_MUTATION,
    # kubectl
    "kubectl_apply":                                     ACTION_KUBECTL,
    "kubectl_delete":                                    ACTION_KUBECTL,
    "kubectl_patch":                                     ACTION_KUBECTL,
    "mcp_homelab-operator_kubectl_apply_tool":           ACTION_KUBECTL,
    "mcp_homelab-operator_kubectl_delete_deployment_tool": ACTION_KUBECTL,
    "mcp_homelab-operator_kubectl_rollout_restart_tool": ACTION_KUBECTL,
    "mcp_homelab-operator_kubectl_rollout_status_tool":  ACTION_KUBECTL,
    # Docker
    "docker_build":                                      ACTION_DOCKER,
    "docker_push":                                       ACTION_DOCKER,
    "mcp_homelab-operator_docker_build_tool":            ACTION_DOCKER,
    "mcp_homelab-operator_docker_push_tool":             ACTION_DOCKER,
    # SSH exec
    "ssh_exec":                                          ACTION_SSH_EXEC,
    "mcp_homelab-ssh_ssh_exec":                          ACTION_SSH_EXEC,
    # General exec
    "bash":                                              ACTION_EXEC,
    "terminal":                                          ACTION_EXEC,
    "run_command":                                       ACTION_EXEC,
    # External network / API
    "web_search":                                        ACTION_EXTERNAL_API,
    "web_extract":                                       ACTION_EXTERNAL_API,
    "web_scrape":                                        ACTION_EXTERNAL_API,
    "search_web":                                        ACTION_EXTERNAL_API,
}


def categorise_tool(tool_name: str) -> str:
    """Return the action type category for a tool name."""
    # Exact match first
    if tool_name in _TOOL_ACTION_MAP:
        return _TOOL_ACTION_MAP[tool_name]
    # Substring heuristics for dynamically-named MCP tools
    n = tool_name.lower()
    if "git_commit" in n or "git_push" in n:
        return ACTION_GIT_MUTATION
    if "kubectl" in n:
        return ACTION_KUBECTL
    if "docker_build" in n or "docker_push" in n:
        return ACTION_DOCKER
    if "ssh_exec" in n:
        return ACTION_SSH_EXEC
    if "files_write" in n or "write_file" in n or "patch_replace" in n or "patch_v4a" in n:
        return ACTION_FILE_WRITE
    if "terminal" in n or "bash" in n or "exec" in n:
        return ACTION_EXEC
    if "web_search" in n or "web_extract" in n or "web_scrape" in n or "search_web" in n:
        return ACTION_EXTERNAL_API
    return ACTION_OTHER


# ---------------------------------------------------------------------------
# Policy violation / check
# ---------------------------------------------------------------------------

class PolicyViolation(Exception):
    """Hard policy block — action must not proceed even with approval."""
    def __init__(self, dimension: str, action: str, policy_value: str, message: str = ""):
        self.dimension   = dimension
        self.action      = action
        self.policy_value = policy_value
        super().__init__(message or f"Policy blocked: {dimension}={policy_value} forbids {action}")


def check_tool(tool_name: str, policy: ActionPolicy) -> tuple[bool, bool, str]:
    """Check a tool call against an action policy.

    Returns:
        (allowed, requires_approval, reason)

    - allowed=False, requires_approval=False → hard block (PolicyViolation equivalent)
    - allowed=True,  requires_approval=True  → must go through approval gate
    - allowed=True,  requires_approval=False → proceed immediately
    """
    action_type = categorise_tool(tool_name)

    # ── External network actions ──────────────────────────────────────────
    if action_type == ACTION_EXTERNAL_API:
        if policy.network_policy == NetworkPolicy.LOCAL_ONLY:
            return False, False, f"network_policy=local_only blocks external call {tool_name}"
        return True, False, ""

    # ── Exec actions ─────────────────────────────────────────────────────
    if action_type in (ACTION_SSH_EXEC, ACTION_EXEC):
        if policy.exec_policy == ExecPolicy.NONE:
            return False, False, f"exec_policy=none blocks {tool_name}"
        # RESTRICTED: local terminal is allowed but remote exec (ssh) is blocked
        if policy.exec_policy == ExecPolicy.RESTRICTED and action_type == ACTION_SSH_EXEC:
            return False, False, f"exec_policy=restricted blocks remote execution: {tool_name}"
        if policy.write_policy == WritePolicy.DRY_RUN:
            return False, False, f"write_policy=dry_run blocks exec tool {tool_name}"
        if policy.write_policy == WritePolicy.APPROVAL_REQUIRED:
            return True, True, f"write_policy=approval_required: {tool_name} needs approval"
        return True, False, ""

    # ── Write/mutation actions ────────────────────────────────────────────
    if action_type in (ACTION_FILE_WRITE, ACTION_GIT_MUTATION,
                       ACTION_KUBECTL, ACTION_DOCKER):
        if action_type == ACTION_FILE_WRITE and policy.filesystem_policy == FilesystemPolicy.READ_ONLY:
            return False, False, f"filesystem_policy=read_only blocks {tool_name}"
        if policy.write_policy == WritePolicy.DRY_RUN:
            return False, False, f"write_policy=dry_run blocks {tool_name}"
        if policy.write_policy == WritePolicy.APPROVAL_REQUIRED:
            return True, True, f"write_policy=approval_required: {tool_name} needs approval"
        return True, False, ""

    # All other tools pass through
    return True, False, ""


def _get_repo_roots() -> list:
    """Return the list of resolved absolute paths allowed under repo_scoped policy.

    Reads HERMES_REPO_ROOTS (colon-separated) env var.  Falls back to sensible
    defaults: /repo (homelab-infra mount in the Hermes pod), ~/work, ~/workspace.

    Symlinks in the configured roots are resolved so comparisons are consistent.
    """
    import os as _os
    raw = _os.getenv("HERMES_REPO_ROOTS", "")
    if raw:
        paths = [p.strip() for p in raw.split(":") if p.strip()]
    else:
        paths = ["/repo", "~/work", "~/workspace"]
    resolved = []
    for p in paths:
        try:
            r = _os.path.realpath(_os.path.expanduser(p))
            resolved.append(r)
        except Exception:
            pass
    return resolved


def check_filesystem_path(
    path: str,
    policy: "ActionPolicy",
    workspace_path=None,  # str | Path | None
) -> tuple[bool, str]:
    """Check whether a write to *path* is permitted under *policy*.

    Returns (allowed, reason).
    Called by check_policy_for_tool() for FILE_WRITE actions after the
    coarse check_tool() pass.

    TRUST BOUNDARY: path containment is verified with os.path.realpath(), which
    resolves symlinks at check time.  A symlink swap between check and write
    (TOCTOU) cannot be prevented here — that requires kernel-level sandboxing.

    This function fails *closed*: if path resolution raises an OS error, the
    write is denied rather than allowed.
    """
    import os as _os

    fp = policy.filesystem_policy

    if fp == FilesystemPolicy.READ_ONLY:
        return False, f"filesystem_policy=read_only blocks all writes to '{path}'"

    if fp == FilesystemPolicy.WORKSPACE_ONLY:
        if workspace_path is None:
            # No workspace assigned yet — allow (graceful degradation for sessions
            # where workspace creation failed silently).
            return True, ""
        try:
            resolved_path = _os.path.realpath(_os.path.expanduser(path))
            resolved_ws = _os.path.realpath(str(workspace_path))
            sep = _os.sep
            inside = (
                resolved_path == resolved_ws
                or resolved_path.startswith(resolved_ws + sep)
            )
        except Exception as exc:
            # Fail closed: if we cannot resolve the path, block the write.
            return False, (
                f"filesystem_policy=workspace_only: could not resolve path '{path}' "
                f"({exc}) — write blocked for safety"
            )
        if not inside:
            return False, (
                f"filesystem_policy=workspace_only: '{path}' resolves outside the task "
                f"workspace ({workspace_path}). Write to the workspace directory or "
                "request a policy change."
            )
        return True, ""

    if fp == FilesystemPolicy.REPO_SCOPED:
        # Allow writes that land inside the workspace (if one is active) OR
        # inside one of the configured repo roots.
        repo_roots = _get_repo_roots()
        try:
            resolved_path = _os.path.realpath(_os.path.expanduser(path))
            sep = _os.sep
            # Workspace writes are always allowed under repo_scoped
            if workspace_path is not None:
                resolved_ws = _os.path.realpath(str(workspace_path))
                if resolved_path == resolved_ws or resolved_path.startswith(resolved_ws + sep):
                    return True, ""
            # Check configured repo roots
            for root in repo_roots:
                if resolved_path == root or resolved_path.startswith(root + sep):
                    return True, ""
        except Exception as exc:
            return False, (
                f"filesystem_policy=repo_scoped: could not resolve path '{path}' "
                f"({exc}) — write blocked for safety"
            )
        roots_display = ", ".join(repo_roots) if repo_roots else "(none configured)"
        return False, (
            f"filesystem_policy=repo_scoped: '{path}' is outside the allowed repo roots "
            f"({roots_display}). Set HERMES_REPO_ROOTS or write to a listed root."
        )

    # FULL — any path allowed
    return True, ""


def check_provider(provider_trust: str, policy: ActionPolicy) -> tuple[bool, str]:
    """Check a provider's trust level against the policy's provider_policy.

    provider_trust: 'local' | 'trusted_external' | 'untrusted_external'
    Returns: (allowed, reason)
    """
    if policy.provider_policy == ProviderPolicy.LOCAL_ONLY:
        if provider_trust != "local":
            return False, f"provider_policy=local_only blocks {provider_trust} provider"
    elif policy.provider_policy == ProviderPolicy.TRUSTED_EXTERNAL:
        if provider_trust == "untrusted_external":
            return False, f"provider_policy=trusted_external blocks untrusted_external provider"
    return True, ""
