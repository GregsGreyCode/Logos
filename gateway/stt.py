"""Speech-to-text for the house speaker, and who said it (Voice VO.1).

Home Assistant sends what the speaker heard after the wake word to this server over the Wyoming
protocol (Settings > Devices & services > Wyoming Protocol, like the speaker voices in
gateway/tts.py) and gets the words back (Parakeet TDT 0.6B v3: English and French, whichever is
spoken, as quick as v2, the English-only model Home Assistant's own Whisper add-on picks by default). From the same audio it works out a voice embedding
(CAM++, sherpa-onnx) and keeps it in memory for 30 minutes under a hash of the words. When the
words then arrive at ``POST /api/v2/voice/converse``, the dashboard asks for that embedding and
compares it with the voice prints it holds (gateway/voice_id.py). The prints never come here.

Two doors, kept apart:

- **Wyoming (the LAN):** speech in, words out. No login (Wyoming has none), no tokens behind it, and
  nothing else is answered.
- **HTTP (127.0.0.1 only, with a token only the dashboard has):** ``GET /heard`` (the embedding
  of recent words, by hash), ``POST /embed`` (a recording from Settings › Your voice -> its quality
  and embeddings) and ``GET /cohort`` (the reference voices' embeddings, for setting a print's
  strictness; ``?channel=speaker``: the same voices made to sound like the house speaker, cut to the
  length of a spoken command, for the speaker's own strictness). The recording goes to a temporary file, is decoded and deleted.

Never logged or kept: the words and the audio. The log line has the length, the speech found and
the time taken. Runs in the ``logos-stt`` container (infra/stt/Dockerfile; gateway/stt_host.py).
Stdlib only apart from what :func:`main` imports, so the logic can be tested without the models.
"""
from __future__ import annotations

import glob
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, urlparse

try:                                     # mounted beside this file in the container
    import voiceprint as vp              # type: ignore
except ImportError:                      # the gateway's tests
    from gateway import voiceprint as vp

WYOMING_PORT = 10300                     # Wyoming's usual port for speech-to-text
HTTP_PORT = 8101
KEEP_S = 1800                            # 30 min: the words reach voice/converse within a second or two, but
                                         # Settings › Your voice can say "That was me" for half an hour
MAX_HEARD = 100
MAX_AUDIO_S = 30                         # a spoken command; Home Assistant stops listening well before
MAX_UPLOAD = 4 * 1024 * 1024             # an enrolment recording: 15 s of browser audio is about 60 KB


