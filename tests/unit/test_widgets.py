"""F42: homelab widgets (gateway/widgets.py)."""
import asyncio
import json
import stat

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import widgets as w


# ── services ─────────────────────────────────────────────────────────────────────────────────

def test_services_are_read_one_per_line():
    got = w.parse_services("Home Assistant, http://192.0.2.20:8123\n\n# a note\nhttps://jellyfin.home/\n")
    assert got == [{"name": "Home Assistant", "url": "http://192.0.2.20:8123"},
                   {"name": "jellyfin.home", "url": "https://jellyfin.home/"}]
    for bad in ("", "Router, 192.0.2.1", "ftp://nas"):
        with pytest.raises(w.Problem):
            w.parse_services(bad)


def test_a_service_that_fails_or_does_not_answer_is_down_and_comes_first(unused_tcp_port_factory):
    async def ok(_):
        return web.Response(status=401, text="sign in")          # a login page: it answers, so up

    async def broken(_):
        return web.Response(status=502)

    async def run():
        app = web.Application()
        app.router.add_get("/ok", ok)
        app.router.add_get("/broken", broken)
        server = TestServer(app)
        await server.start_server()
        base = f"http://127.0.0.1:{server.port}"
        conf = {"services": f"Fine, {base}/ok\nBroken, {base}/broken\nGone, http://127.0.0.1:{unused_tcp_port_factory()}/"}
        memo = {}
        async with aiohttp.ClientSession(timeout=w.TIMEOUT) as s:
            first = await w.fetch_services(conf, s, memo)
            second = await w.fetch_services(conf, s, memo)
        await server.close()
        return first, second
    first, second = asyncio.run(run())
    assert first["state"] == "needs" and first["down"] == 2 and first["summary"] == "2 down"
    assert [i["name"] for i in first["items"]][-1] == "Fine"                    # problems first
    by = {i["name"]: i for i in first["items"]}
    assert by["Fine"]["up"] and by["Broken"]["detail"] == "error 502" and by["Gone"]["detail"] == "not answering"
    assert {i["name"]: i.get("since") for i in second["items"]}["Gone"] == by["Gone"]["since"]   # down since the first


@pytest.fixture
def unused_tcp_port_factory():
    import socket

    def free():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port
    return free


# ── servers (Proxmox) ────────────────────────────────────────────────────────────────────────

def test_proxmox_needs_https_and_a_full_token():
    with pytest.raises(w.Problem):
        w._proxmox_base("http://pve:8006")
    assert w._proxmox_base("https://192.0.2.10:8006/") == "https://192.0.2.10:8006"

    async def run():
        async with aiohttp.ClientSession() as s:
            await w.proxmox_get({"url": "https://pve:8006", "token_id": "no-bang", "secret": "x"}, s, "/nodes")
    with pytest.raises(w.Problem, match="token ID"):
        asyncio.run(run())


def test_nodes_become_meters_and_a_full_disk_needs_you(monkeypatch):
    nodes = [{"node": "pve", "status": "online", "cpu": 0.18, "mem": 41 * 2**30, "maxmem": 64 * 2**30,
              "disk": 96, "maxdisk": 100, "uptime": 23 * 86400},
             {"node": "spare", "status": "offline"}]

    async def fake_get(conf, session, path):
        assert path == "/nodes"
        return nodes
    monkeypatch.setattr(w, "proxmox_get", fake_get)
    d = asyncio.run(w.fetch_servers({}, None, {}))
    pve = d["items"][0]
    assert (pve["cpu"], pve["mem"], pve["disk"], pve["mem_total_gb"]) == (18, 64, 96, 64)
    assert d["state"] == "needs" and d["summary"] == "spare is offline"


# ── fetching: once per refresh, the last copy when the source is out of reach ────────────────

def kind(calls, fail=lambda: False):
    async def fetch(conf, session, memo):
        calls.append(1)
        if fail():
            raise w.Problem("Nothing answered at that address.")
        return {"state": "ok", "summary": f"call {len(calls)}"}
    return w.Kind("demo", "Demo", "home", 60, [], fetch)


def test_fetched_once_per_refresh_and_the_last_copy_kept_when_it_fails(tmp_path):
    now, calls, failing = [1000.0], [], [False]
    k = kind(calls, lambda: failing[0])
    ws = w.Widgets(tmp_path / "widgets.json", clock=lambda: now[0])
    w.save_all({"demo": {"x": 1}}, ws.path)

    async def run():
        a = await ws.get(k)
        b = await ws.get(k)                                   # within the minute: the same copy
        now[0] += 61
        failing[0] = True
        c = await ws.get(k)                                   # the source is out of reach
        await ws.session.close() if ws.session else None
        return a, b, c
    a, b, c = asyncio.run(run())
    assert len(calls) == 2 and a["summary"] == b["summary"] == "call 1"
    assert c["stale"] and c["summary"] == "call 1" and c["at"] == 1000.0 and "Nothing answered" in c["error"]


