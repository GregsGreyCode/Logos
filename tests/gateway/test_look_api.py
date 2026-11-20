"""The look (Calm glass, Terminal HUD, Warm) is each person's own, kept with their login."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import dashboard_api

USERS = {"u-greg": {"username": "greg", "role": "admin"}, "u-bere": {"username": "bere", "role": "user"}}


class Db:
    def __init__(self):
        self.settings = {"u-greg": {"ui_theme": "midnight"}, "u-bere": {"ui_theme": "midnight"}}

    def get_user_by_id(self, uid):
        return USERS.get(uid)

    def get_user_settings(self, uid):
        return self.settings.get(uid)

    def update_user_settings(self, uid, **fields):
        self.settings[uid].update(fields)
        return self.settings[uid]


@pytest.fixture
def api(monkeypatch):
    db = Db()
    monkeypatch.setattr(dashboard_api, "auth_db", db)

    def go(uid, method, path, body=None):
        @web.middleware
        async def as_user(request, handler):
            request["current_user"] = {"sub": uid, "role": USERS[uid]["role"]}
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
    return go, db


def test_no_look_until_chosen_then_it_is_yours_only(api):
    go, db = api
    assert go("u-bere", "GET", "/api/v2/me")[1]["look"] is None          # the old "midnight" means not chosen
    assert go("u-bere", "PUT", "/api/v2/me/look", {"look": "warm", "mode": "auto"}) == (200, {"look": "warm", "mode": "auto"})
    assert go("u-bere", "GET", "/api/v2/me")[1]["look"] == {"look": "warm", "mode": "auto"}
    assert go("u-greg", "GET", "/api/v2/me")[1]["look"] is None          # Greg's is untouched


def test_hud_is_dark_only_and_nonsense_is_refused(api):
    go, db = api
    assert go("u-greg", "PUT", "/api/v2/me/look", {"look": "hud", "mode": "light"})[1] == {"look": "hud", "mode": "dark"}
    assert go("u-greg", "PUT", "/api/v2/me/look", {"look": "neon", "mode": "dark"})[0] == 400
    assert go("u-greg", "PUT", "/api/v2/me/look", {"look": "glass", "mode": "sepia"})[0] == 400
    assert db.settings["u-greg"]["ui_theme"] == "hud:dark"


def test_chat_options_are_kept_with_the_login_and_only_known_ones(api):
    go, db = api
    assert go("u-bere", "GET", "/api/v2/me")[1]["prefs"] == {}
    assert go("u-bere", "PUT", "/api/v2/me/prefs", {"panel": "open"}) == (200, {"panel": "open"})
    assert go("u-bere", "PUT", "/api/v2/me/prefs", {"thinking": True})[1] == {"panel": "open", "thinking": True}
    assert go("u-bere", "PUT", "/api/v2/me/prefs", {"panel": "sideways"})[0] == 400
    assert go("u-bere", "PUT", "/api/v2/me/prefs", {"admin": True})[0] == 400
    assert go("u-bere", "GET", "/api/v2/me")[1]["prefs"] == {"panel": "open", "thinking": True}
    assert go("u-greg", "GET", "/api/v2/me")[1]["prefs"] == {}
