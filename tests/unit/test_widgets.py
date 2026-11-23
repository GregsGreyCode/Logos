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
    return w.Kind("demo", "Demo", "home", 60, [w.Field("x", "X")], fetch)


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
    ids = [k["id"] for k in listing["widgets"]]
    assert "services" in ids and not {"servers", "proxmox", "backups"} & set(ids)
    assert all("fields" not in k for k in listing["widgets"])
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


# ── the second batch: Proxmox guests, backups, ad blocking, now playing, downloads, bookmarks ──

def serve_app(routes):
    """A local stand-in service; returns (base url, stop)."""
    async def start():
        app = web.Application()
        for method, path, handler in routes:
            app.router.add_route(method, path, handler)
        server = TestServer(app)
        await server.start_server()
        return server
    return start


def run_with(routes, fn):
    async def go():
        server = await serve_app(routes)()
        try:
            async with aiohttp.ClientSession(timeout=w.TIMEOUT) as s:
                return await fn(f"http://127.0.0.1:{server.port}", s)
        finally:
            await server.close()
    return asyncio.run(go())


def test_proxmox_guests_without_templates(monkeypatch):
    async def fake_get(conf, session, path):
        assert path.startswith("/cluster/resources")
        return [{"vmid": 110, "name": "logos", "status": "running", "type": "qemu", "cpu": 0.09, "mem": 6.1 * 2**30, "node": "pve"},
                {"vmid": 130, "name": "test", "status": "stopped", "type": "lxc", "node": "pve"},
                {"vmid": 9000, "name": "tmpl", "status": "stopped", "template": 1}]
    monkeypatch.setattr(w, "proxmox_get", fake_get)
    d = asyncio.run(w.fetch_guests({}, None, {}))
    assert [g["id"] for g in d["items"]] == [110, 130] and d["summary"] == "1 running · 1 stopped"
    assert d["items"][1]["kind"] == "container" and d["items"][0]["cpu"] == 9


def test_backups_late_or_failed_need_you(monkeypatch):
    now = time_now = __import__("time").time()
    monkeypatch.setattr(w, "logos_export_status", lambda: {"ran": int(now - 3600), "ok": True})

    async def fake_get(conf, session, path):
        if path == "/nodes":
            return [{"node": "pve"}]
        return [{"endtime": int(now - 9 * 86400), "status": "OK"}]                 # nine days ago: late
    monkeypatch.setattr(w, "proxmox_get", fake_get)
    d = asyncio.run(w.fetch_backups({}, None, {"with_conf": {"url": "https://pve:8006"}}))
    assert d["state"] == "needs" and d["summary"] == "Proxmox · pve: late"
    assert [i["name"] for i in d["items"]] == ["Proxmox · pve", "Logos export"]
    # without Servers set up: only Logos's own
    d = asyncio.run(w.fetch_backups({}, None, {}))
    assert d["state"] == "ok" and d["summary"] == "all 1 OK"


def test_pihole_signs_in_reads_and_always_signs_out():
    seen = []

    async def auth(r):
        body = await r.json()
        return web.json_response({"session": {"sid": "abc" if body["password"] == "right" else None}},
                                 status=200 if body["password"] == "right" else 401)

    async def summary(r):
        seen.append(r.headers.get("X-FTL-SID"))
        return web.json_response({"queries": {"total": 20940, "blocked": 4812}})

    async def top(r):
        return web.json_response({"domains": [{"domain": "ads.example"}, {"domain": "telemetry.example"}]})

    async def logout(r):
        seen.append("logout")
        return web.Response(status=204)
    routes = [("POST", "/api/auth", auth), ("GET", "/api/stats/summary", summary), ("GET", "/api/stats/top_domains", top),
              ("DELETE", "/api/auth", logout)]
    d = run_with(routes, lambda base, s: w.fetch_adblock({"product": "pihole", "url": base, "password": "right"}, s, {}))
    assert (d["percent"], d["blocked"], d["top"]) == (23, 4812, ["ads.example", "telemetry.example"])
    assert seen == ["abc", "logout"]
    with pytest.raises(w.Problem, match="refused the password"):
        run_with(routes, lambda base, s: w.fetch_adblock({"product": "pihole", "url": base, "password": "wrong"}, s, {}))


def test_adguard_with_its_login():
    async def stats(r):
        if r.headers.get("Authorization") != aiohttp.BasicAuth("admin", "pw").encode():
            return web.Response(status=401)
        return web.json_response({"num_dns_queries": 1000, "num_blocked_filtering": 150, "top_blocked_domains": [{"ads.example": 40}]})
    d = run_with([("GET", "/control/stats", stats)],
                 lambda base, s: w.fetch_adblock({"product": "adguard", "url": base, "username": "admin", "password": "pw"}, s, {}))
    assert (d["percent"], d["top"], d["summary"]) == (15, ["ads.example"], "AdGuard")


