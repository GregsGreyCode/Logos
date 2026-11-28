"""Agent settings (story E6): the private overlay, its checks, and the dashboard's endpoints."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import agent_settings as S
from gateway import agent_settings_api as api
from gateway.lib.redact import redact_sensitive_text
from tests.unit.test_agent_runtime import FakeRunner, repo, rt  # noqa: F401  (repo is a fixture)

LISTING = {"data": [
    {"id": "some/model", "type": "vlm", "state": "loaded", "max_context_length": 262144, "loaded_context_length": 65536},
    {"id": "small/model", "type": "llm", "state": "not-loaded", "max_context_length": 32768},
    {"id": "nomic-embed", "type": "embeddings", "state": "not-loaded", "max_context_length": 2048},
]}


@pytest.fixture
def env(repo):
    return {"HOME": str(repo), "XDG_CONFIG_HOME": str(repo / ".test-config")}


def runtime(repo, env, runner=None):
    return rt(repo, runner, env=env)


# ── where it lives, and what it may set ──────────────────────────────────────────────────────

def test_overlay_lives_outside_the_repo_and_keeps_only_allowed_keys(repo, env):
    path = S.agent_dir(env, "alpha")
    assert path == repo / ".test-config" / "logos" / "agents" / "alpha"
    assert S.agent_dir({"LOGOS_AGENT_SETTINGS": "/x"}, "alpha") == __import__("pathlib").Path("/x/alpha")
    path.mkdir(parents=True)
    (path / "settings.yaml").write_text("display_name: Al\nowners: [mallory]\npolicy: /tmp/open.yaml\n"
                                        "model: {default: x/y, provider: openai, base_url: http://evil}\n")
    assert S.overlay(env, "alpha") == {"display_name": "Al", "model": {"default": "x/y"}}
    with pytest.raises(S.Invalid):
        S.agent_dir(env, "../etc")


def test_save_overlay_is_private_and_an_empty_one_removes_the_file(repo, env):
    S.save_overlay(env, "alpha", {"display_name": "Al", "owners": ["x"]})
    f = S.agent_dir(env, "alpha") / "settings.yaml"
    assert f.stat().st_mode & 0o777 == 0o600 and f.parent.stat().st_mode & 0o777 == 0o700
    assert "owners" not in f.read_text()
    S.save_overlay(env, "alpha", {})
    assert not f.exists()


def test_load_lays_the_overlay_under_the_local_files(repo, env):
    r = runtime(repo, env)
    S.save_overlay(env, "alpha", {"display_name": "Al", "voice_echo": True, "model": {"default": "small/model"}})
    a = r.load("alpha")
    assert a["display_name"] == "Al" and a["voice_echo"] is True
    assert a["model"] == {"provider": "lmstudio", "base_url": "http://10.0.0.5:1234/v1", "default": "small/model",
                          "context_length": 65536}
    assert r.load("alpha", settings=False)["display_name"] == "Alpha agent"
    (repo / "agents" / "alpha.local.yaml").write_text("display_name: By hand\n")
    assert r.load("alpha")["display_name"] == "By hand"                  # a hand edit on the machine wins
    assert api.locked(r, "alpha") == {"display_name": "Set on this machine in agents/alpha.local.yaml, which comes first."}


def test_soul_text_takes_the_dashboard_soul_before_the_shared_rules(repo, env):
    r = runtime(repo, env)
    agent = r.load("alpha")
    assert r.soul_text(agent).startswith("You are Alpha.\n\nShared brain rules.")
    S.save_soul(env, repo, agent, "You are Al, and brief.\n", "greg")
    assert r.soul_text(agent) == "You are Al, and brief.\n\nShared brain rules.\n\nJudgement rules.\n"
    assert (repo / "souls" / "general" / "soul.md").read_text() == "You are Alpha.\n\n"     # the repo is untouched


def test_up_without_model_load_never_asks_lm_studio_and_notes_what_it_used(repo, env, capsys):
    runner = FakeRunner()
    r = runtime(repo, env, runner)
    assert r.up("alpha", load_model=False) == 0
    assert not any("1234" in c[0] for c in runner.http_calls)
    assert "not checked or loaded (--no-model-load)" in capsys.readouterr().out
    assert not S.agent_dir(env, "alpha").exists()                      # nobody changed it: no folder
    S.save_overlay(env, "alpha", {"display_name": "Al"})
    assert S.pending(env, "alpha")
    assert r.up("alpha", load_model=False) == 0
    assert not S.pending(env, "alpha")
    S.save_overlay(env, "alpha", {"display_name": "Al 2"})
    assert S.pending(env, "alpha")


def test_cli_passes_no_model_load(monkeypatch):
    import importlib.util
    from importlib.machinery import SourceFileLoader
    from pathlib import Path
    loader = SourceFileLoader("logos_agent_cli_e6", str(Path(__file__).resolve().parents[2] / "scripts" / "logos-agent"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    seen = []

    class RT:
        def __init__(self, repo): pass
        def up(self, name, load_model=True): seen.append((name, load_model)); return 0

    class Module:
        AgentRuntime = RT
        AgentRuntimeError = RuntimeError
    monkeypatch.setattr(mod, "_runtime_module", lambda: Module)
    assert mod.main(["x", "up", "dev", "--no-model-load"]) == 0 and mod.main(["x", "up", "dev"]) == 0
    assert seen == [("dev", False), ("dev", True)]
    assert mod.main(["x", "up", "dev", "--frobnicate"]) == 2


# ── the soul: checks, history, restore, reset ────────────────────────────────────────────────

def test_check_soul_limits_and_refuses_secrets():
    assert S.check_soul("Hi\r\nthere  \n\n") == "Hi\nthere\n"
    for bad in ("", "   \n", "x" * (S.SOUL_LIMIT + 1), "bell\x07", 42):
        with pytest.raises(S.Invalid):
            S.check_soul(bad)
    with pytest.raises(S.Invalid, match="password or key"):
        S.check_soul("Use the key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 for things.", redact_sensitive_text)
    assert S.check_soul("Be kind.", redact_sensitive_text) == "Be kind.\n"


def test_history_keeps_versions_restores_and_resets(repo, env):
    agent = runtime(repo, env).load("alpha")
    t = [1_800_000_000 + i * 60 for i in range(40)]
    S.save_soul(env, repo, agent, "One.\n", "greg", now=t[1])
    S.save_soul(env, repo, agent, "Two.\n", "bere", now=t[2])
    assert S.save_soul(env, repo, agent, "Two.\n", "bere", now=t[3]) is None        # unchanged: nothing kept
    rows = S.history(env, "alpha")
    assert [r["what"] for r in rows] == ["edited", "edited", "default"]
    assert [S.history_text(env, "alpha", r["id"]) for r in rows] == ["Two.\n", "One.\n", "You are Alpha.\n\n"]
    assert rows[0]["by"] == "bere" and rows[0]["chars"] == 5
    S.save_soul(env, repo, agent, S.history_text(env, "alpha", rows[1]["id"]), "greg", "restored", now=t[4])
    assert S.own_soul(repo, env, agent) == "One.\n" and S.history(env, "alpha")[0]["what"] == "restored"
    S.reset_soul(env, repo, agent, "greg", now=t[5])
    assert S.soul_file(env, "alpha") is None and S.own_soul(repo, env, agent) == "You are Alpha.\n\n"
    assert S.reset_soul(env, repo, agent, "greg") is None
    S.save_soul(env, repo, agent, "Three.\n", "greg", now=t[6])
    S.save_soul(env, repo, agent, "You are Alpha.\n\n", "greg", now=t[7])            # the default text = reset
    assert S.soul_file(env, "alpha") is None
    for i in range(30):
        S.save_soul(env, repo, agent, f"V{i}.\n", "greg", now=t[8] + i)
    kept = S.history(env, "alpha")
    assert len(kept) == S.HISTORY_KEEP
    assert len(list((S.agent_dir(env, "alpha") / "history").glob("*.md"))) == S.HISTORY_KEEP
    with pytest.raises(S.Invalid):
        S.history_text(env, "alpha", "../../settings")


# ── chat settings and the model ──────────────────────────────────────────────────────────────

def test_display_and_speaker_names():
    assert S.check_display_name("  Finn's   agent ") == "Finn's agent"
    for bad in ("", "x" * 41, "<script>", "a\x00b"):
        with pytest.raises(S.Invalid):
            S.check_display_name(bad)
    assert S.check_speaker_name("Aunt May", {}) == "Aunt May" and S.check_speaker_name("Zoé", {}) == "Zoé"
    for bad in ("", "R2D2", "one two three", "x", "Hey!"):
        with pytest.raises(S.Invalid):
            S.check_speaker_name(bad, {})
    with pytest.raises(S.Invalid, match="already wakes Household"):
        S.check_speaker_name("home", {S.wake_key("Hey Home"): "Household"})


def test_model_choices_and_guard_rails():
    choices = S.model_choices(LISTING)
    assert [c["id"] for c in choices] == ["some/model", "small/model"]          # loaded first; no embeddings
    current = {"default": "some/model", "context_length": 65536, "vision": True}
    with pytest.raises(S.Invalid, match="not on the model server"):
        S.check_model({"default": "nope"}, choices, current, {})
    with pytest.raises(S.Invalid, match="at most 32,768"):
        S.check_model({"default": "small/model", "context_length": 65536}, choices, current, {})
    with pytest.raises(S.Invalid, match="sizes listed"):
        S.check_model({"default": "some/model", "context_length": 12345}, choices, current, {})
    block, warns = S.check_model({"default": "small/model", "context_length": 16384}, choices, current, {})
    assert block == {"default": "small/model", "context_length": 16384, "vision": False}
    assert [w["confirm"] for w in warns] == [True, False, False]                # not loaded; small; no photos
    _, warns = S.check_model({"default": "some/model", "context_length": 131072}, choices, current,
                             {"household": {"default": "some/model", "load_context_length": 96512}})
    assert warns[0]["confirm"] and "pauses household" in warns[0]["text"]
    assert S.check_model({"default": "some/model", "context_length": 65536}, choices, current, {})[1] == []


# ── the endpoints ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(repo, env, monkeypatch):
    runner = FakeRunner()
    runner.http_response = json.dumps(LISTING).encode()
    r = runtime(repo, env, runner)
    who = {"id": "u1", "username": "greg", "display_name": "Greg", "role": "user"}
    owners = {"alpha": ["greg"]}
    audits = []
    from gateway import dashboard_api as d
    monkeypatch.setattr(d, "_user", lambda request: dict(who))
    monkeypatch.setattr(api.agent_chat, "sync_definitions", lambda runtime, db: dict(owners))
    monkeypatch.setattr(api.auth_db, "write_audit_log", lambda *a, **k: audits.append((a, k)))
    busy = {"left": 0}

    async def is_busy(app, runtime, name):
        busy["left"] -= 1
        return busy["left"] >= 0
    monkeypatch.setattr(api, "is_busy", is_busy)
    monkeypatch.setattr(api, "POLL_S", 0.01)
    ups = []

    async def run_up(runtime, name, job):
        ups.append(name)
        job["lines"].append(f"{name}: Ready")
        return 0
    monkeypatch.setattr(api, "run_up", run_up)

    class Backend:
        runtime = r

    def make():
        app = web.Application()
        app["sandbox_backend"] = Backend()
        api.register(app)
        return app
    return make, r, who, owners, audits, busy, ups, runner


def call(make, *steps):
    """Several requests against one app (so the restart job is still there for the next)."""
    async def go():
        out = []
        async with TestClient(TestServer(make())) as c:
            for step in steps:
                if callable(step):
                    await step()
                    continue
                method, path, body = (step + (None,))[:3]
                resp = await c.request(method, path, json=body, headers={"X-CSRF-Token": "t"}, cookies={"csrf_token": "t"})
                out.append((resp.status, await resp.json()))
        return out
    return asyncio.run(go())


def test_settings_page_shows_soul_model_and_chat(client):
    make, r, *_ = client
    (status, view), = call(make, ("GET", "/api/v2/agents/alpha/settings"))
    assert status == 200
    assert view["soul"]["text"] == "You are Alpha.\n\n" and view["soul"]["custom"] is False
    assert view["model"]["default"] == "some/model" and view["model"]["server_up"] is True
    assert [c["id"] for c in view["model"]["choices"]] == ["some/model", "small/model"]
    assert view["chat"] == {"display_name": {"value": "Alpha agent", "default": "Alpha agent", "locked": None}}
    assert view["apply"] == {"state": "idle"} and view["pending"] is False


def test_only_owners_and_admins(client):
    make, r, who, owners, *_ = client
    who["username"] = "bere"
    assert call(make, ("GET", "/api/v2/agents/alpha/settings"))[0][0] == 404
    assert call(make, ("PUT", "/api/v2/agents/alpha/settings", {"display_name": "X"}))[0][0] == 404
    assert call(make, ("POST", "/api/v2/agents/alpha/settings/apply"))[0][0] == 404
    who["role"] = "admin"
    assert call(make, ("GET", "/api/v2/agents/alpha/settings"))[0][0] == 200
    assert call(make, ("GET", "/api/v2/agents/nobody/settings"))[0][0] == 404


def test_save_checks_everything_first_and_keeps_nothing_on_a_refusal(client, env):
    make, r, who, owners, audits, *_ = client
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"display_name": "Al", "soul": "x" * 9000}))
    assert s == 400 and "most is 8,000" in out["message"]
    assert S.overlay(env, "alpha") == {} and audits == []
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"display_name": "Al", "soul": "You are Al.\n"}))
    assert s == 200 and out["changed"] == ["display_name", "soul"] and out["pending"] is True
    assert out["soul"]["custom"] and out["chat"]["display_name"]["value"] == "Al"
    assert audits[-1][0][1] == "agent_settings" and audits[-1][1]["metadata"] == {"changed": ["display_name", "soul"]}
    assert "You are Al" not in json.dumps(audits)                         # the text is not in the audit log
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"display_name": "Alpha agent"}))
    assert "display_name" not in S.overlay(env, "alpha")                  # back to the definition's own
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"voice_echo": True}))
    assert s == 400 and "voice notes" in out["message"]


def test_model_change_asks_first_when_it_would_load_another_model(client, env):
    make, r, *_ = client
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"model": {"default": "small/model", "context_length": 32768}}))
    assert s == 409 and out["error"] == "confirm" and out["warnings"][0]["confirm"]
    assert S.overlay(env, "alpha") == {}
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings",
                            {"model": {"default": "small/model", "context_length": 32768}, "confirm": True}))
    assert s == 200 and S.overlay(env, "alpha") == {"model": {"default": "small/model", "context_length": 32768, "vision": False}}
    assert r.load("alpha")["model"]["provider"] == "lmstudio"
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"model": {"default": "some/model", "context_length": 65536}}))
    assert s == 200 and S.overlay(env, "alpha") == {}       # the definition's own again


def test_model_cannot_change_while_the_model_server_is_off(client, env):
    make, r, *rest = client
    runner = rest[-1]

    def off(url, **kw):
        raise OSError("down")
    runner.http = off
    (s, view), = call(make, ("GET", "/api/v2/agents/alpha/settings"))
    assert view["model"]["server_up"] is False and view["model"]["choices"] == []
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"model": {"default": "some/model"}}))
    assert s == 400 and "not answering" in out["message"]


def test_restore_and_reset_through_the_endpoint(client, env):
    make, r, *_ = client
    call(make, ("PUT", "/api/v2/agents/alpha/settings", {"soul": "First.\n"}),
         ("PUT", "/api/v2/agents/alpha/settings", {"soul": "Second.\n"}))
    rows = S.history(env, "alpha")
    (s, v), = call(make, ("GET", f"/api/v2/agents/alpha/settings/soul/{rows[1]['id']}"))
    assert v == {"text": "First.\n"}
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"soul_restore": rows[1]["id"]}))
    assert out["soul"]["text"] == "First.\n"
    (s, out), = call(make, ("PUT", "/api/v2/agents/alpha/settings", {"soul_reset": True}))
    assert out["soul"]["custom"] is False and out["soul"]["text"] == "You are Alpha.\n\n"
    assert call(make, ("GET", "/api/v2/agents/alpha/settings/soul/19990101T000000Z-abcd"))[0][0] == 404


def test_apply_waits_for_the_reply_then_recreates_one_agent_at_a_time(client):
    make, r, who, owners, audits, busy, ups, _ = client
    busy["left"] = 2                                       # mid-reply for the first two looks

    async def settle():
        for _ in range(200):
            await asyncio.sleep(0.01)
            if ups:
                await asyncio.sleep(0.02)
                return
    out = call(make, ("POST", "/api/v2/agents/alpha/settings/apply"),
               ("POST", "/api/v2/agents/alpha/settings/apply"),
               settle,
               ("GET", "/api/v2/agents/alpha/settings/apply"))
    assert out[0][0] == 202 and out[0][1]["state"] == "waiting"
    assert out[1][0] == 409 and out[1][1]["error"] == "busy"
    assert out[2][1]["state"] == "done" and out[2][1]["lines"] == ["alpha: Ready"] and ups == ["alpha"]
    assert audits[-1][0][1] == "agent_restart" and audits[-1][1]["metadata"]["result"] == "done"


def test_apply_gives_up_while_the_agent_keeps_answering(client, monkeypatch):
    make, r, who, owners, audits, busy, ups, _ = client
    busy["left"] = 10 ** 6
    monkeypatch.setattr(api, "WAIT_IDLE_S", 0.05)

    async def wait():
        await asyncio.sleep(0.2)
    out = call(make, ("POST", "/api/v2/agents/alpha/settings/apply"), wait, ("GET", "/api/v2/agents/alpha/settings/apply"))
    assert out[1][1]["state"] == "failed" and "still answering" in out[1][1]["message"] and ups == []


def test_deploy_never_touches_the_overlay_folder():
    """scripts/logos-deploy works in the checkout only (git fast-forward); the overlay is outside it."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "scripts" / "logos-deploy").read_text()
    assert ".config/logos/agents" not in text and "git clean" not in text
