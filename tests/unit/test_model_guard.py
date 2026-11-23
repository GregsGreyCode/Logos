"""gateway/model_guard.py: pass traffic through; wait up to a minute for a model server that is off or full."""
import http.client
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gateway import model_guard


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/v1/models":          # LM Studio's own list, with reasoning options
            body = json.dumps({"models": [
                {"key": "qwen/q", "capabilities": {"reasoning": {"allowed_options": ["off", "on"], "default": "on"}}},
                {"key": "graded/m", "capabilities": {"reasoning": {"allowed_options": ["off", "minimal", "low"]}}}]}).encode()
        else:
            body = json.dumps({"data": [{"id": "qwen/q"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        sent = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append((self.path, self.headers.get("Authorization"), json.loads(sent)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for word in ("one", "two", "three"):
            data = f"data: {word}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()
            time.sleep(0.15)
        self.wfile.write(b"0\r\n\r\n")


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def guard():
    up = serve(Upstream)
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{up.server_address[1]}", connect_timeout=1))
    yield g.server_address[1]
    g.shutdown(); up.shutdown()


def test_a_plain_request_passes_through(guard):
    conn = http.client.HTTPConnection("127.0.0.1", guard, timeout=5)
    conn.request("GET", "/v1/models")
    r = conn.getresponse()
    assert r.status == 200 and json.loads(r.read()) == {"data": [{"id": "qwen/q"}]}


def test_a_streamed_reply_arrives_piece_by_piece(guard):
    Upstream.seen.clear()
    conn = http.client.HTTPConnection("127.0.0.1", guard, timeout=5)
    body = json.dumps({"model": "qwen/q", "stream": True})
    conn.request("POST", "/v1/chat/completions", body=body,
                 headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
    r = conn.getresponse()
    started = time.monotonic()
    first = r.read1(100)
    first_at = time.monotonic() - started
    rest = r.read()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    assert first == b"data: one\n\n" and first_at < 0.14          # not held back until the end
    assert rest == b"data: two\n\ndata: three\n\n"
    assert Upstream.seen == [("/v1/chat/completions", "Bearer local",
                             {"model": "qwen/q", "stream": True, "presence_penalty": model_guard.PRESENCE_PENALTY})]


def free_port():
    with socket.socket() as sk:                   # a port nothing listens on (yet)
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def post(port, timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("POST", "/v1/chat/completions", body=json.dumps({"model": "qwen/q", "stream": True}),
                 headers={"Content-Type": "application/json"})
    return conn.getresponse()


def test_a_server_that_stays_off_is_refused_in_plain_words_after_the_wait():
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{free_port()}", connect_timeout=0.2,
                                       wait=0.5, retry_every=0.1))
    try:
        started = time.monotonic()
        r = post(g.server_address[1])
        error = json.loads(r.read())["error"]
        waited = time.monotonic() - started
        assert r.status == 503 and 0.3 < waited < 2
        assert error["code"] == "model_server_off" and "the AI computer is off" in error["message"]
        assert "LM Studio" not in error["message"]                  # no product names to people
    finally:
        g.shutdown()


def test_after_one_full_wait_the_next_requests_are_answered_at_once():
    """hermes retries and probes a failed call; those must not each wait another minute."""
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{free_port()}", connect_timeout=0.1,
                                       wait=1.5, retry_every=0.1))
    try:
        times = []
        for _ in range(3):
            started = time.monotonic()
            assert post(g.server_address[1]).status == 503
            times.append(time.monotonic() - started)
        # Wide margins: the suite runs in parallel, and a busy machine slowed a 0.3 s bound (23 Nov).
        assert times[0] > 1.2 and times[1] < 0.75 and times[2] < 0.75
    finally:
        g.shutdown()


def test_lookups_do_not_wait_for_a_server_that_is_off():
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{free_port()}", connect_timeout=0.2, wait=5, retry_every=0.1))
    try:
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        started = time.monotonic()
        conn.request("GET", "/v1/models")
        assert conn.getresponse().status == 503 and time.monotonic() - started < 1
    finally:
        g.shutdown()


def test_a_request_waits_for_a_server_that_comes_back():
    port = free_port()
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{port}", connect_timeout=0.2, wait=5, retry_every=0.1))
    up = None
    try:
        def start_later():
            nonlocal up
            time.sleep(0.4)
            up = ThreadingHTTPServer(("127.0.0.1", port), Upstream)
            up.daemon_threads = True
            threading.Thread(target=up.serve_forever, daemon=True).start()
        threading.Thread(target=start_later, daemon=True).start()
        r = post(g.server_address[1])
        assert r.status == 200 and r.read() == b"data: one\n\ndata: two\n\ndata: three\n\n"
    finally:
        g.shutdown()
        if up:
            up.shutdown()


class FullOnce(Upstream):
    """LM Studio with its shared context in use: refuses the first ``refusals`` requests inside the stream."""
    refusals, calls = 1, 0

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        FullOnce.calls += 1
        if FullOnce.calls <= FullOnce.refusals:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            data = b'data: {"error":{"code":500,"message":"Context size has been exceeded.","type":"server_error"}}\n\n'
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(data), data))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        data = b"data: answer\n\n"
        self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(data), data))


