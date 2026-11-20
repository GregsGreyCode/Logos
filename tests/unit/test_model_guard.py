"""gateway/model_guard.py: pass traffic through; wait up to a minute for a model server that is off or full."""
import http.client
import json
import socket
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
                                       wait=0.6, retry_every=0.1))
    try:
        times = []
        for _ in range(3):
            started = time.monotonic()
            assert post(g.server_address[1]).status == 503
            times.append(time.monotonic() - started)
        assert times[0] > 0.4 and times[1] < 0.3 and times[2] < 0.3
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
        assert json.loads(conn.getresponse().read()) == {"ok": True, "model_server_off": False, "off_since": None}
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
