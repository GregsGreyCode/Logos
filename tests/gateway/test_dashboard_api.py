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


# ── noticeboard ──────────────────────────────────────────────────────────────

class Board:
    def __init__(self):
        self.answers = []
        self.items = {
            "greg": [
                {"id": 5, "ts": 50, "from": "bere", "to": "greg", "from_name": "Béré's agent", "to_name": "Greg's agent",
                 "kind": "question", "text": "Free Saturday?", "status": "open", "urgent": False, "answer": None},
                {"id": 4, "ts": 40, "from": "household", "to": "greg", "from_name": "Household agent",
                 "to_name": "Greg's agent", "kind": "reminder", "text": "Bins Thursday 19:00", "status": "open",
                 "urgent": False, "answer": None},
                {"id": 3, "ts": 30, "from": "greg", "to": "bere", "from_name": "Greg's agent", "to_name": "Béré's agent",
                 "kind": "question", "text": "Boiler evening?", "status": "open", "urgent": False, "answer": None},
                {"id": 2, "ts": 20, "from": "bere", "to": "greg", "from_name": "Béré's agent", "to_name": "Greg's agent",
                 "kind": "answer", "text": "x", "status": "delivered", "urgent": False},
            ],
            "household": [
                {"id": 4, "ts": 40, "from": "household", "to": "greg", "from_name": "Household agent",
                 "to_name": "Greg's agent", "kind": "reminder", "text": "Bins Thursday 19:00", "status": "open",
                 "urgent": False, "answer": None},
                {"id": 6, "ts": 60, "from": "bere", "to": "household", "from_name": "Béré's agent",
                 "to_name": "Household agent", "kind": "shared_change", "text": "Add candles", "status": "open",
                 "urgent": False, "answer": None},
            ],
        }

    def uses(self, definition):
        return True

    async def history(self, agent):
        return [dict(m) for m in self.items[agent]]

    async def answer(self, agent, to, reply_to, decision, text):
        self.answers.append((agent, to, reply_to, decision, text))
        return {"ok": True}


class RunClient:
    def __init__(self):
        self.runs = []

    async def start_run(self, prompt, session_id=None):
        self.runs.append((prompt, session_id))
        return "run_1"


@pytest.fixture
def nb(monkeypatch):
    class Db(FakeDb):
        def write_audit_log(self, *a, **k):
            pass
    monkeypatch.setattr(dashboard_api, "auth_db", Db())
    monkeypatch.setattr(dashboard_api.agent_chat, "sync_definitions",
                        lambda rt, db: {n: dashboard_api.agent_chat.owners_of(d) for n, d in DEFS.items()
                                        if d.get("dashboard") is not False})
    board, runs = Board(), RunClient()

    def go(user_id, method, path, body=None):
        @web.middleware
        async def as_user(request, handler):
            request["current_user"] = {"sub": user_id, "role": "user"}
            return await handler(request)

        async def flow():
            app = web.Application(middlewares=[as_user])
            app["sandbox_backend"] = Backend()
            app["agent_links"] = Links(runs)
            app["noticeboard_client"] = board
            dashboard_api.register(app)
            async with TestClient(TestServer(app)) as client:
                client.session.cookie_jar.update_cookies({"csrf_token": "t"})
                r = await client.request(method, path, json=body, headers={"X-CSRF-Token": "t"})
                return r.status, await r.json()
        return asyncio.run(flow())
    return go, board, runs


def test_noticeboard_lists_what_waits_on_each_person(nb):
    go, _board, _runs = nb
    status, body = go("u-greg", "GET", "/api/v2/noticeboard")
    assert status == 200
    assert [m["id"] for m in body["waiting"]] == [6, 5, 4]              # his agent's and the household's
    assert [m["id"] for m in body["log"]] == [6, 5, 4, 3]               # answers are folded away, no duplicates
    hers = go("u-bere", "GET", "/api/v2/noticeboard")[1]
    assert [m["id"] for m in hers["waiting"]] == [6]                    # only the shared agent's request
    assert all(m["id"] != 5 for m in hers["log"])                       # nothing addressed to Greg's agent


