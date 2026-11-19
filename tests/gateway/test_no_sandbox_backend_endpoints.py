"""S1.5: with the OpenShell 0.0.x runtime gone, chat/cancel answer a
well-formed 503 and /instances returns an empty shape. (The sandbox,
cluster and model-route list stubs went away with their panels in S1.10.)"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from gateway import http_api
from gateway.sandbox_backend import NullBackend


def _req(method="GET", path="/", match_info=None):
    app = web.Application()
    app["sandbox_backend"] = NullBackend()
    req = make_mocked_request(method, path, app=app, match_info=match_info or {})
    req["current_user"] = {"sub": "u1", "role": "admin"}
    return req


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _body(resp):
    return json.loads(resp.body)


def test_chat_cancel_is_503_no_agents_running():
    resp = _run(http_api._handle_chat_cancel(
        _req("POST", "/chat/t1/cancel", {"task_id": "t1"})))
    assert resp.status == 503
    body = _body(resp)
    assert body["error"] == "no_agents_running"
    assert "E2" in body["message"]

