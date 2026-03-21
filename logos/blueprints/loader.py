"""Blueprint loader — reads/writes ~/.hermes/blueprints/*.yaml."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

import yaml

from logos.blueprints.schema import Blueprint, ModelSpec, PolicySpec, ToolsetSpec

logger = logging.getLogger(__name__)

_BLUEPRINT_FIELDS = Blueprint.__dataclass_fields__


def _blueprints_dir(hermes_home: Optional[Path] = None) -> Path:
    base = hermes_home or Path.home() / ".hermes"
    return base / "blueprints"


def load_blueprint(path: Path) -> Blueprint:
    """Parse a single YAML file into a Blueprint."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    ts_raw = raw.pop("toolsets", {})
    model_raw = raw.pop("model", {})
    policy_raw = raw.pop("policy", {})

    toolsets = ToolsetSpec(**{k: v for k, v in (ts_raw or {}).items()
                              if k in ToolsetSpec.__dataclass_fields__})
    model = ModelSpec(**{k: v for k, v in (model_raw or {}).items()
                         if k in ModelSpec.__dataclass_fields__})
    policy = PolicySpec(**{k: v for k, v in (policy_raw or {}).items()
                           if k in PolicySpec.__dataclass_fields__})

    known = {k: v for k, v in raw.items() if k in _BLUEPRINT_FIELDS}
    return Blueprint(toolsets=toolsets, model=model, policy=policy, **known)


def load_all_blueprints(hermes_home: Optional[Path] = None) -> Dict[str, Blueprint]:
    """Return {slug: Blueprint} for every YAML in ~/.hermes/blueprints/."""
    bd = _blueprints_dir(hermes_home)
    if not bd.exists():
        return {}
    result: Dict[str, Blueprint] = {}
    for p in sorted(bd.glob("*.yaml")):
        try:
            bp = load_blueprint(p)
            result[bp.slug or p.stem] = bp
        except Exception as exc:
            logger.debug("Skipping malformed blueprint %s: %s", p.name, exc)
    return result


def save_blueprint(blueprint: Blueprint, hermes_home: Optional[Path] = None) -> Path:
    """Persist a Blueprint to ~/.hermes/blueprints/<slug>.yaml."""
    bd = _blueprints_dir(hermes_home)
    bd.mkdir(parents=True, exist_ok=True)
    slug = blueprint.slug or blueprint.id or "unnamed"
    path = bd / f"{slug}.yaml"

    data: dict = {
        "schema_version": blueprint.schema_version,
        "id": blueprint.id,
        "slug": slug,
        "name": blueprint.name,
        "description": blueprint.description,
        "tags": blueprint.tags,
        "agent_id": blueprint.agent_id,
        "soul_slug": blueprint.soul_slug,
        "version": blueprint.version,
        "status": blueprint.status,
        "created_by": blueprint.created_by,
        "toolsets": {
            "enforced": blueprint.toolsets.enforced,
            "default_enabled": blueprint.toolsets.default_enabled,
            "optional": blueprint.toolsets.optional,
            "forbidden": blueprint.toolsets.forbidden,
        },
        "model": {
            "model": blueprint.model.model,
            "provider": blueprint.model.provider,
            "max_tokens": blueprint.model.max_tokens,
        },
        "policy": {
            "action_policy_id": blueprint.policy.action_policy_id,
            "max_iterations": blueprint.policy.max_iterations,
            "workspace_isolated": blueprint.policy.workspace_isolated,
        },
    }
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path