def test_a_full_context_is_waited_out_and_the_request_sent_again():
    FullOnce.refusals, FullOnce.calls = 2, 0
    up = serve(FullOnce)
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{up.server_address[1]}", wait=5, retry_every=0.1))
    try:
        r = post(g.server_address[1])
        assert r.status == 200 and r.read() == b"data: answer\n\n" and FullOnce.calls == 3
    finally:
        g.shutdown(); up.shutdown()


def test_a_context_that_stays_full_is_passed_on_after_the_wait():
    FullOnce.refusals, FullOnce.calls = 99, 0
    up = serve(FullOnce)
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{up.server_address[1]}", wait=0.35, retry_every=0.1))
    try:
        r = post(g.server_address[1])
        assert b"Context size has been exceeded" in r.read() and 2 <= FullOnce.calls <= 5
    finally:
        g.shutdown(); up.shutdown()


def test_health_finds_a_server_that_is_back():
    up = serve(Upstream)
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{up.server_address[1]}", connect_timeout=0.5))
    try:
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        conn.request("GET", "/guard/health")
        h = json.loads(conn.getresponse().read())
        assert {k: h[k] for k in ("ok", "model_server_off", "off_since")} == {
            "ok": True, "model_server_off": False, "off_since": None}
        assert h["queue"]["running"] == 0 and h["queue"]["slots"] >= 1          # the queue is on show
    finally:
        g.shutdown(); up.shutdown()


def test_the_guard_health_reports_an_off_server():
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{free_port()}", connect_timeout=0.2,
                                       wait=0.1, retry_every=0.1))
    try:
        def health():
            conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
            conn.request("GET", "/guard/health")
            return json.loads(conn.getresponse().read())
        h = health()                              # the health check itself finds the server off
        assert h["ok"] and h["model_server_off"] is True and h["off_since"] > 0
        assert post(g.server_address[1]).status == 503
        assert health()["off_since"] == h["off_since"]
    finally:
        g.shutdown()


def test_fix_reasoning_only_touches_on_off_models():
    opts = {"qwen/q": ["off", "on"], "graded/m": ["off", "minimal", "low"]}.get
    path = "/v1/chat/completions"
    def fixed(body, p=path):
        return json.loads(model_guard.fix_reasoning(p, json.dumps(body).encode(), lambda m: opts(m) or []))
    assert fixed({"model": "qwen/q", "reasoning_effort": "medium", "x": 1}) == {"model": "qwen/q", "x": 1}
    assert fixed({"model": "qwen/q", "reasoning_effort": "none"})["reasoning_effort"] == "none"      # off stays off
    assert fixed({"model": "graded/m", "reasoning_effort": "low"})["reasoning_effort"] == "low"     # it has levels
    assert fixed({"model": "unknown", "reasoning_effort": "medium"})["reasoning_effort"] == "medium"
    assert fixed({"model": "qwen/q", "reasoning_effort": "medium"}, "/v1/embeddings")["reasoning_effort"] == "medium"
    assert model_guard.fix_reasoning(path, b"not json reasoning_effort", lambda m: ["off", "on"]) == b"not json reasoning_effort"