class Heard:
    """Recent embeddings, by the hash of their words. In memory only."""

    def __init__(self, keep_s: float = KEEP_S):
        self.keep_s = keep_s
        self._rows: List[dict] = []
        self._lock = threading.Lock()
        self._n = 0

    def add(self, text: str, embedding: Optional[List[float]], quality: Mapping[str, Any], now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._n += 1
            self._rows = [r for r in self._rows if now - r["at"] <= self.keep_s][-(MAX_HEARD - 1):]
            self._rows.append({"id": self._n, "key": vp.heard_key(text), "at": now, "embedding": embedding, **dict(quality)})

    def find(self, key: str = "", now: Optional[float] = None) -> List[dict]:
        """Rows for these words (newest first); without a key, every recent row."""
        now = time.time() if now is None else now
        with self._lock:
            rows = [r for r in self._rows if now - r["at"] <= self.keep_s and (not key or r["key"] == key)]
        return [{k: v for k, v in r.items() if k != "key"} for r in reversed(rows)]


def authorised(token: str, header: str) -> bool:
    given = header[7:].strip() if (header or "").startswith("Bearer ") else ""
    return bool(token) and bool(given) and hmac.compare_digest(given.encode(), token.encode())


class Ears:
    """The two models, loaded at start and kept (the speaker should not wait for them): Parakeet v3 for the
    words (NVIDIA's speech-to-text, English and French without being told which) and
    CAM++ for the voice. Both run on sherpa-onnx."""

    def __init__(self, words_dir: str, speaker_model: str, cohort_dir: str, threads: int = 4):
        self.words_dir, self.speaker_model, self.cohort_dir, self.threads = words_dir, speaker_model, cohort_dir, threads
        self._words = self._extractor = None
        self._cohorts: Dict[str, List[List[float]]] = {}
        self._lock = threading.Lock()

    def words(self):
        if self._words is None:
            import numpy as np
            import sherpa_onnx
            started = time.monotonic()
            d = self.words_dir
            self._words = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=f"{d}/encoder.int8.onnx", decoder=f"{d}/decoder.int8.onnx", joiner=f"{d}/joiner.int8.onnx",
                tokens=f"{d}/tokens.txt", num_threads=self.threads, model_type="nemo_transducer")
            st = self._words.create_stream()                       # once, so the first command is not slow
            st.accept_waveform(vp.RATE, np.zeros(1600, dtype=np.float32))
            self._words.decode_stream(st)
            print(f"words model loaded in {time.monotonic() - started:.1f} s", flush=True)
        return self._words

    def extractor(self):
        if self._extractor is None:
            import sherpa_onnx
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=self.speaker_model, num_threads=2))
        return self._extractor

    def embed(self, audio) -> List[float]:
        import numpy as np
        ex = self.extractor()
        st = ex.create_stream()
        st.accept_waveform(vp.RATE, np.asarray(audio, dtype=np.float32))
        st.input_finished()
        return vp.normalise(ex.compute(st))

    def transcribe(self, audio) -> str:
        import numpy as np
        rec = self.words()
        st = rec.create_stream()
        st.accept_waveform(vp.RATE, np.asarray(audio, dtype=np.float32))
        rec.decode_stream(st)
        return str(st.result.text or "").strip()

    def hear(self, pcm: bytes, rate: int, width: int, channels: int) -> tuple:
        """(words, embedding or None, quality) for a spoken command."""
        audio = to_float(pcm, rate, width, channels)
        with self._lock:                                 # one at a time
            text = self.transcribe(audio)
            take = vp.analyse(audio.tolist())
            emb = self.embed(take.audio) if take.speech >= vp.MIN_HEARD_S else None
        return text, emb, take.quality()

    def from_recording(self, path: str) -> dict:
        """A recording from Settings: its quality, the embedding of the whole and of each piece."""
        take = vp.analyse(decode(path).tolist())
        with self._lock:
            whole = self.embed(take.audio) if take.speech >= 1.0 else None
            parts = [self.embed(p) for p in vp.pieces(take.audio)] if take.speech >= vp.MIN_ENROL_S else []
        return {"quality": take.quality(), "embedding": whole, "pieces": parts}

    def cohort(self, channel: str = "phone") -> List[List[float]]:
        """The reference voices (public recordings in the image), made into embeddings once. ``speaker``:
        each clip made to sound like the house speaker (:func:`speaker_like`) and cut into two pieces
        as long as a spoken command (:data:`SPEAKER_PIECE_S`)."""
        if channel not in self._cohorts:
            started, out = time.monotonic(), []
            for i, path in enumerate(sorted(glob.glob(os.path.join(self.cohort_dir, "*.flac")))):
                raw = decode(path)
                if channel == "speaker":
                    clips = [speaker_like(c, seed=i * 2 + k) for k, c in enumerate(command_pieces(raw))]
                else:
                    clips = [raw]
                for c in clips:
                    take = vp.analyse(c.tolist())
                    if take.speech >= (1.0 if channel == "speaker" else 2.0):
                        with self._lock:
                            out.append([round(x, 5) for x in self.embed(take.audio)])
            self._cohorts[channel] = out
            print(f"{len(out)} reference voices ({channel}) ready in {time.monotonic() - started:.1f} s", flush=True)
        return self._cohorts[channel]


def decode(path: str, max_s: float = 60.0):
    """Any recording a browser makes (WebM/Opus, Ogg, MP4/AAC, WAV) or FLAC, as 16 kHz mono floats (PyAV)."""
    import av
    import numpy as np
    out, total = [], 0
    with av.open(path) as f:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=vp.RATE)
        for frame in f.decode(audio=0):
            for r in resampler.resample(frame):
                a = r.to_ndarray().reshape(-1)
                out.append(a)
                total += len(a)
            if total > max_s * vp.RATE:
                break
        for r in resampler.resample(None):
            out.append(r.to_ndarray().reshape(-1))
    a = np.concatenate(out) if out else np.zeros(0, dtype=np.int16)
    return a.astype(np.float32) / 32768.0


