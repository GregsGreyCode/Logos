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

The house speaker's first question used to wait some 35 seconds while the model read the Household
agent's instructions (about 24,000 tokens) afresh. So the guard keeps the latest house-speaker
request's instructions in memory (its system message and tools, never what was said) and every
:data:`WARM_EVERY` seconds, when nothing else is running, sends them again with a one-character
question and a one-token answer (:class:`Warmer`). LM Studio then still holds them when someone
speaks. It takes 0.1 to 1 s when they are still held, and adds nothing to any conversation.

Apart from that it keeps nothing, and changes two things in the traffic: models whose reasoning is only on or
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
import hashlib
import threading
import time
import zlib
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
HEALTH_PROBE = 1.0                      # seconds: /guard/health answers well inside its callers' 3 s
FULL = b"Context size has been exceeded"  # LM Studio: the shared context is in use by other requests


# ── keeping the model loaded at its window ──────────────────────────────────

class Loader:
    """Loads a model at the window Logos chose for it, before LM Studio loads it by itself.

    LM Studio loads a model that a request names but that is not loaded ("just in time") with its
    own defaults: on 23 Nov qwen came back at its full 262,144-token window, beside the copy being
    loaded at 96,512, after qwen was unloaded for a test. So, for each model in ``windows`` (the
    agents' ``load_context_length``, from GUARD_LOAD):

    - not loaded: the guard loads it once at its window; other requests wait for that load;
    - one copy loaded at that window or bigger: left alone, whatever settings it was loaded with
      (Greg, 28 Nov: a copy he loaded himself at 135,168 with his own KV-cache quantisation was
      being replaced at 96,512 on every request);
    - two or more copies: the biggest is kept when it is big enough, the others unloaded;
    - loaded smaller than its window: reloaded at its window, but only when no other reply is
      running through the guard (``alone``), so nothing is cut off;
    - anything failing: the request goes on as before, and LM Studio does what it would have done.
    """

    CHECK_EVERY = 5.0                        # seconds a look at what is loaded is trusted

    def __init__(self, host: str, port: int, windows: dict, log=print, timeout: float = 600.0,
                 clock=time.monotonic):
        self.host, self.port, self.windows, self.log, self.timeout, self.clock = host, port, dict(windows or {}), log, timeout, clock
        self.lock = threading.Lock()
        self.seen = {"at": -1e9, "loaded": {}}

    def _call(self, method: str, path: str, body=None, timeout: float = 10.0):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                         headers={"Content-Type": "application/json"} if body is not None else {})
            r = conn.getresponse()
            data = r.read()
            if r.status >= 400:
                raise OSError(f"{path}: {r.status} {data[:200]!r}")
            return json.loads(data or b"{}")
        finally:
            conn.close()

    def loaded(self, fresh: bool = False) -> dict:
        """model -> [(instance id, window)], from LM Studio's own list."""
        if fresh or self.clock() - self.seen["at"] > self.CHECK_EVERY:
            data = self._call("GET", "/api/v1/models")
            self.seen = {"at": self.clock(), "loaded": {
                str(m.get("key")): [(str(i.get("id")), int((i.get("config") or {}).get("context_length") or 0))
                                    for i in (m.get("loaded_instances") or [])]
                for m in data.get("models", []) if isinstance(m, dict) and m.get("loaded_instances")}}
        return self.seen["loaded"]

    def ensure(self, model: str, alone: bool) -> str:
        """What was done: "", "loaded", "reloaded", "trimmed" or "failed"."""
        # LM Studio's own key; also as the agents name it to the gateway ("lm-studio/qwen/…"),
        # in case it arrives with that prefix.
        model = next((k for k in self.windows if model == k or model.endswith("/" + k)), model)
        window = self.windows.get(model)
        if not window:
            return ""
        def fine(have) -> bool:
            return len(have) == 1 and have[0][1] >= window

        try:
            have = self.loaded().get(model, [])
            if have and (fine(have) or not alone):
                return ""
            with self.lock:
                have = self.loaded(fresh=True).get(model, [])
                if have and (fine(have) or not alone):
                    return ""                    # another request has just seen to it
                biggest = max(have, key=lambda h: h[1]) if have else None
                if biggest and biggest[1] >= window:
                    for inst, _ in have:         # keep the copy someone chose; drop the extras
                        if inst != biggest[0]:
                            self._call("POST", "/api/v1/models/unload", {"instance_id": inst}, timeout=120)
                    self.loaded(fresh=True)
                    self.log(json.dumps({"event": "model_extra_copies_unloaded", "model": model, "kept": biggest[1],
                                         "was": [w for _, w in have], "at": time.strftime("%Y-%m-%d %H:%M:%S")}), flush=True)
                    return "trimmed"
                for inst, _ in have:
                    self._call("POST", "/api/v1/models/unload", {"instance_id": inst}, timeout=120)
                self._call("POST", "/api/v1/models/load", {"model": model, "context_length": int(window),
                           "flash_attention": True, "offload_kv_cache_to_gpu": True}, timeout=self.timeout)
                self.loaded(fresh=True)
                what = "reloaded" if have else "loaded"
                self.log(json.dumps({"event": f"model_{what}", "model": model, "window": int(window),
                                     "was": [w for _, w in have], "at": time.strftime("%Y-%m-%d %H:%M:%S")}), flush=True)
                return what
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.log(json.dumps({"event": "model_load_failed", "model": model, "error": str(e)[:200]}), flush=True)
            return "failed"


