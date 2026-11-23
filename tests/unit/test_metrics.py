"""F42: the activity counter, the Prometheus /metrics endpoint and the Agents-today numbers."""
import asyncio
import json
import stat
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import activity, alert_text, metrics


# ── the counter ──────────────────────────────────────────────────────────────────────────────

def test_counts_messages_per_agent_and_day_and_survives_a_restart(tmp_path):
    now = [1_795_400_000.0]                                    # 23 Nov 2026
    a = activity.Activity(tmp_path, clock=lambda: now[0])
    a.record("greg", "$1")
    a.record("greg", "$2")
    a.record("greg", "$2")                                     # the same note again: counted once
    a.record("household", "$3")
    now[0] += 86400
    a.record("greg", "$4")
    assert a.today("greg") == {"messages": 1, "last": int(now[0])} and a.total("greg") == 3
    now[0] -= 86400
    assert a.today("greg")["messages"] == 2 and a.today("household")["messages"] == 1
    b = activity.Activity(tmp_path, clock=lambda: now[0])      # a new start reads what was kept
    assert b.today("greg")["messages"] == 2 and b.total("greg") == 3 and b.last_ever("greg") == int(now[0]) + 86400
    assert stat.S_IMODE((tmp_path / "activity.json").stat().st_mode) == 0o600


def test_only_numbers_are_kept(tmp_path):
    a = activity.Activity(tmp_path)
    a.record("greg", "$secret-looking-event-id")
    saved = (tmp_path / "activity.json").read_text()
    assert "secret-looking" not in saved and set(json.loads(saved)) == {"days", "totals"}


def test_old_days_are_dropped(tmp_path):
    now = [1_795_400_000.0]
    a = activity.Activity(tmp_path, clock=lambda: now[0])
    for _ in range(45):
        a.record("greg")
        now[0] += 86400
    assert len(a.data["days"]) == activity.KEEP_DAYS and a.total("greg") == 45


