"""Tests for gateway.hermes_client against an in-process fake hermes API server.

The fake reproduces the upstream v0.23.1 wire format (see the file:line references in
gateway/hermes_client.py): Bearer auth with the OpenAI-style 401 envelope, 202 from
POST /v1/runs, and SSE frames of the form ``id: <seq>\\ndata: <json>\\n\\n`` whose event
name lives in the JSON ``"event"`` key, bracketed by ``: open`` / ``: stream closed``.
"""

import asyncio
import json
import time
from contextlib import aclosing, asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, unused_port

from gateway.hermes_client import (
    EVENT_TYPES,
    HermesAuthError,
    HermesClient,
    HermesConnectionError,
    HermesError,
    HermesHTTPError,
    HermesNotFoundError,
    HermesProtocolError,
    HermesStreamError,
    HermesTimeoutError,
    normalise_event,
)

API_KEY = "test-key-0123456789abcdef"


def _error(message, status, code=None, err_type="invalid_request_error"):
    return web.json_response(
        {"error": {"message": message, "type": err_type, "param": None, "code": code}}, status=status
    )


class FakeHermes:
    """Minimal stand-in for hermes' api_server platform."""

    def __init__(self):
        self.run_requests = []  # (headers, body) of each POST /v1/runs
        self.event_requests = []  # headers of each GET .../events
        self.stop_requests = []
        self.session_queries = []
        self.scripts = {}  # run_id -> async callable(write_event, response, request)
        self.next_script = None
        self.stop_events = {}
        self.sessions = {
            "sess_1": {"id": "sess_1", "title": "First", "source": "api_server", "message_count": 2},
            "sess_2": {"id": "sess_2", "title": "Second", "source": "api_server", "message_count": 0},
        }
        self.messages = {
            "sess_1": [
                {"id": 1, "session_id": "sess_1", "role": "user", "content": "hi"},
                {"id": 2, "session_id": "sess_1", "role": "assistant", "content": "hello"},
            ]
        }

    def _auth(self, request):
        if request.headers.get("Authorization", "") == f"Bearer {API_KEY}":
            return None
        return _error("Invalid gateway API key (API_SERVER_KEY)", 401, "gateway_auth_failed", "gateway_auth_error")

    def app(self):
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_post("/v1/runs", self.start_run)
        app.router.add_get("/v1/runs/{run_id}", self.get_run)
        app.router.add_get("/v1/runs/{run_id}/events", self.events)
        app.router.add_post("/v1/runs/{run_id}/stop", self.stop)
        app.router.add_get("/api/sessions", self.list_sessions)
        app.router.add_get("/api/sessions/{session_id}", self.get_session)
        app.router.add_get("/api/sessions/{session_id}/messages", self.session_messages)
        return app

    async def health(self, request):
        return web.json_response({"status": "ok", "platform": "hermes-agent", "version": "0.23.1"})

    async def start_run(self, request):
        if (err := self._auth(request)) is not None:
            return err
        body = await request.json()
        self.run_requests.append((dict(request.headers), body))
        if not body.get("input"):
            return _error("Missing 'input' field", 400)
        run_id = f"run_{len(self.run_requests):04d}"
        self.scripts[run_id] = self.next_script
        self.stop_events[run_id] = asyncio.Event()
        return web.json_response({"run_id": run_id, "status": "started", "replayed": False}, status=202)

    async def get_run(self, request):
        if (err := self._auth(request)) is not None:
            return err
        run_id = request.match_info["run_id"]
        if run_id not in self.scripts:
            return _error(f"Run not found: {run_id}", 404, "run_not_found")
        return web.json_response(
            {"object": "hermes.run", "run_id": run_id, "status": "running", "session_id": run_id}
        )

    async def events(self, request):
        if (err := self._auth(request)) is not None:
            return err
        run_id = request.match_info["run_id"]
        if run_id not in self.scripts:
            return _error(f"Run not found: {run_id}", 404, "run_not_found")
        self.event_requests.append(dict(request.headers))
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        await response.write(b": open\n\n")
        seq = -1

        async def write_event(name, **fields):
            nonlocal seq
            seq += 1
            payload = {"event": name, "run_id": run_id, "timestamp": time.time(), **fields, "seq": seq}
            await response.write(f"id: {seq}\ndata: {json.dumps(payload)}\n\n".encode())

        script = self.scripts[run_id]
        closed = await script(write_event, response, request, self.stop_events[run_id])
        if closed is not False:
            await response.write(b": stream closed\n\n")
        return response

    async def stop(self, request):
        if (err := self._auth(request)) is not None:
            return err
        run_id = request.match_info["run_id"]
        if run_id not in self.scripts:
            return _error(f"Run not found: {run_id}", 404, "run_not_found")
        self.stop_requests.append(run_id)
        self.stop_events[run_id].set()
        return web.json_response({"run_id": run_id, "status": "stopping"})

    async def list_sessions(self, request):
        if (err := self._auth(request)) is not None:
            return err
        self.session_queries.append(dict(request.query))
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
        rows = list(self.sessions.values())[offset:offset + limit]
        return web.json_response(
            {"object": "list", "data": rows, "limit": limit, "offset": offset, "has_more": len(rows) >= limit}
        )

    async def get_session(self, request):
        if (err := self._auth(request)) is not None:
            return err
        sid = request.match_info["session_id"]
        if sid not in self.sessions:
            return _error(f"Session not found: {sid}", 404, "session_not_found")
        return web.json_response({"object": "hermes.session", "session": self.sessions[sid]})

    async def session_messages(self, request):
        if (err := self._auth(request)) is not None:
            return err
        sid = request.match_info["session_id"]
        if sid not in self.sessions:
            return _error(f"Session not found: {sid}", 404, "session_not_found")
        self.session_queries.append(dict(request.query))
        rows = self.messages.get(sid, [])
        return web.json_response(
            {
                "object": "list",
                "session_id": sid,
                "data": rows,
                "pagination": {"limit": 500, "offset": 0, "order": "latest", "returned": len(rows)},
            }
        )


