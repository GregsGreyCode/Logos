#!/usr/bin/env python3
"""
Log Inspector Tool - Read and analyse Logos/Hermes log files.

Lets the agent inspect its own runtime logs to diagnose errors, find
warnings, and suggest fixes. Reads log files from $HERMES_HOME/logs/
without requiring a terminal sandbox.
"""

import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
_LOGS_DIR = _HERMES_HOME / "logs"

# Patterns that indicate meaningful log events
_ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|Exception|Traceback|error)\b", re.IGNORECASE)
_WARN_RE = re.compile(r"\b(WARNING|WARN)\b", re.IGNORECASE)

VALID_ACTIONS = {"list", "read", "errors", "search"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_files() -> list[Path]:
    """Return all .log files in the logs directory, newest-modified first."""
    if not _LOGS_DIR.exists():
        return []
    return sorted(_LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)


def _tail(path: Path, n: int) -> list[str]:
    """Return the last n lines of a file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n:] if len(lines) > n else lines
    except Exception as exc:
        return [f"[read error: {exc}]"]


def _head(path: Path, n: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[:n]
    except Exception as exc:
        return [f"[read error: {exc}]"]


def _extract_errors(path: Path, max_lines: int = 200) -> list[dict]:
    """Extract error/warning blocks from a log file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [{"level": "ERROR", "line": 0, "text": f"[read error: {exc}]"}]

    lines = text.splitlines()
    results = []
    i = 0
    while i < len(lines) and len(results) < max_lines:
        line = lines[i]
        if _ERROR_RE.search(line):
            level = "ERROR"
        elif _WARN_RE.search(line):
            level = "WARNING"
        else:
            i += 1
            continue

        # Grab the block: this line + any following indented/continuation lines
        block = [line]
        j = i + 1
        while j < len(lines) and (lines[j].startswith(" ") or lines[j].startswith("\t")):
            block.append(lines[j])
            j += 1

        results.append({
            "level": level,
            "line": i + 1,
            "text": "\n".join(block),
        })
        i = j

    return results


def _search_log(path: Path, pattern: str, max_matches: int = 100) -> list[dict]:
    """Search for a regex pattern in a log file."""
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return [{"error": f"Invalid pattern: {exc}"}]

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [{"error": f"Read error: {exc}"}]

    results = []
    for i, line in enumerate(text.splitlines(), 1):
        if compiled.search(line):
            results.append({"line": i, "text": line})
            if len(results) >= max_matches:
                break
    return results


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def log_inspector_tool(
    action: str,
    filename: Optional[str] = None,
    lines: int = 100,
    pattern: Optional[str] = None,
    max_errors: int = 50,
) -> str:
    action = (action or "").strip().lower()
    if action not in VALID_ACTIONS:
        return json.dumps({"error": f"Unknown action '{action}'. Use: list, read, errors, search."})

    # --- LIST ---
    if action == "list":
        files = _log_files()
        if not files:
            return json.dumps({"ok": True, "files": [], "message": f"No log files found in {_LOGS_DIR}"})
        info = []
        for f in files:
            try:
                stat = f.stat()
                info.append({
                    "name": f.name,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": int(stat.st_mtime),
                })
            except Exception:
                info.append({"name": f.name})
        return json.dumps({"ok": True, "files": info, "logs_dir": str(_LOGS_DIR)})

    # All other actions need a file
    if not filename:
        # Default to the most recently modified log
        files = _log_files()
        if not files:
            return json.dumps({"error": f"No log files found in {_LOGS_DIR}. Use action=list first."})
        path = files[0]
    else:
        path = _LOGS_DIR / filename
        if not path.exists():
            # Try without .log extension
            if not filename.endswith(".log"):
                path = _LOGS_DIR / (filename + ".log")
            if not path.exists():
                return json.dumps({"error": f"Log file not found: {filename}. Use action=list to see available files."})

    # --- READ ---
    if action == "read":
        tail_lines = _tail(path, lines)
        return json.dumps({
            "ok": True,
            "file": path.name,
            "lines_returned": len(tail_lines),
            "content": "\n".join(tail_lines),
        })

    # --- ERRORS ---
    if action == "errors":
        errors = _extract_errors(path, max_lines=max_errors)
        if not errors:
            return json.dumps({"ok": True, "file": path.name, "message": "No errors or warnings found.", "items": []})
        # Group by level for a quick summary
        summary: dict = defaultdict(int)
        for e in errors:
            summary[e["level"]] += 1
        return json.dumps({
            "ok": True,
            "file": path.name,
            "summary": dict(summary),
            "items": errors,
        })

    # --- SEARCH ---
    if action == "search":
        if not pattern:
            return json.dumps({"error": "pattern is required for action=search."})
        matches = _search_log(path, pattern)
        return json.dumps({
            "ok": True,
            "file": path.name,
            "pattern": pattern,
            "matches": len(matches),
            "items": matches,
        })

    return json.dumps({"error": "Unexpected state."})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

LOG_INSPECTOR_SCHEMA = {
    "name": "log_inspector",
    "description": (
        "Read and analyse Logos/Hermes runtime log files to diagnose errors and "
        "suggest fixes. Use this proactively when something seems broken or the "
        "user reports unexpected behaviour.\n\n"
        "**Actions:**\n"
        "- `list` — show all available log files with sizes\n"
        "- `read` — tail the last N lines of a log file (default: most recent log)\n"
        "- `errors` — extract all ERROR/WARNING blocks from a log file\n"
        "- `search` — find lines matching a regex pattern in a log file\n\n"
        "**Workflow:** start with `list`, then `errors` on the most relevant file, "
        "then `search` or `read` to drill into specific issues."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read", "errors", "search"],
                "description": "Operation to perform.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Log file name (e.g. 'instance_worker.log'). "
                    "Omit to use the most recently modified log."
                ),
            },
            "lines": {
                "type": "integer",
                "description": "Number of lines to return for action=read. Default 100.",
                "default": 100,
            },
            "pattern": {
                "type": "string",
                "description": "Regex pattern for action=search (case-insensitive).",
            },
            "max_errors": {
                "type": "integer",
                "description": "Maximum error/warning blocks to return for action=errors. Default 50.",
                "default": 50,
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry

registry.register(
    name="log_inspector",
    toolset="log_inspector",
    schema=LOG_INSPECTOR_SCHEMA,
    handler=lambda args, **kw: log_inspector_tool(
        action=args.get("action", ""),
        filename=args.get("filename"),
        lines=int(args.get("lines", 100)),
        pattern=args.get("pattern"),
        max_errors=int(args.get("max_errors", 50)),
    ),
    check_fn=lambda: True,
    description="Inspect Logos/Hermes log files for errors and diagnostics.",
)
