"""The house speaker's wake words: each agent's name (Voice VW.1, "Logos streaming").

The speaker streams what it hears to Home Assistant, and Home Assistant streams it here over the
Wyoming protocol, as a wake word service (Settings > Devices & services > Wyoming Protocol, then
the pipeline's "streaming wake word"). This server listens for the agents' names with sherpa-onnx's
open-vocabulary keyword spotter: a name is spelled into the model's word pieces, nothing is trained,
so a new or changed name works as soon as the file below changes.

- **Which names.** ``names.json``, written by the dashboard side (gateway/wake_host.py) from the
  agents' ``speaker_name:``: ``{"phrases": [{"id", "phrase", "agent", "threshold"}]}``. Read again
  whenever it changes; a connection in progress keeps the names it started with.
- **Each name is its own wake word** in the Wyoming ``info``, so Home Assistant lists them, plus
  ``logos_any`` ("Any agent's name"): choose that one in the pipeline and every name wakes it.
- **Who was called.** On a detection it notes the agent in ``heard.json`` (beside names.json, the
  one file it writes), which ``POST /api/v2/voice/converse`` reads to pick the agent.
- **Nothing is kept.** Audio goes through the model and is dropped; the log has the phrase, its
  threshold and when, never sound or words.

Runs in the ``logos-wake`` container (infra/wake/Dockerfile). Stdlib only apart from ``wyoming`` and
``sherpa_onnx``, imported where they are used, so the parts that decide can be tested without them.
"""
from __future__ import annotations

import array
import json
import os
import re
import sys
import time
import unicodedata
from typing import Iterable, List, Mapping, Optional, Sequence

PORT = 10400                 # Wyoming's usual port for wake words
ANY = "logos_any"            # the pseudo wake word that listens for every name
ANY_PHRASE = "Any agent's name"
NAMES_FILE = "names.json"
HEARD_FILE = "heard.json"
KEEP_HEARD = 10
HEARD_S = 60                 # older detections are dropped from heard.json
QUIET_S = 1.5                # one call of a name is one detection
THRESHOLD = 0.25             # sherpa-onnx's default: lower wakes more readily
BOOST = 1.0                  # keywords_score: higher favours the name over other paths
MIN_THRESHOLD, MAX_THRESHOLD = 0.05, 0.95


