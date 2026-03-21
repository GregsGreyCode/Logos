"""Local agent catalog — reads/writes ~/.hermes/agents/catalog.yaml.

The catalog is the source of truth for which Logos agents are installed
and enabled. It is a local YAML file; no internet required for operation.
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

CATALOG_VERSION = 1


@dataclass
class CatalogEntry:
    """One installed agent in the catalog."""
    agent_id: str
    slug: str
    name: str
    description: str
    version: str
    adapter: str                           # dotted import path
    status: str = "enabled"               # "enabled" | "disabled"
    toolsets: List[str] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)
    install_type: str = "builtin"         # "builtin" | "local" | "community"
    manifest_path: Optional[str] = None   # absolute path to logos-agent.yaml
    installed_at: Optional[str] = None    # ISO timestamp


@dataclass
class AgentCatalog:
    version: int = CATALOG_VERSION
    agents: Dict[str, CatalogEntry] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    return Path.home() / ".hermes"


def _catalog_path(hermes_home: Optional[Path] = None) -> Path:
    return (hermes_home or _hermes_home()) / "agents" / "catalog.yaml"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_catalog(hermes_home: Optional[Path] = None) -> AgentCatalog:
    """Load catalog.yaml; return empty catalog if file does not exist."""
    path = _catalog_path(hermes_home)
    if not path.exists():
        return AgentCatalog()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agents: Dict[str, CatalogEntry] = {}
    for aid, entry_data in (raw.get("agents") or {}).items():
        try:
            agents[aid] = CatalogEntry(**{
                k: v for k, v in entry_data.items()
                if k in CatalogEntry.__dataclass_fields__
            })
        except Exception:
            pass  # skip malformed entries
    return AgentCatalog(version=raw.get("version", CATALOG_VERSION), agents=agents)


def save_catalog(catalog: AgentCatalog, hermes_home: Optional[Path] = None) -> None:
    """Persist catalog to disk, creating parent dirs if needed."""
    path = _catalog_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": catalog.version,
        "agents": {aid: asdict(e) for aid, e in catalog.agents.items()},
    }
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
