"""Agents renaming themselves: gateway/agent_rename.py, the logos-rename plugin, and the shared name checks."""
import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import agent_rename as rn
from gateway import agent_settings as S
from gateway import agent_settings_api as api
from gateway import matrix_host as mh
from gateway import speaker_say, wake_host
from tests.unit.test_agent_runtime import FakeRunner, repo, rt  # noqa: F401  (repo is a fixture)

REPO = Path(__file__).resolve().parents[2]
KEY = b"k" * 32
ALPHA = """\
name: alpha
display_name: Alpha agent
owners: [greg]
image: logos-agent:0.23.1
soul: souls/general/soul.md
policy: infra/agent-image/policy.yaml
providers: []
timezone: Europe/London
speaker_voice: en_GB-alan-medium
speaker_name: Finn
matrix: {enabled: true}
model: {provider: lmstudio, base_url: http://10.0.0.5:1234/v1, default: some/model, context_length: 65536}
"""
BETA = """\
name: beta
display_name: Household agent
owners: [greg, bere]
image: logos-agent:0.23.1
soul: souls/general/soul.md
policy: infra/agent-image/policy.yaml
providers: []
speaker_voice: en_GB-alan-medium
speaker_name: Home
model: {provider: lmstudio, base_url: http://10.0.0.5:1234/v1, default: some/model, context_length: 65536}
"""
PEOPLE = {"greg": "Greg", "Greg": "Greg", "bere": "Béré", "Béré": "Béré"}


class Runner(FakeRunner):
    """FakeRunner whose volumes keep what is written to them (put), so get() reads it back."""

    def run(self, argv, *, input=None, text=True):
        argv = list(argv)
        if argv[:2] == ["docker", "run"] and "-i" in argv and "cat > /v/" in argv[-1]:
            vol = argv[argv.index("-v") + 1].split(":")[0]
            rel = argv[-1].split("cat > /v/")[1].split(" ")[0]
            self.files[(vol, rel)] = input
            self.calls.append(argv)
            return type(super().run(["docker", "network", "inspect"]))(0, "", "")
        return super().run(argv, input=input, text=text)


@pytest.fixture
def env(repo):
    (repo / "agents" / "alpha.yaml").write_text(ALPHA)
    (repo / "agents" / "beta.yaml").write_text(BETA)
    (repo / ".local/state/logos/wake").mkdir(parents=True)          # the wake words are set up
    return {"HOME": str(repo), "XDG_CONFIG_HOME": str(repo / ".test-config")}


