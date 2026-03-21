"""Logos souls — soul loading and persona management.

Decouples SOUL.md management from hermes_cli.config so any Logos agent
can load and update the agent persona without pulling in the full CLI.

A "soul" is the agent's persona defined in SOUL.md — a markdown document
placed in the Hermes home directory that shapes the agent's behaviour,
tone, and identity.

Usage::

    from logos.souls.loader import load_soul, save_soul
    from pathlib import Path

    home = Path.home() / ".hermes"
    soul_text = load_soul(home)               # load (seeding default if absent)
    save_soul(home, "You are a researcher.")  # update
"""

import os
from pathlib import Path
from typing import Optional


def get_soul_path(home: Path) -> Path:
    """Return the SOUL.md path for a given Hermes home directory."""
    return home / "SOUL.md"


def load_soul(home: Path, create_default: bool = True) -> str:
    """Load the SOUL.md content for a Hermes home directory.

    If SOUL.md does not exist and *create_default* is True, seeds the
    default soul template (same as hermes_cli.config._ensure_default_soul_md).

    Returns the SOUL.md text, or ``""`` if the file is unavailable.
    """
    soul_path = get_soul_path(home)
    if not soul_path.exists():
        if not create_default:
            return ""
        try:
            from hermes_cli.default_soul import DEFAULT_SOUL_MD
            soul_path.parent.mkdir(parents=True, exist_ok=True)
            soul_path.write_text(DEFAULT_SOUL_MD, encoding="utf-8")
            try:
                os.chmod(soul_path, 0o600)
            except (OSError, NotImplementedError):
                pass
        except Exception:
            return ""
    try:
        return soul_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def save_soul(home: Path, content: str) -> None:
    """Write SOUL.md content to a Hermes home directory.

    Creates the directory if it does not exist and sets owner-only
    permissions (0600) on the file.
    """
    soul_path = get_soul_path(home)
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text(content, encoding="utf-8")
    try:
        os.chmod(soul_path, 0o600)
    except (OSError, NotImplementedError):
        pass
