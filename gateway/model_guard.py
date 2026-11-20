"""A doorman in front of the local model server (LM Studio).

When LM Studio is closed, or its machine is busy with something else, that machine
usually drops connections without a word. The model gateway then waits out its full
request timeout (fifteen minutes, sized for long replies) and the agent sits silent.

This small proxy stands between the gateway and the model server. It gives the server
three seconds to accept the connection. If it does, the request and its streamed reply
pass straight through, however long the reply takes. If it does not, the guard tries again
every five seconds for up to a minute (story D1: LM Studio being reloaded, or just closed),
and only then gives the caller a plainly worded 503, so the agent can tell the person.

LM Studio runs several requests side by side in one shared context. When they do not fit
together it refuses one with "Context size has been exceeded", inside an otherwise normal
reply. The guard holds a reply's headers until its first bytes arrive; if those are that
refusal, it waits and sends the request again, within the same minute. A request that really
is too large still fails after that, and hermes then shortens the conversation itself.

Several agents share the one model, and LM Studio shares one context window between its parallel
slots. When two long requests overlap they do not fit, and both slow down on one GPU. So chat
requests go through a queue (:class:`Gate`): at most :data:`SLOTS` at once, and only while the
estimated size of everything running fits in the loaded window. A person's message goes before
scheduled work (consolidation, digests, noticeboard checks). Each request's wait and duration is
logged as one JSON line, and ``/guard/health`` shows the queue, so the number of slots can be
chosen from measurements.

It keeps nothing, and changes two things in the traffic: models whose reasoning is only on or
off get no ``reasoning_effort`` level they cannot take (:func:`fix_reasoning`), and a chat request
that sets no presence penalty gets one (:func:`add_presence_penalty`), against thinking that loops. Stdlib only;
runs in the ``logos-model-guard`` container (see :mod:`gateway.model_gateway`).
"""
from __future__ import annotations

import http.client
import itertools
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
HEALTH_PATH = "/guard/health"
# What the person reads when a reply cannot be written (story D2: the agent answers later by itself).
OFF_MESSAGE = ("I can't answer right now: the AI computer is off. "
               "I'll reply here as soon as it's back on.")


OPTIONS_TTL = 600.0                     # how long a model's reasoning options are remembered
WAIT = 60.0                             # how long a request may wait for the model server (D1)
KNOWN_OFF = 120.0                       # after a full wait in vain: answer "off" at once for this long
RETRY_EVERY = 5.0
FULL = b"Context size has been exceeded"  # LM Studio: the shared context is in use by other requests


# ── the queue ────────────────────────────────────────────────────────────────

SLOTS = 2                    # chat requests at once (GUARD_SLOTS overrides); never more than LM Studio's own
MAX_QUEUE_WAIT = 300.0       # seconds; then it goes anyway (Bifrost gives up on a silent stream after 900)
REPLY_RESERVE = 8192         # tokens kept for the reply (thinking included) when the request sets no max
BYTES_PER_TOKEN = 3.5        # a deliberately high estimate: JSON and tool schemas are dense
CRON_MARK = b"You are running as a scheduled cron job"
PERSON, SCHEDULED = 0, 1     # lower goes first


def estimate(body: bytes) -> int:
    """Tokens a chat request may take in the shared window: its prompt plus room for the reply."""
    try:
        req = json.loads(body)
        reply = int(req.get("max_tokens") or req.get("max_completion_tokens") or REPLY_RESERVE)
    except (ValueError, TypeError, AttributeError):
        reply = REPLY_RESERVE
    return int(len(body) / BYTES_PER_TOKEN) + min(reply, REPLY_RESERVE)


def priority(body: bytes) -> int:
    """A person's message goes before a scheduled job's."""
    return SCHEDULED if CRON_MARK in body else PERSON


