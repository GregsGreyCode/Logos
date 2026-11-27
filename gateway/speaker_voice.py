"""Each agent in its own voice on the house speaker (Voice V1.2).

Home Assistant reads an agent's answer aloud by asking the speaker voices container
(gateway/tts.py) to say the words. That request names no agent, so this module tells the
container who said what:

- **Who said it.** :func:`said` is called by ``voice_converse`` with every answer it returns. It
  keeps, in memory for two minutes, a hash of the words and the voice of the agent that said them,
  and writes them to the one file the container reads (``said.json``, gateway/tts_host.py). Hashes
  only: the words are not written anywhere.
- **Which voice.** The one the agent's person chose in Settings › House speaker (any of the agent's
  owners; for Home either of you), else ``speaker_voice:`` in ``agents/<name>.yaml``, else Home's
  voice (``tts.DEFAULT_VOICE``). The choice is kept with the agents' colours (table ``agent_voices``).

    GET  /api/v2/speaker                       the voices and your agents' choices
    PUT  /api/v2/agents/<name>/speaker-voice   {"voice": id} ("" = back to the definition's)
    GET  /api/v2/speaker/sample?voice=<id>     a short sample, as WAV, for the play buttons

The samples are asked for over Wyoming like Home Assistant does, with a few lines of stdlib here
(the gateway itself has no Wyoming or Piper; they live in the container).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
import wave
from typing import Any, Dict, List, Mapping, Optional, Tuple

from gateway import tts, tts_host

logger = logging.getLogger(__name__)

TTL_S = 120              # Home Assistant asks for the audio within a second or two of the answer
MAX_SAID = 50
CACHE_S = 60             # a voice chosen in Settings is used at once (the cache is cleared); a yaml edit within a minute
SAMPLE_TEXT = "Hello! This is how I sound on the house speaker."
SAMPLE_WAIT_S = 20.0
MAX_AUDIO = 20 * 1024 * 1024

_said: List[dict] = []
_cache: Dict[str, Tuple[str, float]] = {}
_samples: Dict[str, bytes] = {}


def default_of(definition: Mapping[str, Any]) -> str:
    voice = (definition or {}).get("speaker_voice")
    return voice if voice in tts.VOICES else tts.DEFAULT_VOICE


def _choices() -> Dict[str, dict]:
    import gateway.auth.db as auth_db
    getter = getattr(auth_db, "get_agent_voices", None)
    try:
        return getter() if getter else {}
    except Exception:            # no database yet (tests, first start)
        return {}


def _definition(app: Mapping, agent: str) -> Mapping[str, Any]:
    from gateway import agent_chat
    runtime = agent_chat.runtime_of(app.get("sandbox_backend"))
    try:
        return runtime.load(agent) if runtime is not None else {}
    except Exception:
        return {}


def voice_of(app: Mapping, agent: str) -> str:
    hit = _cache.get(agent)
    if hit and time.monotonic() - hit[1] < CACHE_S:
        return hit[0]
    chosen = (_choices().get(agent) or {}).get("voice")
    voice = chosen if chosen in tts.VOICES else default_of(_definition(app, agent))
    _cache[agent] = (voice, time.monotonic())
    return voice


def _home_agent() -> str:
    return os.environ.get("LOGOS_VOICE_AGENT") or "household"


def write(app: Mapping, env: Optional[Mapping[str, str]] = None) -> bool:
    """Write what the container reads. Nothing to do (False) where the speaker voices are not set up."""
    folder = tts_host.state_dir(env)
    if not folder.is_dir():
        return False
    data = {"default": voice_of(app, _home_agent()), "said": _said}
    tmp = folder / (tts_host.STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o644)
    os.replace(tmp, folder / tts_host.STATE_FILE)
    return True


def said(app: Mapping, agent: str, text: str, env: Optional[Mapping[str, str]] = None) -> None:
    """``voice_converse`` returned ``text`` as ``agent``'s answer: when Home Assistant asks for those
    words, use that agent's voice. Never raises: the answer matters more than its voice."""
    try:
        now = time.time()
        _said[:] = [r for r in _said if r["until"] >= now][-(MAX_SAID - 1):]
        _said.append({"key": tts.key(text), "agent": agent, "voice": voice_of(app, agent), "until": now + TTL_S})
        write(app, env)
    except Exception as e:
        logger.warning("speaker voice: could not note who answered: %s", e)


# ── Samples: a tiny Wyoming client ───────────────────────────────────────────────────────────