def model_of(body: bytes) -> str:
    try:
        return str((json.loads(body or b"{}") or {}).get("model") or "")
    except (ValueError, AttributeError):
        return ""


# ── the queue ────────────────────────────────────────────────────────────────

SLOTS = 2                    # chat requests at once (GUARD_SLOTS overrides); never more than LM Studio's own
MAX_QUEUE_WAIT = 300.0       # seconds; then it goes anyway (Bifrost gives up on a silent stream after 900)
REPLY_RESERVE = 8192         # tokens kept for the reply (thinking included) when the request sets no max
BYTES_PER_TOKEN = 3.5        # a deliberately high estimate: JSON and tool schemas are dense
CRON_MARK = b"You are running as a scheduled cron job"
SPEAKER, PERSON, SCHEDULED = -1, 0, 1     # lower goes first
SPEAKER_MAX_WAIT = 5.0       # a house-speaker question waits at most this long, then runs alongside (GUARD_SPEAKER_MAX_WAIT)


# A picture costs the model a fixed number of tokens, whatever the size of its base64 text. On 27 Nov
# two phone screenshots (720x1600, ~118,000 base64 characters each) made a conversation of under 50,000
# tokens look like 119,000, over the 96,512 window, so the queue took it for one that could never
# share the model. hermes measured qwen3.6 at 709 to 1,372 tokens a picture; this is kept above that.
IMAGE_TOKENS = 1600
IMAGE_PARTS = ("image_url", "input_image", "image")


def _pictures(req: dict) -> tuple:
    """(how many pictures the request's messages carry, the bytes their inline data takes)."""
    count = size = 0
    for msg in req.get("messages") or ():
        content = msg.get("content") if isinstance(msg, dict) else None
        for part in content if isinstance(content, list) else ():
            if not isinstance(part, dict) or part.get("type") not in IMAGE_PARTS:
                continue
            count += 1
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            for data in (url, part.get("source", {}).get("data") if isinstance(part.get("source"), dict) else None):
                if isinstance(data, str):
                    size += len(data)
    return count, size


def estimate(body: bytes) -> int:
    """Tokens a chat request may take in the shared window: its prompt plus room for the reply.
    Pictures count :data:`IMAGE_TOKENS` each, not their base64 text."""
    try:
        req = json.loads(body)
        reply = int(req.get("max_tokens") or req.get("max_completion_tokens") or REPLY_RESERVE)
        count, size = _pictures(req)
    except (ValueError, TypeError, AttributeError):
        reply, count, size = REPLY_RESERVE, 0, 0
    return int(max(0, len(body) - size) / BYTES_PER_TOKEN) + count * IMAGE_TOKENS + min(reply, REPLY_RESERVE)


