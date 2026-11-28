"""The agents' copy of models.dev's catalogue (gateway/models_catalogue.py): fetched on the host at deploy,
checked, the last good copy kept, served read-only with an ETag."""
from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import models_catalogue as mc


def catalogue(providers=30, models=60):
    return json.dumps({f"p{i}": {"id": f"p{i}", "name": f"P{i}", "models": {
        f"m{j}": {"id": f"m{j}", "name": f"Model {j}", "limit": {"context": 131072}, "cost": {"input": 1.0}}
        for j in range(models)}} for i in range(providers)}).encode()


def test_a_good_download_is_kept(tmp_path):
    env = {"HOME": str(tmp_path)}
    ok, msg = mc.refresh(env, fetch=lambda url: catalogue())
    assert ok and "30 providers, 1800 models" in msg
    assert json.loads(mc.store(env).read_bytes())["p0"]["models"]["m0"]["name"] == "Model 0"
    assert mc.store(env) == tmp_path / ".logos" / "models-dev.json"


def test_a_bad_download_keeps_the_last_good_copy(tmp_path):
    env = {"HOME": str(tmp_path)}
    good = catalogue()
    assert mc.refresh(env, fetch=lambda url: good)[0]
    bad = [b"<html>captive portal</html>" * 10_000, b"[" + b"1," * 60_000 + b"1]", catalogue(providers=3),
           b'{"a": ' + b" " * 200_000 + b"}", b"x" * (mc.MAX_BYTES + 1)]
    for raw in bad:
        ok, msg = mc.refresh(env, fetch=lambda url, raw=raw: raw)
        assert not ok and "kept the last copy" in msg
        assert mc.store(env).read_bytes() == good

    def offline(url):
        raise OSError("no network")
    ok, msg = mc.refresh(env, fetch=offline)
    assert not ok and "kept the last copy" in msg and mc.store(env).read_bytes() == good


def test_nothing_to_keep_says_so(tmp_path):
    ok, msg = mc.refresh({"HOME": str(tmp_path)}, fetch=lambda url: b"{}")
    assert not ok and "no copy yet" in msg and not mc.store({"HOME": str(tmp_path)}).exists()


def _get(env, headers=None):
    async def flow():
        app = web.Application()
        mc.register(app, env)
        async with TestClient(TestServer(app)) as client:
            r = await client.get(mc.PATH, headers=headers or {})
            return r.status, r.headers.get("ETag", ""), await r.read()
    return asyncio.run(flow())


def test_served_read_only_with_an_etag(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert _get(env)[0] == 404                                   # no copy yet: nothing to serve
    mc.refresh(env, fetch=lambda url: catalogue())
    status, etag, body = _get(env)
    assert status == 200 and etag and json.loads(body)["p1"]["id"] == "p1"
    status, etag2, body = _get(env, {"If-None-Match": etag})    # hermes's conditional refresh
    assert status == 304 and etag2 == etag and body == b""
    mc.store(env).write_text("{ cut short")                     # a broken copy is never served
    assert _get(env)[0] == 404


def test_the_path_needs_no_login_and_is_the_one_the_sandbox_rule_opens():
    from gateway.auth import middleware
    assert mc.PATH in middleware._PUBLIC_PATHS