async def synthesize(address: tuple, text: str, voice: str, wait: float = SAMPLE_WAIT_S) -> Tuple[tuple, bytes]:
    """``((rate, width, channels), pcm)`` for ``text`` in ``voice``, asked of the Wyoming server."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(*address), wait)
    try:
        event = {"type": "synthesize", "data": {"text": text, "voice": {"name": voice}}}
        writer.write(json.dumps(event).encode() + b"\n")
        await writer.drain()
        fmt, pcm = None, bytearray()
        deadline = time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            line = await asyncio.wait_for(reader.readline(), max(left, 0.1))
            if not line:
                raise ConnectionError("the speaker voices closed the connection")
            head = json.loads(line)
            data = dict(head.get("data") or {})
            if head.get("data_length"):
                data.update(json.loads(await reader.readexactly(int(head["data_length"]))))
            payload = await reader.readexactly(int(head["payload_length"])) if head.get("payload_length") else b""
            kind = head.get("type")
            if kind in ("audio-start", "audio-chunk") and "rate" in data:
                fmt = fmt or (int(data["rate"]), int(data["width"]), int(data["channels"]))
            if kind == "audio-chunk":
                pcm += payload
                if len(pcm) > MAX_AUDIO:
                    raise ValueError("sample too long")
            elif kind == "audio-stop":
                return fmt or (22050, 2, 1), bytes(pcm)
            elif kind == "error":
                raise ConnectionError(str(data.get("text") or "the speaker voices failed"))
    finally:
        writer.close()


def wav(fmt: tuple, pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setframerate(fmt[0])
        w.setsampwidth(fmt[1])
        w.setnchannels(fmt[2])
        w.writeframes(pcm)
    return out.getvalue()


def _settings() -> Tuple[Optional[dict], str]:
    try:
        return tts_host.settings(), ""
    except tts_host.TtsError as e:
        return None, str(e)


# ── The dashboard ───────────────────────────────────────────────────────────────────────────

def voice_list() -> List[dict]:
    return [{"id": vid, "name": name, "accent": accent, "who": who} for vid, (name, accent, who) in tts.VOICES.items()]


async def handle_speaker(request: "Any") -> "Any":
    """GET /api/v2/speaker -> {"on", "voices": [...], "agents": [{name, display_name, shared, speaks,
    voice, default, set_by}]}: the agents this person may use."""
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    agents = await d._visible_agents(request, user["username"])
    choices = await asyncio.to_thread(_choices)
    conf, problem = await asyncio.to_thread(_settings)
    home = _home_agent()
    out = []
    for name, info in sorted(agents.items(), key=lambda kv: (kv[0] != home, kv[0])):
        definition = info["definition"]
        chosen = (choices.get(name) or {})
        default = default_of(definition)
        out.append({"name": name, "display_name": str(definition.get("display_name") or name),
                    "shared": info["owners"] is None or len(info["owners"]) > 1,
                    "speaks": name == home or tts_host.uses_speaker(definition),
                    "voice": chosen.get("voice") if chosen.get("voice") in tts.VOICES else default,
                    "default": default, "set_by": chosen.get("set_by") if chosen.get("voice") in tts.VOICES else None})
    return web.json_response({"on": conf is not None, "problem": problem, "voices": voice_list(), "agents": out})


async def handle_agent_voice(request: "Any") -> "Any":
    """PUT /api/v2/agents/{name}/speaker-voice {voice}: only the people who may use the agent."""
    from aiohttp import web
    import gateway.auth.db as auth_db
    from gateway import dashboard_api as d
    name, info, user = await d._agent_or_404(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    voice = str((body or {}).get("voice") or "")
    if voice and voice not in tts.VOICES:
        return web.json_response({"error": "unknown_voice"}, status=400)
    await asyncio.to_thread(auth_db.set_agent_voice, name, voice, user["username"])
    _cache.pop(name, None)
    _cache.pop(_home_agent(), None)
    return web.json_response({"name": name, "voice": voice or default_of(info["definition"])})


async def handle_sample(request: "Any") -> "Any":
    """GET /api/v2/speaker/sample?voice=<id> -> audio/wav, made once per voice and kept in memory."""
    from aiohttp import web
    voice = request.query.get("voice", "")
    if voice not in tts.VOICES:
        return web.json_response({"error": "unknown_voice"}, status=400)
    if voice not in _samples:
        conf, problem = await asyncio.to_thread(_settings)
        if conf is None:
            return web.json_response({"error": "speaker_off", "message": problem or
                                      "The speaker voices are not switched on on this Logos yet."}, status=503)
        try:
            fmt, pcm = await synthesize(tts_host.client_address(conf), SAMPLE_TEXT, voice)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            logger.warning("speaker voice: no sample of %s: %s", voice, type(e).__name__)
            return web.json_response({"error": "speaker_unreachable",
                                      "message": "The speaker voices did not answer."}, status=503)
        _samples[voice] = wav(fmt, pcm)
    return web.Response(body=_samples[voice], content_type="audio/wav",
                        headers={"Cache-Control": "private, max-age=86400"})


async def _clear(app: "Any") -> None:
    _said.clear()
    try:
        await asyncio.to_thread(write, app)
    except Exception as e:
        logger.warning("speaker voice: could not reset %s: %s", tts_host.STATE_FILE, e)


def register(app: "Any") -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/speaker", handle_speaker)
    app.router.add_get("/api/v2/speaker/sample", handle_sample)
    app.router.add_put("/api/v2/agents/{name}/speaker-voice", require_csrf(handle_agent_voice))
    app.on_startup.append(_clear)
