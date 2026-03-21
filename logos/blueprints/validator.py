"""Blueprint validator — checks a Blueprint against the installed catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from logos.blueprints.schema import Blueprint
from logos.registry.catalog import load_catalog


@dataclass
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = []
        for e in self.errors:
            lines.append(f"  ✗ {e}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines) if lines else "  ✓ Blueprint is valid"


def validate_blueprint(
    blueprint: Blueprint,
    hermes_home: Optional[Path] = None,
) -> ValidationResult:
    """Validate a Blueprint against the installed agent catalog.

    Checks:
      1. agent_id exists in catalog and is enabled
      2. enforced/default_enabled toolsets are in the agent's supported list
      3. soul_slug resolves to a soul on disk (if set)
      4. enforced and forbidden toolsets do not overlap
    """
    errors: List[str] = []
    warnings: List[str] = []
    catalog = load_catalog(hermes_home)

    # 1 — agent installed and enabled
    entry = catalog.agents.get(blueprint.agent_id)
    if entry is None:
        errors.append(f"Agent '{blueprint.agent_id}' is not installed")
    elif entry.status != "enabled":
        errors.append(f"Agent '{blueprint.agent_id}' is installed but disabled")
    else:
        # 2 — toolset compatibility
        available = set(entry.toolsets)
        if available:  # skip check if catalog entry has no toolsets declared
            for ts in blueprint.toolsets.enforced + blueprint.toolsets.default_enabled:
                if ts not in available:
                    warnings.append(
                        f"Toolset '{ts}' not in agent '{blueprint.agent_id}' supported list"
                    )

    # 3 — soul slug on disk
    if blueprint.soul_slug:
        souls_dir = Path(__file__).parent.parent.parent / "souls"
        soul_path = souls_dir / blueprint.soul_slug / "soul.manifest.yaml"
        if not soul_path.exists():
            warnings.append(f"Soul '{blueprint.soul_slug}' not found in souls/")

    # 4 — enforced ∩ forbidden = ∅
    overlap = set(blueprint.toolsets.enforced) & set(blueprint.toolsets.forbidden)
    if overlap:
        errors.append(f"Toolsets appear in both enforced and forbidden: {sorted(overlap)}")

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)
