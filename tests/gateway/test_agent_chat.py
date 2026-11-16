"""Dashboard chat <-> hermes run bridge, agent links and agent visibility (S2.8, S3.5)."""

import asyncio

import pytest

from gateway import agent_chat
from gateway.agent_links import AgentLinkError, AgentLinks, api_key_from_env, forward_argv
from gateway.hermes_client import HermesConnectionError, normalise_event


def ev(name, **payload):
    return normalise_event({"event": name, **payload})


class TestTranslate:
    def test_tokens_then_done(self):
        st = agent_chat.TurnState(model="m")
        assert agent_chat.translate(ev("message.delta", delta="po"), st) == [{"type": "token", "content": "po"}]
        out = agent_chat.translate(
            ev("run.completed", output="pong", usage={"input_tokens": 12, "output_tokens": 3}), st)
        assert [o["type"] for o in out] == ["done"]          # text already streamed: no duplicate message
        assert out[0]["prompt_tokens"] == 12 and out[0]["completion_tokens"] == 3
        assert out[0]["model"] == "m" and out[0]["tools_used"] == 0

    def test_unstreamed_output_is_sent_as_a_message(self):
        st = agent_chat.TurnState()
        out = agent_chat.translate(ev("run.completed", output="hello"), st)
        assert out[0] == {"type": "message", "content": "hello"}
        assert out[1]["type"] == "done"

    def test_tools_pair_up_without_call_ids(self):
        st = agent_chat.TurnState()
        a = agent_chat.translate(ev("tool.started", tool="terminal", preview="date"), st)[0]
        b = agent_chat.translate(ev("tool.started", tool="read_file"), st)[0]
        ea = agent_chat.translate(ev("tool.completed", tool="terminal", duration=0.25), st)[0]
        eb = agent_chat.translate(ev("tool.completed", tool="read_file", error=True), st)[0]
        assert a == {"type": "tool_start", "call_id": "call-1", "tool": "terminal", "preview": "date"}
        assert (ea["call_id"], ea["success"], ea["duration_ms"]) == ("call-1", True, 250)
        assert (eb["call_id"], eb["success"]) == (b["call_id"], False)
        assert agent_chat.translate(ev("run.completed"), st)[-1]["tools_used"] == 2

    def test_call_id_from_hermes_is_kept(self):
        st = agent_chat.TurnState()
        out = agent_chat.translate(ev("tool.started", tool="t", tool_call_id="abc"), st)
        assert out[0]["call_id"] == "abc"

    def test_reasoning_failure_cancel_and_unknown(self):
        st = agent_chat.TurnState()
        assert agent_chat.translate(ev("reasoning.available", text="hm"), st) == [{"type": "thinking", "content": "hm"}]
        failed = agent_chat.translate(ev("run.failed", error="boom"), st)[0]
        assert failed["type"] == "error" and failed["content"] == "boom"
        stopped = agent_chat.translate(ev("run.cancelled"), st)
        assert stopped[0] == {"type": "message", "content": "(stopped)"}
        assert stopped[1]["type"] == "done" and stopped[1]["stopped"] is True
        assert agent_chat.translate(ev("something.new"), st) == []


def test_session_ids_are_per_user_and_safe():
    a = agent_chat.session_id_for("usr_1", "chat 1/../x")
    assert a == "web-usr_1-chat-1-..-x"
    assert a != agent_chat.session_id_for("usr_2", "chat 1/../x")
    assert agent_chat.session_id_for("", "") == "web-anon-default"


class FakeClient:
    def __init__(self, events=(), start_error=None, stream_error=None):
        self.events, self.start_error, self.stream_error = list(events), start_error, stream_error
        self.started = []

    async def start_run(self, message, *, session_id):
        if self.start_error:
            raise self.start_error
        self.started.append((message, session_id))
        return "run_1"

    async def stream_events(self, run_id):
        for e in self.events:
            yield e
        if self.stream_error:
            raise self.stream_error