def test_the_guard_drops_a_level_an_on_off_model_cannot_take(guard):
    Upstream.seen.clear()
    conn = http.client.HTTPConnection("127.0.0.1", guard, timeout=5)
    conn.request("POST", "/v1/chat/completions", body=json.dumps({"model": "qwen/q", "reasoning_effort": "medium",
                                                                  "messages": []}),
                 headers={"Content-Type": "application/json"})
    conn.getresponse().read()
    (path, _auth, sent), = Upstream.seen
    assert path == "/v1/chat/completions" and "reasoning_effort" not in sent and sent["model"] == "qwen/q"


def test_add_presence_penalty_only_when_unset():
    path = "/v1/chat/completions"
    out = json.loads(model_guard.add_presence_penalty(path, json.dumps({"model": "m", "messages": []}).encode()))
    assert out["presence_penalty"] == model_guard.PRESENCE_PENALTY
    own = json.dumps({"model": "m", "presence_penalty": 0.2}).encode()
    assert model_guard.add_presence_penalty(path, own) == own                  # the caller's own value wins
    other = json.dumps({"model": "m"}).encode()
    assert model_guard.add_presence_penalty("/v1/embeddings", other) == other  # chat requests only
    assert model_guard.add_presence_penalty(path, b"not json") == b"not json"
    assert model_guard.add_presence_penalty(path, b"[1]") == b"[1]"


# ── the queue ────────────────────────────────────────────────────────────────

def run_in_thread(fn, *args):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn(*args)), daemon=True)
    t.start()
    return t, out


def test_estimate_priority_and_usage():
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "x" * 700}]}).encode()
    assert model_guard.estimate(body) == int(len(body) / model_guard.BYTES_PER_TOKEN) + model_guard.REPLY_RESERVE
    small = json.dumps({"model": "m", "max_tokens": 100}).encode()
    assert model_guard.estimate(small) == int(len(small) / model_guard.BYTES_PER_TOKEN) + 100
    assert model_guard.priority(b'... [IMPORTANT: You are running as a scheduled cron job. ...') == model_guard.SCHEDULED
    assert model_guard.priority(body) == model_guard.PERSON
    assert model_guard.usage(b'data: {"usage": {"prompt_tokens": 5120, "completion_tokens": 311}}\n\n') == {
        "prompt_tokens": 5120, "completion_tokens": 311}
    assert model_guard.usage(b"data: [DONE]") == {}


def test_the_gate_holds_to_its_slots():
    gate = model_guard.Gate(2, lambda: 0)
    a, _, _ = gate.enter(10)
    b, _, _ = gate.enter(10)
    t, out = run_in_thread(gate.enter, 10)
    time.sleep(0.2)
    assert "r" not in out and gate.view()["waiting"] == 1          # the third waits
    gate.leave(a)
    t.join(2)
    assert out["r"][1] > 0.1 and gate.view()["running"] == 2 and gate.view()["queued"] == 1


def test_the_gate_keeps_everything_inside_the_window():
    gate = model_guard.Gate(4, lambda: 96512)
    a, _, _ = gate.enter(70000)
    t, out = run_in_thread(gate.enter, 30000)                      # 100000 would not fit beside it
    time.sleep(0.2)
    assert "r" not in out
    gate.leave(a)
    t.join(2)
    assert "r" in out


def test_one_request_always_runs_alone_however_large():
    gate = model_guard.Gate(2, lambda: 1000)
    ticket, waited, forced = gate.enter(5000)
    assert waited < 0.1 and not forced


def test_a_person_goes_before_scheduled_work():
    gate = model_guard.Gate(1, lambda: 0)
    first, _, _ = gate.enter(10)
    order = []
    def go(prio, name):
        t = gate.enter(10, prio)[0]
        order.append(name)
        gate.leave(t)
    t1 = threading.Thread(target=go, args=(model_guard.SCHEDULED, "digest"), daemon=True)
    t1.start()
    time.sleep(0.1)
    t2 = threading.Thread(target=go, args=(model_guard.PERSON, "greg"), daemon=True)
    t2.start()
    time.sleep(0.1)
    gate.leave(first)
    t1.join(2); t2.join(2)
    assert order == ["greg", "digest"]                             # arrived later, served first


def test_after_the_longest_wait_it_goes_anyway():
    gate = model_guard.Gate(1, lambda: 0, max_wait=0.3)
    gate.enter(10)
    began = time.monotonic()
    ticket, waited, forced = gate.enter(10)
    assert forced and 0.3 <= waited < 1.5 and time.monotonic() - began < 1.5
    assert gate.view()["forced"] == 1


