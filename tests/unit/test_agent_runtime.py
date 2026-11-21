"""Unit tests for gateway/agent_runtime.py and OpenShellBackend (S2.6).

Everything external goes through ``FakeRunner``: no test here runs the real
``docker`` or ``openshell`` binaries or makes an HTTP call.
"""

from __future__ import annotations

import importlib.util
import json

import yaml
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from gateway import agent_runtime as art
from gateway import sandbox_backend as sb
from gateway.agent_runtime import AgentRuntime, AgentRuntimeError, Completed
from gateway.sandbox_backend import NullBackend, OpenShellBackend, SandboxBackend

REPO = Path(__file__).resolve().parents[2]

# `openshell sandbox list` as the CLI prints it on a terminal: bold header,
# coloured phase in the last column.
LISTING = (
    "\x1b[1mNAME\x1b[0m    \x1b[1mNAMESPACE\x1b[0m  \x1b[1mCREATED\x1b[0m              \x1b[1mPHASE\x1b[0m\n"
    "greg    openshell  2026-11-15 09:12:44  \x1b[32mReady\x1b[0m\n"
    "bench   openshell  2026-11-15 21:03:10  \x1b[33mProvisioning\x1b[0m\n"
    "bere    openshell  2026-11-14 18:40:02  \x1b[2mStopped\x1b[0m\n"
    "broken  openshell  2026-11-13 07:00:00  \x1b[31mError\x1b[0m\n"
    "stray   openshell  2026-11-12 07:00:00  \x1b[32mReady\x1b[0m\n"
)

CRON_LIST = (
    "Scheduled jobs:\n"
    "  0123456789ab [active]\n"
    "    Name:      brain-commit\n"
    "  ba9876543210 [active]\n"
    "    Name:      consolidate\n"
)


