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
# The second check (Greg, 28 Nov: a name said across the room was missed). A second, far more willing spotter
# runs beside the first; what only it hears is sent to Logos speech-to-text (WAKE_CHECK=host:port), and the
# speaker wakes only if the words have the name in them. The audio is sent marked as a check, so nothing of it
# is kept there and it never reaches the voice prints.
LENIENT_THRESHOLD, LENIENT_BOOST = 0.05, 2.5
CHECK_S = 2.0                # the audio sent: the name ends about now
CHECK_TIMEOUT_S = 2.5
CHECK_NAME = "logos-wake-check"   # Wyoming transcribe name: gateway/stt.py answers words only
NAME_LIKE = 0.75             # how alike a heard word must be to the name (difflib ratio)
FILLERS = {"HEY", "HI", "OK", "OKAY", "HELLO", "YO", "ALRIGHT"}


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


def name_words(phrase: str) -> List[str]:
    """The words of a wake phrase that are the name ("Hey Finn" -> ["finn"])."""
    words = spell(phrase).split()
    return [w.lower() for w in words if w not in FILLERS] or [w.lower() for w in words]


def says_name(text: str, phrase: str) -> bool:
    """Whether the words speech-to-text heard have the wake phrase's name in them, allowing for spelling
    ("Fin", "Finn's"). Each word of the name must match a heard word (or two heard words run together)."""
    import difflib
    heard = [w.strip("'").lower() for w in spell(text).split() if w.strip("'")]
    heard = [w[:-2] if w.endswith("'s") else w for w in heard]
    pairs = heard + [a + b for a, b in zip(heard, heard[1:])]
    return bool(heard) and all(any(difflib.SequenceMatcher(None, n, h).ratio() >= NAME_LIKE for h in pairs)
                               for n in name_words(phrase))


def float_to_pcm(samples) -> bytes:
    out = array.array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


async def check(address: tuple, pcm: bytes, rate: int, timeout: float = CHECK_TIMEOUT_S) -> Optional[str]:
    """The words Logos speech-to-text hears in ``pcm`` (16-bit mono), or None when it does not answer."""
    import asyncio
    from wyoming.asr import Transcribe, Transcript
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.client import AsyncTcpClient

    async def ask() -> Optional[str]:
        async with AsyncTcpClient(*address) as client:
            await client.write_event(Transcribe(name=CHECK_NAME).event())
            await client.write_event(AudioStart(rate=rate, width=2, channels=1).event())
            step = rate * 2 // 10
            for i in range(0, len(pcm), step):
                await client.write_event(AudioChunk(rate=rate, width=2, channels=1, audio=pcm[i:i + step]).event())
            await client.write_event(AudioStop().event())
            while True:
                event = await client.read_event()
                if event is None:
                    return None
                if Transcript.is_type(event.type):
                    return Transcript.from_event(event).text or ""
    try:
        return await asyncio.wait_for(ask(), timeout)
    except (OSError, asyncio.TimeoutError, ValueError):
        return None