@asynccontextmanager
async def running(script=None, *, api_key=API_KEY, **client_kwargs):
    fake = FakeHermes()
    fake.next_script = script
    server = TestServer(fake.app())
    await server.start_server()
    client = HermesClient(str(server.make_url("")), api_key, **client_kwargs)
    try:
        yield fake, client
    finally:
        await client.close()
        await server.close()


# -- event scripts ----------------------------------------------------------


async def full_run(write_event, response, request, stop):
    await write_event("reasoning.available", text="thinking about it")
    await write_event("message.delta", delta="Hel")
    await write_event("message.delta", delta="lo")
    await write_event("tool.started", tool="terminal", preview="ls -la", tool_call_id="call_1")
    await write_event("tool.completed", tool="terminal", duration=0.123, error=False, tool_call_id="call_1", preview="total 0")
    await write_event("tool.started", tool="read_file", preview="/nope", tool_call_id="call_2")
    await write_event("tool.completed", tool="read_file", duration=0.01, error=True, tool_call_id="call_2", preview="ENOENT")
    await write_event("message.interim", text="Checking one more thing", already_streamed=False)
    await response.write(b": keepalive\n\n")
    await write_event("message.delta", delta=" world")
    await write_event(
        "run.completed",
        completed=True,
        partial=False,
        interrupted=False,
        output="Hello world",
        usage={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    )


async def run_until_stopped(write_event, response, request, stop):
    await write_event("message.delta", delta="working")
    await stop.wait()
    await write_event("run.cancelled", completed=False, partial=False, interrupted=True)


async def with_unknown_event(write_event, response, request, stop):
    await write_event("message.delta", delta="a")
    await write_event("future.shiny_thing", payload={"x": 1})
    await response.write(b"data: this is not json\n\n")
    await write_event("run.completed", completed=True, output="a")


async def cut_mid_run(write_event, response, request, stop):
    await write_event("message.delta", delta="par")
    await write_event("message.delta", delta="tial")
    await asyncio.sleep(0.05)
    request.transport.close()  # connection dies mid-run: no terminal event, no close marker
    return False


async def eof_without_marker(write_event, response, request, stop):
    await write_event("message.delta", delta="x")
    return False  # clean HTTP end, but neither a terminal event nor ": stream closed"


async def queued_handoff(write_event, response, request, stop):
    await write_event("run.queued", message="queued at a retiring owner")


async def failed_run(write_event, response, request, stop):
    await write_event("run.failed", completed=False, partial=False, interrupted=False, error="provider exploded")
    await write_event("message.delta", delta="must never be read")


async def silent(write_event, response, request, stop):
    await write_event("message.delta", delta="x")
    await asyncio.sleep(5)


async def huge_output(write_event, response, request, stop):
    await write_event("run.completed", completed=True, output="x" * 400_000)


# -- tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health():
    async with running() as (_fake, client):
        assert await client.health() == {"status": "ok", "platform": "hermes-agent", "version": "0.23.1"}


@pytest.mark.asyncio
async def test_full_run_deltas_tools_and_completion():
    async with running(full_run) as (fake, client):
        run_id = await client.start_run("say hello", session_id="sess_1", instructions="be brief")
        assert run_id == "run_0001"
        headers, body = fake.run_requests[0]
        assert headers["Authorization"] == f"Bearer {API_KEY}"
        assert body == {"input": "say hello", "session_id": "sess_1", "instructions": "be brief"}

        events = [e async for e in client.stream_events(run_id)]

    assert [e.type for e in events] == [
        "reasoning", "token", "token", "tool_started", "tool_completed", "tool_started",
        "tool_failed", "message", "token", "run_completed",
    ]
    assert all(e.type in EVENT_TYPES for e in events)
    assert [e.seq for e in events] == list(range(10))
    assert all(e.run_id == run_id for e in events)
    assert "".join(e.text for e in events if e.type == "token") == "Hello world"
    assert events[0].text == "thinking about it"

    started = events[3]
    assert (started.tool, started.tool_call_id, started.preview) == ("terminal", "call_1", "ls -la")
    completed = events[4]
    assert (completed.tool, completed.tool_call_id, completed.duration) == ("terminal", "call_1", 0.123)
    failed = events[6]
    assert failed.name == "tool.completed" and failed.raw["error"] is True
    assert (failed.tool, failed.tool_call_id, failed.preview) == ("read_file", "call_2", "ENOENT")

    final = events[-1]
    assert final.terminal and final.text == "Hello world"
    # the raw upstream payload rides alongside the normalised fields
    assert final.raw["usage"]["total_tokens"] == 13
    assert final.raw["event"] == "run.completed"
    assert not any(e.terminal for e in events[:-1])


@pytest.mark.asyncio
async def test_start_run_messages_alias_and_idempotency_key():
    async with running(full_run) as (fake, client):
        msgs = [{"role": "user", "content": "first"}, {"role": "user", "content": "second"}]
        await client.start_run(messages=msgs, model="hermes", idempotency_key="abc-123")
        headers, body = fake.run_requests[0]
        # upstream has no "messages" field on /v1/runs: the list is sent as "input"
        assert body == {"input": msgs, "model": "hermes"}
        assert headers["Idempotency-Key"] == "abc-123"

        with pytest.raises(ValueError):
            await client.start_run("x", messages=msgs)
        with pytest.raises(ValueError):
            await client.start_run("")
        assert len(fake.run_requests) == 1


@pytest.mark.asyncio
async def test_chat_yields_synthetic_start_then_events_to_completion():
    async with running(full_run) as (fake, client):
        events = [e async for e in client.chat("say hello", session_id="sess_1")]
    assert events[0].type == "run_started"
    assert events[0].run_id == "run_0001" and events[0].raw["synthetic"] is True
    assert events[-1].type == "run_completed"
    assert "".join(e.text for e in events if e.type == "token") == "Hello world"
    assert fake.run_requests[0][1] == {"input": "say hello", "session_id": "sess_1"}


@pytest.mark.asyncio
async def test_stop_mid_run():
    async with running(run_until_stopped) as (fake, client):
        seen = []
        stop_reply = None
        async for event in client.chat("long task"):
            seen.append(event)
            if event.type == "token":
                stop_reply = await client.stop_run(event.run_id)
    assert stop_reply == {"run_id": "run_0001", "status": "stopping"}
    assert fake.stop_requests == ["run_0001"]
    assert [e.type for e in seen] == ["run_started", "token", "run_cancelled"]
    assert seen[-1].terminal and seen[-1].raw["interrupted"] is True


@pytest.mark.asyncio
async def test_get_run_and_unknown_run_is_not_found():
    async with running(full_run) as (_fake, client):
        run_id = await client.start_run("hi")
        status = await client.get_run(run_id)
        assert status["object"] == "hermes.run" and status["session_id"] == run_id

        with pytest.raises(HermesNotFoundError) as exc:
            await client.stop_run("run_missing")
        assert exc.value.status == 404 and exc.value.code == "run_not_found"
        assert "Run not found: run_missing" in str(exc.value)

        with pytest.raises(HermesNotFoundError):
            async for _ in client.stream_events("run_missing"):
                pass


@pytest.mark.asyncio
async def test_bad_key_raises_auth_error_everywhere():
    async with running(full_run, api_key="wrong-key") as (fake, client):
        with pytest.raises(HermesAuthError) as exc:
            await client.start_run("hi")
        err = exc.value
        assert isinstance(err, HermesHTTPError) and isinstance(err, HermesError)
        assert err.status == 401 and err.code == "gateway_auth_failed"
        assert err.error_type == "gateway_auth_error"
        assert "401" in str(err) and "Invalid gateway API key" in str(err)
        assert "wrong-key" not in str(err)

        with pytest.raises(HermesAuthError):
            await client.list_sessions()
        with pytest.raises(HermesAuthError):
            async for _ in client.stream_events("run_0001"):
                pass
        with pytest.raises(HermesAuthError):
            async for _ in client.chat("hi"):
                pass
        assert fake.run_requests == []
        # /health is unauthenticated upstream
        assert (await client.health())["status"] == "ok"


@pytest.mark.asyncio
async def test_non_2xx_raises_http_error_with_upstream_message():
    async with running(full_run) as (_fake, client):
        with pytest.raises(HermesHTTPError) as exc:
            # bypass the client-side guard to exercise the server's 400
            await client._request_json("POST", "/v1/runs", json_body={"input": ""})
    err = exc.value
    assert type(err) is HermesHTTPError
    assert err.status == 400 and "Missing 'input' field" in str(err)
    assert err.body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_connection_refused():
    client = HermesClient(f"http://127.0.0.1:{unused_port()}", API_KEY)
    try:
        with pytest.raises(HermesConnectionError) as exc:
            await client.health()
        assert "cannot connect to hermes" in str(exc.value)
        with pytest.raises(HermesConnectionError):
            await client.start_run("hi")
        with pytest.raises(HermesConnectionError):
            async for _ in client.stream_events("run_1"):
                pass
        with pytest.raises(HermesConnectionError):
            async for _ in client.chat("hi"):
                pass
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_and_malformed_events_pass_through_as_other():
    async with running(with_unknown_event) as (_fake, client):
        events = [e async for e in client.chat("hi")]
    assert [e.type for e in events] == ["run_started", "token", "other", "other", "run_completed"]
    unknown = events[2]
    assert unknown.name == "future.shiny_thing"
    assert unknown.raw["payload"] == {"x": 1} and unknown.seq == 1
    malformed = events[3]
    assert malformed.raw["malformed"] is True and malformed.raw["data"] == "this is not json"


@pytest.mark.asyncio
async def test_cut_stream_raises_stream_error_with_last_seq():
    async with running(cut_mid_run) as (_fake, client):
        seen = []
        with pytest.raises(HermesStreamError) as exc:
            async for event in client.chat("hi"):
                seen.append(event)
    assert [e.text for e in seen if e.type == "token"] == ["par", "tial"]
    assert exc.value.run_id == "run_0001" and exc.value.last_seq == 1
    assert "before a terminal event" in str(exc.value)
    assert isinstance(exc.value, HermesError)


@pytest.mark.asyncio
async def test_eof_without_terminal_or_close_marker_is_a_stream_error():
    async with running(eof_without_marker) as (_fake, client):
        run_id = await client.start_run("hi")
        with pytest.raises(HermesStreamError) as exc:
            async for _ in client.stream_events(run_id):
                pass
    assert exc.value.last_seq == 0


@pytest.mark.asyncio
async def test_server_close_marker_without_terminal_event_ends_cleanly():
    async with running(queued_handoff) as (_fake, client):
        run_id = await client.start_run("hi")
        events = [e async for e in client.stream_events(run_id)]
    assert [e.type for e in events] == ["run_queued"]
    assert not events[0].terminal


@pytest.mark.asyncio
async def test_stream_stops_at_terminal_event_and_carries_error():
    async with running(failed_run) as (_fake, client):
        events = [e async for e in client.chat("hi")]
    assert [e.type for e in events] == ["run_started", "run_failed"]
    assert events[-1].error == "provider exploded" and events[-1].terminal


@pytest.mark.asyncio
async def test_resume_sends_last_event_id():
    async with running(full_run) as (fake, client):
        run_id = await client.start_run("hi")
        async with aclosing(client.stream_events(run_id, last_seq=4)) as stream:
            async for _ in stream:
                break
    assert fake.event_requests[0]["Last-Event-ID"] == "4"


@pytest.mark.asyncio
async def test_silent_stream_times_out():
    async with running(silent, stream_idle_timeout=0.2) as (_fake, client):
        run_id = await client.start_run("hi")
        seen = []
        with pytest.raises(HermesTimeoutError) as exc:
            async for event in client.stream_events(run_id):
                seen.append(event)
    assert [e.type for e in seen] == ["token"]
    assert "silent" in str(exc.value) and isinstance(exc.value, HermesError)


@pytest.mark.asyncio
async def test_request_timeout():
    async def slow(request):
        await asyncio.sleep(5)
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/health", slow)
    server = TestServer(app)
    await server.start_server()
    client = HermesClient(str(server.make_url("")), API_KEY, request_timeout=0.2)
    try:
        with pytest.raises(HermesTimeoutError) as exc:
            await client.health()
        assert "timed out" in str(exc.value)
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_non_json_success_body_is_a_protocol_error():
    async def html(request):
        return web.Response(text="<html>proxy login</html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/health", html)
    server = TestServer(app)
    await server.start_server()
    client = HermesClient(str(server.make_url("")), API_KEY)
    try:
        with pytest.raises(HermesProtocolError):
            await client.health()
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_frame_larger_than_line_limit():
    async with running(huge_output) as (_fake, client):
        events = [e async for e in client.chat("hi")]
    assert events[-1].type == "run_completed" and len(events[-1].text) == 400_000


@pytest.mark.asyncio
async def test_sessions_listing_detail_and_messages():
    async with running() as (fake, client):
        page = await client.list_sessions(limit=1, offset=1, source="api_server", include_children=False)
        assert [s["id"] for s in page["data"]] == ["sess_2"]
        assert page["has_more"] is True and page["limit"] == 1
        assert fake.session_queries[0] == {
            "limit": "1", "offset": "1", "source": "api_server", "include_children": "false",
        }

        everything = await client.list_sessions()
        assert [s["id"] for s in everything["data"]] == ["sess_1", "sess_2"]
        assert fake.session_queries[1] == {}

        session = await client.get_session("sess_1")
        assert session["title"] == "First"

        messages = await client.get_session_messages("sess_1", limit=10, order="oldest")
        assert [m["role"] for m in messages["data"]] == ["user", "assistant"]
        assert messages["session_id"] == "sess_1"
        assert fake.session_queries[2] == {"limit": "10", "order": "oldest"}

        with pytest.raises(HermesNotFoundError) as exc:
            await client.get_session("nope")
        assert exc.value.code == "session_not_found"
        with pytest.raises(ValueError):
            await client.get_session_messages("sess_1", order="sideways")


@pytest.mark.asyncio
async def test_caller_supplied_session_is_not_closed():
    import aiohttp

    async with aiohttp.ClientSession() as http:
        fake = FakeHermes()
        server = TestServer(fake.app())
        await server.start_server()
        try:
            async with HermesClient(str(server.make_url("")), API_KEY, session=http) as client:
                assert (await client.health())["status"] == "ok"
            assert not http.closed
        finally:
            await server.close()


def test_normalise_event_never_raises_on_odd_payloads():
    assert normalise_event({}).type == "other"
    assert normalise_event({"event": None, "seq": "7"}).seq is None
    assert normalise_event({"event": "tool.completed", "tool": 5, "error": "yes"}).type == "tool_failed"
    assert normalise_event({"event": "run.interrupted", "error": "Gateway shutdown interrupted the run."}).terminal
    assert normalise_event({"event": "approval.request", "command": "rm -rf /"}).type == "approval_request"
    assert normalise_event({"event": "subagent.start", "preview": "go"}).preview == "go"
    assert normalise_event({"event": "replay.truncated", "oldest_retained_seq": 12}).type == "other"
