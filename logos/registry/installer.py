"""Agent install / list / enable / disable logic.

install_agent() reads a logos-agent.yaml manifest, verifies the adapter
dotted-path can be imported, and writes a CatalogEntry to the catalog.

ensure_hermes_installed() is called automatically on first use of any
`hermes agent` subcommand so Hermes always appears in the catalog.
"""

from __future__ import annotations

import datetime
import importlib
import logging
from pathlib import Path
from typing import List, Optional, Type

import yaml

from logos.registry.catalog import AgentCatalog, CatalogEntry, load_catalog, save_catalog

logger = logging.getLogger(__name__)


class InstallError(Exception):
    pass


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def install_agent(
    manifest_path: Path,
    hermes_home: Optional[Path] = None,
    *,
    force: bool = False,
) -> CatalogEntry:
    """Install an agent from a logos-agent.yaml manifest file.

    Steps:
      1. Parse manifest and validate required fields
      2. Verify the adapter dotted-path resolves to an AgentAdapter subclass
      3. Write CatalogEntry to catalog.yaml

    Raises InstallError on any failure.
    """
    manifest_path = Path(manifest_path).resolve()
    if not manifest_path.exists():
        raise InstallError(f"Manifest not found: {manifest_path}")

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise InstallError(f"Could not parse manifest: {exc}") from exc

    # Required fields
    for field in ("id", "slug", "name", "adapter"):
        if not raw.get(field):
            raise InstallError(f"Manifest missing required field: '{field}'")

    agent_id = raw["id"]

    # Check not already installed (unless force)
    catalog = load_catalog(hermes_home)
    if agent_id in catalog.agents and not force:
        raise InstallError(
            f"Agent '{agent_id}' is already installed. Use force=True to reinstall."
        )

    # Verify adapter can be imported
    adapter_path: str = raw["adapter"]
    _verify_adapter(adapter_path)

    entry = CatalogEntry(
        agent_id=agent_id,
        slug=raw.get("slug", agent_id),
        name=raw.get("name", agent_id),
        description=raw.get("description", ""),
        version=str(raw.get("version", "1.0.0")),
        adapter=adapter_path,
        status="enabled",
        toolsets=raw.get("toolsets") or [],
        platforms=raw.get("platforms") or [],
        install_type=raw.get("install", {}).get("type", "local"),
        manifest_path=str(manifest_path),
        installed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    catalog.agents[agent_id] = entry
    save_catalog(catalog, hermes_home)
    logger.info("Installed agent '%s' v%s", agent_id, entry.version)
    return entry


def list_agents(hermes_home: Optional[Path] = None) -> List[CatalogEntry]:
    """Return all installed agents (enabled and disabled)."""
    return list(load_catalog(hermes_home).agents.values())


def enable_agent(agent_id: str, hermes_home: Optional[Path] = None) -> None:
    catalog = load_catalog(hermes_home)
    if agent_id not in catalog.agents:
        raise KeyError(f"Agent '{agent_id}' not in catalog")
    catalog.agents[agent_id].status = "enabled"
    save_catalog(catalog, hermes_home)


def disable_agent(agent_id: str, hermes_home: Optional[Path] = None) -> None:
    catalog = load_catalog(hermes_home)
    if agent_id not in catalog.agents:
        raise KeyError(f"Agent '{agent_id}' not in catalog")
    catalog.agents[agent_id].status = "disabled"
    save_catalog(catalog, hermes_home)


def get_adapter_class(
    agent_id: str,
    hermes_home: Optional[Path] = None,
) -> Type:
    """Resolve and return the adapter class for agent_id.

    Raises KeyError if not installed, RuntimeError if disabled,
    ImportError if the adapter path no longer resolves.
    """
    catalog = load_catalog(hermes_home)
    entry = catalog.agents.get(agent_id)
    if entry is None:
        raise KeyError(f"Agent '{agent_id}' is not installed")
    if entry.status == "disabled":
        raise RuntimeError(f"Agent '{agent_id}' is disabled")
    return _load_adapter_class(entry.adapter)


def ensure_hermes_installed(hermes_home: Optional[Path] = None) -> None:
    """Auto-install Hermes from its bundled manifest if not in catalog.

    Called at the top of every `hermes agent` subcommand so the catalog
    is never empty on a fresh install.
    """
    catalog = load_catalog(hermes_home)
    if "hermes" in catalog.agents:
        return
    # Locate the bundled manifest relative to this file
    manifest = Path(__file__).parent.parent.parent / "agents" / "hermes" / "logos-agent.yaml"
    if manifest.exists():
        try:
            install_agent(manifest, hermes_home)
        except InstallError as exc:
            logger.debug("Auto-install of hermes skipped: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _verify_adapter(dotted_path: str) -> None:
    """Import the dotted path and confirm it's an AgentAdapter subclass."""
    if "." not in dotted_path:
        raise InstallError(
            f"Adapter path '{dotted_path}' must be a dotted import path "
            "(e.g. logos.adapters.hermes.adapter.HermesAdapter)"
        )
    module_path, _, class_name = dotted_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise InstallError(f"Cannot import adapter module '{module_path}': {exc}") from exc
    cls = getattr(module, class_name, None)
    if cls is None:
        raise InstallError(f"Class '{class_name}' not found in module '{module_path}'")
    # Soft check — don't import AgentAdapter here to avoid circular deps
    if not callable(getattr(cls, "run", None)):
        raise InstallError(f"'{dotted_path}' does not look like an AgentAdapter (no .run method)")


def _load_adapter_class(dotted_path: str) -> Type:
    module_path, _, class_name = dotted_path.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)