def test_answering_a_question_and_declining_go_straight_to_the_noticeboard(nb):
    go, board, runs = nb
    assert go("u-greg", "POST", "/api/v2/noticeboard/5/answer", {"decision": "accepted", "text": ""})[0] == 400
    assert go("u-greg", "POST", "/api/v2/noticeboard/5/answer", {"decision": "accepted", "text": "Yes,  after 2"})[1] == {
        "ok": True, "delegated": False}
    assert go("u-greg", "POST", "/api/v2/noticeboard/4/answer", {"decision": "declined"})[1]["delegated"] is False
    assert board.answers == [("greg", "bere", 5, "accepted", "Yes, after 2"),
                             ("greg", "household", 4, "declined", "Greg declined.")]
    assert runs.runs == []


def test_accepting_a_reminder_or_shared_change_asks_the_agent_to_act(nb):
    go, board, runs = nb
    assert go("u-greg", "POST", "/api/v2/noticeboard/4/answer", {"decision": "accepted"})[1] == {
        "ok": True, "delegated": True}
    assert go("u-bere", "POST", "/api/v2/noticeboard/6/answer", {"decision": "accepted"})[1]["delegated"] is True
    assert board.answers == []
    (p1, s1), (p2, s2) = runs.runs
    assert "request 4" in p1 and "Bins Thursday 19:00" in p1 and "scheduled job" in p1 and "Greg has just accepted" in p1
    assert "request 6" in p2 and "shared brain" in p2 and s2 == "web-u-bere-noticeboard-6"


def test_you_cannot_answer_for_an_agent_that_is_not_yours(nb):
    go, board, _runs = nb
    assert go("u-bere", "POST", "/api/v2/noticeboard/5/answer", {"decision": "declined"})[0] == 404
    assert go("u-greg", "POST", "/api/v2/noticeboard/3/answer", {"decision": "declined"})[0] == 404   # he asked it
    assert go("u-greg", "POST", "/api/v2/noticeboard/5/answer", {"decision": "maybe"})[0] == 400
    assert board.answers == []


# ── brain ────────────────────────────────────────────────────────────────────

def test_brain_paths_cannot_escape_the_vault():
    from gateway import brain_reader as b
    assert b.safe_path("projects/Wedding speech.md") == "projects/Wedding speech.md"
    for bad in ("", "../x.md", "/etc/passwd.md", ".git/config.md", "a/../../b.md", "a//b.md", "x.txt",
                ".obsidian/app.md", "a/.hidden/b.md", "x" * 400 + ".md"):
        with pytest.raises(b.BrainError):
            b.safe_path(bad)
    assert b.new_page_path("projects", "  Garden   plan ") == "projects/Garden plan.md"
    assert b.new_page_path("topics", 'A/B: "notes"?') == "topics/AB notes.md"
    for folder, title in (("daily", "x"), ("..", "x"), ("projects", ""), ("projects", "///"), ("projects", "..")):
        with pytest.raises(b.BrainError):
            b.new_page_path(folder, title)


def test_brain_listing_is_parsed():
    from gateway import brain_reader as b
    out = ("P\tprojects/Trip.md\t200\tTrip to Lisbon\nP\tREADME.md\t100\tBrain\nP\tdaily/2026-11-15.md\t150\t\n"
           "P\tdaily/2026-11-16.md\t190\t\nC\t190\ndaily/2026-11-16.md\nprojects/Trip.md\nC\t150\nprojects/Trip.md\ngone.md\n")
    d = b.parse_listing(out)
    assert [(p["folder"], p["title"]) for p in d["pages"]] == [
        ("", "Brain"), ("daily", "2026-11-16"), ("daily", "2026-11-15"), ("projects", "Trip to Lisbon")]
    assert d["changes"] == [{"path": "daily/2026-11-16.md", "when": 190, "title": "2026-11-16"},
                            {"path": "projects/Trip.md", "when": 190, "title": "Trip to Lisbon"}]


