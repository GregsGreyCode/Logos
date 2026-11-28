"""Story E7: secrets the sandbox only sees as OpenShell placeholders (gateway/agent_secrets.py), the
host store, and their rotation (gateway/secret_rotation.py)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from gateway import agent_runtime as art
from tests.unit.test_agent_runtime import FakeRunner, repo, rt  # noqa: F401  (repo is a fixture)

sec = art._sibling("agent_secrets")
REAL_POLICY = Path(__file__).resolve().parents[2] / "infra" / "agent-image" / "policy.yaml"
PH = "openshell:resolve:env:s0123abcd_LOGOS_NOTICEBOARD_TOKEN"


def _env(text):
    return sec.parse(text)


def _with_services(repo, extra=""):
    """alpha with the noticeboard and voice, and the real sandbox policy (so its rules exist)."""
    (repo / "infra" / "agent-image").mkdir(parents=True, exist_ok=True)
    shutil.copy(REAL_POLICY, repo / "infra" / "agent-image" / "policy.yaml")
    path = repo / "agents" / "alpha.yaml"
    path.write_text(path.read_text().replace("providers: [github]", "providers: []")
                    + "noticeboard: true\nvoice: true\n" + extra)


@pytest.fixture
def quiet_services(monkeypatch):
    """The services' own `up` is tested elsewhere; here they only must not run."""
    for mod, cls in (("noticeboard_host", "NoticeboardHost"), ("voice_host", "VoiceHost")):
        monkeypatch.setattr(getattr(art._sibling(mod), cls), "up", lambda self: False)


class WritingRunner(FakeRunner):
    """A FakeRunner whose volume writes (`put`) are read back by `get`, like real volumes."""

    def run(self, argv, *, input=None, text=True):
        argv = list(argv)
        if argv[:2] == ["docker", "run"] and "cat > /v/" in argv[-1] and input is not None:
            vol = argv[argv.index("-v") + 1].split(":")[0]
            self.files[(vol, argv[-1].split("cat > /v/")[1].split(" ")[0])] = input
        return super().run(argv, input=input, text=text)


def _written(runner, rel):
    """The last content written to ``rel`` in the alpha home volume through `put`."""
    for call, data in zip(reversed(runner.calls), reversed(runner.inputs)):
        if call[:2] == ["docker", "run"] and f"cat > /v/{rel} " in call[-1]:
            return data
    raise AssertionError(f"{rel} was never written")


# ── the switch ──────────────────────────────────────────────────────────

class TestSwitch:
    def test_defaults(self):
        on = sec.switches({})
        assert {k for k, v in on.items() if v} == set(sec.DEFAULT_ON)
        assert on["house"] is False and on["mail"] is False and on["calendar"] is True

    def test_all_off_or_on(self):
        assert not any(sec.switches({"credential_placeholders": False}).values())
        assert all(sec.switches({"credential_placeholders": True}).values())

    def test_one_at_a_time(self):
        on = sec.switches({"credential_placeholders": {"calendar": False, "house": True, "nonsense": True}})
        assert on["calendar"] is False and on["house"] is True and "nonsense" not in on
        only = sec.switches({"credential_placeholders": {"all": False, "voice": True}})
        assert [k for k, v in only.items() if v] == ["voice"]


# ── the host store ──────────────────────────────────────────────────────

class TestStore:
    def test_owner_only_and_never_a_placeholder(self, tmp_path):
        env = {"XDG_CONFIG_HOME": str(tmp_path)}
        sec.save(env, "alpha", {"LOGOS_VOICE_TOKEN": "real-1", "LOGOS_NOTICEBOARD_TOKEN": PH, "EMPTY": ""})
        path = sec.store_path(env, "alpha")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert sec.load(env, "alpha") == {"LOGOS_VOICE_TOKEN": "real-1"}
        sec.update(env, "alpha", {"LOGOS_VOICE_TOKEN": None, "LOGOS_WEB_TOKEN": "w"})
        assert sec.load(env, "alpha") == {"LOGOS_WEB_TOKEN": "w"}

    def test_lookup_prefers_the_host_and_never_returns_a_placeholder(self, repo):
        runner = FakeRunner(files={("logos-home-alpha", ".env"):
                                   f"LOGOS_NOTICEBOARD_TOKEN={PH}\nLOGOS_VOICE_TOKEN=from-env\n"})
        r = rt(repo, runner)
        assert r.secret("alpha", "LOGOS_NOTICEBOARD_TOKEN") == ""          # only a placeholder there
        assert r.secret("alpha", "LOGOS_VOICE_TOKEN") == "from-env"        # not moved yet
        sec.update(r.env, "alpha", {"LOGOS_NOTICEBOARD_TOKEN": "host-real"})
        assert r.secret("alpha", "LOGOS_NOTICEBOARD_TOKEN") == "host-real"
        # and the services ask the same way
        assert art._sibling("noticeboard_host").NoticeboardHost(r).token_for("alpha") == "host-real"