def test_chats_through_one_slot_take_turns_and_are_logged():
    up = serve(Upstream)
    lines = []
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{up.server_address[1]}", connect_timeout=1,
                                       slots=1, log=lambda line, **kw: lines.append(json.loads(line))))
    def chat():
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", body=json.dumps({"model": "qwen/q", "stream": True}),
                     headers={"Content-Type": "application/json"})
        conn.getresponse().read()
    try:
        threads = [threading.Thread(target=chat, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        for _ in range(50):                    # the guard logs just after the reply is sent
            if len(lines) == 2:
                break
            time.sleep(0.05)
        assert len(lines) == 2 and all(l["event"] == "chat" and l["status"] == 200 for l in lines)
        assert sorted(l["waited_s"] for l in lines)[1] >= 0.3          # the second waited for the first
        assert all(l["alongside"] == 0 for l in lines)                 # never two at once
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        conn.request("GET", model_guard.HEALTH_PATH)
        q = json.loads(conn.getresponse().read())["queue"]
        assert q["served"] == 2 and q["queued"] == 1 and q["slots"] == 1
    finally:
        g.shutdown(); up.shutdown()


def test_health_answers_inside_its_callers_limit_while_the_server_drops_connections(monkeypatch):
    # An off LM Studio (its PC on, the app closed) drops connections instead of refusing them, so
    # the probe runs its whole timeout. The dashboard asks with a 3 s limit (dashboard_api._alive).
    real, seen = socket.create_connection, []

    def dropping(addr, timeout=None, *a, **k):
        if addr[0] != "192.0.2.1":
            return real(addr, timeout, *a, **k)
        seen.append(timeout)
        raise socket.timeout("timed out")

    monkeypatch.setattr(model_guard.socket, "create_connection", dropping)
    g = serve(model_guard.make_handler("http://192.0.2.1:1234", connect_timeout=3.0))
    try:
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=3)
        conn.request("GET", "/guard/health")
        assert json.loads(conn.getresponse().read())["model_server_off"] is True
        assert seen and max(seen) <= model_guard.HEALTH_PROBE < 3
    finally:
        g.shutdown()


def test_a_caller_that_hangs_up_leaves_no_traceback(capsys):
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{free_port()}", connect_timeout=0.3))
    try:
        for _ in range(3):
            s = socket.create_connection(("127.0.0.1", g.server_address[1]))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.sendall(b"GET /guard/health HTTP/1.1\r\nHost: x\r\n\r\n")
            s.close()                                   # reset: gone before the probe ends
        time.sleep(0.8)
    finally:
        g.shutdown()
    assert "Traceback" not in capsys.readouterr().err


# ── F33: the loop watch ───────────────────────────────────────────────────────

LOOP = ("Wait, the user asked about holding the button. Let me check that again. Actually, hold, then "
        "swipe up to send or down to cancel. But is that what they meant? Let me re-read. ")
def _thinking(n=400, seed=7):
    """Varied, non-repeating text standing in for real thinking (real replies score under 5)."""
    import random
    rnd = random.Random(seed)
    words = ("the user asked about calendar events tomorrow morning dentist appointment bins collection "
             "rent payment trains delayed Béré wedding tiles kitchen budget shopping list milk eggs check "
             "whether brain page notes mention previous conversation answer briefly politely include time "
             "source date maybe ask clarify first second third option better worse because however then").split()
    return [" ".join(rnd.choice(words) for _ in range(rnd.randint(12, 30))) + ". " for _ in range(n)]


THINK = _thinking()


class Thinker(BaseHTTPRequestHandler):
    """Streams thinking as LM Studio does, looping or not, then an answer. Records each request body."""
    protocol_version = "HTTP/1.1"
    pieces: list = []
    bodies: list = []
    delay = 0.0

    def log_message(self, *a):
        pass

    def do_POST(self):
        Thinker.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for piece in Thinker.pieces + ["\n\n"]:
                time.sleep(Thinker.delay)
                ev = b"data: " + json.dumps({"choices": [{"delta": {"reasoning_content": piece}}]}).encode() + b"\n\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(ev), ev)); self.wfile.flush()
            for ev in (b'data: {"choices": [{"delta": {"content": "Hold, then swipe up."}}]}\n\n', b"data: [DONE]\n\n"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(ev), ev))
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass


