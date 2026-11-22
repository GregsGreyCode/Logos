"""The Models screen's "Local model" endpoints: one block per LM Studio server, admin-only changes."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import dashboard_api, model_server
from gateway.agent_runtime import Completed

QWEN = "qwen/qwen3.6-35b-a3b"
PC, BOX = "http://192.168.1.117:1234", "http://10.0.0.9:1234"
DEFS = {
    "greg": {"model": {"provider": "lmstudio", "base_url": PC + "/v1", "default": QWEN, "context_length": 65536,
                       "load_context_length": 96512}},
    "box": {"model": {"provider": "lmstudio", "base_url": BOX + "/v1", "default": "big/model", "context_length": 32768}},
    "hidden": {"dashboard": False, "model": {"provider": "lmstudio", "base_url": "http://192.0.2.1:1/v1", "default": "x"}},
}


class Runner:
    def __init__(self):
        self.calls = []

    def http(self, url, data=None, headers=None, timeout=10, method=None):
        self.calls.append((url, json.loads(data) if data else None))
        if url.startswith(BOX):
            raise OSError("closed")                     # the second server is off
        if url.endswith("/api/v1/models"):
            return json.dumps({"models": [{"key": QWEN, "size_bytes": 22.1e9, "params_string": "35B-A3B",
                                           "quantization": {"name": "Q4_K_M"}, "loaded_instances": [
                                               {"id": QWEN, "config": {"context_length": 96512, "parallel": 4,
                                                                       "flash_attention": True,
                                                                       "offload_kv_cache_to_gpu": True}}]}]}).encode()
        if url.endswith("/guard/health"):
            return json.dumps({"queue": {"slots": 2, "running": 0, "waiting": 0}}).encode()
        if url.endswith("/load"):
            return json.dumps({"load_config": {"context_length": 96512}}).encode()
        return b"{}"

    def run(self, argv, input=None, text=True):
        return Completed(0, json.dumps({"event": "chat", "at": "2026-11-20 14:00:00", "status": 200, "seconds": 20,
                                        "alongside": 0, "waited_s": 0, "completion_tokens": 600}) + "\n", "")


class Runtime:
    def __init__(self, env):
        self.env, self.runner = env, Runner()

    def defined(self):
        return sorted(DEFS)

    def load(self, name):
        return DEFS[name]

    def run(self, cmd, input=None, check=True):
        return ""


class Db:
    def get_user_by_id(self, uid):
        return {"username": uid}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard_api, "auth_db", Db())
    runtime = Runtime({"XDG_CONFIG_HOME": str(tmp_path)})
    monkeypatch.setattr(dashboard_api.agent_chat, "runtime_of", lambda backend: runtime)

    def go(method, path, body=None, role="admin"):
        @web.middleware
        async def as_user(request, handler):
            request["current_user"] = {"sub": "greg", "role": role}
            return await handler(request)

        async def flow():
            app = web.Application(middlewares=[as_user])
            app["sandbox_backend"] = object()
            dashboard_api.register(app)
            async with TestClient(TestServer(app)) as client:
                client.session.cookie_jar.update_cookies({"csrf_token": "t"})
                r = await client.request(method, path, json=body, headers={"X-CSRF-Token": "t"})
                return r.status, await r.json()
        return asyncio.run(flow())
    return go, runtime


def test_every_server_an_agent_uses_gets_its_own_block(api):
    go, _ = api
    code, body = go("GET", "/api/v2/models/local", role="user")
    assert code == 200 and body["can_change"] is False
    servers = {s["server"]: s for s in body["servers"]}
    assert set(servers) == {"192.168.1.117:1234", "10.0.0.9:1234"}            # not the hidden agent's
    assert servers["192.168.1.117:1234"]["up"] and servers["192.168.1.117:1234"]["loaded"][0]["key"] == QWEN
    assert not servers["10.0.0.9:1234"]["up"] and "did not answer" in servers["10.0.0.9:1234"]["error"]
    assert servers["10.0.0.9:1234"]["advice"]                                   # advice even when it is off
    assert body["queue"]["slots"] == 2 and body["speed"]["alone"]["replies"] == 1


def test_a_gpu_told_on_the_page_drives_the_advice(api):
    go, _ = api
    assert go("POST", "/api/v2/models/local/hardware", {"server": "192.168.1.117:1234", "gpu": "RTX 4080 Super",
                                                         "vram_gb": 16})[0] == 200
    srv = next(s for s in go("GET", "/api/v2/models/local")[1]["servers"] if s["server"] == "192.168.1.117:1234")
    assert srv["hardware"] == {"gpu": "RTX 4080 Super", "vram_gb": 16.0}
    assert {a["setting"]: a["value"] for a in srv["advice"]}["Force model expert weights onto CPU"] == "On"
    go("POST", "/api/v2/models/local/hardware", {"server": "192.168.1.117:1234", "gpu": "New GPU", "vram_gb": 48})
    srv = next(s for s in go("GET", "/api/v2/models/local")[1]["servers"] if s["server"] == "192.168.1.117:1234")
    assert {a["setting"]: a["value"] for a in srv["advice"]}["Force model expert weights onto CPU"] == "Off"


def test_changes_are_for_admins_and_known_servers_only(api):
    go, rt = api
    assert go("POST", "/api/v2/models/local/reload", {"server": "192.168.1.117:1234"}, role="user")[0] == 403
    assert go("POST", "/api/v2/models/local/reload", {"server": "evil.example:80"})[1]["error"] == "unknown_server"
    assert go("POST", "/api/v2/models/local/hardware", {"server": "192.168.1.117:1234", "vram_gb": 9999})[0] == 400
    assert go("POST", "/api/v2/models/local/unload", {"server": "192.168.1.117:1234", "instance": QWEN})[0] == 400
    code, body = go("POST", "/api/v2/models/local/reload", {"server": "192.168.1.117:1234"})
    assert code == 200 and body["config"]["context_length"] == 96512
    loads = [c for c in rt.runner.calls if c[0].endswith("/load")]
    assert loads[-1] == (PC + "/api/v1/models/load", {"model": QWEN, "context_length": 96512, "flash_attention": True,
                                                      "offload_kv_cache_to_gpu": True, "echo_load_config": True})
    assert not any(c[0].startswith("http://evil") for c in rt.runner.calls)


def test_model_progress_passes_on_numbers_only(monkeypatch):
    # F33: the chat's "still thinking" line reads what the model guard says is streaming now.
    rt = Runtime({})
    live = [{"seconds": 95, "who": "person", "thinking_chars": 5400, "answer_chars": 0, "loop_ratio": 2.4, "text": "x"}]
    rt.runner.http = lambda url, timeout=10, **k: json.dumps({"model_server_off": False, "live": live}).encode()
    got = dashboard_api.model_progress(rt)
    assert got == {"live": [{"seconds": 95, "who": "person", "thinking_chars": 5400, "answer_chars": 0}],
                   "model_server_off": False}

    def down(url, timeout=10, **k):
        raise OSError("refused")
    rt.runner.http = down
    assert dashboard_api.model_progress(rt) == {"live": None}
