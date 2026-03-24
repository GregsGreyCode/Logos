#!/usr/bin/env python3
"""
Bug Notes Tool - Self-Reported Issue Tracker

Lets the agent log notes about things that didn't work: failed prompts,
tool misbehaviours, incorrect assumptions, delivery failures, or anything
else worth fixing later. Notes are saved to $HERMES_HOME/bug_notes.md in a
human-readable format so the user can pull them up and work through
them at any time.

Design:
- Single `bug_notes` tool with action: add | list | resolve
- Each note has: id, timestamp, category, description, and optional context
- File format is Markdown so it's readable without tools
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BUG_NOTES_PATH = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "bug_notes.md"

VALID_CATEGORIES = {
    "tool_failure",      # A tool didn't do what was expected
    "delivery_failure",  # Message delivery failed (telegram, discord, etc.)
    "reasoning_error",   # Agent reasoned incorrectly or hallucinated
    "prompt_issue",      # A prompt or skill instruction needs improvement
    "config_issue",      # Something misconfigured in the environment
    "other",             # Catch-all
}

VALID_ACTIONS = {"add", "list", "resolve"}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _parse_notes() -> List[Dict[str, str]]:
    """Parse bug_notes.md into a list of note dicts."""
    if not BUG_NOTES_PATH.exists():
        return []
    try:
        text = BUG_NOTES_PATH.read_text(encoding="utf-8")
    except Exception:
        return []

    notes = []
    # Each note is delimited by a level-2 heading: ## [id] ...
    blocks = re.split(r"\n(?=## \[)", text)
    for block in blocks:
        block = block.strip()
        if not block.startswith("## ["):
            continue
        note: Dict[str, str] = {}
        # Header line: ## [id] category — timestamp
        header_match = re.match(r"^## \[([^\]]+)\]\s+([\w_]+)\s*[—-]\s*(.+)$", block, re.MULTILINE)
        if not header_match:
            continue
        note["id"] = header_match.group(1).strip()
        note["category"] = header_match.group(2).strip()
        note["timestamp"] = header_match.group(3).strip()

        # Status tag
        if "**Status:** resolved" in block:
            note["status"] = "resolved"
        else:
            note["status"] = "open"

        # Description: first paragraph after header (not a metadata line)
        body_lines = block.split("\n")[1:]
        desc_lines = []
        for line in body_lines:
            if line.startswith("**") or line.strip() == "":
                if desc_lines:
                    break
            elif line.strip():
                desc_lines.append(line.strip())
        note["description"] = " ".join(desc_lines)

        notes.append(note)
    return notes


def _next_id(notes: List[Dict[str, str]]) -> str:
    """Generate the next sequential note ID (BUG-001, BUG-002, …)."""
    existing = []
    for n in notes:
        m = re.match(r"BUG-(\d+)", n.get("id", ""))
        if m:
            existing.append(int(m.group(1)))
    next_num = max(existing, default=0) + 1
    return f"BUG-{next_num:03d}"


def _append_note(note_id: str, category: str, description: str, context: str) -> None:
    """Append a new note to bug_notes.md."""
    BUG_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header_needed = not BUG_NOTES_PATH.exists() or BUG_NOTES_PATH.stat().st_size == 0
    lines = []
    if header_needed:
        lines += [
            "# Hermes Bug Notes\n",
            "Auto-generated issue log. Use `bug_notes` tool to add, list, or resolve.\n",
            "\n",
        ]

    lines += [
        f"\n## [{note_id}] {category} — {timestamp}\n",
        "\n",
        f"{description}\n",
    ]
    if context:
        lines += ["\n", f"**Context:** {context}\n"]
    lines += ["\n", "**Status:** open\n"]

    with open(BUG_NOTES_PATH, "a", encoding="utf-8") as f:
        f.writelines(lines)


def _resolve_note(note_id: str) -> bool:
    """Mark a note as resolved in-place. Returns True if found."""
    if not BUG_NOTES_PATH.exists():
        return False
    text = BUG_NOTES_PATH.read_text(encoding="utf-8")
    # Find the note block and flip its status
    pattern = rf"(## \[{re.escape(note_id)}\][^\n]*\n(?:.*\n)*?)\*\*Status:\*\* open"
    replacement = r"\1**Status:** resolved"
    new_text, count = re.subn(pattern, replacement, text)
    if count == 0:
        return False
    BUG_NOTES_PATH.write_text(new_text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def bug_notes_tool(
    action: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
    context: Optional[str] = None,
    note_id: Optional[str] = None,
    show_resolved: bool = False,
) -> str:
    action = (action or "").strip().lower()
    if action not in VALID_ACTIONS:
        return json.dumps({"error": f"Unknown action '{action}'. Use: add, list, resolve."})

    # --- ADD ---
    if action == "add":
        if not description or not description.strip():
            return json.dumps({"error": "description is required for action=add."})
        category = (category or "other").strip().lower()
        if category not in VALID_CATEGORIES:
            category = "other"

        notes = _parse_notes()
        new_id = _next_id(notes)
        _append_note(new_id, category, description.strip(), (context or "").strip())

        return json.dumps({
            "ok": True,
            "id": new_id,
            "message": f"Bug note {new_id} saved.",
        })

    # --- LIST ---
    if action == "list":
        notes = _parse_notes()
        if not show_resolved:
            notes = [n for n in notes if n.get("status") != "resolved"]
        if not notes:
            return json.dumps({"ok": True, "notes": [], "message": "No open bug notes."})
        return json.dumps({"ok": True, "notes": notes, "count": len(notes)})

    # --- RESOLVE ---
    if action == "resolve":
        if not note_id or not note_id.strip():
            return json.dumps({"error": "note_id is required for action=resolve."})
        note_id = note_id.strip().upper()
        if _resolve_note(note_id):
            return json.dumps({"ok": True, "message": f"{note_id} marked as resolved."})
        return json.dumps({"error": f"Note {note_id} not found or already resolved."})

    return json.dumps({"error": "Unexpected state."})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

BUG_NOTES_SCHEMA = {
    "name": "bug_notes",
    "description": (
        "Log, list, and resolve self-reported bug notes — things that didn't work "
        "correctly and need attention later. Use this proactively whenever:\n\n"
        "- A tool call fails unexpectedly or returns a wrong result\n"
        "- A message delivery fails or goes to the wrong destination\n"
        "- You catch yourself having reasoned incorrectly or hallucinated a command\n"
        "- A skill or prompt instruction produced a bad outcome\n"
        "- Something is misconfigured in the environment\n\n"
        "Notes are saved to $HERMES_HOME/bug_notes.md so you can review and fix "
        "them later. Always add a note before giving up on a failed task.\n\n"
        "**Actions:**\n"
        "- `add` — save a new bug note (requires description)\n"
        "- `list` — show all open notes (set show_resolved=true to include resolved)\n"
        "- `resolve` — mark a note fixed (requires note_id)\n\n"
        "**Categories:** tool_failure, delivery_failure, reasoning_error, "
        "prompt_issue, config_issue, other"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "resolve"],
                "description": "Operation to perform.",
            },
            "description": {
                "type": "string",
                "description": (
                    "What went wrong. Be specific: include what you tried, what "
                    "happened, and what you expected. Required for action=add."
                ),
            },
            "category": {
                "type": "string",
                "enum": sorted(VALID_CATEGORIES),
                "description": "Category of the issue. Defaults to 'other'.",
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional extra context: relevant tool args, error messages, "
                    "session state, or reproduction steps."
                ),
            },
            "note_id": {
                "type": "string",
                "description": "Note ID to resolve (e.g. 'BUG-003'). Required for action=resolve.",
            },
            "show_resolved": {
                "type": "boolean",
                "description": "Include resolved notes in list output. Default false.",
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
    name="bug_notes",
    toolset="bug_notes",
    schema=BUG_NOTES_SCHEMA,
    handler=lambda args, **kw: bug_notes_tool(
        action=args.get("action", ""),
        description=args.get("description"),
        category=args.get("category"),
        context=args.get("context"),
        note_id=args.get("note_id"),
        show_resolved=args.get("show_resolved", False),
    ),
    check_fn=lambda: True,
)