def test_jellyfin_now_playing():
    async def sessions(r):
        assert r.headers.get("X-Emby-Token") == "k"
        return web.json_response([
            {"DeviceName": "Living room TV", "UserName": "Béré", "PlayState": {"PositionTicks": 3720 * 10**7, "IsPaused": False},
             "NowPlayingItem": {"Name": "A film", "RunTimeTicks": 6420 * 10**7}},
            {"DeviceName": "Laptop", "UserName": "Greg", "PlayState": {"IsPaused": True},
             "NowPlayingItem": {"Name": "Ep", "SeriesName": "A series", "ParentIndexNumber": 2, "IndexNumber": 4}},
            {"DeviceName": "Phone"}])
    d = run_with([("GET", "/Sessions", sessions)], lambda base, s: w.fetch_playing({"url": base, "api_key": "k"}, s, {}))
    assert [i["title"] for i in d["items"]] == ["A film", "A series · S2 E4"] and d["items"][0]["percent"] == 58
    assert d["items"][1]["paused"] and d["summary"] == "2 playing"


def test_downloads_from_sonarr_and_radarr_with_a_stalled_one():
    async def queue(r):
        assert r.headers.get("X-Api-Key") == "k"
        return web.json_response({"records": [
            {"title": "Show S01E05", "size": 100, "sizeleft": 28, "timeleft": "00:04:00", "status": "downloading"},
            {"title": "Show S01E06", "size": 100, "sizeleft": 100, "status": "warning", "trackedDownloadState": "importBlocked"}]})

    async def calendar(r):
        return web.json_response([{}, {}, {}, {}])
    routes = [("GET", "/api/v3/queue", queue), ("GET", "/api/v3/calendar", calendar)]
    d = run_with(routes, lambda base, s: w.fetch_downloads({"sonarr_url": base, "sonarr_key": "k"}, s, {}))
    assert [i["percent"] for i in d["items"]] == [72, 0] and d["items"][1]["stalled"]
    assert d["state"] == "needs" and d["summary"] == "1 stalled" and d["upcoming"] == 4
    with pytest.raises(w.Problem, match="Sonarr, Radarr"):
        asyncio.run(w.fetch_downloads({}, None, {}))


def test_proxmox_shares_the_servers_setup_and_backups_needs_none(app, tmp_path):
    conf = {"url": "https://pve:8006", "token_id": "logos@pve!home", "secret": "s3cret", "own_certificate": True}
    out = call(app, [
        ("admin", "GET", "/api/v2/widgets", None),
        ("admin", "PUT", "/api/v2/widgets/proxmox", {"fields": conf}),      # set up from the Proxmox widget
        ("admin", "GET", "/api/v2/widgets", None),
    ])
    before = {k["id"]: k for k in out[0][1]["widgets"]}
    after = {k["id"]: k for k in out[2][1]["widgets"]}
    assert not before["proxmox"]["set_up"] and before["backups"]["set_up"] and before["proxmox"]["uses"] == "servers"
    assert [f["key"] for f in before["proxmox"]["fields"]] == ["url", "token_id", "secret", "own_certificate"]
    assert after["proxmox"]["set_up"] and after["servers"]["set_up"]
    assert set(json.loads((tmp_path / "widgets.json").read_text())) == {"servers"}


# ── the third batch: energy and UPS, network, cameras, Plex ──────────────────────────────────

def ha_routes(states):
    async def all_states(r):
        if r.headers.get("Authorization") != "Bearer t":
            return web.Response(status=401)
        return web.json_response(states)
    return [("GET", "/api/states", all_states)]


STATES = [
    {"entity_id": "sensor.house_power", "state": "0.412", "attributes": {"unit_of_measurement": "kW", "device_class": "power"}},
    {"entity_id": "sensor.energy_today", "state": "6.84", "attributes": {"device_class": "energy"}},
    {"entity_id": "sensor.ups_battery", "state": "100", "attributes": {"device_class": "battery"}},
    {"entity_id": "sensor.ups_status", "state": "Online", "attributes": {}},
    {"entity_id": "sensor.ups_runtime", "state": "2280", "attributes": {"unit_of_measurement": "s"}},
]


