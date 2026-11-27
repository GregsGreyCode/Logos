"""Each agent in its own voice on the house speaker (Voice V1.2).

Home Assistant reads an agent's answer aloud by asking the speaker voices container
(gateway/tts.py) to say the words. That request names no agent, so this module tells the
container who said what:

- **Who said it.** :func:`said` is called by ``voice_converse`` with every answer it returns (and by
  ``speaker_say`` with every announcement). It returns the words with an invisible mark in front
  that names the voice (``tts.mark``, V3.3): Home Assistant caches spoken phrases by their exact
  text, so the mark keeps one agent's "Done." from coming back in another's voice, and the container
  reads the voice straight from it. As a fallback it also keeps, in memory for two minutes, a hash
  of the words and the voice, written to the one file the container reads (``said.json``,
  gateway/tts_host.py). Hashes only: the words are not written anywhere.
- **Which language.** Words in French (gateway/speech_lang.py) are read by the agent's French voice
  (V3.4): the one chosen in Settings, else ``speaker_voice_fr:``, else Tom for an agent with a man's
  voice and Siwis for a woman's.
- **Which voice.** The one the agent's person chose in Settings › House speaker (any of the agent's
  owners; for Home either of you), else ``speaker_voice:`` in ``agents/<name>.yaml``, else Home's
  voice (``tts.DEFAULT_VOICE``). The choice is kept with the agents' colours (table ``agent_voices``).

    GET  /api/v2/speaker                       the voices and your agents' choices
    PUT  /api/v2/agents/<name>/speaker-voice   {"voice": id} or {"voice_fr": id} ("" = back to the definition's)
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

from gateway import speech_lang, tts, tts_host

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


def english(vid: Any) -> bool:
    return vid in tts.VOICES and tts.language(vid) == "en"


def french(vid: Any) -> bool:
    return vid in tts.VOICES and tts.language(vid) == "fr"


def default_of(definition: Mapping[str, Any]) -> str:
    voice = (definition or {}).get("speaker_voice")
    return voice if english(voice) else tts.DEFAULT_VOICE


def default_fr_of(definition: Mapping[str, Any], english_voice: Optional[str] = None) -> str:
    """``speaker_voice_fr:``, else a French voice like its English one (a man's or a woman's)."""
    voice = (definition or {}).get("speaker_voice_fr")
    if french(voice):
        return voice
    who = tts.VOICES.get(english_voice or default_of(definition), ("", "", "man"))[2]
    return tts.DEFAULT_FRENCH.get(who, tts.DEFAULT_FRENCH["man"])


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


def voice_of(app: Mapping, agent: str, lang: str = "en") -> str:
    """The agent's voice for ``lang`` ("en", or "fr" for answers in French)."""
    hit = _cache.get(f"{agent}:{lang}")
    if hit and time.monotonic() - hit[1] < CACHE_S:
        return hit[0]
    row = _choices().get(agent) or {}
    definition = None
    voice = row.get("voice")
    if not english(voice):
        definition = _definition(app, agent)
        voice = default_of(definition)
    if lang == "fr":
        chosen = row.get("voice_fr")
        voice = chosen if french(chosen) else default_fr_of(
            definition if definition is not None else _definition(app, agent), voice)
    _cache[f"{agent}:{lang}"] = (voice, time.monotonic())
    return voice


def _forget(agent: str) -> None:
    for k in [k for k in _cache if k.split(":", 1)[0] in (agent, _home_agent())]:
        _cache.pop(k, None)


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


def said(app: Mapping, agent: str, text: str, env: Optional[Mapping[str, str]] = None) -> str:
    """``agent`` is about to have ``text`` read out: the words to hand Home Assistant, with the
    invisible mark of the agent's voice in front (in French when the words are French). Also noted
    by hash for a container that meets the words without their mark. Never raises: the answer
    matters more than its voice (the words come back unmarked)."""
    try:
        voice = voice_of(app, agent, speech_lang.detect(text))
        now = time.time()
        _said[:] = [r for r in _said if r["until"] >= now][-(MAX_SAID - 1):]
        _said.append({"key": tts.key(text), "agent": agent, "voice": voice, "until": now + TTL_S})
    except Exception as e:
        logger.warning("speaker voice: could not note who answered: %s", e)
        return text
    try:
        write(app, env)
    except Exception as e:
        logger.warning("speaker voice: could not write %s: %s", tts_host.STATE_FILE, e)
    return tts.mark(text, voice)


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
    return [{"id": vid, "name": name, "accent": accent, "who": who, "lang": tts.language(vid)}
            for vid, (name, accent, who) in tts.VOICES.items()]


async def handle_speaker(request: "Any") -> "Any":
    """GET /api/v2/speaker -> {"on", "voices": [...], "agents": [{name, display_name, shared, speaks,
    voice, default, voice_fr, default_fr, set_by}]}: the agents this person may use."""
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
        voice = chosen.get("voice") if english(chosen.get("voice")) else default
        default_fr = default_fr_of(definition, voice)
        out.append({"name": name, "display_name": str(definition.get("display_name") or name),
                    "shared": info["owners"] is None or len(info["owners"]) > 1,
                    "speaks": name == home or tts_host.uses_speaker(definition),
                    "voice": voice, "default": default,
                    "voice_fr": chosen.get("voice_fr") if french(chosen.get("voice_fr")) else default_fr,
                    "default_fr": default_fr,
                    "set_by": chosen.get("set_by") if english(chosen.get("voice")) or french(chosen.get("voice_fr")) else None})
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
    body = body if isinstance(body, dict) else {}
    lang = "fr" if "voice_fr" in body else "en"
    voice = str(body.get("voice_fr" if lang == "fr" else "voice") or "")
    if voice and not (french(voice) if lang == "fr" else english(voice)):
        return web.json_response({"error": "unknown_voice"}, status=400)
    if lang == "fr":
        await asyncio.to_thread(auth_db.set_agent_voice, name, voice, user["username"], "fr")
    else:
        await asyncio.to_thread(auth_db.set_agent_voice, name, voice, user["username"])
    _forget(name)
    if lang == "fr":
        return web.json_response({"name": name, "voice_fr": voice or default_fr_of(info["definition"])})
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