def priority(body: bytes) -> int:
    """Someone standing at the house speaker first (Greg, 27 Nov), then a person's message, then a
    scheduled job's."""
    if CRON_MARK in body:
        return SCHEDULED
    return SPEAKER if WARM_MARK.encode() in body else PERSON


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

    def enter(self, tokens: int, prio: int = PERSON, max_wait: float = None) -> tuple:
        """Block until this request may run. Returns (ticket, seconds waited, forced).
        ``max_wait`` overrides the gate's own (a speaker question's is a few seconds)."""
        limit = self.max_wait if max_wait is None else max_wait
        with self.cond:
            me = (prio, next(self.seq), tokens, self.clock())
            self.waiting.append(me)
            self.waiting.sort()
            queued = False
            while True:
                waited = self.clock() - me[3]
                forced = waited >= limit
                if self.waiting[0] is me and (self._fits(tokens) or (forced and len(self.running) < self.slots)
                                              or (forced and prio != SPEAKER)):
                    break
                queued = True
                self.cond.wait(timeout=max(0.05, min(5.0, limit - waited)))
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

    def idle(self) -> bool:
        with self.cond:
            return not self.running and not self.waiting

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


# ── keeping the house speaker's instructions warm ───────────────────────────
# Measured 27 Nov on qwen3.6-35b-a3b (docs/voice.md, "Why the first question was slow"): the Household
# agent's speaker requests start with ~24,000 tokens of tools and instructions. Read afresh that takes
# ~35 s; already held by LM Studio, 0.1 s, and ~1 s when only the end of the instructions changed (a new
# day's date, the hourly brain index). The model only reuses a prefix it computed itself, so the
# warm-up has to send exactly the same tools and system message as hermes does: the guard copies them.
WARM_MARK = "spoken aloud to the house speaker"  # in gateway.voice_converse.INSTRUCTIONS (a test checks)
WARM_EVERY = 900.0      # seconds between warm-ups of one prompt (GUARD_WARM_EVERY; 0 turns them off)
WARM_IDLE = 60.0        # only once no chat request has started or ended for this long
WARM_HOLD = 90.0        # a speaker request waits at most this long for a warm-up of its own prompt
WARM_KEEP = 3           # prompts remembered: one for each agent the speaker talks to
WARM_TEXT = "."         # the warm-up's question; the real question differs from here on anyway


def warm_seed(body: bytes):
    """``(key, warm-up body)`` for a house-speaker chat request, else None.

    The warm-up is the same request cut after its leading system message, with :data:`WARM_TEXT` as the
    question, one token to answer and no streaming. The key names the agent's prompt by model and tools,
    so a newer prompt (the next day's) replaces the older one."""
    if WARM_MARK.encode() not in body:
        return None
    try:
        req = json.loads(body)
    except ValueError:
        return None
    msgs = req.get("messages") if isinstance(req, dict) else None
    if not isinstance(msgs, list):
        return None
    head = list(itertools.takewhile(lambda m: isinstance(m, dict) and m.get("role") in ("system", "developer"), msgs))
    if not any(WARM_MARK in str(m.get("content")) for m in head):
        return None                              # the words were in a message, not in the instructions
    seed = {k: v for k, v in req.items()
            if k not in ("messages", "stream", "stream_options", "max_tokens", "max_completion_tokens", "n")}
    seed.update(messages=head + [{"role": "user", "content": WARM_TEXT}], max_tokens=1, stream=False)
    key = hashlib.sha256(json.dumps([req.get("model"), req.get("tools")], sort_keys=True).encode()).hexdigest()[:12]
    return key, json.dumps(seed).encode()


