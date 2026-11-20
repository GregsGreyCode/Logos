"""The dashboard's advisor endpoints: you see and decide only your own agent's questions."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import dashboard_api

USERS = {"u-greg": {"username": "greg", "role": "admin"}, "u-bere": {"username": "bere", "role": "user"}}


class Db:
    def get_user_by_id(self, uid):
        return USERS.get(uid)


class Host:
    def base(self):
        return "http://advisor"


@pytest.fixture
def api(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard_api, "auth_db", Db())
    monkeypatch.setattr(dashboard_api, "_advisor", lambda request: (Host(), "dash-token"))

    def fake_call(host, token, method, path, body=None):
        calls.append((method, path, body, token))
        if method == "GET":
            return 200, {"pending": [], "history": [], "budget": {}, "key": True}
        return 200, {"status": "sending"}
    monkeypatch.setattr(dashboard_api, "_advisor_call", fake_call)

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
    return go, calls


def test_each_person_sees_their_own_and_greg_may_read_beres_log(api):
    go, calls = api
    status, body = go("u-bere", "GET", "/api/v2/advisor")
    assert status == 200 and body["person"] == "bere" and body["can_approve"] is True
    assert calls[-1][:2] == ("GET", "/approvals/bere") and calls[-1][3] == "dash-token"
    assert go("u-bere", "GET", "/api/v2/advisor?person=greg")[0] == 403        # not someone else's
    status, body = go("u-greg", "GET", "/api/v2/advisor?person=bere")          # the admin may read it
    assert status == 200 and body["can_approve"] is False


def test_a_decision_is_always_for_the_signed_in_person(api):
    go, calls = api
    assert go("u-greg", "POST", "/api/v2/advisor/abc123/send", {"model": "opus"})[0] == 200
    assert calls[-1][:3] == ("POST", "/approvals/greg/abc123/send", {"approver": "greg", "model": "opus"})
    go("u-bere", "POST", "/api/v2/advisor/abc123/decline", {})
    assert calls[-1][1] == "/approvals/bere/abc123/decline"                    # Béré can only act as Béré
    assert go("u-greg", "POST", "/api/v2/advisor/abc123/delete", {})[0] == 404


def test_budget_asks_are_your_own_and_changes_are_admin_only(api):
    go, calls = api
    assert go("u-bere", "POST", "/api/v2/advisor-budget/ask", {"reason": "honeymoon planning"})[0] == 200
    assert calls[-1][:3] == ("POST", "/approvals/bere/budget/ask", {"reason": "honeymoon planning"})
    assert go("u-bere", "POST", "/api/v2/advisor-budget/bere/raise", {"dollars": 50})[0] == 403   # not her own
    n = len(calls)
    assert go("u-greg", "POST", "/api/v2/advisor-budget/bere/raise", {"dollars": 10})[0] == 200
    assert calls[n][:3] == ("POST", "/approvals/bere/budget/raise", {"admin": "greg", "dollars": 10})
    assert go("u-greg", "POST", "/api/v2/advisor-budget/bere/delete", {})[0] == 404
