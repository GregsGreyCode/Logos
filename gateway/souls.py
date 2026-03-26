"""
Soul registry — agent persona definitions.

Extracted from gateway/http_api.py so that both the HTTP API layer and the
KubernetesExecutor can import soul logic without circular dependencies.

Public API:
  SoulManifest          — dataclass describing an agent soul
  get_soul_registry()   — lazy-loaded dict of slug → SoulManifest
  load_souls()          — force-reload from disk (used at startup)
  validate_soul_overrides(soul, overrides) — raise ValueError on policy violation
  compute_effective_toolsets(soul, overrides) → list[str]
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib

import yaml

logger = logging.getLogger(__name__)

_SOULS_DIR = pathlib.Path(__file__).parent.parent / "souls"

_SOUL_REGISTRY: dict[str, "SoulManifest"] = {}


@dataclasses.dataclass
class SoulManifest:
    id: str
    slug: str
    name: str
    description: str
    category: str
    role_summary: str
    status: str          # "stable" | "experimental" | "deprecated"
    version: str
    created_by: str
    tags: list
    enforced_toolsets: list
    default_enabled_toolsets: list
    optional_toolsets: list
    forbidden_toolsets: list
    user_accessible: bool = True
    soul_md: str = ""

    def to_dict(self, include_soul_md: bool = False) -> dict:
        d = {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "role_summary": self.role_summary,
            "status": self.status,
            "version": self.version,
            "tags": self.tags,
            "user_accessible": self.user_accessible,
            "toolsets": {
                "enforced": self.enforced_toolsets,
                "default_enabled": self.default_enabled_toolsets,
                "optional": self.optional_toolsets,
                "forbidden": self.forbidden_toolsets,
            },
        }
        if include_soul_md:
            d["soul_md"] = self.soul_md
        return d


def load_souls() -> dict[str, SoulManifest]:
    """Load souls from the souls/ directory alongside the hermes-agent package."""
    global _SOUL_REGISTRY
    registry: dict[str, SoulManifest] = {}
    if not _SOULS_DIR.exists():
        logger.warning("Souls directory not found: %s", _SOULS_DIR)
        _SOUL_REGISTRY = registry
        return registry
    for soul_dir in sorted(_SOULS_DIR.iterdir()):
        if not soul_dir.is_dir():
            continue
        manifest_path = soul_dir / "soul.manifest.yaml"
        soul_md_path = soul_dir / "soul.md"
        if not manifest_path.exists():
            continue
        try:
            data = yaml.safe_load(manifest_path.read_text()) or {}
            toolsets = data.get("toolsets", {})
            soul = SoulManifest(
                id=data.get("id", soul_dir.name),
                slug=data.get("slug", soul_dir.name),
                name=data.get("name", soul_dir.name),
                description=data.get("description", ""),
                category=data.get("category", "general"),
                role_summary=data.get("role_summary", ""),
                status=data.get("status", "stable"),
                version=str(data.get("version", "1.0")),
                created_by=data.get("created_by", ""),
                tags=data.get("tags", []),
                enforced_toolsets=toolsets.get("enforced", []),
                default_enabled_toolsets=toolsets.get("default_enabled", []),
                optional_toolsets=toolsets.get("optional", []),
                forbidden_toolsets=toolsets.get("forbidden", []),
                user_accessible=data.get("user_accessible", True),
                soul_md=soul_md_path.read_text() if soul_md_path.exists() else "",
            )
            registry[soul.slug] = soul
        except Exception as e:
            logger.warning("Failed to load soul from %s: %s", soul_dir, e)
    _SOUL_REGISTRY = registry
    logger.info("Loaded %d souls: %s", len(registry), list(registry.keys()))
    return registry


def get_soul_registry() -> dict[str, SoulManifest]:
    if not _SOUL_REGISTRY:
        load_souls()
    return _SOUL_REGISTRY


def validate_soul_overrides(soul: SoulManifest, overrides: dict) -> None:
    """Raise ValueError if overrides violate soul policy."""
    to_remove = set(overrides.get("remove", []))
    to_add = set(overrides.get("add", []))
    for ts in to_remove:
        if ts in soul.enforced_toolsets:
            raise ValueError(f"cannot_remove_enforced:{ts}")
    for ts in to_add:
        if ts in soul.forbidden_toolsets:
            raise ValueError(f"toolset_not_available:{ts}")
        if ts not in soul.optional_toolsets:
            raise ValueError(f"toolset_not_in_soul:{ts}")


def compute_effective_toolsets(soul: SoulManifest, overrides: dict) -> list[str]:
    effective = set(soul.enforced_toolsets)
    effective |= set(soul.default_enabled_toolsets)
    effective -= set(overrides.get("remove", []))
    effective |= set(overrides.get("add", []))
    return sorted(effective)
