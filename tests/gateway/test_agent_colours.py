"""ST2: an agent's colour is the same for everyone, and only the people who may use the agent set it."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import dashboard_api
from tests.gateway import test_dashboard_api as base


class Db(base.FakeDb):
    def __init__(self):
        self.colours, self.settings = {}, {"u-greg": {}, "u-bere": {}}

    def get_agent_colours(self):
        return dict(self.colours)

    def set_agent_colour(self, name, colour, set_by):
        self.colours[name] = colour

    def get_user_settings(self, uid):
        return self.settings.get(uid)

    def update_user_settings(self, uid, **fields):
        self.settings[uid].update(fields)


@pytest.fixture
def api(monkeypatch):
    db = Db()
    monkeypatch.setattr(dashboard_api, "auth_db", db)
    monkeypatch.setattr(dashboard_api.agent_chat, "sync_definitions",
                        lambda rt, d: {n: dashboard_api.agent_chat.owners_of(x) for n, x in base.DEFS.items()
                                       if x.get("dashboard") is not False})

    def go(uid, method, path, body=None):
        @web.middleware
        async def as_user(request, handler):
            request["current_user"] = {"sub": uid, "role": "user"}
            return await handler(request)

        async def flow():
            app = web.Application(middlewares=[as_user])
            app["sandbox_backend"] = base.Backend()
            app["agent_links"] = base.Links(base.Client(base.SESSIONS, base.MESSAGES))
            dashboard_api.register(app)
            async with TestClient(TestServer(app)) as client:
                client.session.cookie_jar.update_cookies({"csrf_token": "t"})
                r = await client.request(method, path, json=body, headers={"X-CSRF-Token": "t"})
                return r.status, (await r.json() if r.content_type == "application/json" else None)
        return asyncio.run(flow())
    return go, db


def test_you_colour_your_own_agent_and_either_of_you_the_household(api):
    go, db = api
    assert go("u-greg", "PUT", "/api/v2/agents/greg/colour", {"colour": "#7C8CFF"})[0] == 200
    assert go("u-bere", "PUT", "/api/v2/agents/household/colour", {"colour": "#5be3a6"})[0] == 200
    assert go("u-bere", "PUT", "/api/v2/agents/greg/colour", {"colour": "#ff0000"})[0] == 404   # not hers
    assert go("u-greg", "PUT", "/api/v2/agents/greg/colour", {"colour": "red"})[0] == 400
    assert db.colours == {"greg": "#7c8cff", "household": "#5be3a6"}
    assert go("u-bere", "GET", "/api/v2/me")[1]["agent_colours"] == {"greg": "#7c8cff", "household": "#5be3a6"}  # everyone sees them


def test_your_own_colours_motion_and_names_are_kept_with_your_login(api):
    go, db = api
    assert go("u-greg", "PUT", "/api/v2/me/prefs", {"primary": "#2f9e8f", "attention": "#ff8a6b", "motion": "still", "names": "always"})[0] == 200
    assert go("u-greg", "GET", "/api/v2/me")[1]["prefs"] == {"primary": "#2f9e8f", "attention": "#ff8a6b", "motion": "still", "names": "always"}
    assert go("u-greg", "PUT", "/api/v2/me/prefs", {"primary": ""})[1]["primary"] == ""          # back to the look's own
    assert go("u-greg", "PUT", "/api/v2/me/prefs", {"primary": "url(x)"})[0] == 400
    assert go("u-greg", "PUT", "/api/v2/me/prefs", {"motion": "wild"})[0] == 400