class Warmer:
    """The speaker prompts to keep warm, and when each was last sent (by a real request or a warm-up)."""

    def __init__(self, every: float = WARM_EVERY, idle: float = WARM_IDLE, keep: int = WARM_KEEP,
                 clock=time.monotonic):
        self.every, self.idle, self.keep, self.clock = every, idle, keep, clock
        self.lock = threading.Lock()
        self.seeds: dict = {}            # key -> {"body", "auth", "path", "at"}
        self.busy: dict = {}             # key -> Event, set when its warm-up is over
        self.last = clock()              # when a chat request last started or ended
        self.stats = {"warmed": 0, "cold": 0, "last_s": None}

    def saw(self, body: bytes, auth: str = None, path: str = "/v1/chat/completions"):
        """Every chat request passes here. Returns the prompt's key for a speaker request."""
        seed = warm_seed(body)
        with self.lock:
            self.last = self.clock()
            if not seed:
                return None
            key, warm = seed
            self.seeds.pop(key, None)
            self.seeds[key] = {"body": warm, "auth": auth, "path": path, "at": self.clock()}
            while len(self.seeds) > self.keep:
                self.seeds.pop(next(iter(self.seeds)))
            return key

    def ended(self) -> None:
        with self.lock:
            self.last = self.clock()

    def hold(self, key: str, timeout: float = WARM_HOLD) -> float:
        """Wait for a warm-up of this prompt that is under way: reading it twice at once is slower
        for both. Returns the seconds waited."""
        with self.lock:
            ev = self.busy.get(key)
        if ev is None:
            return 0.0
        t = self.clock()
        ev.wait(timeout)
        return self.clock() - t

    def due(self) -> list:
        """``[(key, seed)]`` to warm now: quiet for ``idle``, and not sent for ``every``."""
        now = self.clock()
        with self.lock:
            if self.every <= 0 or now - self.last < self.idle:
                return []
            return [(k, dict(s)) for k, s in self.seeds.items() if now - s["at"] >= self.every and k not in self.busy]

    def begin(self, key: str) -> None:
        with self.lock:
            self.busy[key] = threading.Event()

    def end(self, key: str, seconds: float, ok: bool) -> None:
        with self.lock:
            ev = self.busy.pop(key, None)
            if key in self.seeds:
                self.seeds[key]["at"] = self.clock()
            self.last = self.clock()
            if ok:
                self.stats["warmed"] += 1
                self.stats["cold"] += seconds >= COLD_S
                self.stats["last_s"] = round(seconds, 1)
        if ev is not None:
            ev.set()

    def view(self) -> dict:
        with self.lock:
            return {"prompts": len(self.seeds), "every_s": self.every, **self.stats}


COLD_S = 5.0     # a warm-up this slow had to read the prompt afresh: LM Studio had let it go


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


def quick_for_speaker(path: str, body: bytes) -> bytes:
    """A house-speaker request answers without thinking (``reasoning_effort: "none"``).

    Someone is standing at the speaker: 27 Nov the model thought for over three minutes about a
    misheard "This is the list." while the speaker gave up after 45 s. The voice instructions already
    ask for a short answer; this makes the model start it at once. Only the end of the request changes
    (how the answer begins), so the instructions LM Studio holds (:class:`Warmer`) still match.
    ``GUARD_SPEAKER_THINKING=1`` leaves speaker requests as hermes sent them."""
    if os.environ.get("GUARD_SPEAKER_THINKING") == "1" or not path.split("?", 1)[0].endswith("/chat/completions"):
        return body
    if WARM_MARK.encode() not in body:
        return body
    try:
        req = json.loads(body)
    except ValueError:
        return body
    msgs = req.get("messages") if isinstance(req, dict) else None
    if not isinstance(msgs, list):
        return body
    head = itertools.takewhile(lambda m: isinstance(m, dict) and m.get("role") in ("system", "developer"), msgs)
    if not any(WARM_MARK in str(m.get("content")) for m in head):
        return body
    req["reasoning_effort"] = "none"
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


# ── the loop watch (F33) ─────────────────────────────────────────────────────
# A local model can loop in its thinking: on 21 Nov qwen3.6 thought in circles for 715 s before
# hermes's own check cut it, and Greg read "Your request was not processed". Text that goes round in
# circles compresses far better than real thinking (prod replies, 22 Nov: at most 5x over a 4,000
# character window; loops written to test this, 27x to 52x), so the guard scores the newest
# LOOP_WINDOW characters of the reply's THINKING every LOOP_EVERY characters. At LOOP_RATIO or more it
# drops the connection without the closing chunk: hermes then sees a dropped stream and asks again
# by itself, because no answer text has reached the person yet (thinking never counts as
# delivered). The answer itself is not watched: a long table can be repetitive and still right, and
# hermes's own check covers answers that loop. The request that
# looped is remembered for LOOP_MEMORY seconds; if it comes again it goes with a little more
# temperature, so the second try does not walk into the same circle.
LOOP_WINDOW = 12000          # characters, about 3,000 tokens
LOOP_EVERY = 2000
LOOP_RATIO = 8.0
LOOP_MEMORY = 600.0
LOOP_TEMPERATURE = 0.3       # added on a retry after a cut (from 0.6 when the request sets none), at most 1.0