def test_a_note_from_an_agent_is_counted_never_its_text(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = web.Application()
    alert_text.register(app)
    app["activity"] = activity.Activity(tmp_path)

    async def run():
        async with TestClient(TestServer(app)) as c:
            r = await c.post(alert_text.PATH, json={"agent": "greg", "token": alert_text.token_for("greg"),
                                                    "event_id": "$e1", "text": "The dentist is at 10:30"})
            bad = await c.post(alert_text.PATH, json={"agent": "greg", "token": "wrong", "event_id": "$e2", "text": "x"})
            return r.status, bad.status
    assert asyncio.run(run()) == (200, 403)
    assert app["activity"].today("greg")["messages"] == 1                # the refused note is not counted
    assert "dentist" not in (tmp_path / "activity.json").read_text()


# ── the endpoint ─────────────────────────────────────────────────────────────────────────────

ROWS = [{"agent": "greg", "name": "Greg's agent", "ready": True, "messages": 5, "last": 1_795_400_000, "total": 120, "today": True},
        {"agent": "bere", "name": "Béré's agent", "ready": False, "messages": 0, "last": 0, "total": 3, "today": False}]


def test_the_exposition_has_numbers_only():
    text = metrics.render(ROWS, True, {"services": True, "backups": False}, {"commit": "abc1234"})
    assert 'logos_agent_up{agent="greg"} 1' in text and 'logos_agent_up{agent="bere"} 0' in text
    assert 'logos_agent_messages_today{agent="greg"} 5' in text
    assert 'logos_agent_messages_total{agent="greg"} 120' in text
    assert 'logos_agent_last_message_timestamp_seconds{agent="bere"} 0' in text
    assert "logos_model_up 1" in text and 'logos_widget_needs{widget="services"} 1' in text
    assert "# TYPE logos_agent_messages_total counter" in text and 'logos_info{version="abc1234"} 1' in text
    assert "Béré" not in text and "Greg's" not in text                       # display names are not exported
    assert "logos_model_up" not in metrics.render(ROWS, None, {}, {})        # no local model: no metric


@pytest.fixture
def app(tmp_path, monkeypatch):
    role = {"role": "admin"}
    monkeypatch.setattr(metrics, "_admin", lambda request: role["role"] == "admin")
    monkeypatch.setattr("gateway.auth.middleware.require_csrf", lambda f: f)

    async def rows(app, names=None):
        return ROWS

    async def model(app, names=None):
        return True
    monkeypatch.setattr(metrics, "agent_rows", rows)
    monkeypatch.setattr(metrics, "model_up", model)
    a = web.Application()
    metrics.register(a, tmp_path / "metrics.json")
    a["role"] = role
    return a


def scenario(app, *rounds):
    """Each round is a list of steps; all of them run on one event loop (an app belongs to one)."""
    async def run():
        out = []
        async with TestClient(TestServer(app)) as c:
            for steps in rounds:
                got = []
                for role, method, path, body, headers in steps:
                    app["role"]["role"] = role
                    r = await c.request(method, path, json=body, headers=headers or {})
                    got.append((r.status, await r.json() if r.content_type == "application/json" else await r.text()))
                out.append(got)
        return out
    return asyncio.run(run())


def test_off_by_default_then_on_with_a_token_shown_once(app, tmp_path):
    async def run():
        async with TestClient(TestServer(app)) as c:
            app["role"]["role"] = "admin"
            off = (await c.get("/metrics")).status, (await (await c.get("/api/v2/metrics")).json())["enabled"]
            on = await (await c.put("/api/v2/metrics", json={"enabled": True})).json()
            token = on["token"]
            saved = (tmp_path / "metrics.json").read_text()
            app["role"]["role"] = "user"
            good = await c.get("/metrics", headers={"Authorization": f"Bearer {token}"})
            wrong = await c.get("/metrics", headers={"Authorization": "Bearer wrong"})
            none = await c.get("/metrics")
            app["role"]["role"] = "admin"
            status = await (await c.get("/api/v2/metrics")).json()
            return off, on, saved, good.status, await good.text(), wrong.status, none.status, status
    off, on, saved, good, text, wrong, none, status = asyncio.run(run())
    assert off == (404, False) and on["enabled"] and len(on["token"]) >= 40
    assert on["token"] not in saved and stat.S_IMODE((tmp_path / "metrics.json").stat().st_mode) == 0o600
    assert good == 200 and 'logos_agent_messages_today{agent="greg"} 5' in text
    assert (wrong, none) == (404, 404)                                         # wrong or no token: not there
    assert status["enabled"] is True and "token" not in status


def test_turning_it_off_forgets_the_token_and_on_again_is_a_new_one(app):
    async def run():
        async with TestClient(TestServer(app)) as c:
            t1 = (await (await c.put("/api/v2/metrics", json={"enabled": True})).json())["token"]
            off = await (await c.put("/api/v2/metrics", json={"enabled": False})).json()
            gone = (await c.get("/metrics", headers={"Authorization": f"Bearer {t1}"})).status
            t2 = (await (await c.put("/api/v2/metrics", json={"enabled": True})).json())["token"]
            old = (await c.get("/metrics", headers={"Authorization": f"Bearer {t1}"})).status
            new = (await c.get("/metrics", headers={"Authorization": f"Bearer {t2}"})).status
            return off["enabled"], gone, t1 != t2, old, new
    assert asyncio.run(run()) == (False, 404, True, 404, 200)


def test_rotating_gives_a_new_token_and_the_old_stops_working(app):
    async def run():
        async with TestClient(TestServer(app)) as c:
            t1 = (await (await c.put("/api/v2/metrics", json={"enabled": True})).json())["token"]
            t2 = (await (await c.put("/api/v2/metrics", json={"enabled": True, "rotate": True})).json())["token"]
            return [(await c.get("/metrics", headers={"Authorization": f"Bearer {t}"})).status for t in (t1, t2)]
    assert asyncio.run(run()) == [404, 200]


def test_only_whoever_manages_logos_switches_it(app):
    async def run():
        async with TestClient(TestServer(app)) as c:
            app["role"]["role"] = "user"
            return [(await c.get("/api/v2/metrics")).status, (await c.put("/api/v2/metrics", json={"enabled": True})).status,
                    (await c.get("/metrics")).status]
    assert asyncio.run(run()) == [403, 403, 404]


# ── the Agents-today widget ──────────────────────────────────────────────────────────────────

def test_the_agents_widget_is_per_person_and_needs_no_setup(monkeypatch, tmp_path):
    from gateway import dashboard_api, widgets as w

    async def visible(request, username):
        return {"greg": {"definition": {}}} if username == "greg" else {"household": {"definition": {}}}

    async def rows(app, names=None):
        return [r for r in ROWS if r["agent"] in (names or {})] or [dict(ROWS[0], agent="household", name="Household agent")]

    async def model(app, names=None):
        return False
    monkeypatch.setattr(dashboard_api, "_visible_agents", visible)
    monkeypatch.setattr(metrics, "agent_rows", rows)
    monkeypatch.setattr(metrics, "model_up", model)
    who = {"username": "greg", "role": "user", "id": "1"}
    monkeypatch.setattr(w, "_who", lambda request: who)
    a = web.Application()
    w.register(a, tmp_path / "widgets.json")

    async def run():
        async with TestClient(TestServer(a)) as c:
            listing = (await (await c.get("/api/v2/widgets")).json())["widgets"]
            mine = await (await c.get("/api/v2/widgets/agents")).json()
            who["username"] = "bere"
            hers = await (await c.get("/api/v2/widgets/agents")).json()
            return listing, mine, hers
    listing, mine, hers = asyncio.run(run())
    entry = next(k for k in listing if k["id"] == "agents")
    assert entry["set_up"] and entry["who"] == "home"
    assert [i["agent"] for i in mine["items"]] == ["greg"] and [i["agent"] for i in hers["items"]] == ["household"]
    assert mine["state"] == "ok" and mine["summary"] == "AI computer off" and mine["model_up"] is False


def test_vault_numbers_are_counts_only_and_agents_without_a_vault_are_left_out():
    text = metrics.render(ROWS, None, {}, {}, {ROWS[0]["agent"]: {"files": 4, "bytes": 2048, "oldest": 100, "newest": 200, "state_new": 1, "state_done": 3},
                                               "other": None})
    assert f'logos_vault_files{{agent="{ROWS[0]["agent"]}"}} 4' in text and f'logos_vault_bytes{{agent="{ROWS[0]["agent"]}"}} 2048' in text
    assert f'logos_vault_files_by_state{{agent="{ROWS[0]["agent"]}",state="new"}} 1' in text and 'state="failed"} 0' in text
    assert 'agent="other"' not in text and "logos_vault" not in metrics.render(ROWS, None, {}, {})


def test_the_vault_helper_output_is_read_and_cached(monkeypatch):
    from gateway import vault_stats
    assert vault_stats.parse("files 3\nbytes 900\noldest 5\nnewest 9\nstate_new 1\nstate_done 2\n") == {"files": 3, "bytes": 900, "oldest": 5, "newest": 9, "state_new": 1, "state_done": 2}
    assert vault_stats.parse("") is None and vault_stats.parse("garbage") is None
    calls = []

    class R:
        def run(self, argv):
            calls.append(argv[1])
            return types.SimpleNamespace(returncode=0, stdout="files 2\nbytes 10\n")
    rt = types.SimpleNamespace(runner=R())
    vault_stats._cache.clear()
    assert vault_stats.read(rt, "greg", now=1000)["files"] == 2 and vault_stats.read(rt, "greg", now=1100)["files"] == 2
    assert calls == ["volume", "run"]                                  # the second read came from the cache
    seen = []
    vault_stats._cache.clear()

    class R2:
        def run(self, argv):
            seen.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="files 0\n")
    vault_stats.read(types.SimpleNamespace(runner=R2()), "x", now=1)
    run = seen[1]
    assert run[run.index("--entrypoint") + 1] == "sh" and "--network" in run and run[run.index("-v") + 1].endswith(":ro")   # alpine/git's entrypoint is git: sh must be asked for
    assert vault_stats.read(rt, "greg", now=1400) is not None and calls.count("run") == 2


