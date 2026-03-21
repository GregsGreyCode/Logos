"""
Logos eval suite registry.

Suites are .py files in this package that expose a SUITE module-level
variable. Import them here so they're discoverable.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Dict, List, Optional

from evals.schema import EvalSuite

logger = logging.getLogger(__name__)

_SUITE_MODULES = [
    "evals.suites.diagnose_cpu",
    "evals.suites.policy_enforcement",
    "evals.suites.agent_routing",
]

_registry: Dict[str, EvalSuite] = {}


def _load_registry() -> None:
    global _registry
    _registry = {}
    for modname in _SUITE_MODULES:
        try:
            mod = importlib.import_module(modname)
            suite = getattr(mod, "SUITE", None)
            if isinstance(suite, EvalSuite):
                _registry[suite.name] = suite
        except Exception as exc:
            logger.debug("Failed to load eval suite module %s: %s", modname, exc)


def get_suite(name: str) -> Optional[EvalSuite]:
    """Return a named eval suite, or None if not found."""
    if not _registry:
        _load_registry()
    return _registry.get(name)


def list_suites() -> List[EvalSuite]:
    """Return all registered eval suites."""
    if not _registry:
        _load_registry()
    return list(_registry.values())