def test_energy_from_home_assistant_with_the_ups(monkeypatch):
    conf = {"power": "sensor.house_power", "energy": "sensor.energy_today", "price": "25",
            "ups_battery": "sensor.ups_battery", "ups_status": "sensor.ups_status", "ups_runtime": "sensor.ups_runtime"}

    def go(states, conf):
        def fn(base, s):
            monkeypatch.setattr(w, "_ha", lambda: {"url": base, "token": "t"})
            return w.fetch_energy(conf, s, {})
        return run_with(ha_routes(states), fn)
    d = go(STATES, conf)
    assert (d["power_w"], d["energy_kwh"], d["cost"]) == (412, 6.8, 1.71)
    assert d["ups"]["minutes"] == 38 and d["state"] == "ok" and d["summary"] == "UPS on mains"
    on_batt = [dict(s, state="On Battery") if s["entity_id"] == "sensor.ups_status" else s for s in STATES]
    d = go(on_batt, conf)
    assert d["state"] == "needs" and d["summary"] == "UPS on battery · 38 min left"
    with pytest.raises(w.Problem, match="no sensor.nope. Some it has: sensor.house_power"):
        go(STATES, {"power": "sensor.nope"})


def test_energy_without_home_assistant_says_so(monkeypatch):
    from gateway import house_host

    def no_ha(env):
        raise house_host.HouseError("no ~/.config/logos/home-assistant")
    monkeypatch.setattr(house_host, "ha_settings", no_ha)
    with pytest.raises(w.Problem, match="not connected to Home Assistant"):
        asyncio.run(w.fetch_energy({"power": "sensor.x"}, None, {}))


def test_network_from_speedtest_tracker_and_much_slower_than_usual():
    speed = {"down": 512e6}

    async def latest(r):
        if r.headers.get("Authorization") != "Bearer k":
            return web.Response(status=401)
        return web.json_response({"data": {"download_bits": speed["down"], "upload_bits": 96e6, "ping": 9.2, "created_at": "2026-11-23 07:00"}})
    memo = {}

    def once(base, s):
        return w.fetch_network({"url": base, "token": "k"}, s, memo)
    routes = [("GET", "/api/v1/results/latest", latest)]
    for _ in range(5):
        d = run_with(routes, once)
    assert (d["down"], d["up"], d["ping"], d["state"]) == (512, 96, 9, "ok")
    speed["down"] = 120e6
    d = run_with(routes, once)
    assert d["state"] == "needs" and d["summary"] == "slow: 120 Mbit/s (usually 512)"
    with pytest.raises(w.Problem, match="refused the API token"):
        run_with(routes, lambda base, s: w.fetch_network({"url": base, "token": "bad"}, s, {}))


def frigate_routes():
    async def config(r):
        return web.json_response({"cameras": {"front_door": {}, "hallway": {}, "old": {"enabled": False}}})

    async def events(r):
        if r.query["camera"] == "front_door":
            return web.json_response([{"label": "person", "start_time": 1795369320}])
        return web.json_response([])

    async def latest(r):
        return web.Response(body=b"\xff\xd8jpeg", content_type="image/jpeg")
    return [("GET", "/api/config", config), ("GET", "/api/events", events), ("GET", "/api/{cam}/latest.jpg", latest)]


def test_cameras_from_frigate():
    d = run_with(frigate_routes(), lambda base, s: w.fetch_cameras({"url": base}, s, {}))
    assert d["items"] == [{"name": "front_door", "label": "person", "at": 1795369320}, {"name": "hallway", "label": "", "at": None}]
    assert d["summary"] == "2 cameras"


def test_a_snapshot_comes_through_logos_only_for_a_known_camera_and_an_admin(app, tmp_path):
    async def run():
        server = await serve_app(frigate_routes())()
        try:
            w.save_all({"cameras": {"url": f"http://127.0.0.1:{server.port}"}}, tmp_path / "widgets.json")
            app["widgets"].cache["cameras"] = {"data": {"items": [{"name": "front_door"}]}}
            out = []
            async with TestClient(TestServer(app)) as c:
                for role, cam in (("admin", "front_door"), ("admin", "not_a_camera"), ("user", "front_door")):
                    app["role"]["role"] = role
                    r = await c.get(f"/api/v2/widgets/cameras/snapshot/{cam}")
                    out.append((r.status, r.content_type, await r.read() if r.status == 200 else b""))
            return out
        finally:
            await server.close()
    out = asyncio.run(run())
    assert out[0] == (200, "image/jpeg", b"\xff\xd8jpeg")
    assert [s for s, _, _ in out[1:]] == [404, 404]


def test_plex_now_playing():
    async def sessions(r):
        assert r.headers.get("X-Plex-Token") == "tok"
        return web.json_response({"MediaContainer": {"Metadata": [
            {"title": "Ep", "grandparentTitle": "A series", "parentIndex": 2, "index": 4, "duration": 2800000, "viewOffset": 600000,
             "Player": {"title": "Laptop", "state": "paused"}, "User": {"title": "Greg"}}]}})
    d = run_with([("GET", "/status/sessions", sessions)],
                 lambda base, s: w.fetch_playing({"product": "plex", "url": base, "api_key": "tok"}, s, {}))
    assert d["items"][0]["title"] == "A series · S2 E4" and d["items"][0]["paused"] and d["items"][0]["percent"] == 21


