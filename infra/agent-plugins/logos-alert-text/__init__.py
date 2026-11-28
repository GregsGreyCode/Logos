"""logos-alert-text: the phone alert for an agent's reply says what it wrote (gateway/alert_text.py).

Matrix messages are end-to-end encrypted, so the alert Logos sends a phone knows who wrote but not
what. As this agent sends a text reply, this hands Logos the reply's event id and its text over the
one path the sandbox may reach on the dashboard (``LOGOS_ALERT_URL``), signed with the agent's own
token (``LOGOS_ALERT_TOKEN``). Logos keeps the first words in memory for two minutes for the alert.

It never delays or stops a reply: the note goes after the message is sent, in the background, and
any failure is ignored (the alert then says "New message", as before). Nothing here logs text.
Only text and notices: photos, files and voice keep the plain alert.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import urllib.request

logger = logging.getLogger("logos.alert_text")
ADAPTER_MODULE_SUFFIX = "platforms__matrix.adapter"
HOME = os.environ.get("HERMES_HOME", "/opt/data")
TOKEN_ENV, URL_ENV, AGENT_ENV = "LOGOS_ALERT_TOKEN", "LOGOS_ALERT_URL", "LOGOS_AGENT_NAME"
TEXT_TYPES = {"m.text", "m.notice"}
SENT_CHARS = 1000                   # Logos shows about 180; this is plenty to cut at a word


def settings() -> dict:
    """The agent's name, token and Logos's address: from the environment, else its .env."""
    got = {k: os.environ.get(k, "") for k in (TOKEN_ENV, URL_ENV, AGENT_ENV)}
    if all(got.values()):
        return got
    try:
        with open(os.path.join(HOME, ".env"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k in got and not got[k]:
                    got[k] = v
    except OSError:
        pass
    return got


def note(event_id: str, text: str, conf: dict, timeout: float = 3.0) -> bool:
    # The token goes in the Authorization header: with an OpenShell placeholder (E7, docs/secrets.md)
    # that is the only place the real value is put in. A real token also goes in the body, for an older Logos.
    fields = {"agent": conf[AGENT_ENV], "event_id": event_id, "text": text[:SENT_CHARS]}
    if not conf[TOKEN_ENV].startswith("openshell:resolve:"):
        fields["token"] = conf[TOKEN_ENV]
    req = urllib.request.Request(conf[URL_ENV], data=json.dumps(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + conf[TOKEN_ENV]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:                                  # never the text, only that it failed
        logger.info("alert text: Logos did not take it (%s)", type(e).__name__)
        return False


def text_of(content) -> str:
    if not isinstance(content, dict) or content.get("msgtype") not in TEXT_TYPES:
        return ""
    if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
        return ""                                           # an edit: the phone was told already
    return str(content.get("body") or "")


def _find_adapter_class():
    for name, mod in list(sys.modules.items()):
        if name.endswith(ADAPTER_MODULE_SUFFIX) and hasattr(mod, "MatrixAdapter"):
            return mod.MatrixAdapter
    return None


def patch(cls, conf=None) -> None:
    if getattr(cls, "_logos_alert_text", False) or not hasattr(cls, "_send_room_message"):
        return
    conf = conf or settings()
    if not all(conf.get(k) for k in (TOKEN_ENV, URL_ENV, AGENT_ENV)):
        logger.info("alert text: not set up for this agent; alerts say \"New message\"")
        return
    original = cls._send_room_message

    async def _send_room_message(self, chat_id, msg_content, *args, **kwargs):
        event_id = await original(self, chat_id, msg_content, *args, **kwargs)
        text = text_of(msg_content)
        if event_id and text:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, note, str(event_id), text, conf)    # after sending; never waited on
        return event_id

    cls._send_room_message = _send_room_message
    cls._logos_alert_text = True
    logger.info("Matrix: phone alerts for this agent's replies say what it wrote")


def _patch_when_loaded(timeout: float = 600.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        cls = _find_adapter_class()
        if cls is not None:
            patch(cls)
            return
        time.sleep(0.2)
    logger.error("Matrix: the adapter never loaded; alerts say \"New message\"")


def register(ctx):
    cls = _find_adapter_class()
    if cls is not None:
        patch(cls)
    else:
        threading.Thread(target=_patch_when_loaded, daemon=True, name="logos-alert-text").start()