class Gate:
    """At most ``slots`` requests at once, and only while their estimated tokens fit ``window()``.

    Waiting requests go in order of priority, then arrival; only the first in line may enter, so a
    large request is never passed over for ever. One request always runs alone, however large (the
    model server then refuses it, and hermes shortens the conversation). After ``max_wait`` the
    first in line goes in anyway."""

    def __init__(self, slots: int, window, max_wait: float = MAX_QUEUE_WAIT, clock=time.monotonic):
        self.slots, self.window, self.max_wait, self.clock = max(1, int(slots)), window, max_wait, clock
        self.cond = threading.Condition()
        self.running = {}                 # ticket -> tokens
        self.waiting = []                 # (priority, seq, tokens, since)
        self.seq = itertools.count()
        self.stats = {"served": 0, "queued": 0, "waited_total": 0.0, "waited_max": 0.0, "forced": 0}

    def _fits(self, tokens: int) -> bool:
        if not self.running:
            return True
        window = int(self.window() or 0)
        return len(self.running) < self.slots and (not window or sum(self.running.values()) + tokens <= window)

    def enter(self, tokens: int, prio: int = PERSON) -> tuple:
        """Block until this request may run. Returns (ticket, seconds waited, forced)."""
        with self.cond:
            me = (prio, next(self.seq), tokens, self.clock())
            self.waiting.append(me)
            self.waiting.sort()
            queued = False
            while True:
                waited = self.clock() - me[3]
                forced = waited >= self.max_wait
                if self.waiting[0] is me and (self._fits(tokens) or forced):
                    break
                queued = True
                self.cond.wait(timeout=max(0.05, min(5.0, self.max_wait - waited)))
            self.waiting.remove(me)
            self.running[me[1]] = tokens
            self.stats["served"] += 1
            if queued:
                self.stats["queued"] += 1
            self.stats["waited_total"] += waited
            self.stats["waited_max"] = max(self.stats["waited_max"], waited)
            if forced and len(self.running) > 1:
                self.stats["forced"] += 1
            self.cond.notify_all()
            return me[1], waited, forced

    def leave(self, ticket) -> None:
        with self.cond:
            self.running.pop(ticket, None)
            self.cond.notify_all()

    def view(self) -> dict:
        with self.cond:
            s = dict(self.stats)
            served = s.pop("waited_total")
            return {"slots": self.slots, "running": len(self.running), "waiting": len(self.waiting),
                    "tokens_running": sum(self.running.values()), "window": int(self.window() or 0),
                    "served": s["served"], "queued": s["queued"], "forced": s["forced"],
                    "wait_avg_s": round(served / s["served"], 1) if s["served"] else 0.0,
                    "wait_max_s": round(s["waited_max"], 1)}


def usage(tail: bytes) -> dict:
    """prompt_tokens and completion_tokens from the end of a reply, when the server reported them."""
    out = {}
    for key in ("prompt_tokens", "completion_tokens"):
        i = tail.rfind(b'"%s":' % key.encode())
        if i >= 0:
            digits = tail[i + len(key) + 3:].lstrip()[:12]
            n = b"".join(itertools.takewhile(lambda c: c.isdigit(), (digits[j:j + 1] for j in range(len(digits)))))
            if n:
                out[key] = int(n)
    return out


def is_chat(command: str, path: str) -> bool:
    return command == "POST" and path.split("?", 1)[0].endswith("/chat/completions")


def is_full(first: bytes) -> bool:
    """True when the start of a reply is LM Studio's "the context is full" refusal."""
    return FULL.lower() in first.lower()


def fix_reasoning(path: str, body: bytes, options_for) -> bytes:
    """Make a chat request's ``reasoning_effort`` one the model can take.

    hermes asks for a level ("medium"). A model whose reasoning is only on or off (LM Studio
    publishes ``["off", "on"]``) cannot take a level: LM Studio accepts the request, logs
    "reasoning 'medium' not supported" and falls back to its default. It refuses "on" and
    "off" in this field outright. So for such a model: "none" (off) passes, and any level is
    dropped, which leaves the model's default (on), exactly what happened before, without the
    warning. Anything else passes untouched. ``options_for(model)`` returns the published
    options, or [] when unknown."""
    if not path.split("?", 1)[0].endswith("/chat/completions") or b"reasoning_effort" not in body:
        return body
    try:
        req = json.loads(body)
    except ValueError:
        return body
    effort = req.get("reasoning_effort") if isinstance(req, dict) else None
    if not isinstance(effort, str) or effort == "none":
        return body
    options = set(options_for(str(req.get("model") or "")) or [])
    if not options or not options <= {"off", "on"}:
        return body
    del req["reasoning_effort"]
    return json.dumps(req).encode()