def compress_ratio(text: str) -> float:
    raw = text.encode("utf-8", "replace")
    return len(raw) / max(1, len(zlib.compress(raw, 6)))


class LoopWatch:
    """Reads a streamed chat reply (server-sent events) and says when its text has become a loop."""

    def __init__(self, window: int = LOOP_WINDOW, every: int = LOOP_EVERY, ratio: float = LOOP_RATIO):
        self.window, self.every, self.ratio = window, every, ratio
        self.pending, self.text, self.chars, self.next, self.worst = b"", "", 0, window, 0.0
        self.answer = 0                  # answer characters so far (counted, never scored)

    def feed(self, chunk: bytes) -> bool:
        self.pending += chunk
        *lines, self.pending = self.pending.split(b"\n")
        for line in lines:
            line = line.strip()
            if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            for choice in (event.get("choices") or []) if isinstance(event, dict) else []:
                delta = (choice or {}).get("delta") or {}
                for key in ("reasoning_content", "reasoning"):     # thinking only (see above)
                    piece = delta.get(key)
                    if isinstance(piece, str) and piece:
                        self.text = (self.text + piece)[-self.window:]
                        self.chars += len(piece)
                if isinstance(delta.get("content"), str):
                    self.answer += len(delta["content"])
        if self.chars < self.next:
            return False
        self.next = self.chars + self.every
        score = compress_ratio(self.text)
        self.worst = max(self.worst, score)
        return score >= self.ratio