def spell(phrase: str) -> str:
    """The words the model knows: capitals, no accents ("Béré" -> "BERE"), letters, apostrophes and spaces."""
    t = unicodedata.normalize("NFKD", str(phrase or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).upper()
    t = re.sub(r"[^A-Z' ]+", " ", t)
    return " ".join(t.split())


def phrase_id(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", spell(phrase).lower()).strip("_")


def clean_phrases(rows: Iterable) -> List[dict]:
    """The usable rows of names.json: a phrase with letters, a known agent, a sane threshold, no repeats."""
    out, seen = [], set()
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        phrase, agent = str(row.get("phrase") or "").strip(), str(row.get("agent") or "").strip()
        pid = phrase_id(phrase)
        if not pid or not agent or pid in seen or pid == ANY:
            continue
        try:
            threshold = float(row.get("threshold", THRESHOLD))
        except (TypeError, ValueError):
            threshold = THRESHOLD
        try:
            boost = float(row.get("boost", BOOST))
        except (TypeError, ValueError):
            boost = BOOST
        seen.add(pid)
        out.append({"id": pid, "phrase": phrase, "agent": agent,
                    "threshold": min(max(threshold, MIN_THRESHOLD), MAX_THRESHOLD),
                    "boost": min(max(boost, 0.1), 10.0)})
    return out


def keyword_lines(phrases: Sequence[Mapping], encode) -> List[str]:
    """sherpa-onnx keyword lines, ``▁HE Y ▁F IN N :1.0 #0.25 @hey_finn``. ``encode`` turns a spelled
    phrase into word pieces (sentencepiece, in the container). A phrase it cannot spell is left out."""
    lines = []
    for p in phrases:
        pieces = [x for x in encode(spell(p["phrase"])) if x.strip()]
        if pieces:
            lines.append(f"{' '.join(pieces)} :{p['boost']:g} #{p['threshold']:g} @{p['id']}")
    return lines


def wanted(names: Optional[Sequence[str]], phrases: Sequence[Mapping]) -> List[Mapping]:
    """The phrases to listen for, given Wyoming's ``detect.names`` (None, empty or logos_any: all)."""
    if not names or ANY in names:
        return list(phrases)
    asked = set(names)
    return [p for p in phrases if p["id"] in asked]


def reported_name(names: Optional[Sequence[str]], pid: str) -> str:
    """The name to report: Home Assistant drops a detection whose name is not the one it asked for,
    so when it asked for logos_any that is the answer (who was called goes in heard.json)."""
    return ANY if names and ANY in names and pid not in names else pid


def read_json(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def note_heard(path: str, row: Mapping, now: Optional[float] = None) -> None:
    """Add a detection to heard.json (newest last), dropping the stale ones. Atomic replace."""
    now = time.time() if now is None else now
    rows = [r for r in (read_json(path).get("heard") or [])
            if isinstance(r, Mapping) and now - float(r.get("at") or 0) < HEARD_S]
    rows = (rows + [dict(row, at=now)])[-KEEP_HEARD:]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"heard": rows}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def pcm_to_float(audio: bytes) -> "array.array":
    samples = array.array("h")
    samples.frombytes(audio[: len(audio) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return array.array("f", (s / 32768.0 for s in samples))


class Gain:
    """Raises quiet speech before the spotter hears it (Greg, 28 Nov: "Hey Finn" from across the room was missed).

    The keyword model was trained on speech at ordinary loudness; a name said a few metres from the speaker
    reaches it 15-25 dB quieter. Each chunk is scaled so that the loudest 100 ms of the last two seconds
    sits near ``TARGET_DB``, by at most ``MAX_DB`` and never down; silence (below ``FLOOR_DB``) is left as it is,
    so a quiet room is not turned into hiss. The gain moves at most ``STEP_DB`` per chunk, so speech is not
    pumped. It also reports the loudest speech of each stretch of sound (levels only, never sound)."""

    TARGET_DB, MAX_DB, FLOOR_DB, STEP_DB = -24.0, 18.0, -58.0, 3.0
    WINDOW_S = 2.0

    def __init__(self, on: bool = True) -> None:
        self.on, self.db, self.peaks, self.burst = on, 0.0, [], None   # peaks: (time s, dBFS) of 100 ms frames

    @staticmethod
    def level(frame) -> float:
        import math
        if not frame:
            return -120.0
        rms = math.sqrt(sum(x * x for x in frame) / len(frame))
        return 20 * math.log10(rms) if rms > 1e-6 else -120.0

    def __call__(self, samples, rate: int, t: float):
        n = max(1, rate // 10)
        for i in range(0, len(samples), n):
            self.peaks.append((t + i / rate, self.level(samples[i:i + n])))
        self.peaks = [p for p in self.peaks if p[0] >= t - self.WINDOW_S]
        loud = max((db for _, db in self.peaks), default=-120.0)
        # A stretch of sound: its loudest level, said once it ends.
        if loud > self.FLOOR_DB + 10:
            self.burst = max(self.burst or -120.0, loud)
        elif self.burst is not None:
            print(f"sound: loudest {self.burst:.0f} dBFS, raised {self.db:.0f} dB", flush=True)
            self.burst = None
        if not self.on:
            return samples
        want = 0.0 if loud < self.FLOOR_DB else min(max(self.TARGET_DB - loud, 0.0), self.MAX_DB)
        self.db += max(-self.STEP_DB, min(self.STEP_DB, want - self.db))
        if self.db < 0.05:
            return samples
        g = 10 ** (self.db / 20)
        return array.array("f", (max(-1.0, min(1.0, x * g)) for x in samples))


class Spotter:
    """The keyword spotter, made again when names.json changes."""

    def __init__(self, model_dir: str, names_path: str, tmp_dir: str = "/tmp", threads: int = 1):
        self.model_dir, self.names_path, self.tmp_dir, self.threads = model_dir, names_path, tmp_dir, threads
        self._mtime: Optional[float] = None
        self.phrases: List[dict] = []
        self.kws = None
        import sentencepiece
        self._sp = sentencepiece.SentencePieceProcessor(model_file=os.path.join(model_dir, "bpe.model"))

    def encode(self, text: str) -> List[str]:
        return list(self._sp.encode(text, out_type=str))

    def refresh(self) -> None:
        try:
            mtime = os.stat(self.names_path).st_mtime
        except OSError:
            mtime = None
        if self.kws is not None and mtime == self._mtime:
            return
        self._mtime = mtime
        phrases = clean_phrases(read_json(self.names_path).get("phrases"))
        lines = keyword_lines(phrases, self.encode)
        self.phrases = [p for p in phrases if any(l.endswith("@" + p["id"]) for l in lines)]
        if not lines:
            self.kws = None
            print("wake words: no names yet (names.json is empty)", flush=True)
            return
        import sherpa_onnx
        kfile = os.path.join(self.tmp_dir, "keywords.txt")
        with open(kfile, "w") as f:
            f.write("\n".join(lines) + "\n")
        m = self.model_dir
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(m, "tokens.txt"), encoder=os.path.join(m, "encoder.onnx"),
            decoder=os.path.join(m, "decoder.onnx"), joiner=os.path.join(m, "joiner.onnx"),
            keywords_file=kfile, num_threads=self.threads, keywords_score=BOOST,
            keywords_threshold=THRESHOLD, num_trailing_blanks=1, provider="cpu")
        print("wake words: " + ", ".join(f"{p['phrase']} -> {p['agent']} (#{p['threshold']:g})"
                                          for p in self.phrases), flush=True)


def info_event(phrases: Sequence[Mapping]):
    from wyoming.info import Attribution, Info, WakeModel, WakeProgram
    k2 = Attribution(name="k2-fsa", url="https://github.com/k2-fsa/sherpa-onnx")
    models = [WakeModel(name=ANY, phrase=ANY_PHRASE, description="Every agent's name (Logos picks the agent)",
                        attribution=k2, installed=True, version=None, languages=["en"])]
    models += [WakeModel(name=p["id"], phrase=p["phrase"], description=f"{p['phrase']} ({p['agent']})",
                         attribution=k2, installed=True, version=None, languages=["en"]) for p in phrases]
    return Info(wake=[WakeProgram(name="logos", description="Logos: each agent wakes to its name",
                                  attribution=Attribution(name="Logos", url="https://github.com/GregsGreyCode/Logos"),
                                  installed=True, version="1", models=models)]).event()


def make_handler(spotter: Spotter, heard_path: str):
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.info import Describe
    from wyoming.ping import Ping, Pong
    from wyoming.server import AsyncEventHandler
    from wyoming.wake import Detect, Detection, NotDetected

    class Handler(AsyncEventHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.names: Optional[List[str]] = None
            self.stream = None
            self.kws = None
            self.listen: dict = {}
            self.samples = 0          # audio seen in this stream, for the detection's timestamp
            self.rate = 16000
            self.quiet_until = 0.0
            self.detected = False
            self.gain = Gain(os.environ.get("WAKE_GAIN", "1") != "0")

        def _start(self) -> None:
            spotter.refresh()
            self.kws, self.samples, self.detected = spotter.kws, 0, False
            self.listen = {p["id"]: p for p in wanted(self.names, spotter.phrases)}
            self.stream = self.kws.create_stream() if self.kws is not None and self.listen else None

        async def handle_event(self, event) -> bool:
            if Describe.is_type(event.type):
                spotter.refresh()
                await self.write_event(info_event(spotter.phrases))
                return True
            if Ping.is_type(event.type):
                await self.write_event(Pong().event())
                return True
            if Detect.is_type(event.type):
                self.names = Detect.from_event(event).names or None
                self.stream = None
                return True
            if AudioStart.is_type(event.type):
                self.rate = AudioStart.from_event(event).rate or 16000
                self._start()
                return True
            if AudioChunk.is_type(event.type):
                if self.stream is None and self.kws is None:
                    self._start()
                if self.stream is None:
                    return True
                chunk = AudioChunk.from_event(event)
                if chunk.width != 2 or chunk.channels != 1:
                    return True              # Home Assistant always sends 16-bit mono
                rate = chunk.rate or self.rate
                t = self.samples / rate
                self.samples += len(chunk.audio) // 2
                self.stream.accept_waveform(rate, self.gain(pcm_to_float(chunk.audio), rate, t))
                while self.kws.is_ready(self.stream):
                    self.kws.decode_stream(self.stream)
                    pid = self.kws.get_result(self.stream)
                    if not pid:
                        continue
                    self.kws.reset_stream(self.stream)
                    now = time.monotonic()
                    p = self.listen.get(pid)
                    if p is None or now < self.quiet_until:
                        continue
                    self.quiet_until, self.detected = now + QUIET_S, True
                    at_ms = int(self.samples * 1000 / (chunk.rate or self.rate))
                    try:
                        note_heard(heard_path, {"agent": p["agent"], "id": pid, "phrase": p["phrase"]})
                    except OSError as e:
                        print(f"wake words: could not write {HEARD_FILE}: {e}", flush=True)
                    print(f"heard {p['phrase']!r} -> {p['agent']} (threshold {p['threshold']:g}) "
                          f"at {at_ms / 1000:.2f} s into the stream; loudest {self.gain.burst or -120:.0f} dBFS, "
                          f"raised {self.gain.db:.0f} dB", flush=True)
                    await self.write_event(Detection(name=reported_name(self.names, pid), timestamp=at_ms).event())
                return True
            if AudioStop.is_type(event.type):
                if not self.detected:
                    await self.write_event(NotDetected().event())
                self.stream = None
                return True
            return False                     # nothing else is answered

    return Handler


def main() -> int:
    import asyncio
    from functools import partial
    from wyoming.server import AsyncServer

    try:
        import onnxruntime
        onnxruntime.disable_telemetry_events()
    except Exception:
        pass
    state = os.environ.get("WAKE_STATE", "/state")
    spotter = Spotter(os.environ.get("WAKE_MODEL", "/model"), os.path.join(state, NAMES_FILE),
                      threads=int(os.environ.get("WAKE_THREADS", "1")))
    spotter.refresh()
    port = int(os.environ.get("WAKE_PORT", str(PORT)))

    async def run() -> None:
        handler = make_handler(spotter, os.path.join(state, HEARD_FILE))
        server = AsyncServer.from_uri(f"tcp://0.0.0.0:{port}")
        print(f"wake words: listening on {port}", flush=True)
        await server.run(partial(handler))

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