# Qwen's advice against endless repetition in thinking mode. Without it the local model sometimes
# repeats itself in its reasoning until hermes cuts the stream ("runaway repetition loop"): four
# failed turns on 19-20 Nov, each after 5 to 11 minutes of silence.
PRESENCE_PENALTY = 1.5


def add_presence_penalty(path: str, body: bytes) -> bytes:
    """Give a chat request :data:`PRESENCE_PENALTY` unless it sets its own. Anything else passes."""
    if not path.split("?", 1)[0].endswith("/chat/completions") or b"presence_penalty" in body:
        return body
    try:
        req = json.loads(body)
    except ValueError:
        return body
    if not isinstance(req, dict):
        return body
    req["presence_penalty"] = PRESENCE_PENALTY
    return json.dumps(req).encode()


def off_body() -> bytes:
    return json.dumps({"error": {"message": OFF_MESSAGE, "type": "model_server_off",
                                 "code": "model_server_off"}}).encode()


def make_handler(upstream: str, connect_timeout: float = 3.0, read_timeout: float = 900.0,
                 wait: float = WAIT, retry_every: float = RETRY_EVERY, sleep=time.sleep,
                 known_off: float = KNOWN_OFF, slots: int = SLOTS, max_queue_wait: float = MAX_QUEUE_WAIT,
                 log=print):
    target = urllib.parse.urlsplit(upstream)
    host, port = target.hostname, target.port or 80
    # For /guard/health: whether the last attempt found the model server off, and since when.
    # ``known_off_at``: when a request last waited the whole minute in vain. hermes tries a failed
    # call several times (and probes it once more without streaming); without this each of those
    # would wait a minute too, and the person would hear nothing for five.
    state = {"off": False, "off_since": None, "known_off_at": -known_off}
    options = {"at": -OPTIONS_TTL, "by_model": {}, "window": 0, "parallel": 0}
    gate = Gate(slots, lambda: options["window"], max_wait=max_queue_wait)

    def options_for(model: str) -> list:
        """LM Studio's published reasoning options for ``model``, refreshed every ten minutes."""
        if time.monotonic() - options["at"] > OPTIONS_TTL:
            options["at"] = time.monotonic()
            try:
                conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
                conn.request("GET", "/api/v1/models")
                data = json.loads(conn.getresponse().read() or b"{}")
                conn.close()
                models = [m for m in data.get("models", data.get("data", [])) if isinstance(m, dict)]
                options["by_model"] = {
                    str(m.get("key") or m.get("id")): list(((m.get("capabilities") or {}).get("reasoning") or {})
                                                           .get("allowed_options") or [])
                    for m in models}
                # The loaded window and LM Studio's own parallel slots, for the queue.
                configs = [i.get("config") or {} for m in models for i in (m.get("loaded_instances") or [])]
                options["window"] = max([int(c.get("context_length") or 0) for c in configs] or [0])
                options["parallel"] = max([int(c.get("parallel") or 0) for c in configs] or [0])
                gate.slots = max(1, min(slots, options["parallel"] or slots))
            except (OSError, ValueError, http.client.HTTPException):
                pass                                # unknown: requests pass unchanged
        return options["by_model"].get(model, [])

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "logos-model-guard"

        def log_message(self, *args) -> None:
            pass

        def _body(self) -> bytes:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                out = b""
                while True:
                    size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        return out
                    out += self.rfile.read(size)
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _off(self) -> None:
            body = off_body()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _health(self) -> None:
            # Tests the model server's connection (a few seconds at most), so agents waiting for
            # it and the dashboard learn when it is back without sending a model request.
            try:
                socket.create_connection((host, port), timeout=connect_timeout).close()
                if state["off"]:
                    print("model server answering again", flush=True)
                state["off"], state["off_since"], state["known_off_at"] = False, None, -known_off
            except OSError:
                if not state["off"]:
                    state["off"], state["off_since"] = True, time.time()
            out = json.dumps({"ok": True, "model_server_off": state["off"],
                              "off_since": state["off_since"], "queue": gate.view()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(out)

        def _connect(self, deadline: float):
            """A socket to the model server, trying every few seconds until ``deadline``; or None."""
            while True:
                try:
                    sock = socket.create_connection((host, port), timeout=connect_timeout)
                except OSError:
                    if not state["off"]:
                        state["off"], state["off_since"] = True, time.time()
                        print("model server not answering; requests wait up to a minute", flush=True)
                    if time.monotonic() + retry_every > deadline:
                        return None
                    sleep(retry_every)
                    continue
                if state["off"]:
                    print("model server answering again", flush=True)
                state["off"], state["off_since"], state["known_off_at"] = False, None, -known_off
                return sock

        def _pass(self) -> None:
            self.close_connection = True
            if self.command == "GET" and self.path == HEALTH_PATH:
                return self._health()
            body = self._body()
            if self.command == "POST":
                body = add_presence_penalty(self.path, fix_reasoning(self.path, body, options_for))
            if not is_chat(self.command, self.path) or time.monotonic() - state["known_off_at"] < known_off:
                return self._forward(body)          # lookups, and "the model is off" answers, never queue
            options_for("")                          # keeps the window and slots fresh (every ten minutes)
            tokens, prio = estimate(body), priority(body)
            ticket, waited, forced = gate.enter(tokens, prio)
            began, alongside = time.monotonic(), len(gate.running) - 1
            record = {}
            try:
                record = self._forward(body) or {}
            finally:
                gate.leave(ticket)
                log(json.dumps({"event": "chat", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "who": "scheduled" if prio == SCHEDULED else "person", "est_tokens": tokens,
                                "waited_s": round(waited, 1), "forced": forced, "alongside": alongside,
                                "seconds": round(time.monotonic() - began, 1), **record}), flush=True)

        def _forward(self, body: bytes):
            """Send the request on and stream the reply back. Returns what is known about the reply."""
            # Only model requests wait; lookups (model lists, health) get their answer at once.
            known = time.monotonic() - state["known_off_at"] < known_off
            deadline = time.monotonic() + (wait if self.command == "POST" and not known else 0)
            while True:
                sock = self._connect(deadline)
                if sock is None:
                    if self.command == "POST" and not known:
                        state["known_off_at"] = time.monotonic()
                    print(f"model server off: {self.command} {self.path.split('?')[0]} refused", flush=True)
                    self._off()
                    return {"status": "model_off"}
                conn = http.client.HTTPConnection(host, port)
                conn.sock = sock
                try:
                    sock.settimeout(read_timeout)
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                    headers["Host"] = f"{host}:{port}"
                    headers["Connection"] = "close"
                    conn.request(self.command, self.path, body=body or None, headers=headers)
                    resp = conn.getresponse()
                    length = resp.getheader("Content-Length")
                    # The first bytes decide: LM Studio's "context full" refusal is sent again later.
                    first = resp.read() if length is not None else resp.read1(65536)
                    if is_full(first) and time.monotonic() + retry_every <= deadline:
                        print("model server context full (other requests running); trying again", flush=True)
                        conn.close()
                        sleep(retry_every)
                        continue
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() not in HOP:
                            self.send_header(k, v)
                    if length is not None:
                        self.send_header("Content-Length", str(len(first)))
                    else:
                        self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    chunk, tail = first, b""
                    while chunk:
                        self.wfile.write(chunk if length is not None else b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()         # streamed replies must not sit in a buffer
                        tail = (tail + chunk)[-4096:]
                        if length is not None:
                            break
                        chunk = resp.read1(65536)
                    if length is None:
                        self.wfile.write(b"0\r\n\r\n")
                    return {"status": resp.status, **usage(tail)}
                except (OSError, http.client.HTTPException):
                    return {"status": "dropped"}   # either side went away; closing both is all there is to do
                finally:
                    conn.close()

        do_GET = do_POST = do_PUT = do_DELETE = _pass

    return Handler


def main() -> int:
    upstream = os.environ["GUARD_UPSTREAM"]
    port = int(os.environ.get("GUARD_PORT", "8094"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(
        upstream, connect_timeout=float(os.environ.get("GUARD_CONNECT_TIMEOUT", "3")),
        wait=float(os.environ.get("GUARD_WAIT", str(WAIT))),
        slots=int(os.environ.get("GUARD_SLOTS", str(SLOTS))),
        max_queue_wait=float(os.environ.get("GUARD_MAX_QUEUE_WAIT", str(MAX_QUEUE_WAIT)))))
    server.daemon_threads = True
    print(f"model guard: {port} -> {upstream}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
