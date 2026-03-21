"""
Ephemeral task workspace lifecycle management.

Each agent run gets an isolated directory at:
  ~/.hermes/workspaces/<run_id>/

The workspace path is:
  - Registered in a per-session in-memory map for policy enforcement
  - Injected into the agent's ephemeral system prompt
  - Stored in the run record
  - Cleaned up by TTL (default 24h) via cleanup_expired()

Usage:
  ws = create_workspace(run_id, session_id)
  # ... run ...
  release_workspace(run_id, session_id)
  # Background: cleanup_expired() removes old dirs

TRUST BOUNDARY
--------------
Workspace containment is enforced at the Python level by resolving symlinks
before comparing paths (os.path.realpath).  This defeats most naive symlink
escape attempts.

Known remaining limitations:
  1. TOCTOU: a symlink could be created *between* the check and the actual
     write.  Eliminating that race requires kernel-level sandboxing (seccomp,
     namespaces, or a container per run).
  2. The terminal/shell backend is NOT workspace-scoped by default; a shell
     command can still `cd` anywhere.  terminal_tool injects workdir=workspace
     for workspace_only policies, but persistent shell state is not enforced.
  3. MCP operator tools (files_write, patch_*) bypass this module entirely
     and are enforced by check_filesystem_path() in auth/policy.py.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# session_id → workspace Path (current active run for that session)
_session_workspaces: dict[str, Path] = {}
# run_id → workspace Path
_run_workspaces: dict[str, Path] = {}

_HERMES_HOME = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
_WORKSPACES_ROOT = _HERMES_HOME / "workspaces"

# TTL is configurable via HERMES_WORKSPACE_TTL_HOURS env var (default 24h).
WORKSPACE_TTL_HOURS: float = float(os.getenv("HERMES_WORKSPACE_TTL_HOURS", "24"))


def create_workspace(run_id: str, session_id: str) -> Path:
    """Create an ephemeral workspace directory for a run.

    Returns the workspace Path (already exists on disk).
    """
    _WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    workspace = _WORKSPACES_ROOT / run_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Write metadata so cleanup_expired() can age-check without stat()
    try:
        (workspace / ".ws_meta").write_text(
            f"run_id={run_id}\nsession_id={session_id}\ncreated_at={int(time.time())}\n"
        )
    except OSError as exc:
        logger.warning("Could not write workspace metadata: %s", exc)

    with _lock:
        _session_workspaces[session_id] = workspace
        _run_workspaces[run_id] = workspace

    logger.debug("Workspace created: %s (session=%s)", workspace, session_id)
    return workspace


def get_workspace_for_session(session_id: str) -> Optional[Path]:
    """Return the active workspace path for a session, or None."""
    with _lock:
        return _session_workspaces.get(session_id)


def get_workspace_for_run(run_id: str) -> Optional[Path]:
    """Return the workspace path for a run_id, or None."""
    with _lock:
        return _run_workspaces.get(run_id)


def release_workspace(run_id: str, session_id: str) -> None:
    """Remove from in-memory maps.

    Does NOT delete the directory — TTL-based cleanup handles that so
    the workspace is still inspectable after run completion.
    """
    with _lock:
        _session_workspaces.pop(session_id, None)
        _run_workspaces.pop(run_id, None)
    logger.debug("Workspace released from maps: run=%s session=%s", run_id, session_id)


def delete_workspace(run_id: str) -> bool:
    """Immediately delete the workspace directory for a run. Returns True if deleted."""
    with _lock:
        ws = _run_workspaces.pop(run_id, None)
    if ws is None:
        # Try to find it on disk
        candidate = _WORKSPACES_ROOT / run_id
        if candidate.exists():
            ws = candidate
        else:
            return False
    try:
        shutil.rmtree(ws, ignore_errors=True)
        logger.debug("Workspace deleted: %s", ws)
        return True
    except Exception as exc:
        logger.warning("Failed to delete workspace %s: %s", ws, exc)
        return False


def cleanup_expired(max_age_hours: float = WORKSPACE_TTL_HOURS) -> int:
    """Delete workspace directories older than max_age_hours.

    Returns the number of directories removed.
    Safe to call from a background thread.
    """
    if not _WORKSPACES_ROOT.exists():
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0

    for entry in _WORKSPACES_ROOT.iterdir():
        if not entry.is_dir():
            continue
        try:
            meta_file = entry / ".ws_meta"
            if meta_file.exists():
                meta = {}
                for line in meta_file.read_text().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        meta[k.strip()] = v.strip()
                created_at = float(meta.get("created_at", 0))
            else:
                # Fall back to directory mtime
                created_at = entry.stat().st_mtime

            if created_at and created_at < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                logger.debug("Expired workspace removed: %s", entry)
        except Exception as exc:
            logger.warning("Error checking workspace %s for expiry: %s", entry, exc)

    return removed


def _safe_resolve(path: str) -> Optional[str]:
    """Resolve a path to its realpath, or return None if resolution is unsafe.

    Rejects paths containing null bytes (a common bypass technique where the
    OS truncates at \\x00 but a naive string check would see the full path).
    Returns None on any OS-level error so callers can fail closed.
    """
    if "\x00" in path:
        logger.warning("Path contains null byte — rejected: %r", path[:80])
        return None
    try:
        return os.path.realpath(os.path.expanduser(path))
    except Exception as exc:
        logger.debug("Path resolution failed for %r: %s", path[:80], exc)
        return None


def is_within_workspace(path: str, workspace: Path) -> bool:
    """Return True if path resolves to inside workspace.

    Fails *closed*: returns False if path resolution fails or path contains
    suspicious characters.  Resolving symlinks before comparing defeats most
    symlink-based escape attempts.

    KNOWN LIMITATION — TOCTOU: a symlink could be swapped between this check
    and the actual filesystem write.  True prevention requires OS-level
    sandboxing; Python-only checks cannot close this race.
    """
    resolved_path = _safe_resolve(path)
    if resolved_path is None:
        return False
    resolved_ws = _safe_resolve(str(workspace))
    if resolved_ws is None:
        return False
    sep = os.sep
    return resolved_path == resolved_ws or resolved_path.startswith(resolved_ws + sep)


def workspace_context_prompt(workspace: Path) -> str:
    """Return a short system-prompt snippet describing the workspace."""
    return (
        f"\n\n## Task Workspace\n\n"
        f"Your isolated task workspace for this run is: `{workspace}`\n\n"
        f"- Use this directory for all temporary files, code edits, and outputs.\n"
        f"- Files written here persist until the workspace expires (24h).\n"
        f"- Do **not** write to paths outside this workspace unless explicitly permitted.\n"
    )