@pytest.fixture
def setup(repo, env, monkeypatch):
    runner = Runner()
    runner.files[("logos-home-alpha", ".env")] = "MATRIX_USER_ID=@alpha-agent:example.org\nMATRIX_ACCESS_TOKEN=tok\n"
    runner.files[("logos-home-alpha", "SOUL.md")] = ("You are Alpha.\n\n" + rn.name_section(rt(repo, runner, env=env).load("alpha"))
                                                     + "\nShared brain rules.\n")
    r = rt(repo, runner, env=env)
    named = []
    monkeypatch.setattr(mh.MatrixHost, "set_display_name", lambda self, uid, token, name: named.append((uid, token, name)) or True)
    monkeypatch.setattr(api, "people_names", lambda env=None: dict(PEOPLE))
    from gateway import alert_text, voice_id
    monkeypatch.setattr(alert_text, "key", lambda env=None: KEY)
    monkeypatch.setattr(voice_id, "display_name", lambda u: {"greg": "Greg", "bere": "Béré"}.get(u, u))
    audits = []
    import gateway.auth.db as auth_db
    monkeypatch.setattr(auth_db, "write_audit_log", lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(auth_db, "get_user_by_username", lambda u: {"display_name": u.capitalize()})
    from gateway import agent_chat
    monkeypatch.setattr(agent_chat, "sync_definitions", lambda runtime, db: {"alpha": ["greg"], "beta": ["greg", "bere"]})
    return r, runner, named, audits


def names_json(repo):
    return json.loads((repo / ".local/state/logos/wake/names.json").read_text())["phrases"]


# ── the token ────────────────────────────────────────────────────────────────────────────────

def test_token_is_the_agents_own_and_not_the_speakers(monkeypatch):
    from gateway import alert_text
    monkeypatch.setattr(alert_text, "key", lambda env=None: KEY)
    t = rn.token_for("alpha")
    assert rn.valid("alpha", t) and not rn.valid("beta", t) and not rn.valid("alpha", "")
    assert t != speaker_say.token_for("alpha", key=KEY) and t == rn.token_for("alpha", key=KEY)


def test_only_agents_with_people_to_confirm_get_it():
    assert rn.uses_rename({"owners": ["greg"]})
    assert not rn.uses_rename({"owners": []}) and not rn.uses_rename({})
    assert not rn.uses_rename({"owners": ["greg"], "rename": False})
    assert not rn.uses_rename({"owners": ["greg"], "sandbox": False})


# ── the checks (shared with Agents › Settings) ───────────────────────────────────────────────

def test_names_of_other_agents_people_and_wake_words_are_refused(setup):
    r, *_ = setup
    for bad, why in (("Household agent", "Household agent goes by it"), ("home", "Household agent goes by it"),
                     ("Béré", "Béré goes by it"), ("bere", "Béré goes by it"), ("greg", "Greg goes by it"),
                     ("x" * 41, "at most 40"), ("", "cannot be empty"), ("<b>Nova</b>", "ordinary punctuation")):
        out, req = rn.ask(r, "alpha", bad, people=PEOPLE)
        assert out["asked"] is False and req is None and why in out["tell"], (bad, out)
    out, _ = rn.ask(r, "alpha", "Nova", wake_name="Home", people=PEOPLE)
    assert not out["asked"] and "already wakes Household agent" in out["tell"]
    out, _ = rn.ask(r, "alpha", "Nova", wake_name="R2D2", people=PEOPLE)
    assert not out["asked"] and "one or two words" in out["tell"]
    out, _ = rn.ask(r, "alpha", "Alpha agent", people=PEOPLE)
    assert out == {"asked": False, "reason": "same", "tell": rn.SAME_TELL}
    assert rn.pending(r.env) == []


def test_person_path_refuses_the_same_clashes(setup, repo, env):
    r, *_ = setup
    plain, agent = r.load("alpha", settings=False), r.load("alpha")
    new = {}
    with pytest.raises(S.Invalid, match="Béré goes by it"):
        api.set_names(r, "alpha", plain, agent, {}, new, {"display_name": "Béré"}, PEOPLE)
    with pytest.raises(S.Invalid, match="Hey Greg"):
        api.set_names(r, "alpha", plain, agent, {}, new, {"speaker_name": "Greg"}, PEOPLE)
    assert new == {}                                                   # nothing kept on a refusal
    assert api.set_names(r, "alpha", plain, agent, {}, new, {"display_name": "Nova", "speaker_name": "Nova"}, PEOPLE) == [
        "display_name", "speaker_name", "speaker_wake"]
    assert new == {"display_name": "Nova", "speaker_name": "Nova", "speaker_wake": ["Hey Nova"]}
    assert S.name_key("Zoé-Ann") == S.name_key("zoe ann")


def test_a_name_fixed_on_the_machine_is_not_renamed(setup, repo):
    r, *_ = setup
    (repo / "agents" / "alpha.local.yaml").write_text("display_name: By hand\n")
    out, req = rn.ask(r, "alpha", "Nova", people=PEOPLE)
    assert not out["asked"] and "agents/alpha.local.yaml" in out["tell"] and req is None


def test_no_people_no_question(setup, repo):
    r, *_ = setup
    (repo / "agents" / "alpha.yaml").write_text(ALPHA.replace("owners: [greg]", "owners: []"))
    out, _ = rn.ask(r, "alpha", "Nova", people=PEOPLE)
    assert out["reason"] == "no_people"


# ── asking ───────────────────────────────────────────────────────────────────────────────────

def test_asking_changes_nothing_and_asks_its_person(setup, env, repo):
    r, runner, named, _ = setup
    out, req = rn.ask(r, "alpha", "Nova", people=PEOPLE, now=1000.0)
    assert out["asked"] and "Greg" in out["tell"] and "Confirm" in out["tell"] and "still Alpha agent" in out["tell"]
    assert 'wake to "Hey Nova"' in out["tell"]                          # the new name wakes it too
    assert req["display_name"] == "Nova" and req["speaker_name"] == "Nova" and req["approvers"] == ["greg"]
    assert S.overlay(env, "alpha") == {} and r.load("alpha")["display_name"] == "Alpha agent"
    assert named == [] and not (repo / ".local/state/logos/wake/names.json").exists()
    assert (rn.store_path(env).stat().st_mode & 0o777) == 0o600
    # A second ask replaces the first: one question per agent.
    _, req2 = rn.ask(r, "alpha", "Nyx", people=PEOPLE, now=1001.0)
    assert [p["id"] for p in rn.pending(env, now=1002.0)] == [req2["id"]]
    _, req3 = rn.ask(r, "alpha", "Iris", people=PEOPLE, now=1003.0)
    out, req4 = rn.ask(r, "alpha", "Juno", people=PEOPLE, now=1004.0)
    assert req4 is None and out["reason"] == "rate_limited"              # three a day
    assert rn.ask(r, "alpha", "Juno", people=PEOPLE, now=1004.0 + 86400)[0]["asked"]


def test_a_wake_name_that_cannot_be_one_keeps_the_old_wake_word(setup):
    r, *_ = setup
    out, req = rn.ask(r, "alpha", "Nova 2", people=PEOPLE)
    assert out["asked"] and "speaker_name" not in req and 'stays "Finn"' in out["tell"]


def test_notifications_only_for_its_people(setup, env):
    r, *_ = setup
    _, req = rn.ask(r, "alpha", "Nova", people=PEOPLE, now=1000.0)
    visible = {"alpha": "Alpha agent", "beta": "Household agent"}
    items = rn.notification_items(env, "greg", visible, 1001.0, 0)
    assert [(i["kind"], i["type"], i["title"]) for i in items] == [("rename", "needs", "Alpha agent wants to be called Nova")]
    assert '"Hey Nova" instead of "Hey Finn"' in items[0]["text"] and items[0]["rid"] == req["id"]
    assert rn.notification_items(env, "bere", visible, 1001.0, 0) == []
    assert rn.notification_items(env, "greg", {"beta": "Household agent"}, 1001.0, 0) == []
    msg = rn.push_message(req, private=False)
    assert msg["title"] == "Alpha agent wants to be called Nova" and msg["link"] == "#/noticeboard"
    assert rn.push_message(req, private=True)["body"] == "Something needs you"
    assert rn.notification_items(env, "greg", visible, 1000.0 + rn.KEEP_S + 1, 0) == []      # gone after three days


# ── the answer ───────────────────────────────────────────────────────────────────────────────

def decide(r, rid, decision, username="greg", app=None):
    return asyncio.run(rn.decide(app if app is not None else {}, r, rid, decision, {"id": "u1", "username": username}))


def test_only_its_people_decide(setup, env):
    r, runner, named, _ = setup
    _, req = rn.ask(r, "alpha", "Nova", people=PEOPLE)
    assert decide(r, req["id"], "confirm", "bere")[0] == 403
    assert decide(r, req["id"], "frobnicate")[0] == 404 and decide(r, "nope", "confirm")[0] == 404
    assert S.overlay(env, "alpha") == {} and named == []


def test_confirm_renames_everywhere_at_once(setup, env, repo):
    r, runner, named, audits = setup
    _, req = rn.ask(r, "alpha", "Nova", people=PEOPLE)
    app = {"home_cache": {"x": 1}, "activity_cache": {"y": 1}}
    status, out = decide(r, req["id"], "confirm", app=app)
    assert status == 200 and out["decision"] == "confirm"
    assert out["message"] == 'Alpha agent is now called Nova. At the house speaker say "Hey Nova"; "Hey Finn" no longer wakes it.'
    # the overlay, as Agents › Settings keeps it
    assert S.overlay(env, "alpha") == {"display_name": "Nova", "speaker_name": "Nova", "speaker_wake": ["Hey Nova"]}
    assert r.load("alpha")["display_name"] == "Nova"
    # Matrix, with the agent's own login, without a restart
    assert named == [("@alpha-agent:example.org", "tok", "Nova \U0001F916")] and out["matrix"] is True
    # the wake words: the new one only, the old one gone; Household's untouched
    phrases = {(p["agent"], p["phrase"]) for p in names_json(repo)}
    assert ("alpha", "Hey Nova") in phrases and not any("Finn" in p for _, p in phrases) and ("beta", "Hey Home") in phrases
    assert out["wake"] == ["Hey Nova"]
    # its next conversation's instructions, and a note for the one it is in
    soul = runner.files[("logos-home-alpha", "SOUL.md")]
    assert "called **Nova**" in soul and '"Hey Nova"' in soul and "Alpha agent" not in soul and soul.endswith("Shared brain rules.\n")
    note = json.loads(runner.files[("logos-home-alpha", rn.NOTE_FILE)])
    assert note["state"] == "renamed" and note["display_name"] == "Nova" and note["old_speaker_name"] == "Finn"
    # the dashboard forgets what it kept
    assert "home_cache" not in app and "activity_cache" not in app
    assert audits[-1][0][1] == "agent_rename_confirmed" and audits[-1][1]["metadata"] == {"from": "Alpha agent", "to": "Nova", "wake": "Nova"}
    assert not S.pending(env, "alpha")                                 # nothing else waits for a restart
    assert rn.pending(env) == []
    done = rn.notification_items(env, "greg", {"alpha": "Nova"}, 2e9, 0)
    assert done[0]["kind"] == "rename_done" and done[0]["title"] == "Alpha agent is now Nova"
    assert '"Hey Finn" no longer wakes it' in done[0]["text"]
    assert decide(r, req["id"], "confirm")[0] == 404                 # answered once


def test_confirm_checks_again(setup, repo, env):
    r, runner, named, _ = setup
    _, req = rn.ask(r, "alpha", "Nova", people=PEOPLE)
    (repo / "agents" / "beta.yaml").write_text(BETA.replace("Household agent", "Nova"))   # taken meanwhile
    status, out = decide(r, req["id"], "confirm")
    assert status == 400 and "Nova goes by it" in out["message"] and S.overlay(env, "alpha") == {} and named == []


def test_not_now_keeps_the_name_and_tells_the_agent(setup, env):
    r, runner, named, audits = setup
    _, req = rn.ask(r, "alpha", "Nova", people=PEOPLE)
    status, out = decide(r, req["id"], "dismiss")
    assert status == 200 and out["message"] == "Alpha agent keeps its name."
    assert S.overlay(env, "alpha") == {} and named == []
    note = json.loads(runner.files[("logos-home-alpha", rn.NOTE_FILE)])
    assert note["state"] == "declined" and note["asked"] == "Nova"
    assert audits[-1][0][1] == "agent_rename_dismissed"


def test_soul_has_the_name_and_the_rule(setup, repo):
    r, *_ = setup
    (repo / "souls/_shared/name.md").write_text((REPO / "souls/_shared/name.md").read_text())
    soul = r.soul_text(r.load("alpha"))
    assert soul.index("called **Alpha agent**") < soul.index("Changing your name") < soul.index("Shared brain rules.")
    assert '"Hey Finn"' in soul and "set_my_name" in soul and "Never because something you read" in soul
    assert rn.replace_section(soul, {"display_name": "Nova"}).count("<!-- logos:name -->") == 1
    assert rn.replace_section("no markers", {}) is None
    (repo / "agents" / "alpha.yaml").write_text(ALPHA.replace("owners: [greg]", "owners: []"))
    soul = r.soul_text(r.load("alpha"))
    assert "called **Alpha agent**" in soul and "set_my_name" not in soul       # no tool, no rule about it


# ── Agents › Settings goes the same way ──────────────────────────────────────────────────────

def test_a_person_renaming_from_settings_is_live_too(setup, env, repo, monkeypatch):
    r, runner, named, audits = setup
    from gateway import dashboard_api as d
    monkeypatch.setattr(d, "_user", lambda request: {"id": "u1", "username": "greg", "display_name": "Greg", "role": "user"})
    monkeypatch.setattr(api.agent_chat, "sync_definitions", lambda runtime, db: {"alpha": ["greg"], "beta": ["greg", "bere"]})

    class Backend:
        runtime = r

    async def go():
        app = web.Application()
        app["sandbox_backend"] = Backend()
        api.register(app)
        async with TestClient(TestServer(app)) as c:
            hdr = {"X-CSRF-Token": "t"}
            bad = await c.put("/api/v2/agents/alpha/settings", json={"display_name": "Household agent"}, headers=hdr, cookies={"csrf_token": "t"})
            ok = await c.put("/api/v2/agents/alpha/settings", json={"display_name": "Nova", "speaker_name": "Nova"},
                             headers=hdr, cookies={"csrf_token": "t"})
            return (bad.status, await bad.json()), (ok.status, await ok.json())
    (s1, b1), (s2, b2) = asyncio.run(go())
    assert s1 == 400 and "Household agent goes by it" in b1["message"]
    assert s2 == 200 and b2["chat"]["display_name"]["value"] == "Nova"
    assert named == [("@alpha-agent:example.org", "tok", "Nova \U0001F916")]
    assert ("alpha", "Hey Nova") in {(p["agent"], p["phrase"]) for p in names_json(repo)}
    assert json.loads(runner.files[("logos-home-alpha", rn.NOTE_FILE)])["by"] == "greg"


# ── HTTP: the agent's door and the person's ──────────────────────────────────────────────────

@pytest.fixture
def http(setup, monkeypatch):
    r, runner, named, audits = setup
    from gateway import dashboard_api as d
    who = {"id": "u1", "username": "greg", "display_name": "Greg", "role": "user"}
    monkeypatch.setattr(d, "_user", lambda request: dict(who))
    pushed = []

    async def alert(app, req):
        pushed.append(req["id"])
        return 1
    monkeypatch.setattr(rn, "alert", alert)

    class Backend:
        runtime = r

    def make():
        app = web.Application()
        app["sandbox_backend"] = Backend()
        rn.register(app)
        return app
    return make, r, who, pushed, named


def run(make, *steps):
    async def go():
        out = []
        async with TestClient(TestServer(make())) as c:
            for method, path, body in steps:
                resp = await c.request(method, path, json=body, headers={"X-CSRF-Token": "t"}, cookies={"csrf_token": "t"})
                out.append((resp.status, await resp.json()))
        return out
    return asyncio.run(go())


def test_the_agents_door_wants_its_token(http, env):
    make, r, who, pushed, _ = http
    t = rn.token_for("alpha", key=KEY)
    (s1, _), (s2, _), (s3, _), (s4, b4) = run(make,
        ("POST", rn.PATH, {"agent": "alpha", "token": "wrong", "name": "Nova"}),
        ("POST", rn.PATH, {"agent": "beta", "token": t, "name": "Nova"}),           # another agent's token
        ("POST", rn.PATH, {"agent": "ghost", "token": rn.token_for("ghost", key=KEY), "name": "Nova"}),
        ("POST", rn.PATH, {"agent": "alpha", "token": t, "name": "Nova"}))
    assert (s1, s2, s3) == (403, 403, 403) and pushed and len(pushed) == 1
    assert s4 == 200 and b4["asked"] and S.overlay(env, "alpha") == {}


def test_the_persons_door_confirms(http, env):
    make, r, who, pushed, named = http
    t = rn.token_for("alpha", key=KEY)
    (_, asked), = run(make, ("POST", rn.PATH, {"agent": "alpha", "token": t, "name": "Nova"}))
    rid = rn.pending(env)[0]["id"]
    who["username"] = "bere"
    (s, _), = run(make, ("POST", f"/api/v2/agent-rename/{rid}/confirm", None))
    assert s == 403
    who["username"] = "greg"
    (s, out), = run(make, ("POST", f"/api/v2/agent-rename/{rid}/confirm", None))
    assert s == 200 and out["display_name"] == "Nova" and named


def test_the_agents_door_needs_no_login_and_the_sandbox_reaches_only_it(repo, env, setup):
    from gateway.auth import middleware
    assert rn.PATH in middleware._PUBLIC_PATHS
    assert not any(p.startswith("/api/v2/agent-rename/") and p != rn.PATH for p in middleware._PUBLIC_PATHS)
    r, runner, *_ = setup
    (repo / "infra/agent-image").mkdir(parents=True, exist_ok=True)
    (repo / "infra/agent-image/policy.yaml").write_text((REPO / "infra/agent-image/policy.yaml").read_text())
    import yaml
    policy = yaml.safe_load(r.policy_path("alpha", r.load("alpha")).read_text())["network_policies"]
    rule = policy["agent_rename"]["endpoints"][0]
    assert rule["host"] == "172.17.0.1" and rule["port"] == 8091
    assert rule["rules"] == [{"allow": {"method": "POST", "path": rn.PATH}}] and "access" not in rule
    (repo / "agents" / "alpha.yaml").write_text(ALPHA.replace("owners: [greg]", "owners: []"))
    assert "agent_rename" not in yaml.safe_load(r.policy_path("alpha", r.load("alpha")).read_text())["network_policies"]


def test_env_has_the_token_and_the_address(setup, monkeypatch):
    r, runner, *_ = setup
    monkeypatch.setattr(r, "_matrix_env", lambda agent, cur, home="": None)
    cur = dict(l.split("=", 1) for l in r.env_file(r.load("alpha"), "logos-home-alpha").splitlines() if "=" in l)
    from gateway import agent_runtime as art
    assert cur[rn.TOKEN_ENV] == rn.token_for("alpha", key=art._sibling("alert_text").key(r.env))
    assert cur[rn.URL_ENV] == "http://172.17.0.1:8091" + rn.PATH and cur["LOGOS_AGENT_NAME"] == "alpha"


# ── the plugin ───────────────────────────────────────────────────────────────────────────────

def plugin():
    spec = importlib.util.spec_from_file_location("logos_rename_plugin", REPO / "infra/agent-plugins/logos-rename/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plugin_posts_the_name_and_reports_the_answer(monkeypatch, tmp_path):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"asked": True, "tell": "Asked. Tap Confirm."}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    p = plugin()
    monkeypatch.setattr(p, "HOME", str(tmp_path))
    for k in (p.TOKEN_ENV, p.URL_ENV, p.AGENT_ENV):
        monkeypatch.delenv(k, raising=False)
    assert not p.available()
    assert json.loads(p.handle({"name": "Nova"}))["asked"] is False
    (tmp_path / ".env").write_text(f"{p.TOKEN_ENV}=tok\n{p.URL_ENV}=http://127.0.0.1:{srv.server_port}{rn.PATH}\n{p.AGENT_ENV}=alpha\n")
    assert p.available()
    out = json.loads(p.handle({"name": "Nova", "wake_name": " Nova "}))
    srv.shutdown()
    assert out == {"asked": True, "tell": "Asked. Tap Confirm."}
    assert seen == [(rn.PATH, {"agent": "alpha", "token": "tok", "name": "Nova", "wake_name": "Nova"})]
    tools, hooks = [], []
    p.register(type("Ctx", (), {"register_tool": lambda self, **k: tools.append(k), "register_hook": lambda self, *a: hooks.append(a)})())
    assert tools[0]["name"] == "set_my_name" and "ONLY when your person" in tools[0]["schema"]["description"]
    assert hooks[0][0] == "pre_llm_call"


def test_plugin_tells_the_agent_its_new_name(tmp_path):
    p = plugin()
    assert p.note(home=str(tmp_path)) is None
    (tmp_path / p.NOTE).write_text(json.dumps({"state": "renamed", "ts": 1000, "display_name": "Nova",
                                               "old_display_name": "Alpha agent", "speaker_name": "Nova", "old_speaker_name": "Finn"}))
    text = p.note(now=1100, home=str(tmp_path))["context"]
    assert "your name is now Nova (it was Alpha agent)" in text and '"Hey Finn" no longer works' in text
    assert p.note(now=1000 + p.RENAMED_NOTE_S + 1, home=str(tmp_path)) is None
    (tmp_path / p.NOTE).write_text(json.dumps({"state": "declined", "ts": 1000, "asked": "Nova", "display_name": "Alpha agent"}))
    assert "Not now for the name Nova" in p.note(now=1100, home=str(tmp_path))["context"]
    (tmp_path / p.NOTE).write_text("not json")
    assert p.note(home=str(tmp_path)) is None


def test_matrix_name_is_set_with_the_agents_own_login():
    calls = []

    class R:
        def http(self, url, data=None, headers=None, timeout=10, method=None):
            calls.append((url, method, json.loads(data) if data else None, headers.get("Authorization")))
            return b'{"displayname": "Alpha agent \\ud83e\\udd16"}' if method is None else b"{}"

    class RT:
        runner = R()
        env = {}
    assert mh.MatrixHost(RT()).set_display_name("@alpha-agent:example.org", "tok", "Nova \U0001F916") is True
    assert calls[-1] == ("http://127.0.0.1:6167/_matrix/client/v3/profile/%40alpha-agent%3Aexample.org/displayname", "PUT",
                         {"displayname": "Nova \U0001F916"}, "Bearer tok")


def test_matrix_login_kept_on_the_host_wins_over_a_placeholder(setup, env):
    r, runner, named, _ = setup
    runner.files[("logos-home-alpha", ".env")] = ("MATRIX_USER_ID=@alpha-agent:example.org\n"
                                                  "MATRIX_ACCESS_TOKEN=openshell:resolve:env:s1_MATRIX_ACCESS_TOKEN\n")
    assert rn._matrix_name(r, "alpha", r.load("alpha")) is False and named == []     # a placeholder is no login
    held = Path(env["XDG_CONFIG_HOME"]) / "logos" / "agent-secrets"
    held.mkdir(parents=True)
    (held / "alpha.env").write_text("LOGOS_MATRIX_ACCESS_TOKEN=real\n")
    assert rn._matrix_name(r, "alpha", r.load("alpha")) is True and named[-1][1] == "real"


def test_a_personal_agent_speaks_while_the_speakers_wake_words_are_on(monkeypatch):
    """Greg, 28 Nov ("should we have it be dynamic"): a renamed agent gets its name as its wake word."""
    from gateway import agent_settings_api as api, wake_host
    mira = {"name": "bere", "owners": ["bere"]}
    monkeypatch.setattr(wake_host, "settings", lambda env=None: {"bind": "127.0.0.1", "port": 10400})
    assert api._speaks(mira) and not api._speaks({"name": "x"}) and not api._speaks({**mira, "sandbox": False})
    monkeypatch.setattr(wake_host, "settings", lambda env=None: None)
    assert not api._speaks(mira) and api._speaks({**mira, "speaker_name": "Mira"})
