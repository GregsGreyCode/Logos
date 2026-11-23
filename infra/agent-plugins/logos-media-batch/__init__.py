"""Photos sent together, with the message typed for them, are one turn (Greg, 23 Nov).

The dashboard sends several photos and then the message (Matrix has one picture per event). hermes'
Matrix adapter merges quick successive *text* messages into one turn (``_enqueue_text_event``, whose
merge already carries attachments: ``extend_media``), but hands photos, videos and files straight to
``handle_message``: four photos and a question were five turns, the agent answering each picture alone.

This sends those through the same merge: a photo, video or file event is buffered with what follows
it in the same chat (more photos, the message), and the batch is dispatched once the chat has been quiet
for MEDIA_WAIT seconds (photos upload one after another, slower than a typed message's chunks). Voice
notes are left alone (hermes has its own handling for them). A photo's text that is only its file name
("IMG_1234.jpg") is dropped: hermes keeps the name with the attachment, and it is not something said.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time

logger = logging.getLogger("logos.media_batch")
ADAPTER_MODULE_SUFFIX = "platforms__matrix.adapter"
BATCHED = {"photo", "video", "document"}
MEDIA_WAIT = 2.5
FILE_NAME = re.compile(r"^[^\s/\\]{1,200}\.[A-Za-z0-9]{2,5}$")


def _find_adapter_class():
    for name, mod in list(sys.modules.items()):
        if name.endswith(ADAPTER_MODULE_SUFFIX) and hasattr(mod, "MatrixAdapter"):
            return mod.MatrixAdapter
    return None


def _kind(event) -> str:
    t = getattr(event, "message_type", None)
    return str(getattr(t, "value", t) or "")


def patch(cls) -> None:
    if getattr(cls, "_logos_media_batch", False):
        return
    original_handle = cls.handle_message
    original_delay = getattr(cls, "_text_batch_delay_for", None)

    async def handle_message(self, event, *args, **kwargs):
        if (_kind(event) in BATCHED and getattr(event, "media_urls", None) and not getattr(event, "_logos_batched", False)
                and getattr(self, "_text_batch_delay_seconds", 0) > 0 and hasattr(self, "_enqueue_text_event")):
            event._logos_batched = True                 # its own dispatch, after the merge, goes straight on
            if FILE_NAME.match((getattr(event, "text", "") or "").strip()):
                event.text = ""
            self._enqueue_text_event(event)
            return None
        return await original_handle(self, event, *args, **kwargs)

    def delay_for(self, pending):
        base = original_delay(self, pending) if original_delay else getattr(self, "_text_batch_delay_seconds", 0.6)
        return max(base, MEDIA_WAIT) if pending is not None and getattr(pending, "media_urls", None) else base

    cls.handle_message = handle_message
    if original_delay is not None:
        cls._text_batch_delay_for = delay_for
    cls._logos_media_batch = True
    logger.info("Matrix: photos sent together, with their message, are one turn")


def _patch_when_loaded(timeout: float = 600.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        cls = _find_adapter_class()
        if cls is not None:
            patch(cls)
            return
        time.sleep(0.2)
    logger.error("Matrix: the adapter never loaded; photos stay one turn each")


def register(ctx):
    cls = _find_adapter_class()
    if cls is not None:
        patch(cls)
    else:
        threading.Thread(target=_patch_when_loaded, daemon=True, name="logos-media-batch").start()
