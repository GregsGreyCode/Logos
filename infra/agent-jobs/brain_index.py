#!/usr/bin/env python3
"""Write a compact index of the brain into hermes's built-in memory (no model involved).

The agent's built-in memory (MEMORY.md) is loaded into every new conversation, but the brain is
only read when the agent thinks to search it. This job keeps one managed entry in MEMORY.md that
lists the brain's pages with a one-line summary each, so the agent starts every conversation
knowing what exists. Entries the agent wrote itself are left alone.

Runs as a script-only hermes cron job. Prints nothing unless something is wrong.
"""
import os
import re
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
BRAIN = HOME / "brain"
MEMORY = HOME / "memories" / "MEMORY.md"
DELIM = "\n§\n"                       # hermes's separator between memory entries
MARK = "BRAIN INDEX"
FOLDERS = ("people", "projects", "topics", "files")        # files: a note on each photo or document sent (the vault)
SKIP = re.compile(r"sync-conflict|^\.")


def describe(path: Path) -> str:
    """'Title: first line of substance' for one page, kept short."""
    title, summary = path.stem.replace("-", " "), ""
    try:
        lines = [l.strip() for l in path.read_text(errors="ignore").splitlines()]
    except OSError:
        lines = []
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            lines = lines[i + 1:]
            break
    for line in lines:
        if line and not line.startswith("#"):
            summary = re.sub(r"^[-*]\s+|\*\*|`", "", line)
            break
    text = f"{title}: {summary}" if summary else title
    return text if len(text) <= 90 else text[:87].rstrip() + "..."


def build_index(brain: Path, budget: int, now: float) -> str:
    pages = []
    for folder in FOLDERS:
        for p in sorted((brain / folder).glob("*.md")) if (brain / folder).is_dir() else []:
            if not SKIP.search(p.name):
                pages.append(p)
    pages.sort(key=lambda p: p.stat().st_mtime, reverse=True)       # most recently changed first
    inbox = sorted(p.name[:-3] for p in (brain / "inbox").glob("*.md")) if (brain / "inbox").is_dir() else []
    head = (f"{MARK} (automatic, {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}). "
            f"Your brain is the folder {brain}. Pages you have, newest first. "
            f"Read a page before answering from it; search the folder for anything not listed.")
    lines, used, shown = [head], len(head), 0
    for p in pages:
        line = f"{p.parent.name}/{p.name} — {describe(p)}"
        if used + len(line) + 1 > budget - 80:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(pages):
        lines.append(f"(+{len(pages) - shown} older pages not listed)")
    if not pages:
        lines.append("(no pages yet)")
    if inbox:
        lines.append(f"inbox/ has {len(inbox)} day file(s), latest {inbox[-1]}.")
    return "\n".join(lines)


def merge(existing: str, index: str) -> str:
    """Replace the managed entry, keep every other entry in its original order after it."""
    others = [e.strip() for e in existing.split(DELIM.strip("\n")) if e.strip() and not e.strip().startswith(MARK)]
    return DELIM.join([index] + others) + "\n"


def main() -> int:
    if not BRAIN.is_dir():
        print(f"brain folder missing: {BRAIN}")
        return 0
    limit = int(os.environ.get("LOGOS_MEMORY_CHAR_LIMIT", "6000"))
    existing = MEMORY.read_text(errors="ignore") if MEMORY.exists() else ""
    others_len = sum(len(e) + len(DELIM) for e in existing.split(DELIM.strip("\n"))
                     if e.strip() and not e.strip().startswith(MARK))
    budget = max(400, limit - others_len - 50)
    new = merge(existing, build_index(BRAIN, budget, time.time()))
    if new != existing:
        MEMORY.parent.mkdir(parents=True, exist_ok=True)
        tmp = MEMORY.with_suffix(".md.tmp")
        tmp.write_text(new)
        tmp.replace(MEMORY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
