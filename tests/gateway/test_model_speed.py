"""S1.9: the only survivor of the v1 model benchmark is a tokens/second
measurement against an OpenAI-compatible base URL."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from gateway import model_speed
from gateway.model_speed import measure_speed, tokens_per_second


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _delta(**fields) -> bytes:
    return _sse({"choices": [{"delta": fields}]})


def _fake_server(events, *, status=200, delay=0.0, seen=None):
    async def chat(request: web.Request):
        if seen is not None:
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = await request.json()
        if status != 200:
            return web.Response(status=status, text="model not loaded")
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for ev in events:
            await resp.write(ev)
            if delay:
                await asyncio.sleep(delay)
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def _measure(app, **kwargs):
    async def _main():
        async with TestServer(app) as server:
            base = str(server.make_url("/v1"))
            return await measure_speed(base, "test-model", **kwargs)
    return asyncio.run(_main())


def test_tokens_per_second_math():
    assert tokens_per_second(0, 0.0, 1.0) == 0.0
    assert tokens_per_second(10, 0.0, 0.0) == 0.0
    # 21 tokens, first at 0.5s, last at 2.5s -> 20 tokens in 2s.
    assert tokens_per_second(21, 0.5, 2.5) == pytest.approx(10.0)
    # One token, or everything in one burst: fall back to tokens / total.
    assert tokens_per_second(1, 0.5, 0.5) == pytest.approx(2.0)
    assert tokens_per_second(8, 2.0, 2.0) == pytest.approx(4.0)


def test_counts_stream_chunks_when_server_reports_no_usage():
    seen = {}
    events = [_delta(role="assistant")] + [_delta(content=f"w{i} ") for i in range(6)]
    result = _measure(_fake_server(events, delay=0.02, seen=seen), max_tokens=32)

    assert result.tokens == 6 and result.exact is False
    assert 0 < result.ttft_s <= result.total_s
    # 5 tokens after the first, at least 5 * 20ms apart in total.
    assert 0 < result.tokens_per_second <= 5 / 0.1 * 1.05
    assert seen["auth"] is None            # no key -> no Authorization header
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["stream"] is True
    assert seen["body"]["max_tokens"] == 32


def test_prefers_server_reported_usage_and_sends_api_key():
    seen = {}
    events = [
        _delta(reasoning_content="thinking"),   # thinking models: still generation
        _delta(content="a"),
        b": keep-alive comment\n\n",
        b"data: {broken json\n\n",
        _delta(content="b"),
        _sse({"choices": [], "usage": {"completion_tokens": 40}}),
    ]
    result = _measure(_fake_server(events, delay=0.01, seen=seen), api_key="sk-test")

    assert result.tokens == 40 and result.exact is True
    assert result.tokens_per_second > 0
    assert seen["auth"] == "Bearer sk-test"


def test_http_error_and_empty_stream_raise():
    with pytest.raises(RuntimeError, match="HTTP 503: model not loaded"):
        _measure(_fake_server([], status=503))
    with pytest.raises(RuntimeError, match="no tokens"):
        _measure(_fake_server([_delta(role="assistant")]))


def test_cli_prints_json(capsys, monkeypatch):
    async def fake(base_url, model, api_key="", **kw):
        assert (base_url, model, api_key, kw["max_tokens"]) == ("http://x/v1", "m", "k", 16)
        return model_speed.SpeedResult(10, 0.1, 1.1, 9.0, True)

    monkeypatch.setattr(model_speed, "measure_speed", fake)
    assert model_speed.main(["http://x/v1", "m", "--api-key", "k", "--max-tokens", "16"]) == 0
    assert json.loads(capsys.readouterr().out)["tokens_per_second"] == 9.0

    async def boom(*a, **kw):
        raise RuntimeError("HTTP 500: nope")

    monkeypatch.setattr(model_speed, "measure_speed", boom)
    assert model_speed.main(["http://x/v1", "m"]) == 1
    assert "HTTP 500" in capsys.readouterr().out


def test_module_is_small_and_has_no_eval_prompts():
    src = Path(model_speed.__file__).read_text(encoding="utf-8")
    assert len(src.splitlines()) < 200
    for gone in ("_HARD_", "_INSTRUCT_", "_REASON_", "_eval_", "score"):
        assert gone not in src, gone
