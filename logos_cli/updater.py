"""
Gateway self-updater — shared between the CLI (`logos gateway update`)
and the HTTP endpoint that the UI banner calls.

Both entry points call the same ``check_for_update`` / ``apply_update``
functions so the logic and safety rails stay in one place. The CLI runs
in the user's terminal (out-of-process from the gateway); the HTTP
endpoint runs inside the gateway's aiohttp worker. The relaunch path
differs between the two:

  - CLI: invoke the project's existing `logos gateway restart` flow
    (systemd/launchd-aware, falls back to manual respawn).
  - HTTP: post-response, the gateway process ``os.execv``s itself with
    the same argv/cwd so the running aiohttp worker picks up the new
    code without relying on an external supervisor.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _run_git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    """Run git with a short timeout and captured output. Never raises —
    the caller branches on ``returncode``."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def resolve_repo_dir() -> Path:
    """Find the repo we should operate on.

    Preference order:
      1. ``LOGOS_REPO_DIR`` env var if set — lets CI / test rigs point
         the updater at an isolated checkout.
      2. The directory containing the running Python module
         (PROJECT_ROOT / ``logos_cli/updater.py``'s parent's parent).
         That's the checkout the current process was launched from;
         updating anywhere else would be a no-op for this process.
    """
    override = os.environ.get("LOGOS_REPO_DIR")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent


def check_for_update(repo_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Fetch origin and describe how far behind the local HEAD is.

    Returns ``{ok, current_sha, current_message, latest_sha,
    latest_message, behind_by, has_update, branch, error}``.
    ``has_update`` is True when there's at least one new commit on the
    tracked upstream branch. ``ok=False`` when we couldn't resolve a
    tracked upstream or git failed — the caller should surface the
    ``error`` to the user rather than pretending everything's fine.
    """
    repo = repo_dir or resolve_repo_dir()
    out: Dict[str, Any] = {
        "ok": False,
        "repo_dir": str(repo),
        "current_sha": None,
        "current_message": None,
        "latest_sha": None,
        "latest_message": None,
        "behind_by": 0,
        "has_update": False,
        "branch": None,
        "error": None,
    }

    if not (repo / ".git").exists():
        out["error"] = f"not a git repo: {repo}"
        return out

    try:
        branch_res = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
        if branch_res.returncode != 0:
            out["error"] = branch_res.stderr.strip() or "could not read branch"
            return out
        branch = branch_res.stdout.strip()
        out["branch"] = branch

        # Fetch. We intentionally do NOT prune or write to the working
        # tree here — this is a read-only check.
        fetch_res = _run_git(["fetch", "--quiet", "origin"], repo)
        if fetch_res.returncode != 0:
            out["error"] = fetch_res.stderr.strip() or "git fetch failed"
            return out

        cur_res = _run_git(["log", "-1", "--format=%H%n%s", "HEAD"], repo)
        lat_res = _run_git(["log", "-1", "--format=%H%n%s", f"origin/{branch}"], repo)
        if cur_res.returncode != 0 or lat_res.returncode != 0:
            out["error"] = (cur_res.stderr + lat_res.stderr).strip() or "git log failed"
            return out

        cur_lines = cur_res.stdout.strip().split("\n", 1)
        lat_lines = lat_res.stdout.strip().split("\n", 1)
        out["current_sha"] = cur_lines[0] if cur_lines else None
        out["current_message"] = cur_lines[1] if len(cur_lines) > 1 else ""
        out["latest_sha"] = lat_lines[0] if lat_lines else None
        out["latest_message"] = lat_lines[1] if len(lat_lines) > 1 else ""

        count_res = _run_git(
            ["rev-list", "--count", f"HEAD..origin/{branch}"], repo,
        )
        if count_res.returncode == 0:
            try:
                out["behind_by"] = int(count_res.stdout.strip())
            except ValueError:
                out["behind_by"] = 0

        out["has_update"] = (
            out["current_sha"] != out["latest_sha"] and out["behind_by"] > 0
        )
        out["ok"] = True
    except subprocess.TimeoutExpired:
        out["error"] = "git command timed out"
    except Exception as exc:
        logger.exception("check_for_update failed")
        out["error"] = str(exc)
    return out


def apply_update(
    repo_dir: Optional[Path] = None,
    *,
    restart: bool = True,
) -> Dict[str, Any]:
    """Pull origin/<branch> (ff-only) then optionally respawn.

    Fast-forward only on purpose: merge commits or diverged history
    should require a human touch, not an unattended "git pull --no-edit"
    that could produce a bad merge and cripple the running gateway.

    Returns ``{ok, applied, new_sha, error}``. When ``restart=True``
    and applied=True, the function DOES NOT RETURN — it replaces the
    current process via ``os.execv`` (preserves PID, closes the HTTP
    socket cleanly on the way out). The caller should therefore flush
    any final response BEFORE invoking this with restart=True.
    """
    repo = repo_dir or resolve_repo_dir()
    res: Dict[str, Any] = {
        "ok": False,
        "applied": False,
        "new_sha": None,
        "error": None,
        "repo_dir": str(repo),
    }

    if not (repo / ".git").exists():
        res["error"] = f"not a git repo: {repo}"
        return res

    try:
        # Refuse to clobber dirty working trees — local edits on the
        # deploy checkout are almost always unintended, and silently
        # blowing them away with a reset would be worse than erroring.
        status = _run_git(["status", "--porcelain"], repo)
        if status.returncode != 0:
            res["error"] = status.stderr.strip() or "git status failed"
            return res
        if status.stdout.strip():
            res["error"] = "working tree not clean; refusing to update"
            return res

        # Fast-forward-only pull. If we can't fast-forward, the branches
        # have diverged and a human should look at it.
        pull = _run_git(["pull", "--ff-only", "origin", "HEAD"], repo)
        if pull.returncode != 0:
            res["error"] = (pull.stderr or pull.stdout).strip() or "git pull failed"
            return res

        head = _run_git(["rev-parse", "HEAD"], repo)
        if head.returncode == 0:
            res["new_sha"] = head.stdout.strip()
        res["applied"] = True
        res["ok"] = True
    except subprocess.TimeoutExpired:
        res["error"] = "git command timed out"
        return res
    except Exception as exc:
        logger.exception("apply_update failed")
        res["error"] = str(exc)
        return res

    if restart and res["applied"]:
        _self_exec()
        # os.execv doesn't return; flag it for callers reading the
        # dict if they somehow get here.
        res["restarted"] = True
    return res


def _self_exec() -> None:
    """Re-exec the current process with the same argv/cwd.

    Preserves PID and any OS-level supervisor relationship (systemd
    will not re-spawn because the process technically doesn't exit).
    Logs just before the hand-off because anything after ``os.execv``
    never runs.
    """
    logger.info("apply_update: re-execing %s with argv=%s", sys.executable, sys.argv)
    # Flush stdio so log lines actually appear in tail -f.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)
