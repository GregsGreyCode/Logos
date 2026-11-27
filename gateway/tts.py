"""The house speaker's voices: Piper text-to-speech for Home Assistant, a voice for each agent (Voice V1.2).

Home Assistant reads the agents' answers aloud by asking this server, over the Wyoming protocol
(the ``wyoming`` package; Home Assistant adds it under Settings > Devices & services > Wyoming
Protocol). A request carries only the words and the voice Home Assistant's pipeline names, not
which agent answered. The dashboard knows: when ``POST /api/v2/voice/converse`` returns an answer
it notes a hash of the words and the voice that agent's person chose (gateway/speaker_voice.py) in
a small file this container can read. So:

- **Words an agent has just said** (within two minutes): that agent's voice.
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
# speakers in one model. id -> (name, accent, who). Keep in step with infra/tts/Dockerfile.
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
}
DEFAULT_VOICE = "en_GB-northern_english_male-medium"     # Home's, chosen by Greg in Home Assistant


def key(text: str) -> str:
    """The same for the same words, whatever Home Assistant does to spaces, case or punctuation."""
    words = re.findall(r"\w+", re.sub(r"['’]", "", str(text or "").casefold()))
    return hashlib.sha256(" ".join(words).encode()).hexdigest()


def choose(text: str, requested: Optional[str], state: Mapping, now: Optional[float] = None) -> Tuple[str, Optional[str]]:
    """(voice, agent) for these words. ``state`` is what the dashboard wrote: ``{"default": voice,
    "said": [{"key", "agent", "voice", "until"}]}``; ``agent`` is None when no agent said them."""
    now = time.time() if now is None else now
    k = key(text)
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
                voice = PiperVoice.load(os.path.join(self.folder, vid + ".onnx"))
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
            started = time.monotonic()
            async with lock:                     # one at a time
                voice = await asyncio.to_thread(voices.get, vid)
                chunks = iter(voice.synthesize(text or "."))   # a generator: the work happens in next()
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