async def collect(client, **kw):
    return [p async for p in agent_chat.stream_run(client, "hi", session_id="s", **kw)]


class TestStreamRun:
    def test_happy_path(self):
        seen = []
        client = FakeClient([ev("message.delta", delta="pong"), ev("run.completed", output="pong")])
        out = asyncio.run(collect(client, on_started=seen.append))
        assert [p["type"] for p in out] == ["task_started", "token", "done"]
        assert out[0]["task_id"] == "run_1" and seen == ["run_1"]
        assert client.started == [("hi", "s")]

    def test_start_failure_becomes_an_error_event(self):
        out = asyncio.run(collect(FakeClient(start_error=HermesConnectionError("refused"))))
        assert [p["type"] for p in out] == ["error"] and out[0]["error_type"] == "agent_unreachable"

    def test_broken_stream_and_silent_end(self):
        out = asyncio.run(collect(FakeClient([ev("message.delta", delta="x")],
                                             stream_error=HermesConnectionError("reset"))))
        assert out[-1]["error_type"] == "stream_broken"
        out = asyncio.run(collect(FakeClient([ev("message.delta", delta="x")])))
        assert out[-1]["error_type"] == "stream_ended"


class FakeRuntime:
    def __init__(self, defs, env="API_SERVER_KEY=secret-key\n"):
        self.defs, self.env, self.reads = defs, env, []

    def defined(self):
        return sorted(self.defs)

    def load(self, name):
        return self.defs[name]

    def get(self, vol, rel):
        self.reads.append((vol, rel))
        return self.env


class FakeAuthDb:
    def __init__(self):
        self.rows, self.users = {}, {"u1": {"username": "Greg"}}

    def get_agent_by_name(self, name):
        return self.rows.get(name)

    def create_agent(self, name, **kw):
        self.rows[name] = {"id": f"agent_{name}", "name": name, **kw}

    def update_agent(self, agent_id, **kw):
        next(r for r in self.rows.values() if r["id"] == agent_id).update(kw)

    def get_user_by_id(self, uid):
        return self.users.get(uid)


class TestDefinitionsAndOwners:
    DEFS = {
        "greg": {"display_name": "Greg's agent", "owners": ["Greg"], "model": {"default": "m1"}},
        "house": {"display_name": "Household", "model": {"default": "m1"}},
        "bench": {"dashboard": False, "model": {"default": "m1"}},
    }

    def test_sync_creates_updates_and_hides(self):
        db, rt = FakeAuthDb(), FakeRuntime(dict(self.DEFS))
        assert agent_chat.sync_definitions(rt, db) == {"greg": ["greg"], "house": None}
        assert set(db.rows) == {"greg", "house"}
        assert db.rows["greg"]["model"] == "m1" and db.rows["greg"]["description"] == "Greg's agent"
        rt.defs["greg"] = {**self.DEFS["greg"], "model": {"default": "m2"}}
        agent_chat.sync_definitions(rt, db)
        assert db.rows["greg"]["model"] == "m2"

    def test_owners_and_usernames(self):
        assert agent_chat.may_use(["greg"], "Greg") and not agent_chat.may_use(["greg"], "bere")
        assert agent_chat.may_use(None, "anyone") and not agent_chat.may_use([], "greg")
        assert agent_chat.owners_of({"owners": "Bere"}) == ["bere"]
        db = FakeAuthDb()
        assert agent_chat.username_of({"sub": "u1"}, db) == "Greg"
        assert agent_chat.username_of({"sub": "nobody"}, db) == "" and agent_chat.username_of({}, db) == ""


class FakeProcess:
    def __init__(self):
        self.returncode, self.terminated = None, False

    def terminate(self):
        self.terminated, self.returncode = True, 0

    async def wait(self):
        return 0


class FakeHermes:
    def __init__(self, base_url, key):
        self.base_url, self.key, self.closed, self.fail = base_url, key, False, False

    async def health(self):
        if self.fail:
            raise HermesConnectionError("gone")
        return {"status": "ok"}

    async def close(self):
        self.closed = True