class BrainRunner:
    def __init__(self):
        self.calls, self.rc, self.out = [], 0, "123\n"

    def run(self, argv, *, input=None, text=True):
        self.calls.append((list(argv), input))
        class R:
            pass
        r = R()
        r.returncode = 0 if argv[:3] == ["docker", "volume", "inspect"] else self.rc
        r.stdout, r.stderr = self.out, ""
        return r


class BrainRuntime:
    def __init__(self):
        self.runner = BrainRunner()


def test_brain_write_is_guarded():
    from gateway import brain_reader as b
    rt = BrainRuntime()
    assert b.write_page(rt, "greg", "projects/Trip.md", "# Trip", 100) == {"path": "projects/Trip.md", "modified": 123}
    argv, sent = rt.runner.calls[-1]
    assert "logos-brain-greg:/v" in argv and "--network" in argv and "P=projects/Trip.md" in argv and "EXPECT=100" in argv
    assert sent == "# Trip\n"                                   # the path is never part of the shell text
    assert "projects/Trip.md" not in argv[-1]
    rt.runner.rc = 3
    with pytest.raises(b.PageChanged):
        b.write_page(rt, "greg", "projects/Trip.md", "x", 100)
    with pytest.raises(b.BrainError):
        b.write_page(rt, "greg", "../outside.md", "x", 100)
    with pytest.raises(b.BrainError):
        b.write_page(rt, "greg", "projects/Trip.md", "x" * (b.MAX_PAGE_BYTES + 1), 100)
    rt.runner.rc = 5
    with pytest.raises(b.BrainError, match="already exists"):
        b.create_page(rt, "greg", "projects", "Trip")


def test_reading_someone_elses_brain_is_a_404(run):
    assert run("u-bere", "/api/v2/agents/greg/brain")[0] == 404
    assert run("u-bere", "/api/v2/agents/greg/brain/page?path=README.md")[0] == 404


def test_reformat_result_must_keep_the_content():
    ok = dashboard_api.reformat_is_sane
    before = "boiler service 20 November british gas\nbooked 9am cost 85"
    assert ok(before, "## Boiler service\n\n- **Date:** 20 November\n- British Gas, booked 9am\n- Cost: 85")
    assert not ok(before, "")                                              # nothing came back
    assert not ok(before, "## Boiler\n\n- 21 November, 9am, cost 85")       # a number changed
    assert not ok(before, "Boiler booked.")                                # most of it dropped
    assert not ok(before, before + "\n" + "Extra invented detail. " * 10)  # padded


# ── agent status and people ──────────────────────────────────────────────────

CRON = """
  ea3114aa4471 [active]
    Name:      birthday restaurant booking
    Schedule:  once at 2026-11-27 09:00
    Next run:  2026-11-27T09:00:00+00:00

  9339ebcb8dea [active]
    Name:      brain-commit
    Schedule:  0 * * * *
    Next run:  2026-11-16T15:00:00+00:00
    Last run:  2026-11-16T14:00:20.974284+00:00  ok

  aa11bb22cc33 [active]
    Name:      consolidate
    Schedule:  30 2 * * *
    Last run:  2026-11-16T02:33:01+00:00  error
"""


def test_parse_jobs():
    jobs = dashboard_api.parse_jobs(CRON)
    assert [(j["label"], j["managed"]) for j in jobs] == [
        ("birthday restaurant booking", False), ("Save brain history", True), ("Tidy the brain", True)]
    assert jobs[0]["id"] == "ea3114aa4471" and jobs[0]["last_run"] is None
    assert jobs[1]["last_ok"] is True and jobs[1]["last_run"].startswith("2026-11-16T14:00")
    assert jobs[2]["last_ok"] is False


class StatusRuntime(Runtime):
    env = {"LOGOS_MODELS_ENV": "/nonexistent/models.env"}

    def __init__(self):
        self.scripts = []

    def sbx(self, name, script, input=None):
        self.scripts.append((name, script))
        return CRON


