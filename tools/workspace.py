#!/usr/bin/env python3
"""
Workspace isolation and filesystem policy enforcement for Logos agent runs.

This module provides Python-level enforcement of file access boundaries.
It is NOT a kernel-level sandbox — a sufficiently clever agent or tool
could still escape via mechanisms not covered here (e.g. direct syscalls,
network-accessible side channels, or shell built-ins that bypass Python's
file wrappers).  True isolation requires containers, namespaces, seccomp,
or similar OS primitives.

What this module materially hardens:
- Path traversal via .. sequences (os.path.realpath resolves before check)
- Symlink-based escapes (realpath follows all symlinks before access check)
- Indirect path references (all paths normalized to absolute real paths)
- Writes outside workspace root when policy=workspace_only
- Writes outside repo roots when policy=repo_scoped
- Shell command classification for dry_run write_policy
- Workspace TTL expiry and automatic cleanup

What this module does NOT prevent:
- Shell commands that open /proc/self/fd/... or similar OS escapes
- Native code that bypasses Python file wrappers entirely
- Network-accessible writes (HTTP PUT, S3, scp, etc.)
- Symlinks INSIDE the workspace that point outside — caught on access,
  not on symlink creation (realpath at access time is the defense)
- Any escape in container backends (Docker/Modal enforce their own
  namespace boundaries; this module's checks are redundant for those
  backends but harmless)

Trust model:
  Enforcement is in Python before tool dispatch.  If the agent can execute
  arbitrary Python or shell code, it can bypass these checks.  Use container
  backends for stronger isolation.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Policy enums
# =============================================================================

class FilesystemPolicy(str, Enum):
    """Controls which paths a run is allowed to write."""
    FULL_ACCESS    = "full_access"    # No path restrictions (default behaviour)
    WORKSPACE_ONLY = "workspace_only" # Writes confined to the run workspace dir
    REPO_SCOPED    = "repo_scoped"    # Writes confined to configured repo roots
    READ_ONLY      = "read_only"      # No writes permitted anywhere


class WritePolicy(str, Enum):
    """Controls how writes are executed when permitted by FilesystemPolicy."""
    FULL_WRITE = "full_write"  # Execute writes normally
    DRY_RUN    = "dry_run"     # Log writes but do not execute; exec blocked
    READ_ONLY  = "read_only"   # No writes at all (same effect as READ_ONLY fs policy)


# =============================================================================
# Shell command classifier (used by dry_run enforcement in terminal_tool)
# =============================================================================

# Read-only command patterns — safe to execute even in dry_run mode.
# Anchored at start of command (after optional leading whitespace).
_READ_ONLY_RE = re.compile(
    r"""
    ^\s*(
        # Directory listing / navigation
        ls(\s|$) | ll(\s|$) | la(\s|$) | dir(\s|$) | pwd(\s|$) | cd(\s|$)
        # File reading
      | cat(\s|$) | head(\s|$) | tail(\s|$) | more(\s|$) | less(\s|$)
        # Searching
      | grep(\s|$) | rg(\s|$) | ack(\s|$) | ag(\s|$) | find(\s|$) | locate(\s|$)
        # Text processing (read-only modes)
      | wc(\s|$) | sort(\s|$) | uniq(\s|$) | tr(\s|$) | cut(\s|$)
        # Output / formatting
      | echo(\s|$) | printf(\s|$) | diff(\s|$) | cmp(\s|$)
        # File info
      | stat(\s|$) | file(\s|$) | du(\s|$) | df(\s|$)
        # Process / system info
      | ps(\s|$) | top(\s|$) | htop(\s|$) | jobs(\s|$) | uptime(\s|$)
      | date(\s|$) | uname(\s|$) | hostname(\s|$)
        # Identity
      | whoami(\s|$) | id(\s|$) | groups(\s|$)
        # Lookup
      | which(\s|$) | type(\s|$) | command(\s|$)
        # Env inspection (printenv and export without assignment)
      | env(\s|$) | printenv(\s|$)
        # Version queries
      | python3?\s+--version | python3?\s+-V(\s|$)
      | node(\s+--version|\s+-v)(\s|$) | npm\s+--version(\s|$) | pip3?\s+--version(\s|$)
        # Git read commands
      | git\s+(log|status|diff|show|branch|tag|remote\s+-v|config\s+--list)(\s|$)
        # curl in read-only modes (no -O/-o/-T flags)
      | curl\s+(-[a-zA-Z]*[sSkKvV]*\s+)*https?://(?!.*\s(-[a-zA-Z]*[oOT]|--output|--upload-file))
        # Data format querying
      | jq(\s|$) | yq(\s|$)
        # Shell builtins / test constructs
      | test(\s|$) | true(\s|$) | false(\s|$)
        # Help / docs
      | man(\s|$) | info(\s|$) | help(\s|$)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Mutating command patterns — block or simulate in dry_run mode.
_MUTATING_RE = re.compile(
    r"""
    ^\s*(
        # Deletion
        rm(\s|$) | rmdir(\s|$) | shred(\s|$) | wipe(\s|$)
        # Move / copy / link
      | mv(\s|$) | cp(\s|$) | ln(\s|$) | install(\s|$) | rsync(\s|$)
        # File creation / modification
      | touch(\s|$) | mkdir(\s|$) | truncate(\s|$)
      | chmod(\s|$) | chown(\s|$) | chgrp(\s|$)
        # Disk ops
      | dd(\s|$) | mkfs | mkswap(\s|$) | mount(\s|$) | umount(\s|$)
        # Write via tee
      | tee(\s|$)
        # Build systems
      | make(\s|$) | cmake(\s|$) | ninja(\s|$)
        # Package managers
      | pip3?\s+(install|uninstall|download)(\s|$)
      | npm\s+(install|uninstall|build|ci|publish|run)(\s|$)
      | yarn\s+(install|add|remove|build|publish)(\s|$)
      | apt(-get)?\s+(install|remove|purge|autoremove)(\s|$)
      | yum\s+(install|remove|update)(\s|$)
      | dnf\s+(install|remove|update)(\s|$)
      | pacman\s+-[Ssy]
      | brew\s+(install|uninstall|update|upgrade)(\s|$)
      | cargo\s+(build|install|publish)(\s|$)
      | go\s+(build|install|get)(\s|$)
        # Mutating git operations
      | git\s+(clone|commit|push|pull|rebase|reset|clean|rm
               |add|checkout\s+-[Bb]|switch\s+-[Cc]|merge|cherry-pick
               |revert|stash\s+pop|submodule\s+(init|update))(\s|$)
        # Service management
      | systemctl\s+(start|stop|restart|enable|disable|mask)(\s|$)
      | service\s+(start|stop|restart)(\s|$)
        # Firewall / cron
      | crontab(\s|$) | iptables(\s|$) | nftables(\s|$) | ufw(\s|$)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Redirection that implies a write: > or >> (but not <<), or piping to tee
_REDIRECT_WRITE_RE = re.compile(r'(?<![<>])>{1,2}|[|]\s*tee\b')


def classify_shell_command(command: str) -> Tuple[str, str]:
    """
    Classify a shell command's write risk for dry_run enforcement.

    Returns:
        (classification, reason) where classification is one of:
        - "read_only"  — safe to execute even in dry_run mode
        - "mutating"   — should be blocked in dry_run mode
        - "uncertain"  — unknown; treated conservatively as mutating

    Note: This is heuristic pattern matching, not a parser.  A determined
    agent can bypass it (e.g. via aliases, here-docs, eval, shell functions).
    It catches the common cases and errs on the side of caution for unknowns.
    """
    stripped = command.strip()

    # Output redirection is almost always a write
    if _REDIRECT_WRITE_RE.search(stripped):
        return "mutating", "output redirection or pipe-to-tee detected"

    if _READ_ONLY_RE.match(stripped):
        return "read_only", "matched known read-only command pattern"

    if _MUTATING_RE.match(stripped):
        return "mutating", "matched known mutating command pattern"

    return "uncertain", "unknown command — treating conservatively as mutating"


# =============================================================================
# RunWorkspace
# =============================================================================

@dataclass
class RunWorkspace:
    """
    Represents one per-run ephemeral workspace.

    The workspace directory is created under the configured base_dir and is
    the only location where writes are permitted when policy=workspace_only.
    """
    workspace_id: str
    run_id: Optional[str]
    session_id: Optional[str]
    path: Path
    policy: FilesystemPolicy
    write_policy: WritePolicy
    created_at: float
    expires_at: Optional[float]  # None = no TTL
    status: str = "active"       # active | expired | cleaned
    repo_roots: List[Path] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        if self.status != "active":
            return True
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def check_path_allowed(self, path: str, operation: str = "write") -> Tuple[bool, str]:
        """
        Check whether a filesystem operation on path is permitted by this workspace.

        This is the primary enforcement gate for file tools.  It must be called
        BEFORE any write is dispatched to the underlying file system.

        Path resolution strategy (defense against symlink/traversal escapes):
        - os.path.realpath() is called on the input path.  This expands all
          symlink components and collapses any .. sequences, yielding the
          actual on-disk path the OS would access.
        - The resolved real path is what we compare against allowed roots.
        - This means a path like /workspace/../../etc/passwd resolves to
          /etc/passwd and is caught correctly even before the OS is consulted.
        - A symlink inside the workspace that points outside is also caught
          at access time (the resolved real path will be outside the allowed
          root), though the symlink itself may already exist.

        Args:
            path: Path the tool wants to access (may be relative, contain
                  symlinks, or .. components)
            operation: "read" or "write"

        Returns:
            (allowed: bool, reason: str)
        """
        # --- Step 1: enforce write_policy (applies regardless of path) ------
        if operation == "write":
            if self.write_policy == WritePolicy.READ_ONLY:
                return False, "write denied: write_policy=read_only"
            if self.write_policy == WritePolicy.DRY_RUN:
                return False, "write denied: write_policy=dry_run (no writes executed in this mode)"

        # --- Step 2: resolve path to real absolute form ---------------------
        # os.path.realpath follows symlinks and collapses .. — this is the
        # key defence against path traversal and symlink-based escape attacks.
        # We cannot trust the caller's string; we trust only the resolved path.
        try:
            real_path = Path(os.path.realpath(os.path.abspath(path)))
        except (OSError, ValueError) as e:
            return False, f"path resolution failed: {e}"

        # --- Step 3: apply filesystem policy --------------------------------
        if self.policy == FilesystemPolicy.FULL_ACCESS:
            # No path restrictions (subject to write_policy check above)
            return True, "allowed: policy=full_access"

        if self.policy == FilesystemPolicy.READ_ONLY:
            if operation == "write":
                return False, "write denied: filesystem_policy=read_only"
            return True, "allowed: read access under read_only policy"

        if self.policy == FilesystemPolicy.WORKSPACE_ONLY:
            if operation == "read":
                # Reads are unrestricted — blocking reads breaks most workflows
                return True, "allowed: reads unrestricted under workspace_only"
            # Writes must resolve to within the workspace directory
            try:
                real_path.relative_to(self.path.resolve())
                return True, f"allowed: path within workspace {self.path}"
            except ValueError:
                return False, (
                    f"write denied: {real_path} is outside workspace "
                    f"{self.path} (policy=workspace_only). "
                    "Write to a path inside the workspace, or change policy."
                )

        if self.policy == FilesystemPolicy.REPO_SCOPED:
            if not self.repo_roots:
                return False, (
                    "write denied: policy=repo_scoped but workspace.repo_roots "
                    "is empty. Configure allowed roots in config.yaml."
                )
            if operation == "read":
                return True, "allowed: reads unrestricted under repo_scoped"
            # Writes must resolve to within one of the configured repo roots
            for root in self.repo_roots:
                try:
                    real_root = Path(os.path.realpath(str(root)))
                    real_path.relative_to(real_root)
                    return True, f"allowed: path within repo root {real_root}"
                except ValueError:
                    continue
            roots_display = ", ".join(str(r) for r in self.repo_roots)
            return False, (
                f"write denied: {real_path} is outside all configured "
                f"repo_roots ({roots_display}) (policy=repo_scoped)"
            )

        # Unknown policy value — fail open but log loudly
        logger.error("Unknown filesystem policy %r — defaulting to allow. "
                     "This is a bug; please report it.", self.policy)
        return True, f"allowed: unknown policy {self.policy!r}, defaulting permissive (bug)"

    def get_terminal_cwd_prefix(self) -> str:
        """
        Return a shell command prefix that scopes the terminal to the workspace.

        Prepended to commands when policy=workspace_only so that the default
        working directory for every shell invocation is inside the workspace.

        LIMITATION: This sets the starting CWD but does not prevent the agent
        from using 'cd' or absolute paths to escape.  True shell confinement
        requires OS-level mechanisms (chroot, Linux namespaces, seccomp).
        """
        if self.policy == FilesystemPolicy.WORKSPACE_ONLY:
            # Single-quote the path and escape any embedded single-quotes
            safe_path = str(self.path).replace("'", "'\"'\"'")
            return f"cd '{safe_path}' && "
        return ""

    def check_terminal_command(self, command: str) -> Tuple[bool, str]:
        """
        Check whether a terminal command should be allowed under current policies.

        For dry_run write_policy: block mutating commands, allow read-only ones.
        For workspace_only filesystem_policy: warn on obvious cd-escapes.

        Returns:
            (allowed: bool, reason: str)
        """
        # --- dry_run enforcement for exec -----------------------------------
        if self.write_policy == WritePolicy.DRY_RUN:
            classification, detail = classify_shell_command(command)
            if classification == "read_only":
                return True, f"dry_run: allowed read-only command ({detail})"
            # mutating or uncertain — block
            return False, (
                f"dry_run: command blocked (classified as {classification!r}: {detail}). "
                "In dry_run mode only clearly read-only commands are executed. "
                "Change write_policy to full_write to run this command."
            )

        # --- read_only filesystem_policy blocks all exec writes -------------
        if self.write_policy == WritePolicy.READ_ONLY:
            return False, "exec denied: write_policy=read_only"

        # --- workspace_only: warn on obvious cd escapes (heuristic) --------
        if self.policy == FilesystemPolicy.WORKSPACE_ONLY:
            workspace_str = str(self.path)
            # Detect `cd /absolute/path` where path is NOT under the workspace
            cd_escape = re.search(
                r'(?:^|[;&|])\s*cd\s+([^\s;&|]+)',
                command,
            )
            if cd_escape:
                target = cd_escape.group(1).strip("'\"")
                if os.path.isabs(target):
                    try:
                        Path(target).relative_to(self.path)
                    except ValueError:
                        # Log warning but do not block — shell enforcement is
                        # heuristic only.  Blocking would break legitimate uses
                        # like `cd /tmp && ls` in read-only contexts.
                        logger.warning(
                            "workspace_only: cd to %r may escape workspace %s. "
                            "Python-level enforcement cannot prevent shell escapes. "
                            "Use a container backend for strong isolation.",
                            target, workspace_str,
                        )

        return True, "allowed"


# =============================================================================
# WorkspaceManager
# =============================================================================

class WorkspaceManager:
    """
    Manages per-run ephemeral workspaces.

    Workspaces are created on demand (lazily, keyed by task_id), tracked
    in memory, optionally persisted to the state DB, and cleaned up by TTL
    or explicit call.

    This is a process-wide singleton accessed via get_workspace_manager().
    """

    def __init__(self, base_dir: Path, default_ttl_hours: float = 24.0):
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._default_ttl_hours = default_ttl_hours
        self._workspaces: Dict[str, RunWorkspace] = {}    # workspace_id → ws
        self._task_map: Dict[str, str] = {}               # task_id → workspace_id
        self._lock = threading.Lock()
        self._db = None  # SessionDB injected lazily
        self._cleanup_done_at_startup = False

    # -------------------------------------------------------------------------
    # DB injection
    # -------------------------------------------------------------------------

    def inject_db(self, db) -> None:
        """Inject a SessionDB for workspace persistence (optional, best-effort)."""
        self._db = db

    # -------------------------------------------------------------------------
    # Workspace lifecycle
    # -------------------------------------------------------------------------

    def get_or_create_for_task(
        self,
        task_id: str,
        policy: Optional[FilesystemPolicy] = None,
        write_policy: Optional[WritePolicy] = None,
        ttl_hours: Optional[float] = None,
        repo_roots: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> RunWorkspace:
        """
        Return the workspace for task_id, creating one if needed.

        Policy arguments are only used on first creation; subsequent calls
        return the existing workspace unchanged.
        """
        with self._lock:
            if task_id in self._task_map:
                ws_id = self._task_map[task_id]
                ws = self._workspaces.get(ws_id)
                if ws is not None and ws.status == "active":
                    return ws
                # Workspace was cleaned up — create a new one
                del self._task_map[task_id]

        # Load policy from config if not provided
        if policy is None or write_policy is None:
            cfg_policy, cfg_write, cfg_ttl, cfg_roots = load_workspace_policy_from_config()
            policy = policy or cfg_policy
            write_policy = write_policy or cfg_write
            if ttl_hours is None:
                ttl_hours = cfg_ttl
            if repo_roots is None:
                repo_roots = cfg_roots

        ws = self._create_workspace(
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            policy=policy,
            write_policy=write_policy,
            ttl_hours=ttl_hours,
            repo_roots=repo_roots or [],
        )
        return ws

    def _create_workspace(
        self,
        task_id: str,
        run_id: Optional[str],
        session_id: Optional[str],
        policy: FilesystemPolicy,
        write_policy: WritePolicy,
        ttl_hours: float,
        repo_roots: List[str],
    ) -> RunWorkspace:
        workspace_id = str(uuid.uuid4())
        workspace_path = self._base_dir / workspace_id

        # Only create a directory for workspace_only policy.
        # For other policies the directory is unused.
        if policy == FilesystemPolicy.WORKSPACE_ONLY:
            workspace_path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(workspace_path, 0o700)
            except OSError:
                pass
        else:
            # Use a placeholder path (never created on disk)
            workspace_path = self._base_dir / workspace_id

        expires_at = (time.time() + ttl_hours * 3600) if ttl_hours > 0 else None

        resolved_roots: List[Path] = []
        for r in repo_roots:
            p = Path(r).expanduser()
            try:
                resolved_roots.append(Path(os.path.realpath(str(p))))
            except OSError:
                resolved_roots.append(p)

        ws = RunWorkspace(
            workspace_id=workspace_id,
            run_id=run_id,
            session_id=session_id,
            path=workspace_path,
            policy=policy,
            write_policy=write_policy,
            created_at=time.time(),
            expires_at=expires_at,
            status="active",
            repo_roots=resolved_roots,
        )

        with self._lock:
            self._workspaces[workspace_id] = ws
            self._task_map[task_id] = workspace_id

        self._persist_workspace(ws)

        logger.info(
            "Workspace created: id=%s task=%s policy=%s write=%s ttl=%.1fh",
            workspace_id[:8], task_id[:8] if len(task_id) >= 8 else task_id,
            policy.value, write_policy.value, ttl_hours,
        )
        return ws

    def get_for_task(self, task_id: str) -> Optional[RunWorkspace]:
        """Return the active workspace for task_id, or None."""
        with self._lock:
            ws_id = self._task_map.get(task_id)
            if ws_id is None:
                return None
            ws = self._workspaces.get(ws_id)
            if ws is not None and ws.status == "active":
                return ws
        return None

    def cleanup_workspace(self, workspace_id: str) -> bool:
        """Mark workspace cleaned and remove its directory. Returns True if found."""
        with self._lock:
            ws = self._workspaces.get(workspace_id)
            if ws is None:
                return False
            ws.status = "cleaned"
            # Remove reverse task mapping
            for tid, wid in list(self._task_map.items()):
                if wid == workspace_id:
                    del self._task_map[tid]

        _remove_workspace_dir(ws.path)
        self._update_workspace_status(workspace_id, "cleaned")
        return True

    def cleanup_expired(self, remove_files: bool = True) -> int:
        """
        Expire and remove all workspaces past their TTL.

        Safe to call at platform startup and periodically.  Skips workspaces
        that are actively in use (status != 'active') or have no TTL.

        Returns the count of workspaces cleaned.
        """
        now = time.time()
        to_clean: List[RunWorkspace] = []

        with self._lock:
            for ws in list(self._workspaces.values()):
                if (ws.status == "active"
                        and ws.expires_at is not None
                        and now > ws.expires_at):
                    ws.status = "expired"
                    to_clean.append(ws)
            # Remove from task map for expired workspaces
            for ws in to_clean:
                for tid, wid in list(self._task_map.items()):
                    if wid == ws.workspace_id:
                        del self._task_map[tid]

        for ws in to_clean:
            logger.info(
                "Cleaning expired workspace %s (expired %s ago)",
                ws.workspace_id[:8],
                _age_str(now - ws.expires_at),
            )
            if remove_files:
                _remove_workspace_dir(ws.path)
            self._update_workspace_status(ws.workspace_id, "cleaned")

        if to_clean:
            logger.info("cleanup_expired: removed %d workspace(s)", len(to_clean))

        # Also sweep orphaned on-disk directories from prior process crashes
        if remove_files:
            self._sweep_orphaned_dirs()

        return len(to_clean)

    def cleanup_expired_once_at_startup(self) -> int:
        """Call cleanup_expired() once per process lifetime (idempotent)."""
        if self._cleanup_done_at_startup:
            return 0
        self._cleanup_done_at_startup = True
        return self.cleanup_expired()

    def _sweep_orphaned_dirs(self) -> None:
        """Remove UUID-named workspace dirs on disk with no in-memory record."""
        if not self._base_dir.exists():
            return
        with self._lock:
            known_paths = {str(ws.path) for ws in self._workspaces.values()}
        try:
            for child in self._base_dir.iterdir():
                if not child.is_dir():
                    continue
                # Only touch directories that look like UUIDs we created
                if len(child.name) == 36 and child.name.count("-") == 4:
                    if str(child) not in known_paths:
                        try:
                            shutil.rmtree(child)
                            logger.info("Removed orphaned workspace dir: %s", child.name[:8])
                        except OSError as e:
                            logger.debug("Could not remove orphaned dir %s: %s", child, e)
        except OSError as e:
            logger.debug("Error sweeping workspace base dir: %s", e)

    # -------------------------------------------------------------------------
    # DB persistence (all best-effort — failures never affect the agent)
    # -------------------------------------------------------------------------

    def _persist_workspace(self, ws: RunWorkspace) -> None:
        if self._db is None:
            return
        try:
            self._db.create_workspace(
                workspace_id=ws.workspace_id,
                run_id=ws.run_id,
                session_id=ws.session_id,
                path=str(ws.path),
                policy=ws.policy.value,
                write_policy=ws.write_policy.value,
                created_at=ws.created_at,
                expires_at=ws.expires_at,
                repo_roots=json.dumps([str(r) for r in ws.repo_roots]),
            )
        except Exception as e:
            logger.debug("Failed to persist workspace to DB (non-fatal): %s", e)

    def _update_workspace_status(self, workspace_id: str, status: str) -> None:
        if self._db is None:
            return
        try:
            self._db.update_workspace_status(workspace_id, status)
        except Exception as e:
            logger.debug("Failed to update workspace status in DB (non-fatal): %s", e)


# =============================================================================
# Helpers
# =============================================================================

def _remove_workspace_dir(path: Path) -> None:
    if path.exists():
        try:
            shutil.rmtree(path)
            logger.info("Workspace directory removed: %s", path.name[:8] if len(path.name) >= 8 else path.name)
        except OSError as e:
            logger.warning("Failed to remove workspace directory %s: %s", path, e)


def _age_str(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


# =============================================================================
# Module-level singleton
# =============================================================================

_manager: Optional[WorkspaceManager] = None
_manager_lock = threading.Lock()


def get_workspace_manager() -> WorkspaceManager:
    """
    Return the process-wide WorkspaceManager singleton.

    On first call, reads base_dir and TTL from config or environment.
    Subsequent calls return the same instance.
    """
    global _manager
    if _manager is not None:
        return _manager
    with _manager_lock:
        if _manager is None:
            base_dir, ttl_hours = _resolve_manager_config()
            _manager = WorkspaceManager(base_dir=base_dir, default_ttl_hours=ttl_hours)
    return _manager


def _resolve_manager_config() -> Tuple[Path, float]:
    """Determine workspace base dir and default TTL from config / env."""
    # Environment override (useful for tests and Kubernetes ConfigMaps)
    env_dir = os.getenv("HERMES_WORKSPACE_DIR")
    env_ttl = os.getenv("HERMES_WORKSPACE_TTL_HOURS")

    base_dir: Optional[Path] = Path(env_dir).expanduser() if env_dir else None
    ttl_hours: Optional[float] = float(env_ttl) if env_ttl else None

    if base_dir is None or ttl_hours is None:
        try:
            from logos_cli.config import load_config
            cfg = load_config()
            ws_cfg = cfg.get("workspace", {})
            if base_dir is None:
                raw_dir = ws_cfg.get("workspace_base_dir", "~/.hermes/workspaces")
                base_dir = Path(raw_dir).expanduser()
            if ttl_hours is None:
                ttl_hours = float(ws_cfg.get("workspace_ttl_hours", 24.0))
        except Exception:
            pass

    if base_dir is None:
        hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
        base_dir = hermes_home / "workspaces"
    if ttl_hours is None:
        ttl_hours = 24.0

    return base_dir, ttl_hours


def load_workspace_policy_from_config() -> Tuple[FilesystemPolicy, WritePolicy, float, List[str]]:
    """
    Load workspace policy settings from config.yaml.

    Returns:
        (filesystem_policy, write_policy, ttl_hours, repo_roots)
    """
    defaults = (FilesystemPolicy.FULL_ACCESS, WritePolicy.FULL_WRITE, 24.0, [])
    try:
        from logos_cli.config import load_config
        cfg = load_config()
        ws_cfg = cfg.get("workspace", {})

        fs_policy = FilesystemPolicy(ws_cfg.get("filesystem_policy", "full_access"))
        wr_policy = WritePolicy(ws_cfg.get("write_policy", "full_write"))
        ttl = float(ws_cfg.get("workspace_ttl_hours", 24.0))
        roots = list(ws_cfg.get("repo_roots", []) or [])
        return fs_policy, wr_policy, ttl, roots
    except Exception as e:
        logger.debug("Failed to load workspace policy from config (non-fatal): %s", e)
        return defaults


def check_write_allowed(path: str, task_id: str) -> Tuple[bool, str]:
    """
    Convenience function for file tools: check if a write to path is allowed.

    Returns (allowed, reason).  If no workspace exists for the task_id,
    the policy is full_access (no restriction), preserving default behaviour.
    """
    mgr = get_workspace_manager()
    ws = mgr.get_for_task(task_id)
    if ws is None:
        # No workspace configured for this task — check global policy
        policy, write_pol, _, _ = load_workspace_policy_from_config()
        if policy == FilesystemPolicy.FULL_ACCESS and write_pol == WritePolicy.FULL_WRITE:
            return True, "allowed: no workspace configured, policy=full_access"
        # Policy is non-default but workspace not yet created — create lazily
        ws = mgr.get_or_create_for_task(task_id)
    return ws.check_path_allowed(path, operation="write")
