"""
Dry-run simulation for write/mutation tool calls.

When write_policy=dry_run, the agent loop calls simulate_tool() instead
of blocking with a generic policy_blocked error.  The simulation result
describes WHAT WOULD HAPPEN without making any actual changes.

This gives the model (and the human reviewing run records) a clear picture
of the planned mutations, enabling informed approval decisions.

Exec command classification
---------------------------
For terminal/shell tools, dry_run behaviour depends on command classification:

  read_only   — command is on the conservative safe-read allowlist.  Under
                write_policy=dry_run these are allowed to *actually execute*
                (they do not mutate state).  check_policy_for_tool() in
                approval.py gates this decision.

  mutating    — command is known to mutate state.  Blocked with a simulation
                response describing what would have happened.

  ambiguous   — command cannot be confidently classified.  Blocked
                conservatively.

Logging: every exec classification decision is logged at DEBUG level so
operators can review dry_run policy behaviour in the gateway logs.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exec command classification
# ---------------------------------------------------------------------------

# Commands whose base names are safe read-only operations.
# This is a conservative *allowlist* — unknown commands default to 'mutating'.
_EXEC_READ_ONLY: frozenset = frozenset({
    # Filesystem inspection
    "ls", "ll", "la", "lsblk", "lscpu", "lsusb", "lspci",
    "cat", "less", "more", "head", "tail", "wc",
    "file", "stat", "du", "df",
    # Text search / processing (without output redirection, see classify logic)
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "diff", "cmp", "comm",
    "sort", "uniq", "cut", "tr",
    "jq", "yq", "xmllint",
    # Path / environment
    "pwd", "echo", "printf", "date", "uptime", "uname", "hostname",
    "whoami", "id", "groups",
    "env", "printenv",
    "which", "type", "whereis", "whatis",
    # Process inspection
    "ps", "pgrep", "top", "htop", "pstree",
    "free", "vmstat",
    # Network inspection (read-only intent; nmap excluded — can be aggressive)
    "netstat", "ss", "ip",
    # Checksum / hash
    "md5sum", "sha256sum", "sha512sum", "cksum",
})

# git subcommands that are read-only
_GIT_READ_ONLY: frozenset = frozenset({
    "log", "diff", "status", "show", "branch", "tag", "remote",
    "stash", "blame", "shortlog", "describe", "reflog",
    "ls-files", "ls-tree", "rev-parse", "rev-list", "cat-file",
})

# kubectl subcommands that are read-only
_KUBECTL_READ_ONLY: frozenset = frozenset({
    "get", "describe", "logs", "explain",
    "api-resources", "api-versions", "version", "cluster-info", "top",
})

# docker subcommands that are read-only
_DOCKER_READ_ONLY: frozenset = frozenset({
    "ps", "images", "logs", "inspect", "stats", "info", "version",
})

# helm subcommands that are read-only
_HELM_READ_ONLY: frozenset = frozenset({
    "list", "ls", "status", "get", "history", "version",
})


def _extract_command_name(command: str) -> str:
    """Return the base executable name from a shell command string (lowercase)."""
    try:
        tokens = shlex.split(command.strip())
    except ValueError:
        # shlex fails on unmatched quotes — fall back to whitespace split
        tokens = command.strip().split()
    if not tokens:
        return ""
    # Skip leading env var assignments like FOO=bar CMD
    for token in tokens:
        if not token.startswith("-") and "=" not in token:
            return os.path.basename(token).lower()
    return os.path.basename(tokens[0]).lower()


def classify_exec_command(command: str) -> tuple[str, str]:
    """Classify a shell command as 'read_only', 'mutating', or 'ambiguous'.

    Returns (classification, reason_string).

    Conservative by design: unknown commands default to 'mutating'.
    The classification is used only to decide whether to allow execution
    under write_policy=dry_run; it is logged but does not override other
    safety gates (dangerous-command detection, exec_policy, etc.).
    """
    cmd = command.strip()
    if not cmd:
        return "ambiguous", "empty command"

    cmd_name = _extract_command_name(cmd)
    if not cmd_name:
        return "ambiguous", "could not determine command name"

    # ── find: read-only unless -exec/-delete present ───────────────────────
    if cmd_name == "find":
        lower = cmd.lower()
        if any(flag in lower for flag in ("-exec ", "-execdir ", "-delete")):
            return "mutating", "find with -exec/-delete/-execdir can modify files"
        return "read_only", "find (no -exec/-delete)"

    # ── sed: -i flag modifies files in-place ──────────────────────────────
    if cmd_name == "sed":
        tokens = cmd.split()
        for t in tokens:
            if t == "-i" or t.startswith("-i'") or t.startswith('-i"') or t == "--in-place":
                return "mutating", "sed -i modifies files in-place"
        return "read_only", "sed (no -i flag, read-only stream editing)"

    # ── awk: output redirection means file write ───────────────────────────
    if cmd_name == "awk":
        if ">" in cmd or ">>" in cmd:
            return "ambiguous", "awk with redirection may write files"
        return "read_only", "awk (no output redirection)"

    # ── git: check subcommand ─────────────────────────────────────────────
    if cmd_name == "git":
        parts = cmd.split()
        if len(parts) >= 2:
            sub = parts[1].lstrip("-")
            if sub in _GIT_READ_ONLY:
                return "read_only", f"git {sub} is read-only"
        return "mutating", "git mutation subcommand (commit, push, merge, reset, …)"

    # ── kubectl: check subcommand ─────────────────────────────────────────
    if cmd_name == "kubectl":
        parts = cmd.split()
        if len(parts) >= 2 and parts[1] in _KUBECTL_READ_ONLY:
            return "read_only", f"kubectl {parts[1]} is read-only"
        return "mutating", "kubectl mutation subcommand (apply, delete, patch, …)"

    # ── docker: check subcommand ──────────────────────────────────────────
    if cmd_name == "docker":
        parts = cmd.split()
        if len(parts) >= 2 and parts[1] in _DOCKER_READ_ONLY:
            return "read_only", f"docker {parts[1]} is read-only"
        return "mutating", "docker mutation subcommand (run, build, push, rm, …)"

    # ── helm: check subcommand ────────────────────────────────────────────
    if cmd_name == "helm":
        parts = cmd.split()
        if len(parts) >= 2 and parts[1] in _HELM_READ_ONLY:
            return "read_only", f"helm {parts[1]} is read-only"
        return "mutating", "helm mutation subcommand (install, upgrade, uninstall, …)"

    # ── curl/wget: write if -o/--output or redirection ────────────────────
    if cmd_name in ("curl", "wget"):
        tokens = cmd.split()
        if "-o" in tokens or "--output" in tokens or "-O" in tokens or ">" in cmd:
            return "mutating", f"{cmd_name} writing to a file"
        return "ambiguous", f"{cmd_name} may have network side-effects"

    # ── Shell interpreters and script runners ─────────────────────────────
    if cmd_name in ("bash", "sh", "zsh", "fish", "ksh", "dash", "tcsh",
                    "python", "python3", "node", "ruby", "perl", "php"):
        return "mutating", f"{cmd_name} can execute arbitrary code"

    # ── Build/package tools ───────────────────────────────────────────────
    if cmd_name in ("make", "cmake", "npm", "yarn", "pnpm", "pip", "pip3",
                    "cargo", "go", "mvn", "gradle", "ant",
                    "apt", "apt-get", "yum", "dnf", "pacman", "brew", "snap"):
        return "mutating", f"{cmd_name} can install packages or modify files"

    # ── Filesystem mutators ───────────────────────────────────────────────
    if cmd_name in ("rm", "mv", "cp", "mkdir", "rmdir", "touch",
                    "ln", "chmod", "chown", "chgrp", "truncate",
                    "dd", "mkfs", "tee", "xargs"):
        return "mutating", f"{cmd_name} modifies filesystem state"

    # ── Process/system management ─────────────────────────────────────────
    if cmd_name in ("kill", "pkill", "killall",
                    "systemctl", "service",
                    "reboot", "shutdown", "halt", "poweroff",
                    "mount", "umount"):
        return "mutating", f"{cmd_name} modifies system/process state"

    # ── Network tools that can send or modify ─────────────────────────────
    if cmd_name in ("ssh", "scp", "rsync", "sftp",
                    "iptables", "nftables", "ufw", "firewall-cmd"):
        return "mutating", f"{cmd_name} performs network or remote operations"

    # ── Archive/compression (can extract files) ────────────────────────────
    if cmd_name in ("tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "xz"):
        return "mutating", f"{cmd_name} can create or extract files"

    # ── Known safe read-only commands ─────────────────────────────────────
    if cmd_name in _EXEC_READ_ONLY:
        return "read_only", f"{cmd_name} is a read-only inspection command"

    # Unknown — default conservative
    return "ambiguous", f"unknown command '{cmd_name}' — treated as potentially mutating"


def simulate_exec_tool(tool_name: str, command: str, workspace_path: Optional[str] = None) -> str:
    """Return a dry-run simulation result for a shell/exec tool call."""
    classification, reason = classify_exec_command(command)
    cmd_preview = command[:200] + ("…" if len(command) > 200 else "")

    logger.debug(
        "dry_run exec classify: tool=%s classification=%s reason=%s command=%r",
        tool_name, classification, reason, command[:80],
    )

    if classification == "read_only":
        return json.dumps({
            "dry_run": True,
            "simulated": True,
            "tool": tool_name,
            "command": cmd_preview,
            "classification": "read_only",
            "message": (
                f"DRY RUN: Command classified as read-only ({reason}). "
                f"Blocked by write_policy=dry_run. "
                f"Would execute: {cmd_preview}"
            ),
        })

    if classification == "mutating":
        return json.dumps({
            "dry_run": True,
            "simulated": True,
            "tool": tool_name,
            "command": cmd_preview,
            "classification": "mutating",
            "message": (
                f"DRY RUN: Mutating command blocked ({reason}). "
                f"Would have run: {cmd_preview}. "
                "No changes applied."
            ),
        })

    # ambiguous — block conservatively
    return json.dumps({
        "dry_run": True,
        "simulated": True,
        "tool": tool_name,
        "command": cmd_preview,
        "classification": "ambiguous",
        "message": (
            f"DRY RUN: Command classification ambiguous ({reason}) — "
            f"blocked conservatively. "
            f"Would have run: {cmd_preview}. "
            "No changes applied."
        ),
    })


# ---------------------------------------------------------------------------
# Main simulation dispatcher
# ---------------------------------------------------------------------------

def simulate_tool(tool_name: str, tool_args: dict, workspace_path: Optional[str] = None) -> str:
    """Return a JSON simulation result for a write/mutation tool call.

    The result is returned to the model as the tool's output so it can
    report the planned action to the user.  The 'dry_run' and 'simulated'
    fields allow run-record post-processing to distinguish real from
    simulated actions.
    """
    name = tool_name.lower()

    # ── Shell / exec tools ───────────────────────────────────────────────
    if name in ("terminal", "bash", "run_command") or "terminal" in name:
        command = tool_args.get("command") or tool_args.get("cmd") or ""
        return simulate_exec_tool(tool_name, command, workspace_path)

    # ── File write ──────────────────────────────────────────────────────────
    if name in ("write_file", "files_write"):
        path = tool_args.get("path", "<unknown>")
        content = tool_args.get("content", "")
        line_count = content.count("\n") + 1 if content else 0
        byte_count = len(content.encode("utf-8")) if content else 0
        return json.dumps({
            "dry_run": True,
            "simulated": True,
            "tool": tool_name,
            "path": path,
            "would_write_bytes": byte_count,
            "would_write_lines": line_count,
            "message": (
                f"DRY RUN: Would write {byte_count} bytes ({line_count} lines) to '{path}'. "
                "No file was modified."
            ),
        })

    # ── Patch / edit ────────────────────────────────────────────────────────
    if name in ("patch_replace", "patch_v4a", "patch"):
        path = tool_args.get("path", "<unknown>")
        old = tool_args.get("old_string") or tool_args.get("diff") or ""
        new = tool_args.get("new_string") or ""
        return json.dumps({
            "dry_run": True,
            "simulated": True,
            "tool": tool_name,
            "path": path,
            "would_replace_chars": len(old),
            "would_insert_chars": len(new),
            "message": (
                f"DRY RUN: Would patch '{path}' "
                f"(replace {len(old)} chars with {len(new)} chars). "
                "No file was modified."
            ),
        })

    # ── kubectl ─────────────────────────────────────────────────────────────
    if "kubectl" in name:
        raw_args = tool_args.get("args") or tool_args.get("manifest") or tool_args.get("command") or ""
        preview = str(raw_args)[:300]
        return json.dumps({
            "dry_run": True,
            "simulated": True,
            "tool": tool_name,
            "message": (
                f"DRY RUN: kubectl operation '{tool_name}' simulated. "
                f"Args preview: {preview}. "
                "No cluster state was modified."
            ),
        })

    # ── Docker ──────────────────────────────────────────────────────────────
    if "docker" in name:
        return json.dumps({
            "dry_run": True,
            "simulated": True,
            "tool": tool_name,
            "message": (
                f"DRY RUN: Docker operation '{tool_name}' simulated. "
                "No container state was modified."
            ),
        })

    # ── Git mutations ────────────────────────────────────────────────────────
    if "git_commit" in name or "git_push" in name:
        msg = tool_args.get("message") or tool_args.get("commit_message") or ""
        return json.dumps({
            "dry_run": True,
            "simulated": True,
            "tool": tool_name,
            "message": (
                f"DRY RUN: Git '{tool_name}' simulated"
                + (f" (message: {msg[:80]})" if msg else "")
                + ". No repository was modified."
            ),
        })

    # ── Generic fallback ────────────────────────────────────────────────────
    return json.dumps({
        "dry_run": True,
        "simulated": True,
        "tool": tool_name,
        "message": (
            f"DRY RUN: Tool '{tool_name}' simulated. No changes were applied."
        ),
    })
