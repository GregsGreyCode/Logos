"""The house speaker's voices: Piper text-to-speech for Home Assistant, a voice for each agent (Voice V1.2).

Home Assistant reads the agents' answers aloud by asking this server, over the Wyoming protocol
(the ``wyoming`` package; Home Assistant adds it under Settings > Devices & services > Wyoming
Protocol). A request carries only the words and the voice Home Assistant's pipeline names, not
which agent answered. The dashboard knows: when ``POST /api/v2/voice/converse`` returns an answer
it notes a hash of the words and the voice that agent's person chose (gateway/speaker_voice.py) in
a small file this container can read. So:

- **Marked words** (the usual case, V3.3): the dashboard puts an invisible mark at the start of
  every answer it hands Home Assistant (:func:`mark`): a few zero-width characters that spell the
  voice. They make the same words from two agents different words to Home Assistant's phrase
  cache (its key is a SHA-1 of the exact text), and they name the voice directly. They are taken
  off before Piper reads the words.
- **Words an agent has just said** (within two minutes) without a mark (Home Assistant took it
  off, or an older dashboard): that agent's voice, found by a hash of the words.
- **Anything else** (Home Assistant's own announcements, a sample from Settings): the voice the
  request names, when it is one of ours, else Home's.

- It only speaks. Nothing else is answered; Wyoming has no login, so it is reachable only where
  ``~/.config/logos/speaker`` says (gateway/tts_host.py).
- The words are never logged or kept: the log line has the voice, the agent, the length and the time.
- One request at a time. A voice is loaded when first used (about a second) and dropped after ten
  quiet minutes; at most two are kept loaded.

Runs in the ``logos-tts`` container (infra/tts/Dockerfile). Stdlib only apart from ``wyoming`` and
``piper``, imported in :func:`main`, so the choosing can be tested without them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from typing import Mapping, Optional, Tuple

PORT = 10200                 # Wyoming's usual port for text-to-speech
MAX_TEXT = 2000              # an answer is at most about 700 characters (voice_converse.MAX_SPOKEN)
KEEP_LOADED = 2
IDLE_S = 600

# The English "medium" voices Home Assistant's Piper add-on offers, apart from those with many
# speakers in one model, and four French ones (V3.4, for Béré; UPMC is one model with two speakers).
# id -> (name, accent, who). Keep the model files in step with infra/tts/Dockerfile.
VOICES = {
    "en_GB-alan-medium": ("Alan", "British", "man"),
    "en_GB-alba-medium": ("Alba", "Scottish", "woman"),
    "en_GB-cori-medium": ("Cori", "British", "woman"),
    "en_GB-jenny_dioco-medium": ("Jenny", "British", "woman"),
    "en_GB-northern_english_male-medium": ("Northern English", "British", "man"),
    "en_US-amy-medium": ("Amy", "American", "woman"),
    "en_US-hfc_female-medium": ("HFC female", "American", "woman"),
    "en_US-hfc_male-medium": ("HFC male", "American", "man"),
    "en_US-joe-medium": ("Joe", "American", "man"),
    "en_US-kristin-medium": ("Kristin", "American", "woman"),
    "en_US-lessac-medium": ("Lessac", "American", "woman"),
    "en_US-ryan-medium": ("Ryan", "American", "man"),
    "fr_FR-siwis-medium": ("Siwis", "French", "woman"),
    "fr_FR-tom-medium": ("Tom", "French", "man"),
    "fr_FR-upmc_jessica-medium": ("Jessica", "French", "woman"),
    "fr_FR-upmc_pierre-medium": ("Pierre", "French", "man"),
}
DEFAULT_VOICE = "en_GB-northern_english_male-medium"     # Home's, chosen by Greg in Home Assistant
#: voice id -> (model file, speaker) where they differ (one Piper model with two speakers).
MODELS = {"fr_FR-upmc_jessica-medium": ("fr_FR-upmc-medium", 0), "fr_FR-upmc_pierre-medium": ("fr_FR-upmc-medium", 1)}
#: The French voice an agent has until someone chooses one: a man's for a man's voice, else a woman's.
DEFAULT_FRENCH = {"man": "fr_FR-tom-medium", "woman": "fr_FR-siwis-medium"}


def language(vid: str) -> str:
    """"en" or "fr", from the voice id."""
    return str(vid or "").split("_", 1)[0]


def model_of(vid: str) -> Tuple[str, Optional[int]]:
    """(model file without .onnx, speaker number or None)."""
    return MODELS.get(vid, (vid, None))


def model_files() -> list:
    """The Piper models the image must hold, in order, once each."""
    out: list = []
    for vid in VOICES:
        m = model_of(vid)[0]
        if m not in out:
            out.append(m)
    return out


# ── The invisible mark (V3.3) ────────────────────────────────────────────────────────────────
# Home Assistant caches spoken audio by its exact words, so "Done." said by Finn came back in Home's
# voice once Home had said it. A mark at the start, made of zero-width characters, makes each
# voice's words different to that cache without changing what is heard, and names the voice.
# Python's str.strip() and split() leave these characters alone (they are not whitespace), and Home
# Assistant hands the answer to the text-to-speech service as it is (its 2026.11 source: the cache
# key is sha1(message), and the Wyoming service is sent Synthesize(text=message)).
#: Append-only: a voice keeps its number for ever, so a mark in Home Assistant's cache stays right.
CODES = ("en_GB-alan-medium", "en_GB-alba-medium", "en_GB-cori-medium", "en_GB-jenny_dioco-medium",
         "en_GB-northern_english_male-medium", "en_US-amy-medium", "en_US-hfc_female-medium",
         "en_US-hfc_male-medium", "en_US-joe-medium", "en_US-kristin-medium", "en_US-lessac-medium",
         "en_US-ryan-medium", "fr_FR-siwis-medium", "fr_FR-tom-medium", "fr_FR-upmc_jessica-medium",
         "fr_FR-upmc_pierre-medium")
FRAME = "⁣"                                 # INVISIBLE SEPARATOR, before and after the digits
DIGITS = "​‌‍⁠"              # zero width space, non-joiner, joiner, word joiner
_MARK = re.compile(f"{FRAME}([{DIGITS}]{{3}}){FRAME}")
_INVISIBLE = re.compile(f"[{FRAME}{DIGITS}﻿]")


def mark(text: str, vid: str) -> str:
    """``text`` with the invisible mark of voice ``vid`` in front (unchanged for an unknown voice)."""
    if vid not in CODES:
        return str(text or "")
    n = CODES.index(vid)
    return FRAME + "".join(DIGITS[(n >> s) & 3] for s in (4, 2, 0)) + FRAME + str(text or "")


def unmark(text: str) -> Tuple[str, Optional[str]]:
    """(the words without any invisible characters, the voice the mark names or None)."""
    text = str(text or "")
    vid = None
    m = _MARK.search(text)
    if m:
        n = sum(DIGITS.index(c) << s for c, s in zip(m.group(1), (4, 2, 0)))
        vid = CODES[n] if n < len(CODES) and CODES[n] in VOICES else None
    return _INVISIBLE.sub("", text), vid


def key(text: str) -> str:
    """The same for the same words, whatever Home Assistant does to spaces, case or punctuation."""
    words = re.findall(r"\w+", re.sub(r"['’]", "", str(text or "").casefold()))
    return hashlib.sha256(" ".join(words).encode()).hexdigest()


def choose(text: str, requested: Optional[str], state: Mapping, now: Optional[float] = None) -> Tuple[str, Optional[str]]:
    """(voice, agent) for these words. ``state`` is what the dashboard wrote: ``{"default": voice,
    "said": [{"key", "agent", "voice", "until"}]}``; ``agent`` is None when no agent said them."""
    now = time.time() if now is None else now
    text, marked = unmark(text)
    k = key(text)
    if marked:
        for row in reversed(list(state.get("said") or [])):     # only to name the agent in the log
            if isinstance(row, Mapping) and row.get("key") == k and row.get("voice") == marked:
                return marked, str(row.get("agent") or "") or None
        return marked, "an agent"
    for row in reversed(list(state.get("said") or [])):
        if isinstance(row, Mapping) and row.get("key") == k and float(row.get("until") or 0) >= now \
                and row.get("voice") in VOICES:
            return str(row["voice"]), str(row.get("agent") or "") or None
    if requested in VOICES:
        return str(requested), None
    default = state.get("default")
    return (str(default) if default in VOICES else DEFAULT_VOICE), None


def read_state(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def quiet_onnxruntime() -> None:
    """Recent onnxruntime builds collect usage telemetry: off (and the container has no DNS)."""
    try:
        import onnxruntime
        onnxruntime.disable_telemetry_events()
    except Exception:
        pass


class Voices:
    """Piper voices, loaded when first needed; the least recently used goes when a third is asked for."""

    def __init__(self, folder: str, keep: int = KEEP_LOADED, idle_s: int = IDLE_S):
        self.folder, self.keep, self.idle_s = folder, keep, idle_s
        self._loaded: dict = {}          # voice id -> [PiperVoice, last used]
        self._lock = threading.Lock()
        if idle_s > 0:
            threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            with self._lock:
                for vid in [v for v, (_, t) in self._loaded.items() if time.monotonic() - t > self.idle_s]:
                    del self._loaded[vid]
                    print(f"{vid}: unloaded after a quiet spell", flush=True)

    def get(self, vid: str):
        with self._lock:
            if vid not in self._loaded:
                from piper import PiperVoice
                started = time.monotonic()
                voice = PiperVoice.load(os.path.join(self.folder, model_of(vid)[0] + ".onnx"))
                while len(self._loaded) >= self.keep:
                    del self._loaded[min(self._loaded, key=lambda v: self._loaded[v][1])]
                self._loaded[vid] = [voice, 0.0]
                print(f"{vid}: loaded in {time.monotonic() - started:.1f} s", flush=True)
            self._loaded[vid][1] = time.monotonic()
            return self._loaded[vid][0]


def info_event():
    from wyoming.info import Attribution, Info, TtsProgram, TtsVoice
    piper = Attribution(name="rhasspy", url="https://github.com/OHF-Voice/piper1-gpl")
    voices = [TtsVoice(name=vid, description=f"{name} ({accent} {who})", attribution=piper, installed=True,
                       version=None, languages=[vid.split("-", 1)[0]])
              for vid, (name, accent, who) in VOICES.items()]
    return Info(tts=[TtsProgram(name="logos", description="Logos: each agent in its own voice",
                                attribution=Attribution(name="Logos", url="https://github.com/GregsGreyCode/Logos"),
                                installed=True, version="1", voices=voices)]).event()


def make_handler(voices: Voices, state_path: str, lock):
    import asyncio
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.info import Describe
    from wyoming.ping import Ping, Pong
    from wyoming.server import AsyncEventHandler
    from wyoming.tts import Synthesize

    info = info_event()

    class Handler(AsyncEventHandler):
        async def handle_event(self, event) -> bool:
            if Describe.is_type(event.type):
                await self.write_event(info)
                return True
            if Ping.is_type(event.type):
                await self.write_event(Pong().event())
                return True
            if not Synthesize.is_type(event.type):
                return False                     # it only speaks: anything else ends the connection
            syn = Synthesize.from_event(event)
            text = " ".join(str(syn.text or "").split())[:MAX_TEXT]
            vid, agent = choose(text, syn.voice.name if syn.voice else None, read_state(state_path))
            text = unmark(text)[0].strip()       # Piper never sees the mark
            started = time.monotonic()
            async with lock:                     # one at a time
                voice = await asyncio.to_thread(voices.get, vid)
                config = None
                if model_of(vid)[1] is not None:
                    from piper import SynthesisConfig
                    config = SynthesisConfig(speaker_id=model_of(vid)[1])
                # A generator: the work happens in next().
                chunks = iter(voice.synthesize(text or ".", syn_config=config))
                first = True
                while True:
                    chunk = await asyncio.to_thread(next, chunks, None)
                    if chunk is None:
                        break
                    if first:
                        await self.write_event(AudioStart(rate=chunk.sample_rate, width=chunk.sample_width,
                                                          channels=chunk.sample_channels).event())
                        first = False
                    await self.write_event(AudioChunk(audio=chunk.audio_int16_bytes, rate=chunk.sample_rate,
                                                      width=chunk.sample_width, channels=chunk.sample_channels).event())
                if first:                        # nothing to say: an empty, well-formed answer
                    await self.write_event(AudioStart(rate=22050, width=2, channels=1).event())
                await self.write_event(AudioStop().event())
            print(f"{vid} for {agent or 'Home Assistant'}: {len(text)} characters in "
                  f"{time.monotonic() - started:.1f} s", flush=True)
            return True

    return Handler


def main() -> int:
    import asyncio
    from wyoming.server import AsyncServer

    quiet_onnxruntime()
    voices = Voices(os.environ.get("TTS_VOICES", "/voices"))
    state = os.environ.get("TTS_STATE", "/state/said.json")
    port = int(os.environ.get("TTS_PORT", str(PORT)))

    async def run() -> None:
        handler = make_handler(voices, state, asyncio.Lock())
        server = AsyncServer.from_uri(f"tcp://0.0.0.0:{port}")
        print(f"speaker voices: listening on {port} with {len(VOICES)} voices", flush=True)
        await server.run(handler)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
