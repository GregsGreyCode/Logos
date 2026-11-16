"""Model gateway config and lifecycle (S2.9). No docker: everything goes through a fake runtime."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"logos_{name}", REPO / "gateway" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mg = _load("model_gateway")


def agent(**model):
    return {"model": {"provider": "lmstudio", "base_url": "http://10.0.0.5:1234/v1", "default": "qwen/q",
                      "context_length": 65536, "gateway": True, **model}}


class TestConfig:
    def test_local_only(self):
        cfg = mg.build_config({"greg": {**agent(), "budget": {"monthly_usd": 7}}}, {"greg": "sk-bf-g"}, {})
        assert cfg["client"] == {"enforce_auth_on_inference": True}
        local = cfg["providers"]["lm-studio"]
        assert local["network_config"]["base_url"] == "http://10.0.0.5:1234"
        assert local["network_config"]["allow_private_network"] is True
        assert list(cfg["providers"]) == ["lm-studio"]
        vk = cfg["governance"]["virtual_keys"][0]
        assert (vk["id"], vk["value"]) == ("vk-greg", "sk-bf-g")
        assert [p["provider"] for p in vk["provider_configs"]] == ["lm-studio"]
        assert cfg["governance"]["budgets"] == [
            {"id": "budget-greg", "virtual_key_id": "vk-greg", "max_limit": 7.0, "reset_duration": "1M"}]

    def test_paid_provider_only_for_agents_that_allow_it(self):
        agents = {"greg": agent(allow=["anthropic", "openai"]), "bere": agent()}
        cfg = mg.build_config(agents, {"greg": "k1", "bere": "k2"}, {"anthropic": "ANTHROPIC_API_KEY"})
        assert cfg["providers"]["anthropic"]["keys"][0]["value"] == "env.ANTHROPIC_API_KEY"
        by = {v["name"]: [p["provider"] for p in v["provider_configs"]] for v in cfg["governance"]["virtual_keys"]}
        assert by == {"agent-bere": ["lm-studio"], "agent-greg": ["lm-studio", "anthropic"]}   # openai has no key
        assert all(b["max_limit"] == mg.DEFAULT_MONTHLY_USD for b in cfg["governance"]["budgets"])

    def test_rejects_bad_input(self):
        with pytest.raises(ValueError, match="no agent"):
            mg.build_config({}, {}, {})
        with pytest.raises(ValueError, match="one local model server"):
            mg.build_config({"a": agent(), "b": agent(base_url="http://10.0.0.6:1234/v1")}, {"a": "k", "b": "k"}, {})
        with pytest.raises(ValueError, match="above 0"):
            mg.build_config({"a": {**agent(), "budget": {"monthly_usd": 0}}}, {"a": "k"}, {})

    def test_helpers(self):
        assert mg.paid_providers("# c\nANTHROPIC_API_KEY=x\nOPENAI_API_KEY=\nOTHER=1\n") == {"anthropic": "ANTHROPIC_API_KEY"}
        assert mg.gateway_model(agent()) == "lm-studio/qwen/q"
        assert mg.gateway_model(agent(default="anthropic/claude-x", allow=["anthropic"])) == "anthropic/claude-x"
        assert mg.gateway_model(agent(default="anthropic/claude-x")) == "lm-studio/anthropic/claude-x"
        assert mg.uses_gateway(agent()) and not mg.uses_gateway({"model": {}})
        assert mg.keys_file({"LOGOS_MODELS_ENV": "/x/m.env"}) == Path("/x/m.env")
        assert mg.keys_file({"HOME": "/h"}) == Path("/h/.config/logos/models.env")


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class FakeRunner:
    def __init__(self):
        self.calls, self.config, self.volume, self.container, self.label = [], None, False, False, ""

    def run(self, argv, *, input=None, text=True):
        self.calls.append(list(argv))
        a = " ".join(argv)
        if a.startswith("docker volume inspect"):
            return Done(0 if self.volume else 1)
        if "cat /v/config.json" in a:
            return Done(0, self.config) if self.config is not None else Done(1)
        return Done()

    def http(self, url, **kw):
        return b"{}"

    def sleep(self, s):
        pass


class FakeRuntime:
    def __init__(self, tmp_path, defs):
        self.runner, self.defs = FakeRunner(), defs
        self.env = {"LOGOS_MODELS_ENV": str(tmp_path / "models.env")}

    def run(self, cmd, *, input=None, check=True):
        a = " ".join(cmd)
        self.runner.calls.append(list(cmd))
        if a.startswith("docker network inspect"):
            return "172.17.0.1\n"
        if a.startswith("docker ps"):
            return "logos-models\n" if self.runner.container else ""
        if a.startswith("docker volume create"):
            self.runner.volume = True
        if "cat > /v/config.json" in a:
            self.runner.config = input
        if a.startswith("docker run -d"):
            self.runner.container = True
            self.runner.label = next(c for c in cmd if c.startswith("logos.models.keys=")).split("=", 1)[1]
        if a.startswith("docker inspect"):
            return self.runner.label
        return ""

    def defined(self):
        return sorted(self.defs)

    def load(self, name):
        return self.defs[name]

    def get(self, vol, rel):
        return f"API_SERVER_KEY=x\nLOGOS_MODEL_KEY=sk-bf-{vol}\n"


class TestLifecycle:
    def test_up_is_idempotent_and_restarts_on_change(self, tmp_path):
        rt = FakeRuntime(tmp_path, {"greg": agent(), "plain": {"model": {"base_url": "http://x/v1"}}})
        gw = mg.ModelGateway(rt)
        assert gw.up() is True
        cfg = json.loads(rt.runner.config)
        assert [v["value"] for v in cfg["governance"]["virtual_keys"]] == ["sk-bf-logos-home-greg"]
        start = next(c for c in rt.runner.calls if c[:3] == ["docker", "run", "-d"])
        assert "172.17.0.1:8089:8080" in start and start[-1] == mg.IMAGE and "--env-file" not in start
        assert gw.up() is False                                   # nothing changed
        rt.defs["greg"]["budget"] = {"monthly_usd": 9}
        assert gw.up() is True                                    # budget changed
        (tmp_path / "models.env").write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        assert gw.up() is True                                    # provider key added
        start = [c for c in rt.runner.calls if c[:3] == ["docker", "run", "-d"]][-1]
        assert start[start.index("--env-file") + 1] == str(tmp_path / "models.env")
        assert "sk-ant-test" not in rt.runner.config
        assert gw.base_url() == "http://172.17.0.1:8089/v1"
        assert "greg: $9.00 a month, may use local" in gw.status()

    def test_missing_agent_key(self, tmp_path):
        rt = FakeRuntime(tmp_path, {"greg": agent()})
        rt.get = lambda vol, rel: ""
        with pytest.raises(ValueError, match="has a key yet"):
            mg.ModelGateway(rt).up()