def to_float(pcm: bytes, rate: int, width: int, channels: int):
    """Wyoming audio (usually 16 kHz, 16-bit, mono) as 16 kHz mono floats."""
    import numpy as np
    if width != 2:
        raise ValueError("expected 16-bit audio")
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    if rate != vp.RATE and len(a):
        n = int(len(a) * vp.RATE / rate)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


SPEAKER_PIECE_S = 2.0                    # the speaker cohort's pieces: as long as a short spoken command


def command_pieces(a, seconds: float = SPEAKER_PIECE_S, n: int = 2) -> list:
    """Up to ``n`` pieces of ``seconds`` from the start of a clip (skipping its first half second)."""
    size, start = int(seconds * vp.RATE), vp.RATE // 2
    return [a[start + k * size:start + (k + 1) * size] for k in range(n) if start + (k + 1) * size <= len(a)]


def speaker_like(a, seed: int = 0):
    """A clean recording made to sound roughly like the house speaker's far-field microphone: band-limited
    (200 Hz to 3.8 kHz), in a room (a synthetic reverberation of 0.25 to 0.5 s, the direct sound kept) and
    with room noise (pink, 12 to 20 dB below the voice). Deterministic for a seed, so the strictness it sets
    is the same every time the container starts."""
    import numpy as np
    rng = np.random.default_rng(seed)
    a = np.asarray(a, dtype=np.float64)
    n = len(a)
    if n < 160:
        return a.astype(np.float32)
    spec = np.fft.rfft(a)
    f = np.fft.rfftfreq(n, 1 / vp.RATE)
    spec *= 1 / np.sqrt(1 + (200 / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / 3800) ** 8)   # smooth band edges
    a = np.fft.irfft(spec, n)
    rt60 = rng.uniform(0.25, 0.5)
    t = np.arange(int(rt60 * vp.RATE)) / vp.RATE
    ir = rng.standard_normal(len(t)) * np.exp(-6.9 * t / rt60) * 0.25
    ir[: int(0.002 * vp.RATE)] = 0
    ir[0] = 1.0
    size = 1 << int(np.ceil(np.log2(n + len(ir))))
    a = np.fft.irfft(np.fft.rfft(a, size) * np.fft.rfft(ir, size), size)[:n]
    white = np.fft.rfft(rng.standard_normal(n))
    pink = np.fft.irfft(white / np.sqrt(np.maximum(np.fft.rfftfreq(n, 1 / vp.RATE), 20)), n)
    level = np.sqrt(np.mean(a ** 2)) + 1e-9
    snr = rng.uniform(12, 20)
    a = a + pink / (np.sqrt(np.mean(pink ** 2)) + 1e-9) * level / (10 ** (snr / 20))
    peak = np.max(np.abs(a)) + 1e-9
    return (a * min(1.0, 0.7 / peak)).astype(np.float32)


# ── Wyoming: speech in, words out ────────────────────────────────────────────────────────────

def info_event():
    from wyoming.info import AsrModel, AsrProgram, Attribution, Info
    nvidia = Attribution(name="NVIDIA, k2-fsa", url="https://github.com/k2-fsa/sherpa-onnx")
    return Info(asr=[AsrProgram(name="logos", description="Logos: speech to text, and who said it",
                                attribution=Attribution(name="Logos", url="https://github.com/GregsGreyCode/Logos"),
                                installed=True, version="1",
                                models=[AsrModel(name="parakeet-tdt-0.6b-v3", description="Parakeet TDT 0.6B v3 (English, French)",
                                                 attribution=nvidia, installed=True, version=None, languages=["en", "fr"])])]).event()