# ── the profile and the start hook ──────────────────────────────────────

def test_profile_binds_a_stable_bearer_placeholder_to_the_rule_endpoints():
    ep = {"host": "172.17.0.1", "port": 8090, "protocol": "mcp", "enforcement": "enforce",
          "allowed_ips": ["172.17.0.1/32"], "mcp": {"versions": ["2025-06-18"]},
          "rules": [{"allow": {"method": "initialize"}}], "comment_only_in_policy": 1}
    p = sec.profile("alpha", "noticeboard", [ep], ["/opt/hermes/tools/python-*/bin/python3*"])
    assert p["id"] == "logos-alpha-noticeboard"
    (cred,) = p["credentials"]
    assert cred["env_vars"] == ["LOGOS_NOTICEBOARD_TOKEN"] and cred["stable_placeholder"] is True
    assert (cred["auth_style"], cred["header_name"]) == ("bearer", "Authorization")
    assert p["endpoints"] == [{k: v for k, v in ep.items() if k != "comment_only_in_policy"}]
    assert p["binaries"] == ["/opt/hermes/tools/python-*/bin/python3*"]
    assert sec.provider_name("alpha", "speaker_say") == "logos-alpha-speaker-say"


@pytest.mark.skipif(not shutil.which("sh") or not shutil.which("awk"), reason="needs sh and awk")
def test_start_hook_writes_the_placeholders_into_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("API_SERVER_KEY=keep-me\nLOGOS_NOTICEBOARD_TOKEN=stale\nTZ=Europe/London\n")
    hook = sec.START_HOOK.replace("/opt/data/.env", str(env_file)).replace("/tmp/.logos-placeholders",
                                                                          str(tmp_path / "ph"))
    real_run = getattr(subprocess.run, "__wrapped__", None) or subprocess.run
    p = real_run(["sh", "-c", hook], env={"PATH": os.environ["PATH"], "LOGOS_NOTICEBOARD_TOKEN": PH,
                                         "MATRIX_ACCESS_TOKEN": "openshell:resolve:env:sfff_MATRIX_ACCESS_TOKEN",
                                         "OTHER": "not-a-placeholder"}, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    got = _env(env_file.read_text())
    assert got == {"API_SERVER_KEY": "keep-me", "TZ": "Europe/London", "LOGOS_NOTICEBOARD_TOKEN": PH,
                   "MATRIX_ACCESS_TOKEN": "openshell:resolve:env:sfff_MATRIX_ACCESS_TOKEN"}
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not (tmp_path / "ph").exists()


# ── up: what goes where ─────────────────────────────────────────────────

class TestUp:
    def test_placeholders_on_by_default(self, repo, quiet_services):
        _with_services(repo)
        runner = FakeRunner()
        r = rt(repo, runner)
        assert r.up("alpha", load_model=False) == 0
        host = sec.load(r.env, "alpha")
        dotenv = _env(_written(runner, ".env"))
        for key in ("LOGOS_NOTICEBOARD_TOKEN", "LOGOS_VOICE_TOKEN"):
            assert host[key] and key not in dotenv                  # real value on the host only
        assert dotenv["API_SERVER_KEY"]                             # the agent checks requests with it itself
        cfg = yaml.safe_load(_written(runner, "config.yaml"))
        assert cfg["mcp_servers"]["noticeboard"]["headers"]["Authorization"] == "Bearer ${LOGOS_NOTICEBOARD_TOKEN}"
        assert cfg["stt"]["openai"]["api_key"] == "${LOGOS_VOICE_TOKEN}"
        assert host["LOGOS_NOTICEBOARD_TOKEN"] not in _written(runner, "config.yaml")
        # one provider per secret, made before the sandbox, with the value only in its environment
        (create,) = runner.matching("openshell", "sandbox", "create")
        assert ["--provider", "logos-alpha-noticeboard"] == create[create.index("logos-alpha-noticeboard") - 1:
                                                                   create.index("logos-alpha-noticeboard") + 1]
        made = {argv[argv.index("--name") + 1]: (argv, env) for argv, env in runner.env_calls
                if argv[:3] == ["openshell", "provider", "create"]}
        argv, env = made["logos-alpha-noticeboard"]
        assert env == {"LOGOS_NOTICEBOARD_TOKEN": host["LOGOS_NOTICEBOARD_TOKEN"]}
        assert host["LOGOS_NOTICEBOARD_TOKEN"] not in " ".join(c for call in runner.calls for c in call)
        assert runner.calls.index(argv) < runner.calls.index(create)
        # bound to the agent's own noticeboard rules (both of them), at the real address
        prof = yaml.safe_load((repo / ".rendered" / "alpha.providers" / "noticeboard.yaml").read_text())
        assert [(e["host"], e["port"]) for e in prof["endpoints"]] == [("172.17.0.1", 8090), ("172.17.0.1", 8092)]
        assert prof["endpoints"][0]["protocol"] == "mcp"

    def test_a_second_up_keeps_the_values(self, repo, quiet_services):
        _with_services(repo)
        runner = FakeRunner()
        r = rt(repo, runner)
        r.up("alpha", load_model=False)
        first = sec.load(r.env, "alpha")
        # what the sandbox's .env looks like once it ran: the start hook wrote placeholders in
        runner.files[("logos-home-alpha", ".env")] = _written(runner, ".env") + f"LOGOS_NOTICEBOARD_TOKEN={PH}\n"
        r.up("alpha", load_model=False)
        assert sec.load(r.env, "alpha") == first
        # and without the placeholder line at all (never started since): still the same
        runner.files[("logos-home-alpha", ".env")] = _written(runner, ".env")
        r.up("alpha", load_model=False)
        assert sec.load(r.env, "alpha") == first
        updates = [a for a, _ in runner.env_calls if a[:3] == ["openshell", "provider", "update"]]
        assert {a[3] for a in updates} == {"logos-alpha-noticeboard", "logos-alpha-voice"}

    def test_switched_off_the_value_goes_back_to_dotenv_and_the_provider_goes(self, repo, quiet_services):
        _with_services(repo)
        runner = FakeRunner()
        r = rt(repo, runner)
        r.up("alpha", load_model=False)
        token = sec.load(r.env, "alpha")["LOGOS_NOTICEBOARD_TOKEN"]
        runner.files[("logos-home-alpha", ".env")] = _written(runner, ".env")
        (repo / "agents" / "alpha.local.yaml").write_text("credential_placeholders: {noticeboard: false}\n")
        r.up("alpha", load_model=False)
        assert _env(_written(runner, ".env"))["LOGOS_NOTICEBOARD_TOKEN"] == token
        assert "LOGOS_NOTICEBOARD_TOKEN" not in sec.load(r.env, "alpha")
        assert "logos-alpha-noticeboard" not in runner.providers and "logos-alpha-voice" in runner.providers
        create = runner.matching("openshell", "sandbox", "create")[-1]
        assert "logos-alpha-noticeboard" not in create
        cfg = yaml.safe_load(_written(runner, "config.yaml"))
        assert cfg["mcp_servers"]["noticeboard"]["headers"]["Authorization"] == f"Bearer {token}"

    def test_all_off_is_as_before(self, repo, quiet_services):
        _with_services(repo, "credential_placeholders: false\n")
        runner = FakeRunner()
        r = rt(repo, runner)
        r.up("alpha", load_model=False)
        assert runner.env_calls == []
        assert _env(_written(runner, ".env"))["LOGOS_VOICE_TOKEN"]
        assert sec.load(r.env, "alpha") == {}

    def test_matrix_password_moves_to_the_host(self, repo):
        runner = FakeRunner()
        r = rt(repo, runner)
        text = "LOGOS_MATRIX_PASSWORD=pw\nLOGOS_MATRIX_ACCESS_TOKEN=t\nLOGOS_MATRIX_USER_ID=@a:x\nTZ=UTC\n"
        sandbox, host, on = r.split_secrets("alpha", r.load("alpha"), text)
        assert host == {"LOGOS_MATRIX_PASSWORD": "pw", "LOGOS_MATRIX_ACCESS_TOKEN": "t"} and on == []
        assert _env(sandbox) == {"LOGOS_MATRIX_USER_ID": "@a:x", "TZ": "UTC"}

    def test_a_secret_without_a_rule_stays_in_dotenv(self, repo):
        # no policy file in this repo: nothing to bind the credential to, so no placeholder
        r = rt(repo)
        sandbox, host, on = r.split_secrets("alpha", r.load("alpha"), "LOGOS_VOICE_TOKEN=v\n")
        assert on == [] and _env(sandbox) == {"LOGOS_VOICE_TOKEN": "v"}

    def test_a_provider_failure_never_shows_the_value(self, repo):
        class Failing(FakeRunner):
            def run_env(self, argv, env):
                from gateway.agent_runtime import Completed
                return Completed(1, "", "error: bad credential " + next(iter(env.values())))
        r = rt(repo, Failing())
        with pytest.raises(art.AgentRuntimeError) as e:
            r._openshell_secret(["openshell", "provider", "create"], "K", "very-secret-value")
        assert "very-secret-value" not in str(e.value) and "<secret>" in str(e.value)


# ── rotation ────────────────────────────────────────────────────────────

class TestRotate:
    def _ready(self, repo, monkeypatch):
        _with_services(repo)
        runner = WritingRunner()
        r = rt(repo, runner)
        restarted = []
        for mod, cls in (("noticeboard_host", "NoticeboardHost"), ("voice_host", "VoiceHost")):
            monkeypatch.setattr(getattr(art._sibling(mod), cls), "up",
                                lambda self, m=mod: restarted.append(m) or True)
        r.up("alpha", load_model=False)
        runner.files[("logos-home-alpha", ".env")] = _written(runner, ".env")
        restarted.clear()
        return r, runner, restarted

    def test_a_placeholder_secret_changes_live(self, repo, monkeypatch):
        r, runner, restarted = self._ready(repo, monkeypatch)
        before = sec.load(r.env, "alpha")["LOGOS_NOTICEBOARD_TOKEN"]
        creates = len(runner.matching("openshell", "sandbox", "create"))
        out = []
        art._sibling("secret_rotation").Rotator(r, say=out.append).rotate("alpha", "noticeboard")
        after = sec.load(r.env, "alpha")["LOGOS_NOTICEBOARD_TOKEN"]
        assert after != before
        argv, env = runner.env_calls[-1]
        assert argv == ["openshell", "provider", "update", "logos-alpha-noticeboard",
                        "--credential", "LOGOS_NOTICEBOARD_TOKEN", "--wait"] and env == {"LOGOS_NOTICEBOARD_TOKEN": after}
        assert restarted == ["noticeboard_host"]                                  # only its service
        assert len(runner.matching("openshell", "sandbox", "create")) == creates  # the agent kept running
        assert not any(after in line or before in line for line in out)

    def test_a_dotenv_secret_recreates_the_agent(self, repo, monkeypatch):
        (repo / "agents" / "alpha.local.yaml").write_text("credential_placeholders: false\n")
        r, runner, restarted = self._ready(repo, monkeypatch)
        before = _env(_written(runner, ".env"))["LOGOS_VOICE_TOKEN"]
        creates = len(runner.matching("openshell", "sandbox", "create"))
        art._sibling("secret_rotation").Rotator(r, say=lambda s: None).rotate("alpha", "voice")
        assert _env(_written(runner, ".env"))["LOGOS_VOICE_TOKEN"] != before
        assert len(runner.matching("openshell", "sandbox", "create")) == creates + 1

    def test_unknown(self, repo, monkeypatch):
        r, _runner, _ = self._ready(repo, monkeypatch)
        rot = art._sibling("secret_rotation")
        with pytest.raises(rot.RotateError):
            rot.Rotator(r, say=lambda s: None).rotate("alpha", "house")       # it has no house token
        with pytest.raises(rot.RotateError):
            rot.Rotator(r, say=lambda s: None).rotate("nobody", "voice")

    def test_status_names_places_not_values(self, repo, monkeypatch):
        r, _runner, _ = self._ready(repo, monkeypatch)
        rows = dict(art._sibling("secret_rotation").Rotator(r, say=lambda s: None).status("alpha"))
        assert rows["noticeboard"].startswith("placeholder") and rows["api"].startswith(".env")
        assert rows["house"] == "-"
        values = set(sec.load(r.env, "alpha").values())
        assert not any(v in str(rows) for v in values)


def test_matrix_relogin_keeps_the_device():
    mh = art._sibling("matrix_host")

    class Runner:
        def http(self, url, data=None, headers=None, timeout=10, **kw):
            import json
            self.body = json.loads(data)
            return json.dumps({"access_token": "new", "device_id": self.body["device_id"]}).encode()

    class Rt:
        env = {}
        runner = Runner()

    host = mh.MatrixHost(Rt())
    assert host.relogin("@dev-agent:home.test", "pw", "DEVICE1") == "new"
    assert Rt.runner.body["device_id"] == "DEVICE1" and Rt.runner.body["identifier"]["user"] == "@dev-agent:home.test"
    with pytest.raises(mh.MatrixError):
        host.relogin("@dev-agent:home.test", "", "DEVICE1")


def test_the_alert_key_never_signs_when_empty(tmp_path):
    at = art._sibling("alert_text")
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    (tmp_path / "logos").mkdir()
    at.key_path(env).write_text("\n")
    assert at.valid("alpha", "anything", env) is False
    with pytest.raises(RuntimeError):
        at.token_for("alpha", env)


def test_the_alert_token_may_come_in_the_header():
    at = art._sibling("alert_text")

    class Req:
        headers = {"Authorization": "Bearer abc"}
    assert at.request_token(Req(), {"token": "body"}) == "abc"
    Req.headers = {}
    assert at.request_token(Req(), {"token": "body"}) == "body"
