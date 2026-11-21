"""Scheduled messages straight into the Matrix chat, not a "Hermes — <job>" thread each (Greg, 21 Nov).

The agents' config already asks for this (``platforms.matrix.extra.cron_continuable_surface:
in_channel``, #134), but hermes honours it only for an adapter whose class says
``supports_inchannel_continuable = True``; Slack's does, Matrix's does not yet, so hermes quietly fell
back to a thread for every briefing and daily question. This marks the Matrix adapter class. hermes then
posts the message flat and seeds (or, for a briefing, mirrors into) the chat's session, so the agent
still sees what it sent when its person answers.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

logger = logging.getLogger("logos.matrix_flat")
ADAPTER_MODULE_SUFFIX = "platforms__matrix.adapter"


def _find_adapter_class():
    for name, mod in list(sys.modules.items()):
        if name.endswith(ADAPTER_MODULE_SUFFIX) and hasattr(mod, "MatrixAdapter"):
            return mod.MatrixAdapter
    return None


def mark(cls) -> None:
    cls.supports_inchannel_continuable = True
    logger.info("Matrix: scheduled messages go straight into the chat")


def _mark_when_loaded(timeout: float = 600.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        cls = _find_adapter_class()
        if cls is not None:
            mark(cls)
            return
        time.sleep(0.2)
    logger.error("Matrix: the adapter never loaded; scheduled messages will open threads")


def register(ctx):
    cls = _find_adapter_class()
    if cls is not None:
        mark(cls)
    else:
        threading.Thread(target=_mark_when_loaded, daemon=True, name="logos-matrix-flat").start()
