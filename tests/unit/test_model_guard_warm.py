"""gateway/model_guard.py: keeping the house speaker's prompt warm in LM Studio (Warmer)."""
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gateway import model_gateway as mg
from gateway import model_guard
from gateway import voice_converse

TOOLS = [{"type": "function", "function": {"name": "lights", "parameters": {"type": "object"}}}]
SYSTEM = "You are Home.\n\nMEMORY ...\n\n" + voice_converse.INSTRUCTIONS


def speaker(question="Turn on the kitchen lights.", history=(), model="qwen/q", tools=TOOLS):
    return {"model": model, "stream": True, "stream_options": {"include_usage": True}, "tools": tools,
            "presence_penalty": 1.5,
            "messages": [{"role": "system", "content": SYSTEM}, *history, {"role": "user", "content": question}]}


def body(req):
    return json.dumps(req).encode()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_the_speaker_instructions_carry_the_mark_the_guard_looks_for():
    assert model_guard.WARM_MARK in voice_converse.INSTRUCTIONS


def test_a_warm_up_is_the_same_prompt_with_a_one_token_dot_and_nothing_said():
    key, warm = model_guard.warm_seed(body(speaker(history=[
        {"role": "user", "content": "What time is it?"}, {"role": "assistant", "content": "Six."}])))
    w = json.loads(warm)
    assert w["messages"] == [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "."}]
    assert w["tools"] == TOOLS and w["model"] == "qwen/q" and w["presence_penalty"] == 1.5
    assert w["max_tokens"] == 1 and w["stream"] is False and "stream_options" not in w
    assert "kitchen" not in warm.decode() and "Six" not in warm.decode()     # never what was said
    # The same agent's prompt whatever was asked; another agent (other tools) has its own.
    assert model_guard.warm_seed(body(speaker("And tomorrow?")))[0] == key
    assert model_guard.warm_seed(body(speaker(tools=TOOLS + TOOLS)))[0] != key


def test_only_speaker_requests_are_warmed():
    plain = speaker()
    plain["messages"][0]["content"] = "You are Home."
    assert model_guard.warm_seed(body(plain)) is None
    plain["messages"][-1]["content"] = "quote: " + model_guard.WARM_MARK        # said, not instructed
    assert model_guard.warm_seed(body(plain)) is None
    assert model_guard.warm_seed(b"not json " + model_guard.WARM_MARK.encode()) is None


def test_a_prompt_is_warmed_after_quiet_and_not_again_too_soon():
    clock = Clock()
    w = model_guard.Warmer(every=600, idle=60, keep=2, clock=clock)
    key = w.saw(body(speaker()))
    assert key and w.due() == []
    clock.t += 59
    assert w.due() == []                                  # just used: LM Studio holds it
    clock.t += 600
    assert [k for k, _ in w.due()] == [key]
    w.saw(body({"model": "qwen/q", "messages": [{"role": "user", "content": "hi"}]}))   # other traffic
    assert w.due() == []                                  # not while others use the model
    clock.t += 61
    w.begin(key)
    assert w.due() == []                                  # not twice at once
    w.end(key, 0.2, ok=True)
    clock.t += 61
    assert w.due() == []                                  # warmed a minute ago
    assert w.view()["warmed"] == 1 and w.view()["cold"] == 0


def test_only_the_newest_prompts_are_kept():
    w = model_guard.Warmer(keep=2, clock=Clock())
    for n in range(3):
        w.saw(body(speaker(tools=[dict(TOOLS[0], n=n)])))
    assert len(w.seeds) == 2


def test_zero_turns_warming_off():
    clock = Clock()
    w = model_guard.Warmer(every=0, idle=0, clock=clock)
    w.saw(body(speaker()))
    clock.t += 10_000
    assert w.due() == []


# ── through the guard, against a fake LM Studio ──────────────────────────────