def body_key(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def warm_after_loop(body: bytes) -> bytes:
    """The same request again after a loop cut: a little more temperature."""
    try:
        req = json.loads(body)
    except ValueError:
        return body
    if not isinstance(req, dict):
        return body
    try:
        base = float(req.get("temperature")) if req.get("temperature") is not None else 0.6
    except (TypeError, ValueError):
        base = 0.6
    req["temperature"] = round(min(1.0, base + LOOP_TEMPERATURE), 2)
    return json.dumps(req).encode()


def off_body() -> bytes:
    return json.dumps({"error": {"message": OFF_MESSAGE, "type": "model_server_off",
                                 "code": "model_server_off"}}).encode()


def make_handler(upstream: str, connect_timeout: float = 3.0, read_timeout: float = 900.0,
                 wait: float = WAIT, retry_every: float = RETRY_EVERY, sleep=time.sleep,
                 known_off: float = KNOWN_OFF, slots: int = SLOTS, max_queue_wait: float = MAX_QUEUE_WAIT,
                 log=print, loop_ratio: float = LOOP_RATIO, loop_window: int = LOOP_WINDOW, loop_every: int = LOOP_EVERY,
                 windows: dict = None, warm_every: float = WARM_EVERY, warm_idle: float = WARM_IDLE,
                 speaker_wait: float = SPEAKER_MAX_WAIT):
    target = urllib.parse.urlsplit(upstream)
    host, port = target.hostname, target.port or 80
    loader = Loader(host, port, windows or {}, log=log)
    # For /guard/health: whether the last attempt found the model server off, and since when.
    # ``known_off_at``: when a request last waited the whole minute in vain. hermes tries a failed
    # call several times (and probes it once more without streaming); without this each of those
    # would wait a minute too, and the person would hear nothing for five.
    state = {"off": False, "off_since": None, "known_off_at": -known_off}
    looped: dict = {}                       # body key -> when its reply was cut for looping
    # Replies streaming now, for the dashboard's "still thinking" line (F33): how long, how much
    # thinking and answer so far, and the loop score. Numbers only, never text.
    live: dict = {}
    live_ids = itertools.count(1)
    options = {"at": -OPTIONS_TTL, "by_model": {}, "window": 0, "parallel": 0}
    gate = Gate(slots, lambda: options["window"], max_wait=max_queue_wait)
    warmer = Warmer(warm_every, warm_idle)

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
            self._write(body)
            self.close_connection = True

        def _health(self) -> None:
            # Tests the model server's connection (a few seconds at most), so agents waiting for
            # it and the dashboard learn when it is back without sending a model request.
            # An off LM Studio drops connections rather than refusing them, so the probe runs its
            # whole timeout; the dashboard asks with a 3 s limit and, given the full 3 s connect
            # timeout here, hung up first and showed the guard itself as down (and lost the queue).
            try:
                socket.create_connection((host, port), timeout=min(connect_timeout, HEALTH_PROBE)).close()
                if state["off"]:
                    print("model server answering again", flush=True)
                state["off"], state["off_since"], state["known_off_at"] = False, None, -known_off
            except OSError:
                if not state["off"]:
                    state["off"], state["off_since"] = True, time.time()
            now = time.monotonic()
            running = [{"seconds": round(now - r["since"]), "who": r["who"], "thinking_chars": r["watch"].chars,
                        "answer_chars": r["watch"].answer, "loop_ratio": round(r["watch"].worst, 1)}
                       for r in list(live.values())]
            out = json.dumps({"ok": True, "model_server_off": state["off"],
                              "off_since": state["off_since"], "queue": gate.view(), "live": running,
                              "warm": warmer.view()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Connection", "close")
            self.end_headers()
            self._write(out)

        def _write(self, data: bytes) -> None:
            """A caller that gave up and hung up is not an error worth a traceback."""
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

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
                try:
                    return self._health()
                except (BrokenPipeError, ConnectionResetError):
                    return None                     # the caller gave up (see _health)
            body = self._body()
            if self.command == "POST":
                body = add_presence_penalty(self.path, fix_reasoning(self.path, quick_for_speaker(self.path, body), options_for))
                now = time.monotonic()
                for k in [k for k, at in looped.items() if now - at > LOOP_MEMORY]:
                    looped.pop(k, None)
                self._key = body_key(body)
                if is_chat(self.command, self.path) and self._key in looped:
                    body = warm_after_loop(body)       # hermes asking again after a loop cut
            if not is_chat(self.command, self.path) or time.monotonic() - state["known_off_at"] < known_off:
                return self._forward(body)          # lookups, and "the model is off" answers, never queue
            options_for("")                          # keeps the window and slots fresh (every ten minutes)
            tokens, prio = estimate(body), priority(body)
            self._who = {SCHEDULED: "scheduled", SPEAKER: "speaker"}.get(prio, "person")
            spoken = warmer.saw(body, self.headers.get("Authorization"), self.path)
            held = warmer.hold(spoken) if spoken else 0.0
            ticket, waited, forced = gate.enter(tokens, prio, speaker_wait if prio == SPEAKER else None)
            began, alongside = time.monotonic(), len(gate.running) - 1
            record = {"speaker": True, **({"held_s": round(held, 1)} if held >= 0.1 else {})} if spoken else {}
            try:
                # Loaded, and at its window, before LM Studio would load it by itself (Loader).
                fixed = loader.ensure(model_of(body), alone=alongside == 0)
                if fixed:
                    record["model"] = fixed
                    options["at"] = -OPTIONS_TTL     # the window may have changed: the queue looks again
                record.update(self._forward(body) or {})
            finally:
                gate.leave(ticket)
                warmer.ended()
                log(json.dumps({"event": "chat", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "who": {SCHEDULED: "scheduled", SPEAKER: "speaker"}.get(prio, "person"), "est_tokens": tokens,
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
                    tail = b""
                    watch = (LoopWatch(loop_window, loop_every, loop_ratio)
                             if length is None and resp.status == 200 and is_chat(self.command, self.path) and loop_ratio > 0
                             else None)
                    live_id = next(live_ids)
                    if watch is not None:
                        live[live_id] = {"since": time.monotonic(), "who": getattr(self, "_who", "person"), "watch": watch}
                    try:
                        return self._stream(resp, first, length, watch, tail)
                    finally:
                        live.pop(live_id, None)
                except (OSError, http.client.HTTPException):
                    return {"status": "dropped"}   # either side went away; closing both is all there is to do
                finally:
                    conn.close()

        def _stream(self, resp, chunk, length, watch, tail):
            """Copy the reply to the caller as it comes; cut it if its thinking loops (F33)."""
            while chunk:
                self.wfile.write(chunk if length is not None else b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()         # streamed replies must not sit in a buffer
                tail = (tail + chunk)[-4096:]
                if length is not None:
                    break
                if watch is not None and watch.feed(chunk):
                    # No closing chunk: the caller sees a dropped stream and asks again.
                    looped[getattr(self, "_key", "")] = time.monotonic()
                    self.close_connection = True
                    print(f"reply cut: its thinking went round in circles (x{watch.worst:.1f} over the last "
                          f"{loop_window} characters, {watch.chars} streamed)", flush=True)
                    return {"status": "loop_cut", "loop_ratio": round(watch.worst, 1), "chars": watch.chars}
                chunk = resp.read1(65536)
            if length is None:
                self.wfile.write(b"0\r\n\r\n")
            extra = {"loop_ratio": round(watch.worst, 1)} if watch is not None and watch.worst else {}
            return {"status": resp.status, **usage(tail), **extra}

        do_GET = do_POST = do_PUT = do_DELETE = _pass

    def warm_now() -> list:
        """Warm each speaker prompt that is due, one at a time, only while nothing else runs.
        Returns the log records. Never makes LM Studio load a model."""
        done = []
        for key, seed in warmer.due():
            if not gate.idle() or state["off"] or time.monotonic() - state["known_off_at"] < known_off:
                break                                # people first; and no use while it is off
            model = model_of(seed["body"])
            try:
                loaded = loader.loaded()
            except (OSError, ValueError, http.client.HTTPException):
                break
            if not any(model == k or model.endswith("/" + k) or k.endswith("/" + model) for k in loaded):
                continue                             # not loaded: warming would load it
            warmer.begin(key)
            ticket, _, _ = gate.enter(estimate(seed["body"]), SCHEDULED)
            began, record = time.monotonic(), {"status": "dropped"}
            try:
                record = send_warm(seed)
            finally:
                gate.leave(ticket)
                seconds = time.monotonic() - began
                warmer.end(key, seconds, record.get("status") == 200)
                line = {"event": "warm", "at": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt": key,
                        "seconds": round(seconds, 1), "cold": seconds >= COLD_S, **record}
                log(json.dumps(line), flush=True)
                done.append(line)
        return done

    def send_warm(seed: dict) -> dict:
        """One warm-up, its answer read and dropped."""
        headers = {"Content-Type": "application/json", "Host": f"{host}:{port}", "Connection": "close"}
        if seed.get("auth"):
            headers["Authorization"] = seed["auth"]
        conn = http.client.HTTPConnection(host, port, timeout=read_timeout)
        try:
            conn.request("POST", seed.get("path") or "/v1/chat/completions", body=seed["body"], headers=headers)
            resp = conn.getresponse()
            return {"status": resp.status, **usage(resp.read()[-4096:])}
        except (OSError, http.client.HTTPException):
            return {"status": "dropped"}
        finally:
            conn.close()

    Handler.warm_now = staticmethod(warm_now)
    Handler.warmer = warmer
    return Handler


def warm_loop(handler, every: float, sleep=time.sleep) -> None:
    """The guard's background warm-ups (see :class:`Warmer`)."""
    tick = max(5.0, min(60.0, every / 4))
    while True:
        sleep(tick)
        try:
            handler.warm_now()
        except Exception as e:                       # never take the guard down
            print(f"warm-up failed: {e}", flush=True)


def main() -> int:
    upstream = os.environ["GUARD_UPSTREAM"]
    port = int(os.environ.get("GUARD_PORT", "8094"))
    warm_every = float(os.environ.get("GUARD_WARM_EVERY") or WARM_EVERY)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(
        upstream, connect_timeout=float(os.environ.get("GUARD_CONNECT_TIMEOUT", "3")),
        wait=float(os.environ.get("GUARD_WAIT", str(WAIT))),
        slots=int(os.environ.get("GUARD_SLOTS", str(SLOTS))),
        max_queue_wait=float(os.environ.get("GUARD_MAX_QUEUE_WAIT", str(MAX_QUEUE_WAIT))),
        loop_ratio=float(os.environ.get("GUARD_LOOP_RATIO", str(LOOP_RATIO))),
        windows=json.loads(os.environ.get("GUARD_LOAD") or "{}"), warm_every=warm_every,
        speaker_wait=float(os.environ.get("GUARD_SPEAKER_MAX_WAIT", str(SPEAKER_MAX_WAIT)))))
    server.daemon_threads = True
    if warm_every > 0:
        threading.Thread(target=warm_loop, args=(server.RequestHandlerClass, warm_every), daemon=True).start()
    print(f"model guard: {port} -> {upstream}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
