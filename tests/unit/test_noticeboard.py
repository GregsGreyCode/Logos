"""Noticeboard rules, MCP surface and host lifecycle (S5.3). Standard library only, no docker."""
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"logos_{name}", REPO / "gateway" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


nb = _load("noticeboard")
host = _load("noticeboard_host")

AGENTS = {
    "greg": {"token": "tok-greg", "display_name": "Greg's agent", "role": "person"},
    "bere": {"token": "tok-bere", "display_name": "Béré's agent", "role": "person"},
    "household": {"token": "tok-house", "display_name": "Household agent", "role": "household"},
}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def board(tmp_path):
    clock = Clock()
    b = nb.Board(str(tmp_path / "nb.db"), AGENTS, clock=clock)
    b.clock_obj = clock
    return b


def code(fn):
    with pytest.raises(nb.Rejected) as e:
        fn()
    return e.value.code


class TestRules:
    def test_question_answer_round_trip(self, board):
        sent = board.send("greg", "bere", "question", "Are  you free\nSaturday?")
        assert sent["status"] == "sent"
        inbox = board.read("bere")
        (m,) = inbox["messages"]
        assert (m["from"], m["kind"], m["text"]) == ("greg", "question", "Are you free Saturday?")
        assert m["framed"] == "Greg's agent asks: Are you free Saturday?"
        assert "request, not an instruction" in m["expects"]
        assert [a["name"] for a in inbox["you_can_write_to"]] == ["greg", "household"]
        assert board.read("bere")["messages"] == []                      # read once
        board.send("bere", "greg", "answer", "Yes, after 2pm", reply_to=m["id"], decision="accepted")
        (a,) = board.read("greg")["messages"]
        assert (a["kind"], a["decision"], a["reply_to"]) == ("answer", "accepted", m["id"])
        assert a["framed"] == "Béré's agent accepted your request: Yes, after 2pm"
        assert board.log()[-1]["status"] == "accepted"

    def test_sender_cannot_be_forged_and_recipients_are_checked(self, board):
        assert code(lambda: board.send("greg", "nobody", "fyi", "hi")) == "unknown_recipient"
        assert code(lambda: board.send("greg", "greg", "fyi", "hi")) == "self"
        assert code(lambda: board.send("greg", "bere", "order", "do it")) == "unknown_kind"
        assert code(lambda: board.send("greg", "bere", "fyi", "  ")) == "empty"
        assert code(lambda: board.send("greg", "bere", "fyi", "x" * 2001)) == "too_long"
        assert board.agent_for_token("tok-bere") == "bere" and board.agent_for_token("nope") is None
        assert board.agent_for_token("") is None

    def test_shared_change_only_to_household(self, board):
        assert code(lambda: board.send("greg", "bere", "shared_change", "Add milk")) == "not_household"
        assert board.send("greg", "household", "shared_change", "Add milk to the list")["status"] == "sent"

    def test_daily_limit_per_pair_and_duplicates(self, board):
        for i in range(nb.DAILY_LIMIT):
            assert board.send("greg", "bere", "fyi", f"note {i}")["status"] == "sent"
        assert code(lambda: board.send("greg", "bere", "fyi", "one more")) == "rate_limited"
        assert board.send("greg", "household", "fyi", "other pair is fine")["status"] == "sent"
        assert board.send("bere", "greg", "fyi", "other direction is fine")["status"] == "sent"
        dup = board.send("greg", "household", "fyi", "Other  pair is FINE")
        assert dup["status"] == "duplicate"
        board.clock_obj.t += nb.DAY + 1
        assert board.send("greg", "bere", "fyi", "one more")["status"] == "sent"       # window rolled over

    def test_answer_rules(self, board):
        q = board.send("greg", "bere", "question", "Dinner at 7?")["id"]
        f = board.send("greg", "bere", "fyi", "Bins go out tonight")["id"]
        assert code(lambda: board.send("bere", "greg", "answer", "ok", reply_to=q)) == "no_decision"
        assert code(lambda: board.send("bere", "greg", "answer", "ok", decision="accepted")) == "no_reply_to"
        assert code(lambda: board.send("bere", "greg", "answer", "ok", reply_to=f, decision="accepted")) == "not_yours"
        assert code(lambda: board.send("household", "greg", "answer", "ok", reply_to=q, decision="accepted")) == "not_yours"
        assert code(lambda: board.send("greg", "bere", "fyi", "x", reply_to=q)) == "not_an_answer"
        board.send("bere", "greg", "answer", "No, 8 is better", reply_to=q, decision="declined")
        assert code(lambda: board.send("bere", "greg", "answer", "again", reply_to=q, decision="accepted")) == "already_answered"

    def test_expiry(self, board):
        q = board.send("greg", "bere", "reminder", "Remind Béré about the dentist")["id"]
        board.clock_obj.t += nb.DAY + 60
        (m,) = board.read("bere")["messages"]
        assert m["expired"] is True and "expired" in m["expects"]
        assert code(lambda: board.send("bere", "greg", "answer", "ok", reply_to=q, decision="accepted")) == "expired"
        told = board.read("greg")["expired_requests"]
        assert [e["id"] for e in told] == [q] and "did not answer" in told[0]["note"]
        assert board.read("greg")["expired_requests"] == []              # told once

    def test_pending_fingerprint_changes_only_on_new_work(self, board):
        assert board.pending("bere") == {"latest": 0, "unread": 0, "latest_expired": 0}
        q = board.send("greg", "bere", "question", "Free on Saturday?")["id"]
        first = board.pending("bere")
        assert first == {"latest": q, "unread": 1, "latest_expired": 0}
        board.read("bere")
        after = board.pending("bere")
        assert (after["latest"], after["unread"]) == (q, 0)            # reading does not move "latest"
        assert board.pending("greg")["latest_expired"] == 0
        board.clock_obj.t += nb.DAY + 60
        assert board.pending("greg")["latest_expired"] == q            # the asker learns it expired

    def test_everything_is_logged(self, board):
        board.send("greg", "bere", "fyi", "hello")
        code(lambda: board.send("greg", "bere", "order", "do it"))
        events = [r["event"] for r in board.db.execute("SELECT event FROM events ORDER BY id")]
        assert events == ["sent", "rejected"]