def test_not_set_up_and_never_reached(tmp_path):
    calls = []
    ws = w.Widgets(tmp_path / "widgets.json")
    assert asyncio.run(ws.get(kind(calls))) == {"state": "setup"} and calls == []
    w.save_all({"demo": {"x": 1}}, ws.path)
    out = asyncio.run(ws.get(kind(calls, lambda: True)))
    assert out["state"] == "unreachable" and "Nothing answered" in out["error"]


def test_needs_goes_to_notifications_for_who_may_see_it(tmp_path):
    ws = w.Widgets(tmp_path / "widgets.json")
    ws.cache = {"services": {"at": 5, "data": {"state": "needs", "summary": "1 down", "items": [{"name": "Nextcloud", "up": False}]}},
                "servers": {"at": 6, "data": {"state": "needs", "summary": "pve: disk 96%", "items": []}}}
    member = ws.needs(is_admin=False)
    assert [i["title"] for i in member] == ["Services: 1 down"] and member[0]["text"] == "Nextcloud"
    assert len(ws.needs(is_admin=True)) == 2


# ── HTTP: who sees and sets what; a secret never comes back out ──────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    role = {"role": "admin"}
    monkeypatch.setattr(w, "_who", lambda request: {"role": role["role"], "username": "x", "id": "1"})
    monkeypatch.setattr("gateway.auth.middleware.require_csrf", lambda f: f)

    async def fake_test(self, kind, conf):
        if conf.get("secret") == "wrong":
            raise w.Problem("Proxmox refused the token: check the token ID and secret.")
        return {"state": "ok", "summary": "1 node"}
    monkeypatch.setattr(w.Widgets, "test", fake_test)
    a = web.Application()
    w.register(a, tmp_path / "widgets.json")
    a["role"] = role
    return a


def call(app, steps):
    async def run():
        out = []
        async with TestClient(TestServer(app)) as c:
            for role, method, path, body in steps:
                app["role"]["role"] = role
                r = await c.request(method, path, json=body)
                out.append((r.status, await r.json() if r.content_type == "application/json" else None))
        return out
    return asyncio.run(run())


def test_members_see_only_home_widgets_and_cannot_set_them_up(app):
    (s1, listing), (s2, _), (s3, _) = call(app, [
        ("user", "GET", "/api/v2/widgets", None),
        ("user", "GET", "/api/v2/widgets/servers", None),
        ("user", "PUT", "/api/v2/widgets/services", {"fields": {"services": "A, http://a"}}),
    ])
    assert [k["id"] for k in listing["widgets"]] == ["services"] and "fields" not in listing["widgets"][0]
    assert s2 == 404 and s3 == 403


def test_setup_is_tried_then_saved_owner_only_and_the_secret_stays_here(app, tmp_path):
    conf = {"url": "https://pve:8006", "token_id": "logos@pve!home", "secret": "s3cret", "own_certificate": True}
    out = call(app, [
        ("admin", "PUT", "/api/v2/widgets/servers", {"fields": {**conf, "secret": "wrong"}}),
        ("admin", "PUT", "/api/v2/widgets/servers", {"fields": conf}),
        ("admin", "PUT", "/api/v2/widgets/servers", {"fields": {**conf, "secret": ""}}),   # blank: kept
        ("admin", "GET", "/api/v2/widgets", None),
    ])
    assert out[0][0] == 400 and "refused" in out[0][1]["error"]
    assert out[1] == (200, {"ok": True, "summary": "1 node"}) and out[2][0] == 200
    path = tmp_path / "widgets.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["servers"]["secret"] == "s3cret"
    listed = {k["id"]: k for k in out[3][1]["widgets"]}["servers"]
    assert listed["set_up"] and "s3cret" not in json.dumps(listed)
    assert {f["key"]: f.get("set") for f in listed["fields"]}["secret"] is True


def test_the_arrangement_may_name_the_new_widgets():
    from gateway import dashboard_api
    assert dashboard_api._pref_ok("widgets", ["weather", "services", "servers"])
    assert not dashboard_api._pref_ok("widgets", ["weather", "nope"])
