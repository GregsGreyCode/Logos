"""logos-voice: the agent can turn voice-note transcripts on and off itself (docs/voice.md).

hermes posts the text of a voice note before the answer when ``stt.echo_transcripts`` is on.
It reads that setting only once, when the gateway starts, and Logos writes it from the agent's
yaml (``voice_echo``). This plugin adds:

- a tool, ``voice_transcripts``, so the person can just say "show me what you heard" or "stop
  sending the transcripts". The choice is kept in ``/opt/data/voice-echo.json`` in the agent's
  home, so it survives restarts and rebuilds, and from then on wins over the yaml;
- a small patch so hermes checks that choice on every voice note instead of only at start-up,
  so a change applies from the next voice note, with no restart.

It is offered only to agents with voice notes on (the voice service token is in their .env).
Nothing here reads or logs message text.
"""
from __future__ import annotations

import json
import os
import time

HOME = os.environ.get("HERMES_HOME", "/opt/data")
STORE = os.path.join(HOME, "voice-echo.json")
TOKEN_ENV = "LOGOS_VOICE_TOKEN"

SCHEMA = {
    "name": "voice_transcripts",
    "description": (
        "Turn on or off the text of each voice note that is posted in the chat before your answer, "
        "so your person can see what was understood. Use it when your person asks to see (or stop "
        "seeing) the transcripts of their voice notes. Leave out 'on' to only check the setting."),
    "parameters": {
        "type": "object",
        "properties": {"on": {"type": "boolean",
                              "description": "true: post each transcript; false: answer only."}},
        "additionalProperties": False,
    },
}


def chosen():
    """The person's choice (True/False), or None when they never made one."""
    try:
        with open(STORE, encoding="utf-8") as f:
            value = json.load(f).get("on")
        return value if isinstance(value, bool) else None
    except (OSError, ValueError, AttributeError):
        return None


def choose(on: bool) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"on": bool(on), "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    os.replace(tmp, STORE)


def has_voice() -> bool:
    if os.environ.get(TOKEN_ENV):
        return True
    try:
        with open(os.path.join(HOME, ".env"), encoding="utf-8") as f:
            return any(line.startswith(TOKEN_ENV + "=") and line.strip() != TOKEN_ENV + "=" for line in f)
    except OSError:
        return False


def handle(args, **_kw) -> str:
    on = (args or {}).get("on")
    if isinstance(on, bool):
        try:
            choose(on)
        except OSError as e:
            return json.dumps({"error": f"Not changed: {e}"})
        return json.dumps({"transcripts": "on" if on else "off",
                           "note": "Applies from the next voice note. Tell your person in one line."})
    current = chosen()
    return json.dumps({"transcripts": "unknown (the start-up setting applies)" if current is None
                       else ("on" if current else "off")})


def patch_gateway() -> bool:
    """Make hermes ask :func:`chosen` on every voice note, falling back to its start-up setting."""
    try:
        from gateway.run_voice import GatewayVoiceMixin
    except Exception:
        return False
    original = GatewayVoiceMixin._should_echo_stt_transcripts
    if getattr(original, "_logos_voice", False):
        return True

    def _should_echo_stt_transcripts(self) -> bool:
        value = chosen()
        return original(self) if value is None else value

    _should_echo_stt_transcripts._logos_voice = True
    GatewayVoiceMixin._should_echo_stt_transcripts = _should_echo_stt_transcripts
    return True


def register(ctx):
    patch_gateway()
    ctx.register_tool(name="voice_transcripts", toolset="logos_voice", schema=SCHEMA, handler=handle,
                      check_fn=has_voice, emoji="📝")