class FakeRunner:
    """Records every call; answers from in-memory state."""

    def __init__(self, listing: str = "", volumes=(), files=None):
        self.listing = listing
        self.volumes = set(volumes)
        self.files = dict(files or {})       # (volume, rel) -> content
        self.calls: list[list[str]] = []
        self.inputs: list = []
        self.http_calls: list[tuple] = []
        self.http_response = b'{"data": []}'
        self.slept = 0.0
        self.after_create = "Ready"
        self.fail: dict[tuple, Completed] = {}

    # -- Runner protocol --
    def run(self, argv, *, input=None, text=True):
        argv = list(argv)
        self.calls.append(argv)
        self.inputs.append(input)
        for prefix, result in self.fail.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return result
        empty = "" if text else b""
        if argv[0] == "tar":
            return Completed(0, b"TARBYTES", b"")
        if argv[:3] == ["docker", "volume", "inspect"]:
            exists = argv[3] in self.volumes or any(k[0] == argv[3] for k in self.files)
            return Completed(0 if exists else 1, "", "")
        if argv[:3] == ["docker", "volume", "create"]:
            self.volumes.add(argv[-1])
            return Completed(0, "", "")
        if argv[:2] == ["docker", "run"]:
            if "cat" in argv and argv[-1].startswith("/v/"):      # get()
                vol = argv[argv.index("-v") + 1].split(":")[0]
                key = (vol, argv[-1][3:])
                return Completed(0, self.files[key], "") if key in self.files else Completed(1, "", "no such file")
            return Completed(0, empty, empty)
        if argv[:3] == ["openshell", "sandbox", "list"]:
            return Completed(0, self.listing, "")
        if argv[:3] == ["openshell", "sandbox", "delete"]:
            self.listing = "".join(l for l in self.listing.splitlines(True) if l.split()[0] != argv[3])
            return Completed(0, "", "")
        if argv[:3] == ["openshell", "sandbox", "create"]:
            name = argv[argv.index("--name") + 1]
            self.listing += f"{name}  openshell  now  \x1b[32m{self.after_create}\x1b[0m\n"
            return Completed(0, "", "")
        if argv[:3] == ["openshell", "sandbox", "exec"]:
            script = argv[-1]
            if "/health" in script:
                return Completed(0, "200", "")
            if "cron list" in script:
                return Completed(0, CRON_LIST, "")
            return Completed(0, "", "")
        if argv[:3] == ["docker", "network", "inspect"]:
            return Completed(0, "172.17.0.1\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    def call(self, argv):
        self.calls.append(list(argv))
        return 7

    def http(self, url, *, data=None, headers=None, timeout=10):
        self.http_calls.append((url, data, dict(headers or {}), timeout))
        return self.http_response

    def sleep(self, seconds):
        self.slept += seconds

    # -- helpers --
    def matching(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]


AGENT_YAML = """\
name: alpha
display_name: Alpha agent
image: logos-agent:0.23.1
soul: souls/general/soul.md
policy: infra/agent-image/policy.yaml
providers: [github]
timezone: Europe/London
model:
  provider: lmstudio
  base_url: http://10.0.0.5:1234/v1
  default: some/model
  context_length: 65536
consolidation:
  enabled: true
  schedule: "0 3 * * *"
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "alpha.yaml").write_text(AGENT_YAML)
    (tmp_path / "souls" / "general").mkdir(parents=True)
    (tmp_path / "souls" / "general" / "soul.md").write_text("You are Alpha.\n\n")
    (tmp_path / "souls" / "_shared").mkdir()
    (tmp_path / "souls" / "_shared" / "brain.md").write_text("Shared brain rules.\n")
    (tmp_path / "souls" / "_shared" / "judgement.md").write_text("Judgement rules.\n")
    (tmp_path / "infra" / "brain-template").mkdir(parents=True)
    jobs = tmp_path / "infra" / "agent-jobs"
    jobs.mkdir()
    (jobs / "brain_commit.sh").write_text("#!/bin/sh\n")
    (jobs / "brain_inbox_state.sh").write_text("#!/bin/sh\n")
    (jobs / "noticeboard_state.py").write_text("print('latest message 0')\n")
    (jobs / "noticeboard_check.md").write_text("Noticeboard check.\n")
    (jobs / "noticeboard_urgent.py").write_text("print('')\n")
    (jobs / "weekly_digest.md").write_text("Weekly digest.\n")
    (jobs / "briefing_today.py").write_text("print('Weather: dry.')\n")
    (jobs / "morning_briefing.md").write_text("Morning briefing.\n")
    (jobs / "briefing_check.py").write_text("print('')\n")
    (jobs / "carry_monitor_state.py").write_text("print('')\n")
    (jobs / "changes_facts.py").write_text("# facts\n")
    (jobs / "changes_check.py").write_text("# check\n")
    (jobs / "lists_mirror.py").write_text("# lists\n")
    (jobs / "changes_summary.md").write_text("Daily changes.\n")
    (jobs / "rerun_after_outage.py").write_text("print('')\n")
    (jobs / "model_watch.py").write_text("print('')\n")
    plugin = tmp_path / "infra" / "agent-plugins" / "logos-waitlist"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: logos-waitlist\n")
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    voice_plugin = tmp_path / "infra" / "agent-plugins" / "logos-voice"
    voice_plugin.mkdir(parents=True)
    (voice_plugin / "plugin.yaml").write_text("name: logos-voice\n")
    (voice_plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    vo_plugin = tmp_path / "infra" / "agent-plugins" / "logos-verified-only"
    vo_plugin.mkdir(parents=True)
    (vo_plugin / "plugin.yaml").write_text("name: logos-verified-only\n")
    (vo_plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    flat_plugin = tmp_path / "infra" / "agent-plugins" / "logos-matrix-flat"
    flat_plugin.mkdir(parents=True)
    (flat_plugin / "plugin.yaml").write_text("name: logos-matrix-flat\n")
    (flat_plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    (tmp_path / "souls" / "_shared" / "noticeboard.md").write_text("## The household noticeboard\n")
    (tmp_path / "souls" / "_shared" / "voice.md").write_text("# Voice notes\n")
    (jobs / "brain_index.py").write_text("# index\n")
    (jobs / "consolidate.md").write_text("Consolidate the inbox.\n")
    return tmp_path


def rt(repo, runner=None, env=None):
    # The machine's own settings (~/.config/logos: Docmost, the house, mail…) must not leak in: on the
    # server they exist, and tests that expect "not set up" failed there (21 Nov).
    return AgentRuntime(repo, runner or FakeRunner(),
                        env={"HOME": str(repo), "XDG_CONFIG_HOME": str(repo / ".test-config")} if env is None else env)


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    """Belt and braces: a test that reaches the real subprocess layer fails."""
    def boom(*a, **k):
        raise AssertionError("test tried to run a real process")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "call", boom)
    monkeypatch.setattr(art.urllib.request, "urlopen", boom)


# ── phase parsing ────────────────────────────────────────────────────────────

class TestPhases:
    def test_parse_strips_ansi_and_reads_first_and_last_column(self):
        phases = art.parse_phases(LISTING)
        assert phases["greg"] == "Ready"
        assert phases["bench"] == "Provisioning"
        assert phases["bere"] == "Stopped"
        assert phases["broken"] == "Error"

    def test_phase_of_each_sandbox(self, repo):
        r = rt(repo, FakeRunner(LISTING))
        assert r.phase("greg") == "Ready"
        assert r.phase("bench") == "Provisioning"
        assert r.phase("nobody") == "Missing"
        assert r.runner.calls[0] == ["openshell", "sandbox", "list"]

    def test_name_must_match_exactly(self, repo):
        assert rt(repo, FakeRunner(LISTING)).phase("gre") == "Missing"

    def test_empty_or_failed_listing_is_missing(self, repo):
        assert rt(repo, FakeRunner("")).phase("greg") == "Missing"
        runner = FakeRunner(LISTING)
        runner.fail[("openshell", "sandbox", "list")] = Completed(1, "", "gateway unreachable")
        assert rt(repo, runner).phase("greg") == "Missing"


# ── definitions ──────────────────────────────────────────────────────────────

class TestLoad:
    def test_loads_definition(self, repo):
        agent = rt(repo).load("alpha")
        assert agent["image"] == "logos-agent:0.23.1"
        assert "telegram" not in agent

    def test_local_yaml_is_deep_merged(self, repo):
        (repo / "agents" / "alpha.local.yaml").write_text(
            "matrix:\n  enabled: true\n  allowed_users: ['@a:x', '@b:x']\n"
            "model:\n  default: other/model\n"
            "providers: [anthropic]\n")
        agent = rt(repo).load("alpha")
        assert agent["matrix"] == {"enabled": True, "allowed_users": ['@a:x', '@b:x']}
        # nested mapping merged key by key: untouched keys survive
        assert agent["model"]["default"] == "other/model"
        assert agent["model"]["base_url"] == "http://10.0.0.5:1234/v1"
        # non-mapping values are replaced, not appended
        assert agent["providers"] == ["anthropic"]
        assert agent["name"] == "alpha"

    def test_empty_local_yaml_changes_nothing(self, repo):
        before = rt(repo).load("alpha")
        (repo / "agents" / "alpha.local.yaml").write_text("# nothing yet\n")
        assert rt(repo).load("alpha") == before

    def test_all_local_applies_to_every_agent_and_the_agents_own_file_wins(self, repo):
        (repo / "agents" / "all.local.yaml").write_text("model:\n  default: small/model\ntimezone: UTC\n")
        agent = rt(repo).load("alpha")
        assert agent["model"]["default"] == "small/model"
        assert agent["model"]["base_url"] == "http://10.0.0.5:1234/v1"      # the rest of the block is kept
        assert "all" not in rt(repo).defined() and "all.local" not in rt(repo).defined()
        (repo / "agents" / "alpha.local.yaml").write_text("timezone: Europe/Paris\n")
        assert rt(repo).load("alpha")["timezone"] == "Europe/Paris"

    def test_missing_definition(self, repo):
        with pytest.raises(AgentRuntimeError, match="no agent definition at .*nope.yaml"):
            rt(repo).load("nope")

    def test_defined_skips_local_files(self, repo):
        (repo / "agents" / "alpha.local.yaml").write_text("{}\n")
        (repo / "agents" / "beta.yaml").write_text("name: beta\n")
        assert rt(repo).defined() == ["alpha", "beta"]

    def test_merge_helper(self):
        base = {"a": {"x": 1, "y": 2}, "b": [1], "c": "keep"}
        art.merge(base, {"a": {"y": 3, "z": 4}, "b": [2], "d": {"new": True}})
        assert base == {"a": {"x": 1, "y": 3, "z": 4}, "b": [2], "c": "keep", "d": {"new": True}}


# ── volumes and files ────────────────────────────────────────────────────────

class TestVolumes:
    def test_names(self):
        assert art.volumes("alpha") == ("logos-home-alpha", "logos-brain-alpha")

    def test_fresh_volume_is_created_labelled_and_chowned(self, repo):
        runner = FakeRunner()
        rt(repo, runner).ensure_volume("logos-home-alpha")
        assert runner.calls == [
            ["docker", "volume", "inspect", "logos-home-alpha"],
            ["docker", "volume", "create",
             "--label", "openshell.ai/sandbox-attachable=true",
             "--label", "openshell.ai/sandbox-attachable-workspace=default", "logos-home-alpha"],
            ["docker", "run", "--rm", "-v", "logos-home-alpha:/v", "alpine:3", "chown", "10000:10000", "/v"],
        ]

    def test_existing_volume_is_left_alone(self, repo):
        runner = FakeRunner(volumes=["logos-home-alpha"])
        rt(repo, runner).ensure_volume("logos-home-alpha")
        assert runner.calls == [["docker", "volume", "inspect", "logos-home-alpha"]]

    def test_vault_seeding_pipes_template_into_git_helper(self, repo):
        runner = FakeRunner()
        rt(repo, runner).ensure_vault("logos-brain-alpha")
        tar, seed = runner.calls
        assert tar == ["tar", "-C", str(repo / "infra" / "brain-template"), "-c", "."]
        assert seed[:9] == ["docker", "run", "--rm", "-i", "--entrypoint", "sh",
                            "-v", "logos-brain-alpha:/v", "alpine/git:latest"]
        assert runner.inputs[1] == b"TARBYTES"
        script = seed[-1]
        assert '[ -e "/v/$f" ] ||' in script          # never overwrites
        assert "git init -q -b main ." in script
        assert "chown -R 10000:10000 /v" in script

    def test_vault_seeding_failure(self, repo):
        runner = FakeRunner()
        runner.fail[("docker", "run")] = Completed(1, b"", b"no space left")
        with pytest.raises(AgentRuntimeError, match="could not seed the brain volume:\nno space left"):
            rt(repo, runner).ensure_vault("logos-brain-alpha")

    def test_put_writes_through_helper(self, repo):
        runner = FakeRunner()
        rt(repo, runner).put("logos-home-alpha", ".env", "A=1\n", mode="0600")
        (cmd,) = runner.calls
        assert cmd[:8] == ["docker", "run", "--rm", "-i", "-v", "logos-home-alpha:/v", "alpine:3", "sh"]
        assert "cat > /v/.env" in cmd[-1] and "chmod 0600 /v/.env" in cmd[-1]
        assert runner.inputs == ["A=1\n"]

    def test_failed_command_raises_with_ansi_stripped(self, repo):
        runner = FakeRunner()
        runner.fail[("docker", "run")] = Completed(125, "", "\x1b[31mdaemon down\x1b[0m")
        with pytest.raises(AgentRuntimeError) as e:
            rt(repo, runner).put("v", "f", "x")
        assert str(e.value) == "command failed: docker run --rm -i ...\ndaemon down"


# ── env file ─────────────────────────────────────────────────────────────────

def _env(text):
    return dict(l.split("=", 1) for l in text.splitlines())


class TestEnvFile:
    def test_fresh_env(self, repo):
        r = rt(repo)
        env = _env(r.env_file(r.load("alpha"), "logos-home-alpha"))
        assert env["TZ"] == env["HERMES_TIMEZONE"] == "Europe/London"
        assert env["GATEWAY_ALLOW_ALL_USERS"] == "false"
        assert len(env["API_SERVER_KEY"]) >= 32
        assert "TELEGRAM_BOT_TOKEN" not in env

    def test_existing_secrets_are_kept(self, repo):
        runner = FakeRunner(files={("logos-home-alpha", ".env"):
                                   "# comment\nAPI_SERVER_KEY=keepme\nANTHROPIC_API_KEY=sk-x\n"
                                   "TELEGRAM_BOT_TOKEN=old\nTELEGRAM_ALLOWED_USERS=1\nTELEGRAM_HOME_CHANNEL=1\n"})
        r = rt(repo, runner)
        text = r.env_file(r.load("alpha"), "logos-home-alpha")
        env = _env(text)
        assert env["API_SERVER_KEY"] == "keepme"
        assert env["ANTHROPIC_API_KEY"] == "sk-x"
        # Telegram is gone: whatever an older `up` stored for it is dropped, the token included
        assert not [k for k in env if k.startswith("TELEGRAM_")]
        assert text.splitlines() == sorted(text.splitlines())

# ── model ────────────────────────────────────────────────────────────────────

class TestEnsureModel:
    MODEL = {"provider": "lmstudio", "base_url": "http://10.0.0.5:1234/v1",
             "default": "some/model", "context_length": 65536}

    def test_other_providers_make_no_calls(self, repo):
        runner = FakeRunner()
        rt(repo, runner).ensure_model({**self.MODEL, "provider": "anthropic"})
        assert runner.http_calls == []

    def test_already_loaded(self, repo):
        runner = FakeRunner()
        runner.http_response = json.dumps({"data": [
            {"id": "some/model", "state": "loaded", "loaded_context_length": 65536}]}).encode()
        rt(repo, runner).ensure_model(self.MODEL)
        assert [c[0] for c in runner.http_calls] == ["http://10.0.0.5:1234/api/v0/models"]

    def test_ensure_model_accepts_a_larger_loaded_window(self, repo):
        runner = FakeRunner()
        runner.http_response = json.dumps({"data": [
            {"id": "some/model", "state": "loaded", "loaded_context_length": 131072}]}).encode()
        rt(repo, runner).ensure_model(self.MODEL)
        assert len(runner.http_calls) == 1

    def test_loads_when_missing(self, repo, capsys):
        runner = FakeRunner()
        rt(repo, runner).ensure_model(self.MODEL)
        url, data, headers, timeout = runner.http_calls[1]
        assert url == "http://10.0.0.5:1234/api/v1/models/load"
        assert json.loads(data) == {"model": "some/model", "context_length": 65536}
        assert headers == {"Content-Type": "application/json"} and timeout == 600
        assert "loaded some/model in LM Studio" in capsys.readouterr().out

    def test_unreachable_is_only_a_warning(self, repo, capsys):
        runner = FakeRunner()
        runner.http = lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
        rt(repo, runner).ensure_model(self.MODEL)
        assert "warning: could not check or load the model" in capsys.readouterr().err


# ── create / delete / up ─────────────────────────────────────────────────────

class TestCreate:
    def test_create_argv(self, repo):
        r = rt(repo)
        argv = r.create_argv("alpha", r.load("alpha"))
        mounts = argv[argv.index("--driver-config-json") + 1]
        assert argv == [
            "openshell", "sandbox", "create", "--name", "alpha", "--no-tty", "--detach",
            "--restart-policy", "always",
            "--from", "logos-agent:0.23.1",
            "--policy", str(repo / "infra/agent-image/policy.yaml"),
            "--driver-config-json", mounts,
            "--provider", "github",
            "--cpu", "2", "--memory", "2Gi",
            "--", "sh", "-c", art.START,
        ]
        small = r.create_argv("alpha", {**r.load("alpha"), "resources": {"memory": "1Gi"}})
        assert small[small.index("--cpu") + 1] == "2" and small[small.index("--memory") + 1] == "1Gi"
        assert json.loads(mounts) == {"docker": {"mounts": [
            {"type": "volume", "source": "logos-home-alpha", "target": "/opt/data", "read_only": False},
            {"type": "volume", "source": "logos-brain-alpha", "target": "/opt/data/brain", "read_only": False},
        ]}}

    POLICY = """\
version: 1
network_policies:
  lm_studio:
    endpoints:
      - host: 192.0.2.117
        port: 1234
        protocol: rest
        allowed_ips: [192.0.2.117/32]
    binaries:
      - path: /opt/hermes/tools/python-*/bin/python3*
"""

    def _with_policy(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY)
        return path

    def test_policy_is_rendered_for_the_agents_model_address(self, repo):
        self._with_policy(repo)
        r = rt(repo)
        agent = r.load("alpha")
        out = r.policy_path("alpha", agent)
        assert out == repo / ".rendered" / "alpha.policy.yaml"
        rendered = yaml.safe_load(out.read_text())["network_policies"]
        assert rendered["lm_studio"]["endpoints"] == [
            {"host": "10.0.0.5", "port": 1234, "protocol": "rest", "allowed_ips": ["10.0.0.5/32"]}]
        assert rendered["lm_studio"]["binaries"] == [{"path": "/opt/hermes/tools/python-*/bin/python3*"}]
        assert "telegram" not in rendered
        assert str(out) in r.create_argv("alpha", agent)

    def test_policy_is_used_as_is_when_the_address_already_matches(self, repo):
        src = self._with_policy(repo)
        r = rt(repo)
        agent = r.load("alpha")
        agent["model"]["base_url"] = "http://192.0.2.117:1234/v1"
        assert r.policy_path("alpha", agent) == src
        assert not (repo / ".rendered").exists()

    def test_policy_for_a_hostname_drops_the_ip_pin(self, repo):
        self._with_policy(repo)
        r = rt(repo)
        agent = r.load("alpha")
        agent["model"]["base_url"] = "https://models.example.internal/v1"
        ep = yaml.safe_load(r.policy_path("alpha", agent).read_text())["network_policies"]["lm_studio"]["endpoints"][0]
        assert ep == {"host": "models.example.internal", "port": 443, "protocol": "rest"}

    def test_gateway_mode_points_hermes_and_the_policy_at_the_gateway(self, repo):
        self._with_policy(repo)
        r = rt(repo)
        agent = r.load("alpha")
        agent["model"]["gateway"] = True
        cfg = yaml.safe_load(r.config_text("alpha", agent))
        assert cfg["model"] == {"default": "lm-studio/some/model", "provider": "custom",
                                "base_url": "http://172.17.0.1:8089/v1", "key_env": "LOGOS_MODEL_KEY",
                                "context_length": 65536}
        ep = yaml.safe_load(r.policy_path("alpha", agent).read_text())["network_policies"]["lm_studio"]["endpoints"][0]
        assert (ep["host"], ep["port"], ep["allowed_ips"]) == ("172.17.0.1", 8089, ["172.17.0.1/32"])
        assert "access" not in ep and ep["rules"] == [
            {"allow": {"method": "GET", "path": "/v1/**"}}, {"allow": {"method": "POST", "path": "/v1/**"}}]
        env = r.env_file(agent, "logos-home-alpha")
        assert "LOGOS_MODEL_KEY=sk-bf-" in env
        assert "LOGOS_MODEL_KEY" not in r.env_file(r.load("alpha"), "logos-home-alpha")

    def test_chat_settings_for_matrix(self, repo):
        r = rt(repo)
        agent = {**r.load("alpha"), "matrix": {"enabled": True}}
        cfg = yaml.safe_load(r.config_text("alpha", agent))
        assert cfg["platforms"] == {"matrix": {"gateway_restart_notification": False,
                                              "extra": {"cron_continuable_surface": "in_channel"}}}  # no "Hermes —" threads
        assert cfg["display"] == {"platforms": {
            "matrix": {"tool_progress": "log", "interim_assistant_messages": False}},   # answers only
            "busy_input_mode": "queue"}                       # a second message waits, never cuts the first off
        assert "platforms" not in yaml.safe_load(r.config_text("alpha", r.load("alpha")))

    def test_telegram_is_refused(self, repo):
        (repo / "agents" / "alpha.local.yaml").write_text("telegram:\n  enabled: true\n")
        with pytest.raises(AgentRuntimeError, match="Telegram is no longer supported"):
            rt(repo).up("alpha")

    POLICY_NB = POLICY + """\
  noticeboard:
    endpoints:
      - host: 192.0.2.1
        port: 8090
        allowed_ips: [192.0.2.1/32]
"""

    def test_noticeboard_token_config_and_policy(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_NB)
        r = rt(repo)
        plain = r.load("alpha")
        # not on the noticeboard: no token, no MCP server, and the rule is removed
        assert "LOGOS_NOTICEBOARD_TOKEN" not in r.env_file(plain, "logos-home-alpha")
        assert "mcp_servers" not in yaml.safe_load(r.config_text("alpha", plain, "tok"))
        assert "noticeboard" not in yaml.safe_load(r.policy_path("alpha", plain).read_text())["network_policies"]
        on = {**plain, "noticeboard": True}
        assert "LOGOS_NOTICEBOARD_TOKEN=" in r.env_file(on, "logos-home-alpha")
        cfg = yaml.safe_load(r.config_text("alpha", on, "tok"))
        assert cfg["mcp_servers"] == {"noticeboard": {
            "url": "http://172.17.0.1:8090/mcp", "headers": {"Authorization": "Bearer tok"}}}
        ep = yaml.safe_load(r.policy_path("alpha", on).read_text())["network_policies"]["noticeboard"]["endpoints"][0]
        assert (ep["host"], ep["port"], ep["allowed_ips"]) == ("172.17.0.1", 8090, ["172.17.0.1/32"])

    POLICY_WEATHER = """
version: 1
network_policies:
  lm_studio:
    endpoints:
      - {host: 10.0.0.5, port: 1234, allowed_ips: [10.0.0.5/32]}
  web:
    endpoints:
      - {host: 192.0.2.1, port: 8096, protocol: mcp, allowed_ips: [192.0.2.1/32]}
  weather:
    endpoints:
      - host: 192.0.2.1
        port: 8097
        protocol: rest
        allowed_ips: [192.0.2.1/32]
        rules:
          - allow: {method: GET, path: /weather}
"""

    def test_weather_url_and_policy_only_with_the_web_reader(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_WEATHER)
        r = rt(repo)
        plain = r.load("alpha")
        assert "LOGOS_WEATHER_URL" not in r.env_file(plain, "logos-home-alpha")
        rules = yaml.safe_load(r.policy_path("alpha", plain).read_text())["network_policies"]
        assert "weather" not in rules and "web" not in rules
        on = {**plain, "web": {"enabled": True}}
        assert "LOGOS_WEATHER_URL=http://172.17.0.1:8097/weather" in r.env_file(on, "logos-home-alpha")
        ep = yaml.safe_load(r.policy_path("alpha", on).read_text())["network_policies"]["weather"]["endpoints"][0]
        assert (ep["host"], ep["port"], ep["allowed_ips"]) == ("172.17.0.1", 8097, ["172.17.0.1/32"])
        assert ep["rules"] == [{"allow": {"method": "GET", "path": "/weather"}}]

    POLICY_WEB = """
version: 1
network_policies:
  lm_studio:
    endpoints:
      - {host: 10.0.0.5, port: 1234, allowed_ips: [10.0.0.5/32]}
  web:
    endpoints:
      - host: 192.0.2.1
        port: 8096
        protocol: mcp
        allowed_ips: [192.0.2.1/32]
        rules:
          - allow: {method: tools/list}
          - allow: {method: tools/call, tool: {any: [search_web, add_private_name, file_request]}}
  weather:
    endpoints:
      - {host: 192.0.2.1, port: 8097, protocol: rest, allowed_ips: [192.0.2.1/32]}
"""

    def test_household_reaches_the_reader_for_file_request_only(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_WEB)
        (repo / "souls/_shared/requests.md").write_text("## Noting feature requests\n")
        (repo / "souls/_shared/changes.md").write_text("## What changed\n")
        r = rt(repo)
        house = {**r.load("alpha"), "role": "household", "changes": {"enabled": True}}
        rules = yaml.safe_load(r.policy_path("alpha", house).read_text())["network_policies"]
        assert "weather" not in rules
        assert rules["web"]["endpoints"][0]["rules"][-1] == {"allow": {"method": "tools/call",
                                                                       "tool": {"any": ["file_request", "recent_changes",
                                                                       "search_docs", "read_doc", "list_docs"]}}}
        assert [x["allow"]["method"] for x in rules["web"]["endpoints"][0]["rules"]].count("tools/call") == 1
        cfg = yaml.safe_load(r.config_text("alpha", house, changes_token="th"))
        assert cfg["mcp_servers"]["logos_web"]["headers"] == {"Authorization": "Bearer th"}
        assert "Noting feature requests" in r.soul_text(house) and "## What changed" in r.soul_text(house)
        person = {**r.load("alpha"), "web": {"enabled": True}}
        rules = yaml.safe_load(r.policy_path("alpha", person).read_text())["network_policies"]
        assert rules["web"]["endpoints"][0]["rules"][1]["allow"]["tool"]["any"] == [
            "search_web", "add_private_name", "file_request"]
        plain = r.load("alpha")
        assert "web" not in yaml.safe_load(r.policy_path("alpha", plain).read_text())["network_policies"]
        assert "Noting feature requests" not in r.soul_text(plain)

    def test_docs_instructions_only_where_docs_are_set_up(self, repo):
        (repo / "souls/_shared/docs.md").write_text("## The homelab docs\n")
        house = {**rt(repo).load("alpha"), "role": "household", "changes": {"enabled": True}}
        assert "homelab docs" not in rt(repo).soul_text(house)              # no Docmost read roots here
        cfgdir = repo / "xdg" / "logos"
        cfgdir.mkdir(parents=True)
        (cfgdir / "docmost").write_text("DOCMOST_URL=https://docs.example\nDOCMOST_READ_ROOTS=r1\n")
        r = rt(repo, env={"XDG_CONFIG_HOME": str(repo / "xdg")})
        assert "## The homelab docs" in r.soul_text(house)
        assert "homelab docs" not in r.soul_text(r.load("alpha"))            # no reader, no docs

    POLICY_HOUSE = """
version: 1
network_policies:
  lm_studio:
    endpoints:
      - {host: 10.0.0.5, port: 1234, allowed_ips: [10.0.0.5/32]}
  house:
    endpoints:
      - host: 192.0.2.1
        port: 8098
        protocol: mcp
        allowed_ips: [192.0.2.1/32]
        rules:
          - allow: {method: tools/list}
          - allow: {method: tools/call, tool: {any: [list_lights, set_lights, list_scenes, activate_scene]}}
"""

    POLICY_MAIL = """
version: 1
network_policies:
  lm_studio:
    endpoints:
      - {host: 10.0.0.5, port: 1234, allowed_ips: [10.0.0.5/32]}
  mail:
    endpoints:
      - host: 192.0.2.1
        port: 8099
        protocol: mcp
        allowed_ips: [192.0.2.1/32]
        rules:
          - allow: {method: tools/list}
"""

    def test_mail_only_for_an_agent_with_a_person(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_MAIL)
        (repo / "souls/_shared/mail.md").write_text("## Your person's email\n")
        r = rt(repo)
        base = r.load("alpha")
        for agent in (base, {**base, "mail": {"enabled": True}}, {**base, "mail": {"enabled": False, "person": "greg"}}):
            assert "LOGOS_MAIL_TOKEN" not in r.env_file(agent, "logos-home-alpha")
            assert "mail" not in (yaml.safe_load(r.config_text("alpha", agent, mail_token="tm")).get("mcp_servers") or {})
            assert "mail" not in yaml.safe_load(r.policy_path("alpha", agent).read_text())["network_policies"]
            assert "person's email" not in r.soul_text(agent)
        greg = {**base, "mail": {"enabled": True, "person": "greg"}}
        env = r.env_file(greg, "logos-home-alpha")
        assert "LOGOS_MAIL_TOKEN=" in env and "LOGOS_MAIL_URL=http://172.17.0.1:8099/mcp" in env
        cfg = yaml.safe_load(r.config_text("alpha", greg, mail_token="tm"))
        assert cfg["mcp_servers"]["mail"] == {"url": "http://172.17.0.1:8099/mcp", "headers": {"Authorization": "Bearer tm"}}
        ep = yaml.safe_load(r.policy_path("alpha", greg).read_text())["network_policies"]["mail"]["endpoints"][0]
        assert (ep["host"], ep["port"]) == ("172.17.0.1", 8099)
        assert ep["rules"][-1] == {"allow": {"method": "tools/call", "tool": {"any": [
            "mail_new", "mail_needs_reply", "mail_search", "mail_read"]}}}           # read only: nothing that sends
        assert "## Your person's email" in r.soul_text(greg)

    def test_house_only_when_the_definition_asks(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_HOUSE)
        (repo / "souls/_shared/house.md").write_text("## The lights\n")
        r = rt(repo)
        base = r.load("alpha")
        # Only with house on: the household agent, or a person's agent (Greg and Béré, 19 Nov).
        for agent in (base, {**base, "role": "household"}, {**base, "house": {"enabled": False}}):
            assert "LOGOS_HOUSE_TOKEN" not in r.env_file(agent, "logos-home-alpha")
            assert "house" not in (yaml.safe_load(r.config_text("alpha", agent, house_token="th")).get("mcp_servers") or {})
            assert "house" not in yaml.safe_load(r.policy_path("alpha", agent).read_text())["network_policies"]
            assert "The lights" not in r.soul_text(agent)
        person = {**base, "house": {"enabled": True}}
        assert "LOGOS_HOUSE_TOKEN=" in r.env_file(person, "logos-home-alpha") and "The lights" in r.soul_text(person)
        assert "About this home" not in r.soul_text(person)
        cfgdir = repo / "xdg" / "logos"
        cfgdir.mkdir(parents=True)
        (cfgdir / "house-notes.md").write_text("The bathroom lamp is often off at the wall.\n")
        noted = rt(repo, env={"XDG_CONFIG_HOME": str(repo / "xdg")})
        assert "### About this home's lights\n\nThe bathroom lamp is often off at the wall." in noted.soul_text(person)
        assert "About this home" not in noted.soul_text(base)
        house = {**base, "role": "household", "house": {"enabled": True}}
        assert "LOGOS_HOUSE_TOKEN=" in r.env_file(house, "logos-home-alpha")
        cfg = yaml.safe_load(r.config_text("alpha", house, house_token="th"))
        assert cfg["mcp_servers"]["house"] == {"url": "http://172.17.0.1:8098/mcp",
                                               "headers": {"Authorization": "Bearer th"}}
        assert "The lights" in r.soul_text(house)
        ep = yaml.safe_load(r.policy_path("alpha", house).read_text())["network_policies"]["house"]["endpoints"][0]
        assert (ep["host"], ep["port"], ep["allowed_ips"]) == ("172.17.0.1", 8098, ["172.17.0.1/32"])
        assert ep["rules"][-1] == {"allow": {"method": "tools/call", "tool": {"any": [
            "list_lights", "set_lights", "list_scenes", "activate_scene"]}}}
        # Lights only: the scene tools are not let through the sandbox rule either.
        lights = {**house, "house": {"enabled": True, "domains": ["light"]}}
        ep = yaml.safe_load(r.policy_path("alpha", lights).read_text())["network_policies"]["house"]["endpoints"][0]
        assert ep["rules"][-1]["allow"]["tool"] == {"any": ["list_lights", "set_lights"]}
        # The shared lists: the list tools, the house URL for the copy, the lists rules and the copy job.
        lists = {**house, "house": {"enabled": True, "domains": ["light", "scene", "todo"]}}
        ep = yaml.safe_load(r.policy_path("alpha", lists).read_text())["network_policies"]["house"]["endpoints"][0]
        assert ep["rules"][-1]["allow"]["tool"]["any"][-5:] == [
            "list_items", "add_items", "change_item", "tick_items", "remove_items"]
        assert "LOGOS_HOUSE_URL=http://172.17.0.1:8098/mcp" in r.env_file(lists, "logos-home-alpha")
        assert art.uses_lists(lists) and not art.uses_lists(house)

    def test_changes_token_url_and_policy_only_when_asked(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_WEATHER + """  changes:
    endpoints:
      - host: 192.0.2.1
        port: 8097
        protocol: rest
        allowed_ips: [192.0.2.1/32]
        rules:
          - allow: {method: GET, path: "/changes/**"}
""")
        r = rt(repo)
        plain = r.load("alpha")
        assert "LOGOS_CHANGES" not in r.env_file(plain, "logos-home-alpha")
        assert "changes" not in yaml.safe_load(r.policy_path("alpha", plain).read_text())["network_policies"]
        on = {**plain, "role": "household", "changes": {"enabled": True}}
        env = r.env_file(on, "logos-home-alpha")
        assert "LOGOS_CHANGES_URL=http://172.17.0.1:8097/changes" in env and "LOGOS_CHANGES_TOKEN=" in env
        assert "LOGOS_WEB_TOKEN" not in env                     # still no web search
        rules = yaml.safe_load(r.policy_path("alpha", on).read_text())["network_policies"]
        assert "weather" not in rules
        assert rules["web"]["endpoints"][0]["rules"][-1]["allow"]["tool"] == {"any": art.HOUSEHOLD_READER_TOOLS}
        ep = rules["changes"]["endpoints"][0]
        assert (ep["host"], ep["port"]) == ("172.17.0.1", 8097)
        assert ep["rules"] == [{"allow": {"method": "GET", "path": "/changes/**"}}]

    POLICY_HEALTH = """
version: 1
network_policies:
  lm_studio:
    endpoints:
      - {host: 192.0.2.117, port: 1234, allowed_ips: [192.0.2.117/32]}
  model_health:
    endpoints:
      - {host: 192.0.2.1, port: 8094, protocol: rest, allowed_ips: [192.0.2.1/32],
         rules: [{allow: {method: GET, path: /guard/health}}]}
"""

    def test_model_health_url_and_rule_only_behind_the_gateway(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_HEALTH)
        r = rt(repo)
        plain = r.load("alpha")
        assert "LOGOS_MODEL_HEALTH_URL" not in r.env_file(plain, "logos-home-alpha")
        assert "model_health" not in yaml.safe_load(r.policy_path("alpha", plain).read_text())["network_policies"]
        gw = {**plain, "model": {**plain["model"], "gateway": True}}
        assert "LOGOS_MODEL_HEALTH_URL=http://172.17.0.1:8094/guard/health" in r.env_file(gw, "logos-home-alpha")
        ep = yaml.safe_load(r.policy_path("alpha", gw).read_text())["network_policies"]["model_health"]["endpoints"][0]
        assert (ep["host"], ep["port"]) == ("172.17.0.1", 8094)
        assert ep["rules"] == [{"allow": {"method": "GET", "path": "/guard/health"}}]

    POLICY_VOICE = """
version: 1
network_policies:
  lm_studio:
    endpoints:
      - {host: 10.0.0.5, port: 1234, allowed_ips: [10.0.0.5/32]}
  voice:
    endpoints:
      - host: 192.0.2.1
        port: 8093
        protocol: rest
        allowed_ips: [192.0.2.1/32]
        rules:
          - allow: {method: POST, path: /v1/audio/transcriptions}
"""

    def test_voice_token_config_and_policy(self, repo):
        path = repo / "infra/agent-image/policy.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.POLICY_VOICE)
        r = rt(repo)
        plain = r.load("alpha")
        # no voice: no token, speech-to-text off, and the rule is removed
        assert "LOGOS_VOICE_TOKEN" not in r.env_file(plain, "logos-home-alpha")
        assert yaml.safe_load(r.config_text("alpha", plain, voice_token="tok"))["stt"] == {"enabled": False}
        assert "voice" not in yaml.safe_load(r.policy_path("alpha", plain).read_text())["network_policies"]
        on = {**plain, "voice": True}
        assert "LOGOS_VOICE_TOKEN=" in r.env_file(on, "logos-home-alpha")
        assert yaml.safe_load(r.config_text("alpha", on, voice_token="tok"))["stt"] == {
            "enabled": True, "provider": "openai", "echo_transcripts": False,      # no "I heard: ..." message
            "openai": {"api_key": "tok", "base_url": "http://172.17.0.1:8093/v1", "model": "whisper-1"}}
        echo = {**on, "voice_echo": True}                                    # the person sees what was heard
        assert yaml.safe_load(r.config_text("alpha", echo, voice_token="tok"))["stt"]["echo_transcripts"] is True
        ep = yaml.safe_load(r.policy_path("alpha", on).read_text())["network_policies"]["voice"]["endpoints"][0]
        assert (ep["host"], ep["port"], ep["allowed_ips"]) == ("172.17.0.1", 8093, ["172.17.0.1/32"])
        assert ep["rules"] == [{"allow": {"method": "POST", "path": "/v1/audio/transcriptions"}}]

    def test_get_never_creates_a_missing_volume(self, repo):
        runner = FakeRunner()
        assert rt(repo, runner).get("logos-home-nobody", ".env") == ""
        assert runner.matching("docker", "run") == []

    def test_noticeboard_job_and_soul(self, repo, capsys):
        runner = FakeRunner()
        r = rt(repo, runner)
        agent = {**r.load("alpha"), "noticeboard": True}
        assert "## The household noticeboard" in r.soul_text(agent)
        assert "household noticeboard" not in r.soul_text(r.load("alpha"))
        assert "# Voice notes" in r.soul_text({**r.load("alpha"), "voice": True})
        assert "# Voice notes" not in r.soul_text(r.load("alpha"))
        assert "LOGOS_NOTICEBOARD_URL=http://172.17.0.1:8090/mcp" in r.env_file(agent, "logos-home-alpha")
        r.ensure_jobs("alpha", {**agent, "matrix": {"enabled": True}}, "logos-home-alpha")
        created = [c[-1] for c in runner.matching("openshell", "sandbox", "exec") if "cron create" in c[-1]]
        (job,) = [c for c in created if "noticeboard-check" in c]
        assert "--monitor-script noticeboard_state.py" in job and "--deliver matrix" in job
        assert '"*/10 * * * *"' in job
        (digest,) = [c for c in created if "weekly-digest" in c]
        assert '"0 18 * * 0"' in digest and "--deliver matrix" in digest
        (urgent,) = [c for c in created if "noticeboard-urgent" in c]
        assert "--no-agent --script noticeboard_urgent.py" in urgent and "--deliver matrix" in urgent
        assert "LOGOS_NOTICEBOARD_POLL_URL=http://172.17.0.1:8092" in r.env_file(agent, "logos-home-alpha")

    def test_no_weekly_digest_without_matrix_or_when_disabled(self, repo):
        for agent_extra in ({}, {"matrix": {"enabled": True}, "digest": {"enabled": False}}):
            runner = FakeRunner()
            r = rt(repo, runner)
            r.ensure_jobs("alpha", {**r.load("alpha"), **agent_extra}, "logos-home-alpha")
            assert not [c for c in runner.matching("openshell", "sandbox", "exec") if "weekly-digest" in c[-1]]

    def test_morning_briefing_jobs(self, repo):
        runner = FakeRunner()
        r = rt(repo, runner)
        agent = {**r.load("alpha"), "matrix": {"enabled": True}, "calendar": {"enabled": True, "person": "greg"}}
        r.ensure_jobs("alpha", agent, "logos-home-alpha")
        created = [c[-1] for c in runner.matching("openshell", "sandbox", "exec") if "cron create" in c[-1]]
        (weekday,) = [c for c in created if "--name morning-briefing " in c]
        (weekend,) = [c for c in created if "--name morning-briefing-weekend " in c]
        assert '"0 7 * * 1-5"' in weekday and '"0 9 * * 0,6"' in weekend
        for job in (weekday, weekend):
            assert "--script briefing_today.py" in job and "--no-agent" not in job
            assert "--catch-up off" in job and "--deliver matrix" in job and "--failure-deliver local" in job
        (check,) = [c for c in created if "--name morning-briefing-check " in c]
        (wcheck,) = [c for c in created if "--name morning-briefing-weekend-check " in c]
        assert '"15 7 * * 1-5"' in check and '"15 9 * * 0,6"' in wcheck
        for job in (check, wcheck):
            assert "--no-agent --script briefing_check.py" in job and "--deliver matrix" in job
        assert art.minutes_later("50 23 * * *") is None and art.minutes_later("*/5 7 * * *") is None
        assert art.minutes_later("55 6 * * 1-5") == "10 7 * * 1-5"
        own = {**agent, "briefing": {"weekdays": "30 6 * * 1-5"}}
        assert art.briefing(own) == {"weekdays": "30 6 * * 1-5", "weekends": "0 9 * * 0,6"}

    def test_daily_changes_jobs(self, repo):
        runner = FakeRunner()
        r = rt(repo, runner)
        agent = {**r.load("alpha"), "role": "household", "matrix": {"enabled": True}, "changes": {"enabled": True}}
        r.ensure_jobs("alpha", agent, "logos-home-alpha")
        cmds = [c[-1] for c in runner.matching("openshell", "sandbox", "exec")]
        created = [c for c in cmds if "cron create" in c]
        want = {"changes-daily": ("changes_daily.py", '"0 16 * * *"'),
                "changes-week": ("changes_week.py", '"10 16 * * 0"')}
        for job, (script, when) in want.items():
            (c,) = [c for c in created if f"--name {job} " in c]
            assert f"--script {script}" in c and when in c and "--no-agent" not in c
            assert "--catch-up off" in c and "--deliver matrix" in c and "--failure-deliver local" in c
        (check,) = [c for c in created if "--name changes-check " in c]
        assert "--no-agent --script changes_check.py" in check and '"20 16 * * *"' in check
        assert set(want) | {"changes-check"} <= set(art.MANAGED_JOBS)
        # The four retired jobs are still managed, so `up` removes them from existing sandboxes.
        assert {"changes-homelab", "changes-logos", "changes-homelab-week", "changes-logos-week"} <= set(art.MANAGED_JOBS)
        assert not [c for c in created if "changes-homelab" in c or "changes-logos" in c]
        assert art.changes_jobs({**agent, "matrix": {}}) == []             # nowhere to post: no jobs
        assert art.changes_jobs({**agent, "changes": {}}) == []
        assert [j for j, _, _ in art.changes_jobs({**agent, "changes": {"enabled": True, "weekly": False}})] == [
            "changes-daily", "changes-check"]

    def test_monitor_state_is_carried_across_job_recreation(self, repo):
        runner = FakeRunner()
        r = rt(repo, runner)
        r.ensure_jobs("alpha", {**r.load("alpha"), "noticeboard": True}, "logos-home-alpha")
        cmds = [c[-1] for c in runner.matching("openshell", "sandbox", "exec")]
        save = next(i for i, c in enumerate(cmds) if "carry_monitor_state.py save" in c)
        restore = next(i for i, c in enumerate(cmds) if "carry_monitor_state.py restore" in c)
        created = [i for i, c in enumerate(cmds) if "cron create" in c]
        assert save < min(created) and restore > max(created)
        assert "/opt/hermes/.venv/bin/python" in cmds[save]

    def test_outage_jobs(self, repo):
        for extra, rerun, watch in (({}, False, False),
                                    ({"model": {"gateway": True}}, True, False),
                                    ({"model": {"gateway": True}, "role": "household", "matrix": {"enabled": True}}, True, True),
                                    ({"model": {"gateway": True}, "role": "household"}, True, False)):
            runner = FakeRunner()
            r = rt(repo, runner)
            agent = {**r.load("alpha"), **extra}
            agent["model"] = {**r.load("alpha")["model"], **extra.get("model", {})}
            r.ensure_jobs("alpha", agent, "logos-home-alpha")
            created = [c[-1] for c in runner.matching("openshell", "sandbox", "exec") if "cron create" in c[-1]]
            reruns = [c for c in created if "--name rerun-after-outage " in c]
            watches = [c for c in created if "--name model-watch " in c]
            assert bool(reruns) == rerun and bool(watches) == watch, extra
            if reruns:
                assert "--no-agent --script rerun_after_outage.py" in reruns[0] and "--deliver local" in reruns[0]
            if watches:
                assert '"* * * * *"' in watches[0] and "--deliver matrix" in watches[0]

    def test_no_briefing_for_household_without_matrix_or_person_or_when_off(self, repo):
        person = {"calendar": {"enabled": True, "person": "greg"}, "matrix": {"enabled": True}}
        for extra in ({**person, "role": "household"}, {"calendar": person["calendar"]},
                      {"matrix": {"enabled": True}}, {**person, "briefing": {"enabled": False}}):
            runner = FakeRunner()
            r = rt(repo, runner)
            r.ensure_jobs("alpha", {**r.load("alpha"), **extra}, "logos-home-alpha")
            assert not [c for c in runner.matching("openshell", "sandbox", "exec") if "morning-briefing" in c[-1]]

    def test_no_providers_means_no_provider_flags(self, repo):
        r = rt(repo)
        agent = {**r.load("alpha"), "providers": []}
        assert "--provider" not in r.create_argv("alpha", agent)

    def test_config_text(self, repo):
        r = rt(repo)
        text = r.config_text("alpha", r.load("alpha"))
        assert text.startswith("# Written by scripts/logos-agent; edit agents/alpha.yaml instead.\n")
        assert art.yaml.safe_load(text) == {
            "_config_version": 53, "timezone": "Europe/London",
            "model": {"default": "some/model", "provider": "lmstudio",
                      "base_url": "http://10.0.0.5:1234/v1", "context_length": 65536},
            "agent": {"auto_recovery_cycles": 0, "disabled_toolsets": ["web", "browser"]},
            "tools": {"tool_search": {"enabled": "off"}},
            "context_file_max_chars": 40000,                                   # SOUL.md is never cut
            "cron": {"wrap_response": False, "mirror_delivery": True},         # no header; agent sees its own jobs
            "updates": {"check": False}, "model_catalog": {"enabled": False},   # no calling home
            "memory": {"memory_char_limit": 6000},
            "plugins": {"enabled": ["logos-waitlist", "logos-voice", "logos-verified-only", "logos-matrix-flat"]},
            "stt": {"enabled": False}}

    def test_config_text_session_reset_enables_the_plugin(self, repo):
        r = rt(repo)
        agent = r.load("alpha")
        agent["session_reset"] = {"mode": "daily", "at_hour": 3}
        cfg = art.yaml.safe_load(r.config_text("alpha", agent))
        assert cfg["plugins"] == {"enabled": ["logos-waitlist", "logos-voice", "logos-verified-only", "logos-matrix-flat", "hermes-session-reset-policy"]}
        assert cfg["session_reset"] == {"mode": "daily", "at_hour": 3}
        agent["session_reset"] = {"mode": "none"}
        assert art.yaml.safe_load(r.config_text("alpha", agent))["plugins"] == {"enabled": ["logos-waitlist", "logos-voice", "logos-verified-only", "logos-matrix-flat"]}

    def test_config_text_without_brain_index_keeps_default_memory_limit(self, repo):
        r = rt(repo)
        agent = r.load("alpha")
        agent["brain_index"] = False
        assert "memory" not in art.yaml.safe_load(r.config_text("alpha", agent))

    def test_soul_is_persona_plus_shared_rules(self, repo):
        r = rt(repo)
        assert r.soul_text(r.load("alpha")) == "You are Alpha.\n\nShared brain rules.\n\nJudgement rules.\n"

    def test_up_runs_the_whole_sequence(self, repo, capsys):
        runner = FakeRunner()
        r = rt(repo, runner)
        assert r.up("alpha") == 0
        assert runner.volumes == {"logos-home-alpha", "logos-brain-alpha"}
        (create,) = runner.matching("openshell", "sandbox", "create")
        assert create == r.create_argv("alpha", r.load("alpha"))
        # nothing existed, so nothing was deleted first
        assert runner.matching("openshell", "sandbox", "delete") == []
        # config, Logos' plugins, soul and env were written to the home volume before the sandbox was created
        written = [c[-1] for c in runner.calls[:runner.calls.index(create)]
                   if c[:2] == ["docker", "run"] and "cat > /v/" in c[-1]]
        assert [w.split("cat > /v/")[1].split(" ")[0] for w in written] == [
            ".env", "config.yaml", "plugins/logos-waitlist/__init__.py", "plugins/logos-waitlist/plugin.yaml",
            "plugins/logos-voice/__init__.py", "plugins/logos-voice/plugin.yaml",
            "plugins/logos-verified-only/__init__.py", "plugins/logos-verified-only/plugin.yaml",
            "plugins/logos-matrix-flat/__init__.py", "plugins/logos-matrix-flat/plugin.yaml", "SOUL.md"]
        out = capsys.readouterr().out
        assert "alpha: Ready" in out
        assert "jobs: brain-commit, consolidate" in out

    def test_up_starts_the_web_reader_for_a_changes_only_agent(self, repo, monkeypatch):
        # DC1: the household agent has no web search but needs the reader for /changes, and the
        # reader must start after the agent's .env (with its changes token) is written.
        web_host = art._sibling("web_host")          # the copy the runtime itself uses
        runner = FakeRunner()
        r = rt(repo, runner)
        started = []
        monkeypatch.setattr(web_host.WebHost, "up", lambda self: started.append(len(runner.calls)) or False)
        (repo / "agents" / "alpha.yaml").write_text((repo / "agents" / "alpha.yaml").read_text()
                                                    + "changes:\n  enabled: true\n")
        assert r.up("alpha") == 0
        env_written = next(i for i, c in enumerate(runner.calls) if c[:2] == ["docker", "run"] and "cat > /v/.env" in c[-1])
        assert started and started[0] > env_written

    def test_up_replaces_an_existing_sandbox(self, repo):
        runner = FakeRunner("alpha  openshell  then  Ready\n", volumes=["logos-home-alpha", "logos-brain-alpha"])
        assert rt(repo, runner).up("alpha") == 0
        kinds = [c[2] for c in runner.calls if c[:2] == ["openshell", "sandbox"] and c[2] in ("delete", "create")]
        assert kinds == ["delete", "create"]
        assert runner.matching("docker", "volume", "create") == []

    def test_up_reports_error_phase(self, repo, capsys):
        runner = FakeRunner()
        runner.after_create = "Error"
        assert rt(repo, runner).up("alpha") == 1
        assert "alpha: Error" in capsys.readouterr().out
        # no jobs are installed into a sandbox that did not come up
        assert runner.matching("openshell", "sandbox", "exec") == []

    def test_up_gives_up_waiting_without_real_sleep(self, repo):
        runner = FakeRunner()
        runner.after_create = "Provisioning"
        assert rt(repo, runner).up("alpha") == 1
        assert runner.slept == 120

    def test_cron_jobs_installed(self, repo):
        runner = FakeRunner()
        r = rt(repo, runner)
        r.up("alpha")
        scripts = [c[-1] for c in runner.matching("openshell", "sandbox", "exec")]
        assert all(c[:7] == ["openshell", "sandbox", "exec", "--name", "alpha", "--no-tty", "--"]
                   for c in runner.matching("openshell", "sandbox", "exec"))
        # stale copies of our two jobs are removed, then both are created
        removed = [s for s in scripts if "cron remove" in s]
        assert [s.split()[-1] for s in removed] == ["0123456789ab", "ba9876543210"]
        (commit,) = [s for s in scripts if "--name brain-commit" in s]
        assert '--no-agent --script brain_commit.sh --timezone Europe/London --deliver local "0 * * * *"' in commit
        (idx,) = [s for s in scripts if "--name brain-index" in s]
        assert '--no-agent --script brain_index.py --timezone Europe/London --deliver local "50 * * * *"' in idx
        assert any("python /opt/data/scripts/brain_index.py" in s for s in scripts)   # run once at `up`
        (con,) = [s for s in scripts if "--name consolidate" in s]
        assert '--workdir /opt/data/brain --timezone Europe/London --continuity --deliver local "0 3 * * *" "$(cat)"' in con
        assert runner.inputs[runner.calls.index(next(c for c in runner.calls if c[-1] == con))] == "Consolidate the inbox.\n"

    def test_job_ids(self, repo):
        assert rt(repo).job_ids("alpha") == {"brain-commit": "0123456789ab", "consolidate": "ba9876543210"}


class TestDown:
    def test_deletes_and_waits(self, repo):
        runner = FakeRunner(LISTING)
        rt(repo, runner).down("greg")
        assert runner.matching("openshell", "sandbox", "delete") == [["openshell", "sandbox", "delete", "greg"]]
        assert "greg" not in art.parse_phases(runner.listing)
        assert runner.matching("docker") == []          # volumes are kept

    def test_missing_sandbox_is_a_no_op(self, repo):
        runner = FakeRunner(LISTING)
        rt(repo, runner).down("nobody")
        assert runner.matching("openshell", "sandbox", "delete") == []

    def test_delete_failure_raises(self, repo):
        runner = FakeRunner(LISTING)
        runner.fail[("openshell", "sandbox", "delete")] = Completed(1, "", "permission denied")
        with pytest.raises(AgentRuntimeError, match="permission denied"):
            rt(repo, runner).down("greg")


def test_exec_passes_command_through(repo):
    runner = FakeRunner()
    assert rt(repo, runner).exec("alpha", ["hermes", "--version"]) == 7
    assert runner.calls == [["openshell", "sandbox", "exec", "--name", "alpha", "--no-tty", "--", "hermes", "--version"]]


# ── OpenShellBackend ─────────────────────────────────────────────────────────

@pytest.fixture
def backend(repo):
    for n in ("greg", "bench", "bere", "broken", "ghost"):
        (repo / "agents" / f"{n}.yaml").write_text(AGENT_YAML.replace("alpha", n))
    return OpenShellBackend(rt(repo, FakeRunner(LISTING)))


class TestOpenShellBackend:
    def test_satisfies_protocol(self, backend):
        assert isinstance(backend, SandboxBackend)

    @pytest.mark.parametrize("phase,state", [
        ("Ready", "running"), ("Provisioning", "creating"), ("Stopped", "stopped"),
        ("Error", "error"), ("Missing", "missing"),
    ])
    def test_phase_mapping(self, phase, state):
        assert sb.state_for_phase(phase) == state

    def test_unknown_phase_is_error_with_detail(self, repo):
        b = OpenShellBackend(rt(repo, FakeRunner("alpha  ns  now  Hibernating\n")))
        st = b.status("alpha")
        assert st.state == "error" and "Hibernating" in st.detail

    @pytest.mark.parametrize("name,state", [
        ("greg", "running"), ("bench", "creating"), ("bere", "stopped"), ("broken", "error"),
    ])
    def test_status(self, backend, name, state):
        st = backend.status(name)
        assert (st.name, st.state) == (name, state)
        assert st.running == (state == "running")

    def test_status_of_missing_sandbox(self, backend):
        st = backend.status("ghost")
        assert st.state == "missing" and not st.running and st.url is None
        assert st.to_dict() == {"name": "ghost", "state": "missing", "detail": "Missing", "url": None}

    def test_status_never_raises(self, backend):
        backend.runtime.runner.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("openshell"))
        st = backend.status("greg")
        assert st.state == "error" and "openshell" in st.detail
        assert backend.list() == []

    def test_list_is_defined_agents_that_have_a_sandbox(self, backend):
        got = {st.name: st.state for st in backend.list()}
        # "stray" has no definition, "ghost" and "alpha" have no sandbox, the header row is not an agent
        assert got == {"bench": "creating", "bere": "stopped", "broken": "error", "greg": "running"}
        assert len(backend.runtime.runner.matching("openshell", "sandbox", "list")) == 1

    def test_create_uses_the_named_definition(self, backend, capsys):
        st = backend.create({"id": "agent_1", "name": "ghost"})
        assert (st.name, st.state) == ("ghost", "running")
        (create,) = backend.runtime.runner.matching("openshell", "sandbox", "create")
        assert create[create.index("--name") + 1] == "ghost"

    def test_create_without_definition_is_an_error_status(self, backend):
        st = backend.create({"name": "undefined"})
        assert st.state == "error" and "no agent definition" in st.detail
        assert backend.runtime.runner.matching("openshell", "sandbox", "create") == []

    @pytest.mark.parametrize("bad", ["", "../etc", "Has Space", "a.local", "UPPER"])
    def test_unsafe_names_never_reach_a_command(self, backend, bad):
        assert backend.create({"name": bad}).state == "error"
        with pytest.raises(ValueError):
            backend.delete(bad)
        assert backend.runtime.runner.calls == []

    def test_start_is_a_no_op_when_running(self, backend):
        assert backend.start("greg").state == "running"
        assert backend.runtime.runner.matching("openshell", "sandbox", "create") == []

    def test_start_recreates_a_stopped_sandbox(self, backend, capsys):
        assert backend.start("bere").state == "running"
        runner = backend.runtime.runner
        assert len(runner.matching("openshell", "sandbox", "delete")) == 1
        assert len(runner.matching("openshell", "sandbox", "create")) == 1

    def test_stop_deletes_sandbox_and_keeps_volumes(self, backend):
        st = backend.stop("greg")
        assert st.state == "stopped" and "volumes kept" in st.detail
        runner = backend.runtime.runner
        assert runner.matching("openshell", "sandbox", "delete") == [["openshell", "sandbox", "delete", "greg"]]
        assert runner.matching("docker") == []
        assert backend.status("greg").state == "missing"

    def test_delete(self, backend):
        assert backend.delete("greg") is None
        runner = backend.runtime.runner
        assert runner.matching("openshell", "sandbox", "delete") == [["openshell", "sandbox", "delete", "greg"]]
        assert runner.matching("docker") == []
        backend.delete("ghost")                       # already gone: nothing to do
        assert len(runner.matching("openshell", "sandbox", "delete")) == 1


class TestGetBackend:
    def test_default_is_null(self, monkeypatch):
        monkeypatch.delenv("LOGOS_SANDBOX_BACKEND", raising=False)
        assert isinstance(sb.get_backend(), NullBackend)

    @pytest.mark.parametrize("value", ["", "null", "docker", "none"])
    def test_other_values_stay_null(self, monkeypatch, value):
        monkeypatch.setenv("LOGOS_SANDBOX_BACKEND", value)
        assert isinstance(sb.get_backend(), NullBackend)

    def test_openshell_selected_by_env(self, monkeypatch):
        monkeypatch.setattr(sb, "_openshell_backend", None)
        monkeypatch.setenv("LOGOS_SANDBOX_BACKEND", "openshell")
        b = sb.get_backend()
        assert isinstance(b, OpenShellBackend)
        assert sb.get_backend() is b                  # one per process
        assert b.runtime.repo == REPO


# ── scripts/logos-agent stays a thin CLI over the module ─────────────────────

@pytest.fixture
def cli(repo, monkeypatch):
    path = REPO / "scripts" / "logos-agent"
    loader = SourceFileLoader("logos_agent_cli", str(path))
    spec = importlib.util.spec_from_loader("logos_agent_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    runner = FakeRunner(LISTING)

    class Module:
        AgentRuntimeError = AgentRuntimeError

        @staticmethod
        def AgentRuntime(_repo):
            return AgentRuntime(repo, runner, env={})

    monkeypatch.setattr(mod, "_runtime_module", lambda: Module)
    mod.runner = runner
    return mod


class TestCli:
    def test_help(self, cli, capsys):
        assert cli.main(["logos-agent", "--help"]) == 0
        assert cli.main(["logos-agent"]) == 0
        assert "scripts/logos-agent up <name>" in capsys.readouterr().out
        assert cli.runner.calls == []

    def test_unknown_or_incomplete_command_is_usage_error(self, cli, capsys):
        assert cli.main(["logos-agent", "frobnicate"]) == 2
        assert cli.main(["logos-agent", "up"]) == 2
        assert cli.main(["logos-agent", "exec", "greg", "ls"]) == 2
        assert cli.runner.calls == []

    def test_status_all_and_one(self, cli, repo, capsys):
        (repo / "agents" / "greg.yaml").write_text("name: greg\n")
        (repo / "agents" / "greg.local.yaml").write_text("{}\n")
        assert cli.main(["logos-agent", "status"]) == 0
        assert capsys.readouterr().out == "alpha: Missing\ngreg: Ready\n"
        assert cli.main(["logos-agent", "status", "bench"]) == 0
        assert capsys.readouterr().out == "bench: Provisioning\n"

    def test_down(self, cli, capsys):
        assert cli.main(["logos-agent", "down", "greg"]) == 0
        assert capsys.readouterr().out == "greg: deleted (volumes kept)\n"
        assert cli.runner.matching("openshell", "sandbox", "delete") == [["openshell", "sandbox", "delete", "greg"]]

    def test_up(self, cli, capsys):
        assert cli.main(["logos-agent", "up", "alpha"]) == 0
        assert "alpha: Ready" in capsys.readouterr().out

    def test_up_without_definition_exits_with_message(self, cli):
        with pytest.raises(SystemExit) as e:
            cli.main(["logos-agent", "up", "nope"])
        assert "no agent definition at" in str(e.value.code)

    def test_exec_returns_the_command_exit_code(self, cli):
        assert cli.main(["logos-agent", "exec", "greg", "--", "ls", "-la"]) == 7
        assert cli.runner.calls == [
            ["openshell", "sandbox", "exec", "--name", "greg", "--no-tty", "--", "ls", "-la"]]

    def test_real_module_loads_by_path(self):
        path = REPO / "scripts" / "logos-agent"
        loader = SourceFileLoader("logos_agent_cli2", str(path))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("logos_agent_cli2", loader))
        loader.exec_module(mod)
        real = mod._runtime_module()
        assert real.AgentRuntime(REPO).repo == REPO
        assert hasattr(real, "SubprocessRunner")
