"""Blueprint schema — STAMP composition model.

A Blueprint is a reusable run template defined by five orthogonal axes:

  S — Soul     : personality and tone injected via SOUL.md
  T — Toolset  : which capabilities are available
  A — Agent    : which runtime handles the conversation loop
  M — Model    : inference backend (provider, model name, context)
  P — Policy   : what the agent is allowed to do

Blueprints are stored as ~/.hermes/blueprints/<slug>.yaml and resolved
at run time by BlueprintLoader + BlueprintValidator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BLUEPRINT_SCHEMA_VERSION = 1


@dataclass
class ToolsetSpec:
    """T — Toolset axis of a Blueprint.

    Mirrors soul.manifest.yaml toolset structure so the two can be merged
    cleanly. Blueprint toolsets take precedence over soul toolsets.
    """
    enforced: List[str] = field(default_factory=list)        # always on
    default_enabled: List[str] = field(default_factory=list) # on by default
    optional: List[str] = field(default_factory=list)        # user may toggle
    forbidden: List[str] = field(default_factory=list)       # always off


@dataclass
class ModelSpec:
    """M — Model axis of a Blueprint."""
    model: str = "anthropic/claude-opus-4-6"
    provider: Optional[str] = None
    max_tokens: Optional[int] = None
    reasoning_config: Optional[Dict[str, Any]] = None
    fallback: Optional[Dict[str, Any]] = None


@dataclass
class PolicySpec:
    """P — Policy axis of a Blueprint."""
    action_policy_id: Optional[str] = None  # FK into action_policies table
    max_iterations: int = 90
    workspace_isolated: bool = True


@dataclass
class Blueprint:
    """STAMP — Soul × Toolset × Agent × Model × Policy.

    A reusable composition template. Each axis is independently swappable.
    Stored as ~/.hermes/blueprints/<slug>.yaml.
    """
    schema_version: int = BLUEPRINT_SCHEMA_VERSION

    # Identity
    id: str = ""
    slug: str = ""
    name: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)

    # ── STAMP ────────────────────────────────────────────────────────────
    soul_slug: Optional[str] = None        # S — soul persona slug
    toolsets: ToolsetSpec = field(default_factory=ToolsetSpec)  # T
    agent_id: str = "hermes"               # A — must match a catalog entry
    model: ModelSpec = field(default_factory=ModelSpec)         # M
    policy: PolicySpec = field(default_factory=PolicySpec)      # P

    # Metadata
    created_by: Optional[str] = None
    version: str = "1.0"
    status: str = "stable"
