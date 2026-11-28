"""The Lists screen (Greg, 28 Nov): gateway/home.py lists helpers and the /api/v2/lists endpoints."""
import asyncio
import json

import pytest

from gateway import home

LISTS = {"enabled": True, "domains": ["light", "todo"]}
AGENTS = {"household": {"owners": ["greg", "bere"], "definition": {"role": "household", "owners": ["greg", "bere"], "house": LISTS}},
          "greg": {"owners": ["greg"], "definition": {"owners": ["greg"], "house": LISTS}},
          "bere": {"owners": ["bere"], "definition": {"owners": ["bere"], "house": {"enabled": True}}}}


def tool(answer):
    return {"result": {"isError": False, "content": [{"type": "text", "text": json.dumps(answer)}]}}


def test_your_own_agent_gives_your_personal_lists_else_only_the_shared_ones():
    assert home.lists_agent(AGENTS, "greg") == ("greg", True)
    # Béré's agent has no lists (no todo domain): the household agent, shared lists only.
    assert home.lists_agent(AGENTS, "bere") == ("household", False)
    assert home.lists_agent({"bere": AGENTS["bere"]}, "bere") == (None, False)
    # An agent shared by two people is never "your own", whatever its definition says.
    shared = {"x": {"owners": ["greg", "bere"], "definition": {"owners": ["greg"], "house": LISTS}}}
    assert home.lists_agent(shared, "greg") == ("x", False)


def test_list_changes_are_checked_before_anything_is_sent():
    assert home.list_args("add", {"list": "Games", "item": " Zelda ", "kind": "personal"}, True) == {
        "list": "Games", "items": ["Zelda"], "shared": False}
    assert home.list_args("tick", {"list": "Shopping", "item": "Milk", "kind": "shared"}, False) == {
        "list": "Shopping", "items": ["Milk"]}
    assert home.list_args("change", {"list": "Games", "item": "Zelda", "new_title": "Zelda TOTK"}, True) == {
        "list": "Games", "item": "Zelda", "new_title": "Zelda TOTK"}
    assert home.list_args("create", {"name": "Games"}, True) == {"name": "Games"}
    assert home.list_args("create", {"name": "Garden", "kind": "shared"}, True) == {"name": "Garden", "shared": True}
    assert home.list_args("create", {"name": "Garden"}, False) == {"name": "Garden", "shared": True}
    assert home.list_args("move", {"name": "Games", "to": "shared", "kind": "personal"}, True) == {"name": "Games", "to": "shared"}
    assert home.list_args("delete", {"name": "Games", "kind": "personal"}, True) == {"name": "Games"}
    for action, body, personal in (("explode", {}, True), ("add", {"list": "Games"}, True), ("add", {"item": "x"}, True),
                                   ("move", {"name": "Games", "to": "elsewhere"}, True), ("change", {"list": "a", "item": "b"}, True),
                                   ("add", {"list": "Games", "item": "x", "kind": "personal"}, False),
                                   ("add", {"list": "Games", "item": "x", "kind": "secret"}, True), ("create", {}, True)):
        with pytest.raises(ValueError):
            home.list_args(action, body, personal)


def test_all_lists_and_changes_use_the_agents_house_key(monkeypatch):
    from gateway import house_host

    class Host:
        def __init__(self, rt): pass
        def token_for(self, name): return "gtok" if name == "greg" else ""
        def url(self): return "http://house/mcp"
    monkeypatch.setattr(house_host, "HouseHost", Host)
    calls = []

    def post(url, body, token):
        req = json.loads(body)
        calls.append((token, req["params"]["name"], req["params"]["arguments"]))
        if req["params"]["name"] == "list_items":
            return tool({"lists": [
                {"list": "Shopping", "kind": "shared", "id": "todo.example_list", "open": ["Milk"], "done": ["Eggs"]},
                {"list": "Games", "kind": "personal", "open": [{"item": "Zelda", "details": "TOTK"}], "done": []}]})
        if req["params"]["arguments"].get("items") == ["nope"]:
            return {"result": {"isError": True, "content": [{"type": "text", "text": "Not done: 'nope' is not on the list."}]}}
        return tool({"list": "Games", "added": ["x"]})
    assert home.all_lists(object(), "greg", post=post) == {"lists": [
        {"name": "Shopping", "kind": "shared", "id": "todo.example_list", "items": [
            {"text": "Milk", "due": "", "details": "", "done": False}, {"text": "Eggs", "due": "", "details": "", "done": True}]},
        {"name": "Games", "kind": "personal", "items": [{"text": "Zelda", "due": "", "details": "TOTK", "done": False}]}]}
    assert calls[-1] == ("gtok", "list_items", {"done": True})
    assert home.list_change(object(), "greg", "untick", {"list": "Games", "items": ["x"]}, post=post) == {"list": "Games", "added": ["x"]}
    assert calls[-1][1] == "add_items"                       # put back = added again
    home.list_change(object(), "greg", "move", {"name": "Games", "to": "shared"}, post=post)
    assert calls[-1] == ("gtok", "move_list", {"name": "Games", "to": "shared"})
    assert home.list_change(object(), "greg", "tick", {"list": "Games", "items": ["nope"]}, post=post) == {
        "error": "refused", "message": "'nope' is not on the list."}
    assert home.all_lists(object(), "household", post=post) == {"error": "no_house"}


def test_lists_endpoints(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway import dashboard_api as d
    monkeypatch.setattr(d, "_user", lambda r: {"id": "u1", "username": "greg", "display_name": "Greg", "role": "user"})
    agents = dict(AGENTS)

    async def visible(request, username):
        return agents
    monkeypatch.setattr(d, "_visible_agents", visible)
    monkeypatch.setattr(d.agent_chat, "runtime_of", lambda backend: object())
    done, audit = [], []
    monkeypatch.setattr(home, "all_lists", lambda rt, agent: {"lists": [{"name": "Games", "kind": "personal", "items": []}]})
    monkeypatch.setattr(home, "list_change", lambda rt, agent, action, args: done.append((agent, action, args)) or {"ok": True})
    monkeypatch.setattr(d.auth_db, "write_audit_log", lambda *a, **k: audit.append((a, k)))

    async def run():
        app = web.Application()
        app.router.add_get("/api/v2/lists", d.handle_lists)
        app.router.add_post("/api/v2/lists/{action}", d.handle_lists_change)
        async with TestClient(TestServer(app)) as c:
            got = await (await c.get("/api/v2/lists")).json()
            bad = await c.post("/api/v2/lists/add", json={"list": "Games"})
            good = await c.post("/api/v2/lists/move", json={"name": "Games", "to": "shared", "kind": "personal"})
            agents.clear()
            none = await c.get("/api/v2/lists")
            return got, bad.status, good.status, await none.json()
    got, s_bad, s_good, none = asyncio.run(run())
    assert got == {"lists": [{"name": "Games", "kind": "personal", "items": []}], "personal": True}
    assert (s_bad, s_good) == (400, 200)
    assert done == [("greg", "move", {"name": "Games", "to": "shared"})] and audit[0][0][1] == "list_move"
    assert none["error"] == "no_lists"
