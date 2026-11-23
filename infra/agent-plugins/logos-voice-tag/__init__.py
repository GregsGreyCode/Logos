"""logos-voice-tag: the agent is told a message was spoken (Greg, 23 Nov).

The Logos app turns a recording into words and sends them at once as a voice message: an ordinary text
message that also carries ``org.logos.voice`` ({seconds, wave}). Agents ignore that field. This makes the
agent see such a message the way it sees a voice note from Element X: a plain quoted line,

    "Can you remind me about the dentist tomorrow?"

so that its rules for voice notes apply (speech to text mistakes names and unusual words; carry on when
the meaning is clear, ask when it is not). In a group the message starts with the agents it is for
("Household agent: ..."); only the words after that are quoted. Nothing else about the message changes.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

logger = logging.getLogger("logos.voice_tag")
ADAPTER_MODULE_SUFFIX = "platforms__matrix.adapter"


def tagged(source_content):
    """``source_content`` with the words quoted when it is a voice message from the Logos app, else as it was."""
    if not isinstance(source_content, dict):
        return source_content
    v = source_content.get("org.logos.voice")
    if not isinstance(v, dict) or not v.get("seconds"):
        return source_content
    body = str(source_content.get("body") or "")
    if not body.strip():
        return source_content
    prefix, words = "", body
    mentioned = (source_content.get("m.mentions") or {}).get("user_ids")
    if mentioned and ": " in body:                   # a group: "Agent name: words"
        prefix, _, words = body.partition(": ")
        prefix += ": "
    words = words.strip()
    if words.startswith('"') and words.endswith('"') and len(words) > 1:
        return source_content
    return {**source_content, "body": f'{prefix}"{words}"'}


def patch(cls) -> None:
    if getattr(cls, "_logos_voice_tag", False) or not hasattr(cls, "_handle_text_message"):
        return
    original = cls._handle_text_message

    async def _handle_text_message(self, room_id, sender, event_id, event_ts, source_content, relates_to):
        return await original(self, room_id, sender, event_id, event_ts, tagged(source_content), relates_to)

    cls._handle_text_message = _handle_text_message
    cls._logos_voice_tag = True
    logger.info("Matrix: voice messages from the Logos app arrive as quoted voice notes")


def _find_adapter_class():
    for name, mod in list(sys.modules.items()):
        if name.endswith(ADAPTER_MODULE_SUFFIX) and hasattr(mod, "MatrixAdapter"):
            return mod.MatrixAdapter
    return None


def _patch_when_loaded(timeout: float = 600.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        cls = _find_adapter_class()
        if cls is not None:
            patch(cls)
            return
        time.sleep(0.2)
    logger.error("Matrix: the adapter never loaded; voice messages stay plain text")


def register(ctx):
    cls = _find_adapter_class()
    if cls is not None:
        patch(cls)
    else:
        threading.Thread(target=_patch_when_loaded, daemon=True, name="logos-voice-tag").start()
