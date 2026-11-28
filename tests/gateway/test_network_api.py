"""Story E6 over HTTP: who sees a network question, who may answer it, and who may see each agent's sites."""
import asyncio
import types

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import network_api
from gateway import network_approvals as na
from tests.unit.test_network_approvals import AGENTS, PENDING, Shell, public

USERS = {"u-greg": ("greg", "user"), "u-bere": ("bere", "user"), "u-admin": ("alex", "admin")}


def make(tmp_path):
    shell = Shell()
    runtime = types.SimpleNamespace(env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}, runner=shell,
                                    load=lambda n: AGENTS[n], defined=lambda: sorted(AGENTS),
                                    phases=lambda: {n: "Ready" for n in AGENTS})
    return na.Approvals(runtime, resolve=public), shell


def call(net, monkeypatch, uid, method, path, body=None):
    name, role = USERS[uid]
    monkeypatch.setattr("gateway.dashboard_api._user",
                        lambda request: {"id": uid, "username": name, "display_name": name, "role": role})

    async def flow():
        app = web.Application()
        app["network_approvals"] = net
        app.router.add_get("/api/v2/network", network_api.handle_list)
        app.router.add_post("/api/v2/network/{rid}/{decision}", network_api.handle_decide)
        app.router.add_post("/api/v2/network-sites/{agent}/remove", network_api.handle_remove)
        async with TestClient(TestServer(app)) as client:
            r = await (client.get(path) if method == "GET" else client.post(path, json=body or {}))
            return r.status, await r.json()
    return asyncio.run(flow())


def test_the_question_is_for_the_agents_people_and_the_list_for_the_admin_too(tmp_path, monkeypatch):
    net, shell = make(tmp_path)
    shell.rules["finn"] = PENDING
    [req] = net.poll(["finn"])
    status, body = call(net, monkeypatch, "u-greg", "GET", "/api/v2/network")
    assert status == 200 and [r["host"] for r in body["requests"]] == ["example.org"]
    assert [a["name"] for a in body["agents"]] == ["finn", "household"]
    status, body = call(net, monkeypatch, "u-bere", "GET", "/api/v2/network")
    assert body["requests"] == [] and [a["name"] for a in body["agents"]] == ["household"]
    status, body = call(net, monkeypatch, "u-admin", "GET", "/api/v2/network")
    assert body["requests"] == [] and {a["name"] for a in body["agents"]} == {"finn", "household", "nobody"}
    # Neither Béré nor the admin may answer Finn's question; Greg may.
    assert call(net, monkeypatch, "u-bere", "POST", f"/api/v2/network/{req['id']}/always")[0] == 403
    assert call(net, monkeypatch, "u-admin", "POST", f"/api/v2/network/{req['id']}/always")[0] == 403
    assert call(net, monkeypatch, "u-greg", "POST", f"/api/v2/network/{req['id']}/always")[0] == 200
    status, body = call(net, monkeypatch, "u-admin", "GET", "/api/v2/network")
    assert [e["host"] for a in body["agents"] if a["name"] == "finn" for e in a["always"]] == ["example.org"]
    # Taking it away: not Béré (not hers), but the admin may.
    site = {"host": "example.org", "port": 443}
    assert call(net, monkeypatch, "u-bere", "POST", "/api/v2/network-sites/finn/remove", site)[0] == 403
    assert call(net, monkeypatch, "u-admin", "POST", "/api/v2/network-sites/finn/remove", site)[0] == 200
