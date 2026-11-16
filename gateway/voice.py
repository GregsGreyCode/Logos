"""Speech-to-text for every agent: one small HTTP server in front of one Whisper model (F31).

An agent sends a voice note and gets its words back. The endpoint has the shape of
OpenAI's ``POST /v1/audio/transcriptions``, which hermes already knows how to call
(``stt.provider: openai`` with a ``base_url``), so the agent image needs nothing added.

- Each agent has its own token; a request without a known token is refused.
- The audio is written to a temporary file, transcribed and deleted. Neither the audio
  nor the text is kept or logged: the log line has the agent, the durations and the language.
- One recording at a time. The model is loaded on first use and dropped after a quiet
  spell, so an idle service costs almost no memory.

Runs in the ``logos-voice`` container (infra/voice/Dockerfile). Stdlib only apart from
``faster_whisper``, which is imported when the first recording arrives, so the HTTP side
can be tested without it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Mapping, Optional, Tuple

MAX_UPLOAD = 25 * 1024 * 1024           # the same ceiling OpenAI's endpoint has
PATH = "/v1/audio/transcriptions"


class BadRequest(Exception):
    pass


def parse_form(content_type: str, body: bytes) -> Tuple[Dict[str, str], Optional[Tuple[str, bytes]]]:
    """Split a multipart form into its text fields and its one uploaded file ``(filename, bytes)``."""
    if "multipart/form-data" not in (content_type or ""):
        raise BadRequest("expected multipart/form-data")
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields: Dict[str, str] = {}
    upload = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if name == "file":
            upload = (part.get_filename() or "audio", data)
        elif name:
            fields[str(name)] = data.decode("utf-8", "replace").strip()
    return fields, upload


def agent_for(tokens: Mapping[str, str], authorization: str) -> Optional[str]:
    """The agent a bearer token belongs to. ``tokens`` is ``{agent: token}``."""
    scheme, _, given = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not given.strip():
        return None
    return next((name for name, token in tokens.items() if token and token == given.strip()), None)


def quiet_onnxruntime() -> None:
    """faster-whisper uses onnxruntime to find speech in a recording, and recent onnxruntime
    builds collect usage telemetry. Switch it off. The container also has no working DNS
    (gateway/voice_host.py), so nothing could be sent by name anyway."""
    try:
        import onnxruntime
        onnxruntime.disable_telemetry_events()
    except Exception:
        pass


class Whisper:
    """The model, loaded when first needed and dropped after ``idle_s`` without use."""

    def __init__(self, model_path: str, idle_s: int = 600, threads: int = 0):
        self.model_path, self.idle_s, self.threads = model_path, idle_s, threads
        self._model = None
        self._lock = threading.Lock()
        self._last = 0.0
        if idle_s > 0:
            threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            with self._lock:
                if self._model is not None and time.monotonic() - self._last > self.idle_s:
                    self._model = None
                    print("model unloaded after a quiet spell", flush=True)

    def transcribe(self, path: str, language: Optional[str] = None) -> dict:
        with self._lock:                 # one recording at a time; others wait their turn
            if self._model is None:
                quiet_onnxruntime()
                from faster_whisper import WhisperModel
                started = time.monotonic()
                self._model = WhisperModel(self.model_path, device="cpu", compute_type="int8",
                                           cpu_threads=self.threads)
                print(f"model loaded in {time.monotonic() - started:.1f} s", flush=True)
            segments, info = self._model.transcribe(path, language=language or None, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
            self._last = time.monotonic()
            return {"text": text, "language": info.language, "duration": round(float(info.duration), 2)}


def make_handler(tokens: Mapping[str, str], transcribe: Callable[[str, Optional[str]], dict]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "logos-voice"

        def log_message(self, *args) -> None:      # our own log lines only; never the words
            pass

        def _send(self, status: int, body, content_type: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, message: str) -> None:
            self._send(status, {"error": {"message": message}})

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True})
            self._error(404, "not found")

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != PATH:
                return self._error(404, "not found")
            agent = agent_for(tokens, self.headers.get("Authorization", ""))
            if agent is None:
                return self._error(401, "unknown token")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return self._error(400, "empty request")
            if length > MAX_UPLOAD:
                return self._error(413, "recording too large (25 MB at most)")
            try:
                fields, upload = parse_form(self.headers.get("Content-Type", ""), self.rfile.read(length))
            except BadRequest as e:
                return self._error(400, str(e))
            if not upload or not upload[1]:
                return self._error(400, "no audio file in the request")
            suffix = os.path.splitext(upload[0])[1][:8] or ".audio"
            started = time.monotonic()
            with tempfile.NamedTemporaryFile(suffix=suffix) as f:
                f.write(upload[1])
                f.flush()
                try:
                    result = transcribe(f.name, fields.get("language") or None)
                except Exception as e:
                    print(f"{agent}: could not transcribe ({type(e).__name__})", flush=True)
                    return self._error(422, "could not read that recording")
            print(f"{agent}: {result.get('duration', '?')} s of {result.get('language', '?')} "
                  f"in {time.monotonic() - started:.1f} s", flush=True)
            if fields.get("response_format") == "text":
                return self._send(200, result["text"] + "\n", "text/plain; charset=utf-8")
            self._send(200, result)

    return Handler


def main() -> int:
    with open(os.environ.get("VOICE_CONFIG", "/data/config.json")) as f:
        tokens = json.load(f)["agents"]
    whisper = Whisper(os.environ.get("VOICE_MODEL", "/models/whisper"),
                      idle_s=int(os.environ.get("VOICE_IDLE_UNLOAD_S", "600")),
                      threads=int(os.environ.get("VOICE_THREADS", "0")))
    port = int(os.environ.get("VOICE_PORT", "8093"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(tokens, whisper.transcribe))
    print(f"voice: listening on {port} for {', '.join(sorted(tokens)) or 'no agents'}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