def test_the_model_queue_and_disk_numbers():
    text = metrics.render(ROWS, True, {}, {}, {ROWS[0]["agent"]: {"files": 0, "home_kb": 2048}},
                          {"off": False, "off_since": None, "queue": {"running": 1, "waiting": 2, "slots": 2, "tokens_running": 9000, "window": 96512,
                                                                         "served": 40, "queued": 7, "wait_avg_s": 3.2, "wait_max_s": 61.5}})
    for line in ("logos_model_server_off 0", "logos_model_requests_waiting 2", "logos_model_requests_running 1", "logos_model_slots 2",
                 "logos_model_context_window_tokens 96512", "logos_model_requests_served_total 40", "logos_model_wait_seconds_max 61.5",
                 "logos_model_server_off_since_timestamp_seconds 0", f'logos_agent_home_bytes{{agent="{ROWS[0]["agent"]}"}} 2097152'):
        assert line in text, line
    assert "# TYPE logos_model_requests_served_total counter" in text
    off = metrics.render(ROWS, False, {}, {}, None, {"off": True, "off_since": 1795470000, "queue": None})
    assert "logos_model_server_off 1" in off and "logos_model_server_off_since_timestamp_seconds 1795470000" in off and "logos_model_requests_waiting" not in off
    assert "logos_model_server_off" not in metrics.render(ROWS, None, {}, {})              # no guard: nothing invented
