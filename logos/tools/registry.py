"""Logos tools — ToolRegistry re-export.

The real ToolRegistry class (with invoke()) lives in tools/registry.py.
This module re-exports it under the logos namespace so agents can import
from logos.tools.registry and get the same singleton instance.

Usage::

    from logos.tools.registry import registry, ToolRegistry

    result = registry.invoke(
        "terminal",
        {"command": "ls"},
        policy=action_policy,
        session_id=session_id,
        workspace_path=workspace,
    )
"""

from tools.registry import ToolRegistry, registry

__all__ = ["ToolRegistry", "registry"]
