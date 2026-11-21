"""Story M5: the agent answers only verified devices (docs/matrix-chat.md).

When the Matrix library (mautrix) decrypts a message it records how trusted the sending device is:
``event["mautrix"]["trust_state"]`` (unverified 0 ... cross-signed by its owner 100 ... verified 300).
This plugin notes that per event, and wraps the Matrix adapter's per-message check
(``MatrixAdapter._resolve_message_context``, looked up for every message, so patching it at any time
works): a message from a device below "cross-signed by its owner" is dropped and logged, and so is an
unencrypted one (the agents' rooms are all encrypted). Off unless ``LOGOS_ONLY_VERIFIED_DEVICES=1``
(``matrix.verified_only: true`` in the agent's definition).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger("logos.verified_only")

TOFU = 100                       # mautrix TrustState.CROSS_SIGNED_TOFU: signed by the person's own keys
TRUST: dict = {}                 # event id -> trust level, from decryption (bounded below)
MAX_TRUST = 2000
ADAPTER_MODULE_SUFFIX = "platforms__matrix.adapter"


def enabled() -> bool:
    return os.environ.get("LOGOS_ONLY_VERIFIED_DEVICES", "") == "1"


def note_trust(event) -> None:
    """Record a decrypted event's sender-device trust."""
    try:
        info = event.get("mautrix") if hasattr(event, "get") else None
        level = int((info or {}).get("trust_state")) if info else None
    except Exception:
        level = None
    if level is not None and getattr(event, "event_id", None):
        if len(TRUST) > MAX_TRUST:
            for k in list(TRUST)[: MAX_TRUST // 2]:
                TRUST.pop(k, None)
        TRUST[str(event.event_id)] = level


def allowed(event_id: str) -> bool:
    """Answer only messages from a device cross-signed by its owner (or better)."""
    level = TRUST.get(str(event_id))
    return level is not None and level >= TOFU


def patch_decrypt() -> bool:
    try:
        from mautrix.crypto.decrypt_megolm import MegolmDecryptionMachine as M
    except Exception:
        return False
    original = M.decrypt_megolm_event
    if getattr(original, "_logos_verified_only", False):
        return True

    async def decrypt_megolm_event(self, evt, *a, **k):
        result = await original(self, evt, *a, **k)
        note_trust(result)
        return result

    decrypt_megolm_event._logos_verified_only = True
    M.decrypt_megolm_event = decrypt_megolm_event
    return True


def patch_adapter(cls) -> None:
    original = cls._resolve_message_context
    if getattr(original, "_logos_verified_only", False):
        return

    async def _resolve_message_context(self, room_id, sender, event_id, *a, **k):
        if not allowed(event_id):
            logger.warning("Matrix: ignoring message %s from %s in %s: not from a verified device "
                           "(Logos answers verified devices only)", event_id, sender, room_id)
            return None
        return await original(self, room_id, sender, event_id, *a, **k)

    _resolve_message_context._logos_verified_only = True
    cls._resolve_message_context = _resolve_message_context


def _find_adapter_class():
    for name, mod in list(sys.modules.items()):
        if name.endswith(ADAPTER_MODULE_SUFFIX) and hasattr(mod, "MatrixAdapter"):
            return mod.MatrixAdapter
    return None


def _patch_when_loaded(timeout: float = 600.0) -> None:
    """The Matrix adapter may load after this plugin; patch it as soon as it is there."""
    end = time.time() + timeout
    while time.time() < end:
        cls = _find_adapter_class()
        if cls is not None:
            patch_adapter(cls)
            logger.info("Matrix: answering verified devices only")
            return
        time.sleep(0.2)
    logger.error("Matrix: the adapter never loaded, so verified-only is NOT in force")


def register(ctx):
    if not enabled():
        return
    patch_decrypt()
    cls = _find_adapter_class()
    if cls is not None:
        patch_adapter(cls)
        logger.info("Matrix: answering verified devices only")
    else:
        threading.Thread(target=_patch_when_loaded, daemon=True, name="logos-verified-only").start()
