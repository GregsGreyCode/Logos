"""v2 dashboard endpoints: who sees which agent and which conversations."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import dashboard_api
from gateway.sandbox_backend import SandboxStatus

USERS = {"u-greg": {"username": "greg", "display_name": "Greg"}, "u-bere": {"username": "bere", "display_name": "Béré"}}
DEFS = {
    "greg": {"display_name": "Greg's agent", "owners": ["greg"], "model": {"default": "m"}},
    "household": {"display_name": "Household agent", "owners": ["greg", "bere"], "model": {"default": "m"}},
    "bench": {"dashboard": False, "model": {"default": "m"}},
}


class Runtime:
    def defined(self):
        return sorted(DEFS)

    def load(self, name):
        return DEFS[name]


class Backend:
    runtime = Runtime()

    def list(self):
        return [SandboxStatus(name="greg", state="running")]


class Client:
    def __init__(self, sessions, messages):
        self.sessions, self.messages = sessions, messages

    async def list_sessions(self, limit=None):
        return {"data": self.sessions}

    async def get_session(self, sid):
        return next(s for s in self.sessions if s["id"] == sid)

    async def get_session_messages(self, sid):
        return {"data": self.messages}


class Links:
    def __init__(self, client):
        self._client = client

    async def client(self, name):
        return self._client


class FakeDb:
    def get_user_by_id(self, uid):
        return USERS.get(uid)

    def get_agent_by_name(self, name):
        return {"id": f"agent_{name}", "name": name, "model": "m", "description": DEFS[name].get("display_name", "")}

    def create_agent(self, *a, **k):
        pass

    def update_agent(self, *a, **k):
        pass


SESSIONS = [
    {"id": "web-u-greg-abc", "source": "api_server", "title": "Dinner plans", "message_count": 4, "last_active": 30},
    {"id": "web-u-bere-zzz", "source": "api_server", "title": "Hers", "message_count": 2, "last_active": 40},
    {"id": "tg-1", "source": "telegram", "title": None, "preview": "From Telegram", "message_count": 6, "last_active": 20},
    {"id": "local-1", "source": "cron", "title": "job", "message_count": 3, "last_active": 50},
    {"id": "web-u-greg-empty", "source": "api_server", "title": "Empty", "message_count": 0, "last_active": 60},
]
MESSAGES = [
    {"role": "user", "content": "Add milk", "timestamp": "1"},
    {"role": "assistant", "content": "", "tool_calls": "[...]", "timestamp": "2"},
    {"role": "tool", "content": "{}", "tool_name": "write_file", "timestamp": "3"},
    {"role": "assistant", "content": "Added.", "timestamp": "4"},
]


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(dashboard_api, "auth_db", FakeDb())
    monkeypatch.setattr(dashboard_api.agent_chat, "sync_definitions",
                        lambda rt, db: {n: dashboard_api.agent_chat.owners_of(d) for n, d in DEFS.items()
                                        if d.get("dashboard") is not False})

    def go(user_id, path):
        @web.middleware
        async def as_user(request, handler):
            request["current_user"] = {"sub": user_id, "role": "user"}
            return await handler(request)

        async def flow():
            app = web.Application(middlewares=[as_user])
            app["sandbox_backend"] = Backend()
            app["agent_links"] = Links(Client(SESSIONS, MESSAGES))
            dashboard_api.register(app)
            async with TestClient(TestServer(app)) as client:
                r = await client.get(path)
                return r.status, (await r.json() if r.content_type == "application/json" else None)
        return asyncio.run(flow())
    return go


def test_me_and_agents_are_per_user(run):
    assert run("u-greg", "/api/v2/me")[1]["username"] == "greg"
    status, body = run("u-greg", "/api/v2/agents")
    assert status == 200
    assert [(a["name"], a["running"], a["shared"]) for a in body["agents"]] == [
        ("greg", True, False), ("household", False, True)]
    assert [a["name"] for a in run("u-bere", "/api/v2/agents")[1]["agents"]] == ["household"]


def test_someone_elses_agent_is_a_404(run):
    assert run("u-bere", "/api/v2/agents/greg/conversations")[0] == 404
    assert run("u-greg", "/api/v2/agents/bench/conversations")[0] == 404


def test_conversations_on_a_private_agent(run):
    convs = run("u-greg", "/api/v2/agents/greg/conversations")[1]["conversations"]
    assert [(c["id"], c["where"], c["chat_id"]) for c in convs] == [
        ("web-u-greg-abc", "web", "abc"), ("tg-1", "telegram", None)]
    assert convs[1]["title"] == "From Telegram"


def test_shared_agent_shows_only_your_own_web_conversations(run):
    mine = run("u-greg", "/api/v2/agents/household/conversations")[1]["conversations"]
    assert [c["id"] for c in mine] == ["web-u-greg-abc"]
    hers = run("u-bere", "/api/v2/agents/household/conversations")[1]["conversations"]
    assert [c["id"] for c in hers] == ["web-u-bere-zzz"]
    assert run("u-bere", "/api/v2/agents/household/conversations/web-u-greg-abc")[0] == 404
    assert run("u-greg", "/api/v2/agents/household/conversations/tg-1")[0] == 404


def test_messages_fold_tool_calls_into_the_reply(run):
    status, body = run("u-greg", "/api/v2/agents/greg/conversations/web-u-greg-abc")
    assert status == 200
    assert body["messages"] == [
        {"role": "user", "text": "Add milk", "tools": []},
        {"role": "assistant", "text": "Added.", "tools": ["write_file"]}]


def test_conversation_prefix():
    assert dashboard_api.conversation_prefix("usr_1") == "web-usr_1-"
