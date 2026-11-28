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
        app.router.add_post("/api/v2/network-sites/{agent}/add", network_api.handle_add)
        app.router.add_post("/api/v2/network-sites/{agent}/undeny", network_api.handle_undeny)
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


def test_a_new_question_says_what_the_agent_was_doing_and_alerts_its_people(tmp_path, monkeypatch):
    from gateway import push
    net, shell = make(tmp_path)
    shell.rules["finn"] = PENDING
    sent = []

    class Store:
        def vapid_key(self):
            return "k"

        def devices(self, username):
            return [{"pushkey": f"pk-{username}"}]

        def get(self, pushkey):
            return {"pushkey": pushkey}

    async def fake_send(session, sub, message, key):
        sent.append((sub["pushkey"], message))
        return 201

    class Client:
        async def list_sessions(self, limit=None):
            return {"data": [{"id": "s1", "source": "matrix", "title": "Train times to Lyon", "last_active": 1e12}]}

    class Links:
        async def client(self, name):
            return Client()

    monkeypatch.setattr(push, "send", fake_send)
    monkeypatch.setattr(push, "alerts_private", lambda username: False)
    net.clock = lambda: 1e12
    app = {"network_approvals": net, "push_store": Store(), "push_session": object(), "agent_links": Links()}
    [req] = asyncio.run(network_api.poll_once(app))
    assert net.pending()[0]["for"] == "Your chat “Train times to Lyon”"
    assert net.pending()[0]["asker"] == {"kind": "chat", "text": "Your chat “Train times to Lyon” · using curl"}
    assert sent == [("pk-greg", {"title": "Finn wants to reach example.org",
                                 "body": "Unknown: ask the agent. Allow once, always allow or deny, in Logos Notifications.",
                                 "link": "#/noticeboard", "room": f"net-{req['id']}"})]


def _fakes(monkeypatch, sessions):
    from gateway import push
    sent = []

    class Store:
        def vapid_key(self):
            return "k"

        def devices(self, username):
            return [{"pushkey": f"pk-{username}"}]

        def get(self, pushkey):
            return {"pushkey": pushkey}

    async def fake_send(session, sub, message, key):
        sent.append((sub["pushkey"], message["title"]))
        return 201

    class Client:
        def __init__(self, name):
            self.name = name

        async def list_sessions(self, limit=None):
            return {"data": sessions.get(self.name, [])}

    class Links:
        async def client(self, name):
            return Client(name)

    monkeypatch.setattr(push, "send", fake_send)
    monkeypatch.setattr(push, "alerts_private", lambda username: False)
    return sent, {"push_store": Store(), "push_session": object(), "agent_links": Links()}


def test_several_agents_asking_at_once_is_one_alert_each(tmp_path, monkeypatch):
    from tests.unit.test_network_approvals import MODELS_DEV
    net, shell = make(tmp_path)
    net.clock = lambda: 1e12
    shell.rules["finn"] = MODELS_DEV
    shell.rules["household"] = MODELS_DEV
    sent, app = _fakes(monkeypatch, {})
    app["network_approvals"] = net
    asyncio.run(network_api.poll_once(app))
    assert sorted(sent) == [("pk-bere", "Household agent wants to reach models.dev"),
                            ("pk-greg", "Finn and Household agent want to reach models.dev")]
    assert {r["asker"]["kind"] for r in net.pending()} == {"background"}
    # Asked again (a higher hit count) is not a second alert while the question is open.
    shell.rules["finn"] = MODELS_DEV + "  Hits: 3 (first seen x, last seen y)\n"
    asyncio.run(network_api.poll_once(app))
    assert len(sent) == 2
    # One answer over HTTP decides Greg's two agents.
    gid = net.pending()[0]["group"]
    status, body = call(net, monkeypatch, "u-greg", "POST", f"/api/v2/network/{gid}/deny")
    assert status == 200 and body["agents"] == ["finn", "household"] and net.pending() == []


def test_a_job_says_what_it_does_and_a_shared_agents_chat_stays_private(tmp_path, monkeypatch):
    net, shell = make(tmp_path)
    net.clock = lambda: 1e12
    shell.rules["finn"] = PENDING
    shell.rules["household"] = PENDING
    sent, app = _fakes(monkeypatch, {
        "finn": [{"id": "c", "source": "cron", "title": "morning-briefing · Nov 28 07:00", "last_active": 1e12}],
        "household": [{"id": "m", "source": "matrix", "title": "A secret plan", "last_active": 1e12}]})
    app["network_approvals"] = net
    asyncio.run(network_api.poll_once(app))
    got = {r["agent"]: r["asker"]["text"] for r in net.pending()}
    assert got["finn"] == "Scheduled job “morning-briefing”: Morning briefing (weekdays) · using curl"
    assert "secret" not in got["household"] and got["household"].startswith("A chat with one of you")
    status, body = call(net, monkeypatch, "u-greg", "GET", "/api/v2/network")
    assert {r["site"]["domain"] for r in body["requests"]} == {"example.org"}
    assert network_api.job_about("") == "A scheduled job"
    assert network_api.job_about("pay rent · Nov 28 09:00") == "Scheduled job “pay rent” (one the agent set up)"


def test_a_site_added_by_hand_passes_the_same_checks_and_only_for_its_people(tmp_path, monkeypatch):
    # People › Sites › Edit (28 Nov): add a site to an agent's always-allowed list without waiting to be asked.
    net, shell = make(tmp_path)
    audit = []
    net.audit = lambda *a: audit.append(a)
    site = {"host": "Example.org", "port": 443}
    assert call(net, monkeypatch, "u-bere", "POST", "/api/v2/network-sites/finn/add", site)[0] == 403     # not her agent
    assert call(net, monkeypatch, "u-admin", "POST", "/api/v2/network-sites/finn/add", site)[0] == 403    # owners only
    status, body = call(net, monkeypatch, "u-greg", "POST", "/api/v2/network-sites/finn/add", site)
    assert status == 200 and body["host"] == "example.org"
    assert shell.did("openshell", "policy", "update", "finn")
    assert [e["host"] for e in net.allowed("finn")["finn"]["always"]] == ["example.org"]
    assert audit[-1][1:] == ("network_added", "agent", "finn", {"host": "example.org", "port": 443})
    # Never: a home-network name, an address, the agent's model server or its people's own domain.
    for bad in ("nas.local", "192.0.2.117", "home.example.net", "*.example.org", "not a site"):
        status, body = call(net, monkeypatch, "u-greg", "POST", "/api/v2/network-sites/finn/add", {"host": bad, "port": 443})
        assert status == 400 and "cannot be allowed" in body["message"], bad
    assert [e["host"] for e in net.allowed("finn")["finn"]["always"]] == ["example.org"]


def test_a_deny_can_be_lifted_early_by_the_agents_people(tmp_path, monkeypatch):
    net, shell = make(tmp_path)
    shell.rules["finn"] = PENDING
    [req] = net.poll(["finn"])
    assert call(net, monkeypatch, "u-greg", "POST", f"/api/v2/network/{req['id']}/deny")[0] == 200
    site = {"host": "example.org", "port": 443}
    assert call(net, monkeypatch, "u-admin", "POST", "/api/v2/network-sites/finn/undeny", site)[0] == 403
    assert call(net, monkeypatch, "u-greg", "POST", "/api/v2/network-sites/finn/undeny", site)[0] == 200
    assert net.allowed("finn")["finn"]["deny"] == []
    assert call(net, monkeypatch, "u-greg", "POST", "/api/v2/network-sites/finn/undeny", site)[0] == 404