@pytest.fixture
def status(monkeypatch):
    class Db(FakeDb):
        def write_audit_log(self, *a, **k):
            pass

        def list_users(self, page=1, limit=20):
            return [{"id": "u-greg", "username": "greg", "display_name": "Greg", "role": "admin", "last_login": 5},
                    {"id": "u-bere", "username": "bere", "display_name": "Béré", "role": "user", "last_login": None}], 2
    monkeypatch.setattr(dashboard_api, "auth_db", Db())
    monkeypatch.setattr(dashboard_api.agent_chat, "sync_definitions",
                        lambda rt, db: {n: dashboard_api.agent_chat.owners_of(d) for n, d in DEFS.items()
                                        if d.get("dashboard") is not False})
    runtime = StatusRuntime()

    class B(Backend):
        pass
    B.runtime = runtime

    def go(user_id, method, path, role="user"):
        @web.middleware
        async def as_user(request, handler):
            request["current_user"] = {"sub": user_id, "role": role}
            return await handler(request)

        async def flow():
            app = web.Application(middlewares=[as_user])
            app["sandbox_backend"] = B()
            dashboard_api.register(app)
            async with TestClient(TestServer(app)) as client:
                client.session.cookie_jar.update_cookies({"csrf_token": "t"})
                r = await client.request(method, path, headers={"X-CSRF-Token": "t"})
                return r.status, await r.json()
        return asyncio.run(flow())
    return go, runtime


def test_agent_status(status):
    go, _rt = status
    code, body = go("u-greg", "GET", "/api/v2/agents/greg/status")
    assert code == 200 and len(body["jobs"]) == 3
    assert body["model"] == {"name": "m", "local": False, "via_gateway": False, "paid_providers": []}
    assert body["budget"] == {"monthly_usd": None}
    assert body["reach"]["allowed"] == ["Its model server"] and "Your home network" in body["reach"]["blocked"]
    assert go("u-bere", "GET", "/api/v2/agents/greg/status")[0] == 404


def test_only_the_persons_own_reminders_can_be_cancelled(status):
    go, rt = status
    assert go("u-greg", "POST", "/api/v2/agents/greg/jobs/9339ebcb8dea/cancel")[0] == 400       # a managed job
    assert go("u-greg", "POST", "/api/v2/agents/greg/jobs/ffffffffffff/cancel")[0] == 404
    assert go("u-greg", "POST", "/api/v2/agents/greg/jobs/ea31;rm%20-rf/cancel")[0] == 400       # not an id
    assert go("u-bere", "POST", "/api/v2/agents/greg/jobs/ea3114aa4471/cancel")[0] == 404       # not her agent
    assert not any("remove" in s for _n, s in rt.scripts)
    assert go("u-greg", "POST", "/api/v2/agents/greg/jobs/ea3114aa4471/cancel")[1] == {"ok": True}
    assert rt.scripts[-1] == ("greg", "export HERMES_HOME=/opt/data; hermes cron remove ea3114aa4471")


def test_people_shows_who_can_use_what(status):
    go, _rt = status
    code, body = go("u-greg", "GET", "/api/v2/people", role="admin")
    assert code == 200 and body["is_admin"] is True
    assert [a["name"] for a in body["agents"]] == ["greg", "household"]
    assert [(p["username"], p["is_you"], p["agents"]) for p in body["people"]] == [
        ("greg", True, {"greg": True, "household": True}), ("bere", False, {"greg": False, "household": True})]
    assert body["people"][1]["signed_in_before"] is False
    assert go("u-bere", "GET", "/api/v2/people")[1]["is_admin"] is False


def test_agents_you_cannot_use_are_listed_by_name_only(run):
    body = run("u-bere", "/api/v2/agents")[1]
    assert [a["name"] for a in body["agents"]] == ["household"]
    assert body["others"] == [{"name": "greg", "display_name": "Greg's agent", "running": True}]


def test_audio_suffix_accepts_browser_recordings_only():
    from gateway.dashboard_api import audio_suffix
    assert audio_suffix("audio/webm;codecs=opus") == ".webm"
    assert audio_suffix("audio/mp4") == ".m4a"
    assert audio_suffix("Audio/OGG; codecs=opus") == ".ogg"
    assert audio_suffix("application/json") is None
    assert audio_suffix("") is None