def check_address(env: Optional[Mapping[str, str]] = None) -> Optional[tuple]:
    raw = (os.environ if env is None else env).get("WAKE_CHECK", "").strip()
    host, _, port = raw.rpartition(":")
    try:
        return (host, int(port)) if host and 1 <= int(port) <= 65535 else None
    except ValueError:
        return None


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
    WINDOW_S, ROOM_S = 2.0, 10.0

    def __init__(self, on: bool = True) -> None:
        self.on, self.db, self.peaks, self.room = on, 0.0, [], []   # (time s, dBFS) of 100 ms frames
        self.loud = -120.0

    def levels(self) -> str:
        """The name's loudness against the room's, for the log (levels only)."""
        room = sorted(db for _, db in self.room)
        quiet = room[len(room) // 5] if room else -120.0
        return f"loudest {self.loud:.0f} dBFS, room {quiet:.0f} dBFS, raised {self.db:.0f} dB"

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
            frame = (t + i / rate, self.level(samples[i:i + n]))
            self.peaks.append(frame)
            self.room.append(frame)
        self.peaks = [p for p in self.peaks if p[0] >= t - self.WINDOW_S]
        self.room = [p for p in self.room if p[0] >= t - self.ROOM_S]
        loud = self.loud = max((db for _, db in self.peaks), default=-120.0)
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
        self.lenient = None          # the willing spotter of the second check (None without WAKE_CHECK)
        self.check_address = check_address()
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

        def spotter(path):
            return sherpa_onnx.KeywordSpotter(
                tokens=os.path.join(m, "tokens.txt"), encoder=os.path.join(m, "encoder.onnx"),
                decoder=os.path.join(m, "decoder.onnx"), joiner=os.path.join(m, "joiner.onnx"),
                keywords_file=path, num_threads=self.threads, keywords_score=BOOST,
                keywords_threshold=THRESHOLD, num_trailing_blanks=1, provider="cpu")
        self.kws = spotter(kfile)
        self.lenient = None
        if self.check_address:
            willing = [dict(p, threshold=min(p["threshold"], LENIENT_THRESHOLD), boost=max(p["boost"], LENIENT_BOOST))
                       for p in self.phrases]
            lfile = os.path.join(self.tmp_dir, "keywords-willing.txt")
            with open(lfile, "w") as f:
                f.write("\n".join(keyword_lines(willing, self.encode)) + "\n")
            self.lenient = spotter(lfile)
        print("wake words: " + ", ".join(f"{p['phrase']} -> {p['agent']} (#{p['threshold']:g})"
                                          for p in self.phrases)
              + (f"; second check through {self.check_address[0]}:{self.check_address[1]}" if self.lenient else ""),
              flush=True)


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
            self.lstream = None
            self.willing = None
            self.recent: list = []    # the last CHECK_S of audio (after the gain), for the second check
            self.recent_n = 0
            self.lquiet_until = 0.0

        def _start(self) -> None:
            spotter.refresh()
            self.kws, self.samples, self.detected = spotter.kws, 0, False
            self.listen = {p["id"]: p for p in wanted(self.names, spotter.phrases)}
            self.stream = self.kws.create_stream() if self.kws is not None and self.listen else None
            self.willing = spotter.lenient if self.stream is not None else None
            self.lstream = self.willing.create_stream() if self.willing is not None else None
            self.recent, self.recent_n = [], 0

        def _keep(self, audio, rate: int) -> None:
            self.recent.append(audio)
            self.recent_n += len(audio)
            while self.recent and self.recent_n - len(self.recent[0]) >= CHECK_S * rate:
                self.recent_n -= len(self.recent.pop(0))

        async def _detect(self, pid: str, p: Mapping, rate: int, how: str) -> None:
            now = time.monotonic()
            self.quiet_until = self.lquiet_until = now + QUIET_S
            self.detected = True
            at_ms = int(self.samples * 1000 / rate)
            try:
                note_heard(heard_path, {"agent": p["agent"], "id": pid, "phrase": p["phrase"]})
            except OSError as e:
                print(f"wake words: could not write {HEARD_FILE}: {e}", flush=True)
            print(f"heard {p['phrase']!r} -> {p['agent']} ({how}) at {at_ms / 1000:.2f} s into the stream; "
                  f"{self.gain.levels()}", flush=True)
            await self.write_event(Detection(name=reported_name(self.names, pid), timestamp=at_ms).event())

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
                audio = self.gain(pcm_to_float(chunk.audio), rate, t)
                self.stream.accept_waveform(rate, audio)
                sure = False
                while self.kws.is_ready(self.stream):
                    self.kws.decode_stream(self.stream)
                    pid = self.kws.get_result(self.stream)
                    if not pid:
                        continue
                    self.kws.reset_stream(self.stream)
                    p = self.listen.get(pid)
                    if p is None or time.monotonic() < self.quiet_until:
                        continue
                    sure = True
                    await self._detect(pid, p, rate, f"threshold {p['threshold']:g}")
                if self.lstream is None:
                    return True
                # The second check: what only the willing spotter heard goes to speech-to-text.
                self._keep(audio, rate)
                self.lstream.accept_waveform(rate, audio)
                while self.willing.is_ready(self.lstream):
                    self.willing.decode_stream(self.lstream)
                    pid = self.willing.get_result(self.lstream)
                    if not pid:
                        continue
                    self.willing.reset_stream(self.lstream)
                    p = self.listen.get(pid)
                    if sure or p is None or time.monotonic() < self.lquiet_until:
                        continue
                    self.lquiet_until = time.monotonic() + QUIET_S
                    started = time.monotonic()
                    words = await check(spotter.check_address, float_to_pcm([x for a in self.recent for x in a]), rate)
                    took = time.monotonic() - started
                    if words is not None and says_name(words, p["phrase"]):
                        await self._detect(pid, p, rate, f"second check, {took:.2f} s")
                    else:
                        print(f"maybe {p['phrase']!r}: {'no answer from speech-to-text' if words is None else 'not the name'} "
                              f"({took:.2f} s); {self.gain.levels()}", flush=True)
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