def guard_on(up, logs):
    return serve(model_guard.make_handler(f"http://127.0.0.1:{up.server_address[1]}",
                                          log=lambda line, **_: logs.append(line)))


def test_the_loop_score_tells_thinking_from_circles():
    assert model_guard.compress_ratio("".join(THINK)[-model_guard.LOOP_WINDOW:]) < 5
    assert model_guard.compress_ratio((LOOP * 200)[-model_guard.LOOP_WINDOW:]) > 20


def chat(port, req):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/v1/chat/completions", body=json.dumps(req), headers={"Content-Type": "application/json"})
    return conn.getresponse().read()


def test_thinking_that_loops_is_cut_without_a_closing_chunk():
    Thinker.pieces, Thinker.bodies, logs = [LOOP] * 400, [], []
    up = serve(Thinker); g = guard_on(up, logs)
    try:
        with pytest.raises(http.client.IncompleteRead):  # a dropped stream (hermes, through Bifrost, retries it)
            chat(g.server_address[1], {"model": "qwen/q", "stream": True})
        time.sleep(0.2)
        line = json.loads(logs[-1])
        assert line["status"] == "loop_cut" and line["loop_ratio"] >= model_guard.LOOP_RATIO
        assert line["chars"] < len(LOOP) * 400 / 2         # cut early, not at the end
    finally:
        g.shutdown(); up.shutdown()


def test_the_same_request_after_a_cut_goes_a_little_warmer():
    Thinker.pieces, Thinker.bodies, logs = [LOOP] * 400, [], []
    up = serve(Thinker); g = guard_on(up, logs)
    port = g.server_address[1]
    req = {"model": "qwen/q", "stream": True, "messages": [{"role": "user", "content": "voice notes?"}]}
    try:
        for _ in range(2):
            with pytest.raises(http.client.IncompleteRead):
                chat(port, req)
        Thinker.pieces = THINK[:50]                     # a fresh question is not warmed
        chat(port, {**req, "messages": [{"role": "user", "content": "other"}]})
        temps = [b.get("temperature") for b in Thinker.bodies]
        assert temps == [None, 0.9, None]
    finally:
        g.shutdown(); up.shutdown()


def test_long_real_thinking_and_its_answer_pass_whole():
    Thinker.pieces, Thinker.bodies, logs = THINK, [], []
    up = serve(Thinker); g = guard_on(up, logs)
    try:
        body = post(g.server_address[1]).read()
        assert body.endswith(b"data: [DONE]\n\n") and b"swipe up" in body
        time.sleep(0.2)
        line = json.loads(logs[-1])
        assert line["status"] == 200 and 0 < line["loop_ratio"] < model_guard.LOOP_RATIO
    finally:
        g.shutdown(); up.shutdown()


def test_a_repetitive_answer_is_not_watched():
    # A long list in the answer can repeat itself and still be right; only thinking is watched.
    w = model_guard.LoopWatch()
    ev = b'data: ' + json.dumps({"choices": [{"delta": {"content": LOOP}}]}).encode() + b"\n\n"
    assert not any(w.feed(ev) for _ in range(400))


def test_health_shows_each_streaming_reply_as_numbers_only():
    Thinker.pieces, Thinker.bodies, Thinker.delay, logs = THINK[:30], [], 0.05, []
    up = serve(Thinker); g = guard_on(up, logs)
    try:
        t = threading.Thread(target=lambda: post(g.server_address[1], timeout=20).read(), daemon=True)
        t.start(); time.sleep(0.8)
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        conn.request("GET", "/guard/health")
        live = json.loads(conn.getresponse().read())["live"]
        assert len(live) == 1 and live[0]["who"] == "person" and live[0]["thinking_chars"] > 0
        assert live[0]["answer_chars"] == 0 and set(live[0]) == {"seconds", "who", "thinking_chars", "answer_chars", "loop_ratio"}
        t.join(10)
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        conn.request("GET", "/guard/health")
        assert json.loads(conn.getresponse().read())["live"] == []      # gone once it finished
    finally:
        Thinker.delay = 0.0
        g.shutdown(); up.shutdown()