def make_handler(ears: Ears, heard: Heard):
    import asyncio
    from wyoming.asr import Transcribe, Transcript
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.info import Describe
    from wyoming.ping import Ping, Pong
    from wyoming.server import AsyncEventHandler

    info = info_event()

    class Handler(AsyncEventHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.pcm = bytearray()
            self.fmt = (vp.RATE, 2, 1)

        async def handle_event(self, event) -> bool:
            if Describe.is_type(event.type):
                await self.write_event(info)
                return True
            if Ping.is_type(event.type):
                await self.write_event(Pong().event())
                return True
            if Transcribe.is_type(event.type):
                return True                               # the model hears the language itself
            if AudioStart.is_type(event.type):
                s = AudioStart.from_event(event)
                self.pcm, self.fmt = bytearray(), (s.rate, s.width, s.channels)
                return True
            if AudioChunk.is_type(event.type):
                c = AudioChunk.from_event(event)
                if len(self.pcm) < MAX_AUDIO_S * c.rate * c.width * c.channels:
                    self.pcm += c.audio
                return True
            if AudioStop.is_type(event.type):
                started = time.monotonic()
                pcm, self.pcm = bytes(self.pcm), bytearray()
                try:
                    text, emb, q = await asyncio.to_thread(ears.hear, pcm, *self.fmt)
                except Exception as e:
                    print(f"could not transcribe ({type(e).__name__})", flush=True)
                    text, emb, q = "", None, {}
                if text:
                    heard.add(text, emb, q)
                await self.write_event(Transcript(text=text, language="en").event())
                seconds = len(pcm) / max(1, self.fmt[0] * self.fmt[1] * self.fmt[2])
                print(f"heard {seconds:.1f} s ({q.get('speech', 0)} s of speech) in {time.monotonic() - started:.2f} s", flush=True)
                return False                              # one command per connection, as Home Assistant expects
            return True

    return Handler


# ── HTTP for the dashboard ───────────────────────────────────────────────────────────────────

def make_http(token: str, heard: Heard, recording: Callable[[str], dict], cohort: Callable[..., list]):
    import tempfile

    class Handler(BaseHTTPRequestHandler):
        server_version = "logos-stt"

        def log_message(self, *args) -> None:
            pass

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            if authorised(token, self.headers.get("Authorization", "")):
                return True
            self._send(401, {"error": "unknown token"})
            return False

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, {"ok": True})
            if not self._allowed():
                return
            if url.path == "/heard":
                key = (parse_qs(url.query).get("key") or [""])[0]
                return self._send(200, {"heard": heard.find(key)})
            if url.path == "/cohort":
                channel = (parse_qs(url.query).get("channel") or [""])[0]
                if channel and channel != "speaker":
                    return self._send(400, {"error": "unknown channel"})
                return self._send(200, {"cohort": cohort(channel) if channel else cohort()})
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/embed":
                return self._send(404, {"error": "not found"})
            if not self._allowed():
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= MAX_UPLOAD:
                return self._send(413 if length else 400, {"error": "recording missing or too large"})
            with tempfile.NamedTemporaryFile(suffix=".audio") as f:
                f.write(self.rfile.read(length))
                f.flush()
                started = time.monotonic()
                try:
                    out = recording(f.name)
                except Exception as e:
                    print(f"could not read a recording ({type(e).__name__})", flush=True)
                    return self._send(422, {"error": "could not read that recording"})
            print(f"voice print recording: {out['quality'].get('speech')} s of speech, "
                  f"{len(out['pieces'])} pieces in {time.monotonic() - started:.1f} s", flush=True)
            self._send(200, out)

    return Handler


def main() -> int:
    import asyncio
    from wyoming.server import AsyncServer

    token = open(os.environ.get("STT_TOKEN_FILE", "/run/stt.token")).read().strip() \
        if os.path.exists(os.environ.get("STT_TOKEN_FILE", "/run/stt.token")) else os.environ.get("STT_TOKEN", "")
    ears = Ears(os.environ.get("STT_WORDS", "/models/words"), os.environ.get("STT_SPEAKER", "/models/campplus.onnx"),
                os.environ.get("STT_COHORT", "/models/cohort"), threads=int(os.environ.get("STT_THREADS", "4")))
    heard = Heard()
    ears.words()
    ears.extractor()
    http = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("STT_HTTP_PORT", str(HTTP_PORT)))),
                               make_http(token, heard, ears.from_recording, ears.cohort))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    threading.Thread(target=lambda: (ears.cohort(), ears.cohort("speaker")), daemon=True).start()  # before the first enrolment
    port = int(os.environ.get("STT_PORT", str(WYOMING_PORT)))

    async def run() -> None:
        server = AsyncServer.from_uri(f"tcp://0.0.0.0:{port}")
        print(f"speech to text: listening on {port} (Wyoming) and {HTTP_PORT} (dashboard)", flush=True)
        await server.run(make_handler(ears, heard))

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