def test_a_widget_may_be_chosen_compact_or_full_by_each_person():
    from gateway import dashboard_api as d
    assert d._pref_ok("widget_sizes", {"lists": "full"}) and d._pref_ok("widget_sizes", {})
    assert not d._pref_ok("widget_sizes", {"lists": "huge"})          # a size it does not have
    assert not d._pref_ok("widget_sizes", {"weather": "full"})        # a widget with no sizes
    assert not d._pref_ok("widget_sizes", "full")


# ── the Las Vegas sign: your own text and RSS or Atom feeds ──────────────────────────────────

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Prices</title>
<item><title>BTC $64,210 (+1.2%)</title><link>http://x/1</link></item>
<item><title><![CDATA[ETH <b>$3,120</b> &amp; rising]]></title></item>
<item><title>   </title></item></channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Stocks</title>
<entry><title>ACME 123.4 ▲ 0.8%</title></entry><entry><title type="html">&lt;i&gt;GLOBEX&lt;/i&gt; 55.0</title></entry></feed>"""


def test_feeds_give_their_item_titles_as_plain_text():
    assert w.parse_feed(RSS) == ["BTC $64,210 (+1.2%)", "ETH $3,120 & rising"]
    assert w.parse_feed(ATOM) == ["ACME 123.4 ▲ 0.8%", "GLOBEX 55.0"]
    assert len(w.parse_feed("<rss><channel>" + "".join(f"<item><title>t{i}</title></item>" for i in range(40)) + "</channel></rss>")) == w.FEED_ITEMS


def test_what_is_not_a_feed_or_is_unsafe_is_refused_in_plain_words():
    for bad, why in (("<html><body>hello</body></html>", "no items"), ("not xml at all <<", "did not give an RSS"),
                     ("<rss><channel></channel></rss>", "no items"),
                     ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>', "features Logos does not read")):
        with pytest.raises(w.Problem, match=why):
            w.parse_feed(bad)


def feed_routes(state):
    async def stocks(r):
        return web.Response(text=RSS, content_type="application/rss+xml")

    async def crypto(r):
        return web.Response(text=ATOM, content_type="application/atom+xml")

    async def gone(r):
        return web.Response(status=503)

    async def huge(r):
        return web.Response(text="<rss>" + "x" * (w.FEED_MAX_BYTES + 10) + "</rss>")
    return [("GET", "/stocks.rss", stocks), ("GET", "/crypto.atom", crypto), ("GET", "/gone", gone), ("GET", "/huge", huge)]


def test_the_sign_shows_your_text_then_each_feed_and_survives_a_feed_that_fails():
    def go(conf):
        return run_with(feed_routes(None), lambda base, s: w.fetch_marquee({k: (v.replace("BASE", base) if isinstance(v, str) else v) for k, v in conf.items()}, s, {}))
    d = go({"text": "Welcome home\n# a note\nBins out tonight", "feeds": "BASE/stocks.rss\nBASE/crypto.atom", "speed": "slow"})
    assert [i["text"] for i in d["items"]] == ["Welcome home", "Bins out tonight", "BTC $64,210 (+1.2%)", "ETH $3,120 & rising", "ACME 123.4 ▲ 0.8%", "GLOBEX 55.0"]
    assert d["speed"] == "slow" and d["state"] == "ok" and d["problems"] == []
    d = go({"text": "Hi", "feeds": "BASE/gone\nBASE/stocks.rss"})
    assert [i["text"] for i in d["items"]][-2:] == ["BTC $64,210 (+1.2%)", "ETH $3,120 & rising"]
    assert len(d["problems"]) == 1 and "503" in d["problems"][0] and "1 feed not answering" == d["summary"]
    assert go({"text": "Just this"})["items"] == [{"text": "Just this", "source": "text"}]
    with pytest.raises(w.Problem, match="503"):
        go({"feeds": "BASE/gone"})
    with pytest.raises(w.Problem, match="too big"):
        go({"feeds": "BASE/huge"})
    with pytest.raises(w.Problem, match="Write some text"):
        go({"text": "  \n", "feeds": ""})
    with pytest.raises(w.Problem, match="Up to 5"):
        go({"feeds": "\n".join(f"BASE/stocks.rss?{i}" for i in range(6))})
    with pytest.raises(w.Problem, match="start it with http"):
        go({"feeds": "ftp://example.com/x"})


def test_the_sign_is_for_everyone_at_home_and_set_up_by_whoever_manages_logos(app):
    out = call(app, [("user", "GET", "/api/v2/widgets", None), ("user", "PUT", "/api/v2/widgets/marquee", {"fields": {"text": "hi"}})])
    assert "marquee" in [k["id"] for k in out[0][1]["widgets"]] and out[1][0] == 403
    assert w.KINDS["marquee"].who == "home" and not any(f.required for f in w.KINDS["marquee"].fields)
