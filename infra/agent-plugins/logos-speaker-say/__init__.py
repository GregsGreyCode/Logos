"""logos-speaker-say: the agent can say something out loud on the house speaker (Voice V3.1).

A tool, ``say_on_speaker``, for a reminder that is due, the morning briefing, or anything the
person asked to hear at home. The words go to Logos over the one dashboard path this sandbox may
reach (``LOGOS_SPEAKER_SAY_URL``, policy rule ``speaker_say``), signed with the agent's own token
(``LOGOS_SPEAKER_SAY_TOKEN``). Logos decides (gateway/speaker_say.py): quiet hours, private details
only while that person's phone is home, short, not too often. When it is not said, the tool's answer
says why and that it belongs in the chat instead.

Offered only to agents that speak on the speaker (the token is in their .env). Nothing here logs
the words.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("logos.speaker_say")
HOME = os.environ.get("HERMES_HOME", "/opt/data")
TOKEN_ENV, URL_ENV, AGENT_ENV = "LOGOS_SPEAKER_SAY_TOKEN", "LOGOS_SPEAKER_SAY_URL", "LOGOS_AGENT_NAME"

SCHEMA = {
    "name": "say_on_speaker",
    "description": (
        "Say something out loud on the house speaker, in your own voice, without being asked: a reminder "
        "that is due, the morning briefing, or something your person asked to hear at home. One or two "
        "short sentences, plain words. Set private to true when it is about someone's own things (their "
        "calendar, email, messages, notes) and name whose in person: it is then said only while their "
        "phone is at home. It is never said in quiet hours. When it is not said, the answer tells you "
        "why: then put it in the chat instead. Always send your usual chat message as well."),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to say, as it should be heard (no Markdown)."},
            "private": {"type": "boolean", "description": "true for someone's own things; false for shared household things."},
            "person": {"type": "string", "description": "Whose it is, when private: their username (greg, bere)."},
            "chime": {"type": "boolean", "description": "Play the speaker's chime first (default true)."},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


def settings() -> dict:
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


def available() -> bool:
    return all(settings().values())


def handle(args, **_kw) -> str:
    args = args or {}
    conf = settings()
    if not all(conf.values()):
        return json.dumps({"spoken": False, "tell": "The house speaker is not set up for you. Put it in the chat."})
    body = {"agent": conf[AGENT_ENV], "text": str(args.get("text") or ""),
            "private": bool(args.get("private")), "chime": args.get("chime") is not False}
    # The token goes in the Authorization header (E7: the only place OpenShell puts the real value in
    # for a placeholder); a real token also in the body, for an older Logos.
    if not conf[TOKEN_ENV].startswith("openshell:resolve:"):
        body["token"] = conf[TOKEN_ENV]
    if isinstance(args.get("person"), str) and args["person"].strip():
        body["person"] = args["person"].strip()
    req = urllib.request.Request(conf[URL_ENV], data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + conf[TOKEN_ENV]})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:      # never the words, only that it failed
        logger.info("speaker say: Logos did not answer (%s)", type(e).__name__)
        return json.dumps({"spoken": False, "tell": "Logos did not answer, so it was not said. Put it in the chat instead."})
    if out.get("spoken"):
        return json.dumps({"spoken": True, "note": "Said on the house speaker. Still send your usual chat message."})
    return json.dumps({"spoken": False, "reason": out.get("reason") or out.get("error") or "refused",
                       "tell": out.get("tell") or "It was not said. Put it in the chat instead."})


def register(ctx):
    ctx.register_tool(name="say_on_speaker", toolset="logos_speaker", schema=SCHEMA, handler=handle,
                      check_fn=available, emoji="🔊")
