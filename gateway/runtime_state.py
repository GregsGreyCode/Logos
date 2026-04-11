"""Process-wide runtime state shared between the gateway runner and executors.

Why this module exists
──────────────────────
``gateway/run.py`` is started via ``python -m gateway.run``, which loads
``gateway/run.py`` as the ``__main__`` module. When any other code does
``from gateway import run`` or ``import gateway.run``, Python loads the
file *again* as a second, completely independent module object. Both
module objects have their own copy of any module-level globals —
assigning to ``_current_runner`` inside ``main()`` mutates the
``__main__`` module's copy, while ``executors.openshell`` and anyone
else doing ``import gateway.run`` reads from the second module's copy,
which stays at its import-time default forever.

This module sits outside that trap: it is *never* imported as
``__main__``, so every importer gets the same module object and the
same globals. ``gateway/run.py`` now sets the state via
``set_current_runner`` / ``set_current_loop``, and executors read
``current_runner`` / ``current_loop`` as plain attributes.

If you find yourself tempted to add new module-level state to
``gateway/run.py`` that other code needs to read at runtime — resist,
and put it here instead.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from gateway.run import GatewayRunner  # noqa: F401


# Set by gateway.run.main() right after the aiohttp app starts and the
# event loop is running. Cleared on shutdown. Read by any thread-pool
# code that needs to schedule coroutines onto the main loop (e.g.
# OpenShellExecutor.spawn uses asyncio.run_coroutine_threadsafe to call
# WorkerRegistry.ensure_worker from its worker thread).
current_runner: Optional["GatewayRunner"] = None
current_loop: Optional[asyncio.AbstractEventLoop] = None


def set_current_runner(runner: Optional["GatewayRunner"]) -> None:
    """Bind (or clear) the process-wide GatewayRunner reference."""
    global current_runner
    current_runner = runner


def set_current_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Bind (or clear) the process-wide main event loop reference."""
    global current_loop
    current_loop = loop
