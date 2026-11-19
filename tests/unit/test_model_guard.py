"""gateway/model_guard.py: pass traffic through when the model server answers, refuse at once when it does not."""
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
    assert Upstream.seen == [("/v1/chat/completions", "Bearer local", {"model": "qwen/q", "stream": True})]


def test_a_server_that_is_off_is_refused_at_once_in_plain_words():
    with socket.socket() as s:                    # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{dead}", connect_timeout=1))
    try:
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        started = time.monotonic()
        conn.request("POST", "/v1/chat/completions", body="{}", headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        error = json.loads(r.read())["error"]
        assert r.status == 503 and time.monotonic() - started < 2
        assert error["code"] == "model_server_off" and "switched off" in error["message"]
    finally:
        g.shutdown()


def test_a_server_that_was_off_is_refused_without_waiting_again_for_a_while():
    """With paid fallbacks every call would otherwise start with the full connection wait."""
    # 10.255.255.1 is not routable, so a connection attempt hangs until the timeout.
    g = serve(model_guard.make_handler("http://10.255.255.1:9", connect_timeout=0.6, off_memory=30))
    try:
        times = []
        for _ in range(3):
            conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
            started = time.monotonic()
            conn.request("POST", "/v1/chat/completions", body="{}", headers={"Content-Type": "application/json"})
            assert conn.getresponse().status == 503
            times.append(time.monotonic() - started)
        assert times[1] < 0.2 and times[2] < 0.2
    finally:
        g.shutdown()


def test_the_guard_reports_its_own_health_without_asking_the_model_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    g = serve(model_guard.make_handler(f"http://127.0.0.1:{dead}", connect_timeout=1))
    try:
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        conn.request("GET", "/guard/health")
        r = conn.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {"ok": True, "model_server_off": False}
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