class Studio(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bodies: list = []
    loaded = True
    delay = 0.0

    def log_message(self, *a):
        pass

    def _json(self, data):
        out = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        inst = [{"id": "qwen/q", "config": {"context_length": 96512, "parallel": 4}}] if Studio.loaded else []
        self._json({"models": [{"key": "qwen/q", "loaded_instances": inst}]})

    def do_POST(self):
        Studio.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        time.sleep(Studio.delay)
        self._json({"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 24000, "completion_tokens": 1}})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def setup(every=0.01, idle=0.0):
    Studio.bodies, Studio.loaded, Studio.delay = [], True, 0.0
    up = serve(Studio)
    logs = []
    handler = model_guard.make_handler(f"http://127.0.0.1:{up.server_address[1]}", connect_timeout=1,
                                       log=lambda line, **_: logs.append(json.loads(line)),
                                       warm_every=every, warm_idle=idle)
    g = serve(handler)
    return up, g, handler, logs


def ask(port, req):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/v1/chat/completions", body=json.dumps(req), headers={"Content-Type": "application/json"})
    return conn.getresponse().read()


def test_the_guard_warms_the_last_speaker_prompt_with_one_token():
    up, g, handler, logs = setup()
    try:
        ask(g.server_address[1], speaker())
        time.sleep(0.05)
        done = handler.warm_now()
        assert [d["event"] for d in done] == ["warm"] and done[0]["status"] == 200 and not done[0]["cold"]
        assert done[0]["prompt_tokens"] == 24000
        warm = Studio.bodies[-1]
        assert warm["max_tokens"] == 1 and warm["messages"][-1] == {"role": "user", "content": "."}
        assert warm["messages"][0]["content"] == SYSTEM and warm["tools"] == TOOLS
        assert any(l["event"] == "chat" and l.get("speaker") for l in logs)
        conn = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=5)
        conn.request("GET", model_guard.HEALTH_PATH)
        assert json.loads(conn.getresponse().read())["warm"]["warmed"] == 1
    finally:
        g.shutdown(); up.shutdown()


def test_nothing_is_warmed_without_a_speaker_request_or_while_the_model_is_not_loaded():
    up, g, handler, logs = setup()
    try:
        ask(g.server_address[1], {"model": "qwen/q", "messages": [{"role": "user", "content": "hi"}]})
        assert handler.warm_now() == []
        Studio.loaded = False
        ask(g.server_address[1], speaker())
        time.sleep(0.05)
        n = len(Studio.bodies)
        assert handler.warm_now() == [] and len(Studio.bodies) == n      # warming would load it
    finally:
        g.shutdown(); up.shutdown()


def test_a_speaker_request_waits_for_a_warm_up_of_its_own_prompt():
    up, g, handler, logs = setup()
    try:
        ask(g.server_address[1], speaker())
        time.sleep(0.05)
        Studio.delay = 0.6
        warming = threading.Thread(target=handler.warm_now, daemon=True)
        warming.start()
        time.sleep(0.2)
        Studio.delay = 0.0
        t = time.monotonic()
        ask(g.server_address[1], speaker("And the hall?"))
        assert time.monotonic() - t >= 0.3                          # it read the prompt once, after the warm-up
        warming.join(5)
        time.sleep(0.05)
        chat = [l for l in logs if l["event"] == "chat"][-1]
        assert chat["speaker"] and chat["held_s"] >= 0.3
    finally:
        g.shutdown(); up.shutdown()


def test_the_warm_up_interval_reaches_the_guard_container(monkeypatch):
    class Rt:
        def __init__(self):
            self.calls = []

        def run(self, cmd, *, input=None, check=True):
            self.calls.append(list(cmd))
            return "172.17.0.1\n" if cmd[:3] == ["docker", "network", "inspect"] else ""

    rt = Rt()
    mg.ModelGateway(rt).guard_up("http://192.0.2.5:1234/v1", "logos-agent:1")
    run = next(c for c in rt.calls if c[:3] == ["docker", "run", "-d"])
    assert not any(x.startswith("GUARD_WARM_EVERY=") for x in run)       # the guard's default
    monkeypatch.setenv(mg.WARM_ENV, "0")
    rt = Rt()
    mg.ModelGateway(rt).guard_up("http://192.0.2.5:1234/v1", "logos-agent:1")
    assert "GUARD_WARM_EVERY=0" in next(c for c in rt.calls if c[:3] == ["docker", "run", "-d"])


def test_a_speaker_request_answers_without_thinking(monkeypatch):
    from gateway import model_guard as mg
    monkeypatch.delenv("GUARD_SPEAKER_THINKING", raising=False)
    sys_msg = {"role": "system", "content": "Soul. This message was " + mg.WARM_MARK + " and ..."}
    body = json.dumps({"model": "q", "reasoning_effort": "medium",
                       "messages": [sys_msg, {"role": "user", "content": "hi"}]}).encode()
    out = json.loads(mg.quick_for_speaker("/v1/chat/completions", body))
    assert out["reasoning_effort"] == "none" and out["messages"] == [sys_msg, {"role": "user", "content": "hi"}]
    # "none" survives fix_reasoning for an on/off model
    assert json.loads(mg.fix_reasoning("/v1/chat/completions", json.dumps(out).encode(), lambda m: ["off", "on"]))["reasoning_effort"] == "none"
    # the words in a user message are not enough; other paths and the switch leave it alone
    other = json.dumps({"messages": [{"role": "system", "content": "x"}, {"role": "user", "content": mg.WARM_MARK}]}).encode()
    assert mg.quick_for_speaker("/v1/chat/completions", other) == other
    assert mg.quick_for_speaker("/v1/embeddings", body) == body
    monkeypatch.setenv("GUARD_SPEAKER_THINKING", "1")
    assert mg.quick_for_speaker("/v1/chat/completions", body) == body