# ── the model loaded at its window, before LM Studio loads it by itself (23 Nov) ─────────────

class FakeStudio(BaseHTTPRequestHandler):
    """LM Studio's model list, load and unload; ``state`` is shared through the server."""
    def log_message(self, *a):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        st = self.server.state
        self._json(200, {"models": [{"key": k, "loaded_instances": [{"id": i, "config": {"context_length": w}} for i, w in v]}
                                    for k, v in st["loaded"].items()]})

    def do_POST(self):
        st = self.server.state
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        st["calls"].append((self.path, body))
        if self.path == "/api/v1/models/load":
            if st.get("refuse"):
                return self._json(400, {"error": "insufficient system resources"})
            time.sleep(0.2)
            st["loaded"].setdefault(body["model"], []).append((body["model"], body["context_length"]))
            return self._json(200, {"status": "loaded"})
        if self.path == "/api/v1/models/unload":
            for k in st["loaded"]:
                st["loaded"][k] = [x for x in st["loaded"][k] if x[0] != body["instance_id"]]
            st["loaded"] = {k: v for k, v in st["loaded"].items() if v}
            return self._json(200, {"instance_id": body["instance_id"]})
        self._json(404, {})


def studio(loaded=None, refuse=False):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeStudio)
    srv.daemon_threads = True
    srv.state = {"loaded": dict(loaded or {}), "calls": [], "refuse": refuse}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


QWEN, WIN = "qwen/qwen3.6-35b-a3b", 96512


def loader_for(srv, logs=None):
    return model_guard.Loader("127.0.0.1", srv.server_address[1], {QWEN: WIN}, log=lambda *a, **k: (logs if logs is not None else []).append(a[0]))


def test_a_model_that_is_not_loaded_is_loaded_once_at_its_window_however_many_ask():
    srv = studio()
    try:
        ld, out = loader_for(srv), []
        threads = [threading.Thread(target=lambda: out.append(ld.ensure(QWEN, alone=False))) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        loads = [b for p, b in srv.state["calls"] if p.endswith("/load")]
        assert loads == [{"model": QWEN, "context_length": WIN, "flash_attention": True, "offload_kv_cache_to_gpu": True}]
        assert sorted(out) == ["", "", "", "loaded"] and srv.state["loaded"][QWEN] == [(QWEN, WIN)]
    finally:
        srv.shutdown()


def test_a_copy_at_the_wrong_window_is_replaced_only_when_nothing_else_is_running():
    srv = studio({QWEN: [(QWEN, 262144)]})
    try:
        ld = loader_for(srv)
        assert ld.ensure(QWEN, alone=False) == "" and srv.state["calls"] == []     # a reply running: left alone
        assert ld.ensure(QWEN, alone=True) == "reloaded"
        assert srv.state["loaded"][QWEN] == [(QWEN, WIN)]
        assert [p for p, _ in srv.state["calls"]] == ["/api/v1/models/unload", "/api/v1/models/load"]
        assert ld.ensure(QWEN, alone=True) == ""                                     # right now: nothing more
    finally:
        srv.shutdown()


def test_two_copies_become_one_at_its_window():
    srv = studio({QWEN: [(QWEN, 262144), (QWEN + ":2", WIN)]})
    try:
        assert loader_for(srv).ensure(QWEN, alone=True) == "reloaded"
        assert srv.state["loaded"][QWEN] == [(QWEN, WIN)]
    finally:
        srv.shutdown()


def test_other_models_and_a_refused_load_leave_things_as_they_were():
    srv = studio(refuse=True)
    try:
        logs = []
        ld = loader_for(srv, logs)
        assert ld.ensure("someone/else", alone=True) == "" and srv.state["calls"] == []
        assert ld.ensure(QWEN, alone=True) == "failed"
        assert "model_load_failed" in logs[-1]
    finally:
        srv.shutdown()


def test_the_windows_come_from_the_agents():
    from gateway import model_gateway
    agents = {"greg": {"model": {"provider": "lmstudio", "default": QWEN, "context_length": 65536, "load_context_length": WIN}},
              "bere": {"model": {"provider": "lmstudio", "default": QWEN, "context_length": 32768}},
              "paid": {"model": {"provider": "anthropic", "default": "claude-x"}}}
    assert model_gateway.load_windows(agents) == {QWEN: WIN}
