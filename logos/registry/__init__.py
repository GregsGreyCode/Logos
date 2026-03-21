from logos.registry.catalog import (
    AgentCatalog,
    CatalogEntry,
    load_catalog,
    save_catalog,
)
from logos.registry.installer import (
    InstallError,
    install_agent,
    list_agents,
    enable_agent,
    disable_agent,
    get_adapter_class,
    ensure_hermes_installed,
)

__all__ = [
    "AgentCatalog",
    "CatalogEntry",
    "load_catalog",
    "save_catalog",
    "InstallError",
    "install_agent",
    "list_agents",
    "enable_agent",
    "disable_agent",
    "get_adapter_class",
    "ensure_hermes_installed",
]