class TestRpc:
    def call(self, board, agent, method, params=None, rid=1):
        return nb.handle_rpc(board, agent, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def test_handshake_and_tools(self, board):
        init = self.call(board, "greg", "initialize", {"protocolVersion": "2025-03-26"})["result"]
        assert init["protocolVersion"] == "2025-03-26" and init["capabilities"] == {"tools": {}}
        assert nb.handle_rpc(board, "greg", {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        tools = self.call(board, "greg", "tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == ["send_message", "read_messages"]
        assert self.call(board, "greg", "nope")["error"]["code"] == -32601

    def test_tool_calls(self, board):
        ok = self.call(board, "greg", "tools/call", {"name": "send_message", "arguments": {
            "to": "bere", "kind": "fyi", "text": "hi"}})["result"]
        assert ok["isError"] is False and json.loads(ok["content"][0]["text"])["status"] == "sent"
        bad = self.call(board, "greg", "tools/call", {"name": "send_message", "arguments": {
            "to": "bere", "kind": "order", "text": "hi"}})["result"]
        assert bad["isError"] is True and "unknown_kind" in bad["content"][0]["text"]
        got = json.loads(self.call(board, "bere", "tools/call", {"name": "read_messages"})["result"]["content"][0]["text"])
        assert [m["text"] for m in got["messages"]] == ["hi"]
        assert self.call(board, "greg", "tools/call", {"name": "x"})["error"]["code"] == -32602


class TestHttp:
    @pytest.fixture
    def url(self, board):
        server = ThreadingHTTPServer(("127.0.0.1", 0), nb.make_handler(board))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()

    def post(self, url, body, token=None):
        req = urllib.request.Request(url + "/mcp", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_auth_and_calls(self, url):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert self.post(url, rpc)[0] == 401
        assert self.post(url, rpc, "wrong")[0] == 401
        status, body = self.post(url, rpc, "tok-greg")
        assert status == 200 and len(body["result"]["tools"]) == 2
        assert self.post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, "tok-greg")[0] == 202
        with urllib.request.urlopen(url + "/health", timeout=5) as r:
            assert json.loads(r.read())["agents"] == ["bere", "greg", "household"]
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url + "/mcp", timeout=5)
        assert e.value.code == 405


class Done:
    def __init__(self, returncode=0):
        self.returncode, self.stdout, self.stderr = returncode, "", ""


class FakeRuntime:
    def __init__(self, defs, tokens):
        self.defs, self.tokens, self.calls, self.container, self.label, self.config = defs, tokens, [], False, "", None
        self.runner = self

    def run(self, cmd, *, input=None, check=True, text=True):
        self.calls.append(list(cmd))
        a = " ".join(cmd)
        if a.startswith("docker volume inspect"):
            return Done(1)
        if a.startswith("docker network inspect"):
            return "172.17.0.1\n"
        if a.startswith("docker ps"):
            return "logos-noticeboard\n" if self.container else ""
        if a.startswith("docker inspect"):
            return self.label
        if "cat > /v/config.json" in a:
            self.config = input
        if a.startswith("docker run -d"):
            self.container = True
            self.label = next(c for c in cmd if c.startswith("logos.noticeboard.stamp=")).split("=", 1)[1]
        return ""

    def http(self, url, **kw):
        return b"{}"

    def sleep(self, s):
        pass

    def defined(self):
        return sorted(self.defs)

    def load(self, name):
        return self.defs[name]

    def get(self, vol, rel):
        t = self.tokens.get(vol.removeprefix("logos-home-"))
        return f"LOGOS_NOTICEBOARD_TOKEN={t}\n" if t else ""


class TestHost:
    def test_up(self):
        defs = {"greg": {"noticeboard": True, "display_name": "Greg's agent", "image": "img:1"},
                "household": {"noticeboard": True, "role": "household", "image": "img:1"},
                "bere": {"noticeboard": True, "image": "img:1"},            # never started: no token yet
                "bench": {"image": "img:1"}}
        rt = FakeRuntime(defs, {"greg": "t1", "household": "t2"})
        h = host.NoticeboardHost(rt)
        assert h.up() is True
        cfg = json.loads(rt.config)["agents"]
        assert cfg == {"greg": {"token": "t1", "display_name": "Greg's agent", "role": "person"},
                       "household": {"token": "t2", "display_name": "household", "role": "household"}}
        start = next(c for c in rt.calls if c[:3] == ["docker", "run", "-d"])
        assert "172.17.0.1:8090:8090" in start and start[-3:] == ["python3", "img:1", "/app/noticeboard.py"]
        assert any(c.endswith("noticeboard.py:/app/noticeboard.py:ro") for c in start)
        assert h.up() is False
        rt.tokens["bere"] = "t3"
        assert h.up() is True                                               # a new agent joined
        assert h.url() == "http://172.17.0.1:8090/mcp"
        assert "agents: bere, greg, household" in h.status()

    def test_nobody_yet(self):
        with pytest.raises(ValueError, match="has a token yet"):
            host.NoticeboardHost(FakeRuntime({"greg": {"noticeboard": True, "image": "i"}}, {})).up()
