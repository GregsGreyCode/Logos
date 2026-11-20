"""M1: the dashboard passes Matrix client requests through, only for someone signed in."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import matrix_proxy
from gateway.auth import middleware


def run(signed_in, method, path, headers=None, body=None, matrix=None):
    async def flow():
        seen = {}

        async def fake_matrix(request):
            seen.update(path=request.path_qs, auth=request.headers.get("Authorization"), body=await request.read())
            return web.json_response({"ok": True, "user_id": "@greg:home"})
        upstream = web.Application()
        upstream.router.add_route("*", "/{tail:.*}", fake_matrix)
        async with TestServer(upstream) as up:
            app = web.Application()
            app["matrix_url"] = matrix if matrix is not None else str(up.make_url("")).rstrip("/")
            matrix_proxy.register(app)
            orig = matrix_proxy.get_user_from_request
            matrix_proxy.get_user_from_request = lambda r: signed_in
            try:
                async with TestClient(TestServer(app)) as client:
                    r = await client.request(method, path, headers=headers or {}, data=body)
                    return r.status, await r.json(), seen
            finally:
                matrix_proxy.get_user_from_request = orig
    return asyncio.run(flow())


def test_passes_client_requests_with_the_matrix_token_untouched():
    status, out, seen = run({"sub": "u"}, "POST", "/_matrix/client/v3/login?x=1", {"Authorization": "Bearer mx-token"}, b'{"a":1}')
    assert status == 200 and out["ok"] and seen == {"path": "/_matrix/client/v3/login?x=1", "auth": "Bearer mx-token", "body": b'{"a":1}'}


def test_an_expired_dashboard_session_is_try_again_never_401():
    for who in (None, middleware.TOKEN_EXPIRED):
        status, out, seen = run(who, "GET", "/_matrix/client/v3/sync")
        assert status == 503 and out["errcode"] == "M_UNKNOWN" and seen == {}


def test_only_client_and_media_apis_are_passed_on():
    status, out, seen = run({"sub": "u"}, "GET", "/_matrix/federation/v1/version")
    assert status == 404 and seen == {}
    assert run({"sub": "u"}, "GET", "/_matrix/media/v3/config")[0] == 200
