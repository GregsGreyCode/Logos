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

    async def list_sessions(self, limit=None, source=None):
        return {"data": [s for s in self.sessions if source is None or s["source"] == source]}

    async def get_session(self, sid):
        return next(s for s in self.sessions if s["id"] == sid)

    async def get_session_messages(self, sid, limit=None, order=None):
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
    {"id": "local-1", "source": "cron", "title": "consolidate", "message_count": 3, "last_active": 50},
    {"id": "local-2", "source": "cron", "title": "nothing said", "message_count": 0, "last_active": 55},
    {"id": "mx-greg", "source": "matrix", "user_id": "@greg:example.org", "title": "Bins", "message_count": 2, "last_active": 25},
    {"id": "mx-bere", "source": "matrix", "user_id": "@bere:example.org", "title": "Hers too", "message_count": 2, "last_active": 26},
    {"id": "web-u-greg-empty", "source": "api_server", "title": "Empty", "message_count": 0, "last_active": 60},
]
MESSAGES = [
    {"role": "user", "content": "Add milk", "timestamp": "1"},
    {"role": "assistant", "content": "", "reasoning": "The list is in the brain.", "timestamp": "2",
     "tool_calls": '[{"id": "c1", "type": "function", "function": {"name": "write_file", '
                   '"arguments": "{\\"path\\": \\"lists/shopping.md\\"}"}}]'},
    {"role": "tool", "content": "{\"ok\": true, \"key\": \"sk-abcdefghijklmnopqrstuvwxyz123456\"}",
     "tool_name": "write_file", "tool_call_id": "c1", "timestamp": "3"},
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
    assert body["agents"][0]["obsidian_vault"] == "brain-greg"
    assert [(a["name"], a["running"], a["shared"]) for a in body["agents"]] == [
        ("greg", True, False), ("household", False, True)]
    assert [a["name"] for a in run("u-bere", "/api/v2/agents")[1]["agents"]] == ["household"]


def test_someone_elses_agent_is_a_404(run):
    assert run("u-bere", "/api/v2/agents/greg/conversations")[0] == 404
    assert run("u-greg", "/api/v2/agents/bench/conversations")[0] == 404


def test_conversations_on_a_private_agent(run):
    convs = run("u-greg", "/api/v2/agents/greg/conversations")[1]["conversations"]
    assert [(c["id"], c["where"], c["chat_id"]) for c in convs] == [
        ("local-1", "scheduled", None), ("web-u-greg-abc", "web", "abc"), ("mx-bere", "matrix", None),
        ("mx-greg", "matrix", None), ("tg-1", "telegram", None)]
    assert convs[-1]["title"] == "From Telegram"


def test_shared_agent_shows_only_your_own_web_conversations(run):
    mine = run("u-greg", "/api/v2/agents/household/conversations")[1]["conversations"]
    assert [c["id"] for c in mine] == ["local-1", "web-u-greg-abc", "mx-greg"]
    hers = run("u-bere", "/api/v2/agents/household/conversations")[1]["conversations"]
    assert [c["id"] for c in hers] == ["local-1", "web-u-bere-zzz", "mx-bere"]
    assert run("u-bere", "/api/v2/agents/household/conversations/web-u-greg-abc")[0] == 404
    assert run("u-bere", "/api/v2/agents/household/conversations/mx-greg")[0] == 404
    assert run("u-greg", "/api/v2/agents/household/conversations/tg-1")[0] == 404
    assert run("u-bere", "/api/v2/agents/household/conversations/local-1")[0] == 200


def test_messages_fold_tool_calls_into_the_reply(run):
    status, body = run("u-greg", "/api/v2/agents/greg/conversations/web-u-greg-abc")
    assert status == 200
    assert [(m["role"], m["text"], m["tools"]) for m in body["messages"]] == [
        ("user", "Add milk", []), ("assistant", "Added.", ["write_file"])]
    assert body["where"] == "web"
    (step,) = body["messages"][1]["steps"]
    assert step["reasoning"] == "The list is in the brain."
    (call,) = step["calls"]
    assert call["name"] == "write_file" and "lists/shopping.md" in call["args"]
    assert '"ok": true' in call["result"]
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in call["result"]     # keys are masked


def test_long_tool_output_is_cut():
    msgs = [{"role": "assistant", "content": "", "timestamp": 1,
             "tool_calls": [{"id": "x", "function": {"name": "terminal", "arguments": {"command": "ls"}}}]},
            {"role": "tool", "content": "a" * 10000, "tool_call_id": "x", "tool_name": "terminal", "timestamp": 2},
            {"role": "assistant", "content": "Done", "timestamp": 3}]
    (turn,) = dashboard_api.fold_messages(msgs)
    call = turn["steps"][0]["calls"][0]
    assert call["args"] == '{"command": "ls"}'
    assert len(call["result"]) < 4100 and call["result"].endswith("more characters)")


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
    assert d["conflicts"] == []


def test_sync_conflict_copies_are_listed_apart():
    from gateway import brain_reader as b
    out = ("P\tprojects/Trip.md\t200\tTrip to Lisbon\n"
           "P\tprojects/Trip.sync-conflict-20261119-101500-7RIKTYS.md\t210\tTrip to Lisbon\n")
    d = b.parse_listing(out)
    assert [p["path"] for p in d["pages"]] == ["projects/Trip.md"]
    assert d["conflicts"] == [{"path": "projects/Trip.sync-conflict-20261119-101500-7RIKTYS.md",
                               "of": "projects/Trip.md", "modified": 210, "title": "Trip to Lisbon"}]
    assert "-not -path './.*/*'" in b._LIST                      # .trash, .stversions, .obsidian, .git


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
        ("birthday restaurant booking", False), ("Save brain history", True), ("Tidy the brain (nightly)", True)]
    assert jobs[0]["id"] == "ea3114aa4471" and jobs[0]["last_run"] is None
    assert jobs[1]["last_ok"] is True and jobs[1]["last_run"].startswith("2026-11-16T14:00")
    assert jobs[2]["last_ok"] is False