class TestAgentLinks:
    def make(self, env="API_SERVER_KEY=secret-key\n"):
        procs = []

        async def spawn(name):
            procs.append(FakeProcess())
            return procs[-1], 40000 + len(procs)

        rt = FakeRuntime({}, env=env)
        return AgentLinks(rt, spawn=spawn, client_factory=FakeHermes), rt, procs

    def test_one_forward_per_agent_and_reuse(self):
        async def go():
            links, rt, procs = self.make()
            c1 = await links.client("greg")
            c2 = await links.client("greg")
            assert c1 is c2 and len(procs) == 1
            assert (c1.base_url, c1.key) == ("http://127.0.0.1:40001", "secret-key")
            assert rt.reads == [("logos-home-greg", ".env")]
            await links.close()
            assert procs[0].terminated and c1.closed
        asyncio.run(go())

    def test_dead_forward_is_replaced(self):
        async def go():
            links, _rt, procs = self.make()
            c1 = await links.client("greg")
            procs[0].returncode = 1
            c2 = await links.client("greg")
            assert c2 is not c1 and c1.closed and c2.base_url.endswith(":40002")
        asyncio.run(go())

    def test_health_retries_on_a_fresh_link(self):
        async def go():
            links, _rt, procs = self.make()
            (await links.client("greg")).fail = True
            assert await links.health("greg") == {"status": "ok"}
            assert len(procs) == 2 and procs[0].terminated
        asyncio.run(go())

    def test_missing_key_is_reported(self):
        async def go():
            links, _rt, procs = self.make(env="TZ=UTC\n")
            with pytest.raises(AgentLinkError, match="API_SERVER_KEY"):
                await links.client("greg")
            assert procs == []
        asyncio.run(go())


def test_helpers():
    assert api_key_from_env("A=1\nAPI_SERVER_KEY='k'\n") == "k" and api_key_from_env("") == ""
    assert forward_argv("greg") == ["openshell", "forward", "service", "greg",
                                    "--target-port", "8642", "--local", "127.0.0.1:0"]


def test_keep_alive_writes_comment_lines_until_cancelled():
    sent = []

    async def write(data):
        sent.append(data)

    async def go():
        beat = asyncio.ensure_future(agent_chat.keep_alive(write, every=0.01))
        await asyncio.sleep(0.05)
        beat.cancel()
        count = len(sent)
        await asyncio.sleep(0.03)
        return count

    count = asyncio.run(go())
    assert count >= 2 and len(sent) == count
    assert set(sent) == {b": keep-alive\n\n"}


def test_wake_model_says_so_only_when_it_has_to_load():
    import json

    state = {"loaded": False, "loads": 0}

    def http(url, *, data=None, headers=None, timeout=10):
        if url.endswith("/api/v0/models"):
            rows = [{"id": "qwen/q", "state": "loaded", "loaded_context_length": 65536}] if state["loaded"] else []
            return json.dumps({"data": rows}).encode()
        state["loaded"] = True
        state["loads"] += 1
        return b"{}"

    model = {"provider": "lmstudio", "base_url": "http://10.0.0.5:1234/v1", "default": "qwen/q", "context_length": 65536}

    async def events(m):
        return [p async for p in agent_chat.wake_model(m, http)]

    first = asyncio.run(events(model))
    assert [p["type"] for p in first] == ["status"] and state["loads"] == 1
    assert asyncio.run(events(model)) == [] and state["loads"] == 1
    assert asyncio.run(events({"provider": "anthropic"})) == []


def test_wake_model_survives_an_unreachable_server():
    def http(url, **kw):
        raise OSError("no route")

    async def events():
        return [p async for p in agent_chat.wake_model(
            {"provider": "lmstudio", "base_url": "http://10.0.0.5:1234/v1", "default": "qwen/q"}, http)]

    assert asyncio.run(events()) == []
