"""Models › Local vs cloud: the card's figures and its settings are for whoever manages Logos."""
import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import dashboard_api, local_cost


class Db:
    def get_user_by_id(self, uid):
        return {"username": uid}


def call(monkeypatch, tmp_path, steps):
    monkeypatch.setattr(dashboard_api, "auth_db", Db())
    monkeypatch.setattr(dashboard_api.agent_chat, "runtime_of", lambda backend: None)   # no looks in this test
    file = tmp_path / "local-usage.json"

    async def flow():
        role = {"v": "admin"}

        @web.middleware
        async def as_user(request, handler):
            request["current_user"] = {"sub": "greg", "role": role["v"]}
            return await handler(request)

        app = web.Application(middlewares=[as_user])
        local_cost.register(app, file)
        out = []
        async with TestClient(TestServer(app)) as client:
            client.session.cookie_jar.update_cookies({"csrf_token": "t"})
            for method, who, body in steps:
                role["v"] = who
                r = await client.request(method, "/api/v2/models/local-cost", json=body, headers={"X-CSRF-Token": "t"})
                out.append((r.status, await r.json() if r.status != 403 else None))
        return out
    return asyncio.run(flow()), file


def test_admin_sees_the_card_and_changes_the_prices(monkeypatch, tmp_path):
    (got, saved, bad, after, user_get, user_put), file = call(monkeypatch, tmp_path, [
        ("GET", "admin", None),
        ("PUT", "admin", {"price_per_kwh": 0.3, "currency": "EUR", "usd_rate": 0.92, "baseline_watts": 25}),
        ("PUT", "admin", {"price_per_kwh": -2}),
        ("GET", "admin", None),
        ("GET", "user", None),
        ("PUT", "user", {"price_per_kwh": 1}),
    ])
    assert got[0] == 200 and got[1]["settings"]["price_per_kwh"] == 0.25 and got[1]["settings"]["currency"] == "GBP"
    assert {m["model"] for m in got[1]["prices"]["models"]} == {"haiku-5.5", "sonnet-5.5", "opus-5.5"}
    assert saved[0] == 200 and saved[1]["settings"]["currency"] == "EUR"
    assert bad[0] == 400 and "kWh" in bad[1]["error"]
    assert after[1]["settings"]["baseline_used"] == 25 and after[1]["settings"]["baseline_from"] == "setting"
    assert user_get[0] == 403 and user_put[0] == 403
    assert json.loads(file.read_text())["settings"]["usd_rate"] == 0.92