def test_every_job_logos_installs_has_a_label_and_cannot_be_cancelled():
    from gateway import agent_runtime
    assert dashboard_api.MANAGED == set(agent_runtime.MANAGED_JOBS)       # in step with the runtime
    retired = {"changes-homelab", "changes-logos", "changes-homelab-week", "changes-logos-week"}
    for name in set(agent_runtime.MANAGED_JOBS) - retired:
        label = dashboard_api.JOB_LABELS[name]
        assert label and label != name and "-" not in label.split()[0]      # words, not a job id


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
    assert body["model"] == {"name": "m", "local": False, "via_gateway": False, "paid_providers": [], "up": None}
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


def test_answering_now_prefers_local_then_the_first_fallback_with_money_left():
    from gateway.dashboard_api import answering_now
    chain = [{"provider": "anthropic", "model": "claude-haiku-5-5", "spent_out": True},
             {"provider": "openrouter", "model": "google/gemini", "spent_out": False}]
    assert answering_now({"fallbacks": chain}, True) == {"kind": "local", "label": "Local model"}
    assert answering_now({"fallbacks": chain}, False) == {"kind": "paid", "label": "openrouter · google/gemini"}
    assert answering_now({"fallbacks": chain[:1]}, False)["kind"] == "none"
    assert "no paid fallback" in answering_now({"fallbacks": []}, False)["label"]


def test_owes_a_reply():
    f = dashboard_api.owes_a_reply
    assert f({"role": "user", "content": "hi"}) and f({"role": "tool", "content": "{}"})
    assert f({"role": "assistant", "content": "", "tool_calls": "[{}]"})
    assert not f({"role": "assistant", "content": "Done."}) and not f(None)


def test_agent_is_working_looks_at_the_newest_conversation():
    class C:
        def __init__(self, sessions, last):
            self.sessions, self.last, self.asked = sessions, last, None

        async def list_sessions(self, limit=None, source=None):
            return {"data": self.sessions}

        async def get_session_messages(self, sid, limit=None, order=None):
            self.asked = (sid, limit, order)
            return {"data": [self.last]}

    now = 10_000.0
    c = C([{"id": "old", "last_active": now - 50}, {"id": "new", "last_active": now - 5}], {"role": "user"})
    assert asyncio.run(dashboard_api.agent_is_working(c, now)) and c.asked == ("new", 1, "latest")
    c = C([{"id": "new", "last_active": now - 5}], {"role": "assistant", "content": "Here you go"})
    assert not asyncio.run(dashboard_api.agent_is_working(c, now))
    c = C([{"id": "stale", "last_active": now - 3600}], {"role": "user"})       # unanswered for an hour
    assert not asyncio.run(dashboard_api.agent_is_working(c, now)) and c.asked is None
    assert not asyncio.run(dashboard_api.agent_is_working(C([], None), now))


def test_activity_lists_only_your_running_agents(run):
    status, body = run("u-greg", "/api/v2/activity")
    assert status == 200 and set(body) == {"working"}
    assert set(body["working"]) <= {"greg"}          # household is not running in this fake
