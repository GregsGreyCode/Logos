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


@pytest.fixture
def paid_allowed(monkeypatch):
    """The paid-fallback code is kept for the future brain-free advisor; these tests run it
    with the brain-stays-local rule switched off."""
    monkeypatch.setattr(mg, "BRAIN_STAYS_LOCAL", False)


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

    def test_paid_provider_only_for_agents_that_allow_it(self, paid_allowed):
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
    def test_up_is_idempotent_and_restarts_on_change(self, tmp_path, paid_allowed):
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


class TestGuard:
    class Rt:
        def __init__(self, running=False, label=""):
            self.calls, self.running, self.label = [], running, label

        def run(self, cmd, *, input=None, check=True):
            self.calls.append(list(cmd))
            a = " ".join(cmd)
            if a.startswith("docker network inspect"):
                return "172.17.0.1\n"
            if a.startswith("docker ps"):
                return "logos-model-guard\n" if self.running else ""
            if a.startswith("docker inspect"):
                return self.label
            return ""

    def test_guard_starts_in_front_of_the_model_server(self):
        rt = self.Rt()
        url = mg.ModelGateway(rt).guard_up("http://10.0.0.5:1234/v1", "logos-agent:1")
        assert url == "http://172.17.0.1:8094"                 # Bifrost adds /v1 itself
        run = next(c for c in rt.calls if c[:3] == ["docker", "run", "-d"])
        assert "GUARD_UPSTREAM=http://10.0.0.5:1234" in run and "172.17.0.1:8094:8094" in run
        assert run[-3:] == ["python3", "logos-agent:1", "/app/model_guard.py"] and "--read-only" in run

    def test_guard_is_left_alone_when_nothing_changed(self):
        first = self.Rt()
        mg.ModelGateway(first).guard_up("http://10.0.0.5:1234/v1", "logos-agent:1")
        stamp = next(c for c in first.calls if c[:3] == ["docker", "run", "-d"])
        label = next(x for x in stamp if x.startswith("logos.guard.stamp=")).split("=", 1)[1]
        again = self.Rt(running=True, label=label)
        mg.ModelGateway(again).guard_up("http://10.0.0.5:1234/v1", "logos-agent:1")
        assert not [c for c in again.calls if c[:3] == ["docker", "run", "-d"]]

    def test_config_sends_local_calls_to_the_guard(self):
        cfg = mg.build_config({"greg": agent()}, {"greg": "sk-bf-x"}, {}, "http://172.17.0.1:8094")
        assert cfg["providers"]["lm-studio"]["network_config"]["base_url"] == "http://172.17.0.1:8094"


PAID = {"anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


def chained(fallbacks, monthly=5):
    a = agent()
    a["model"]["fallbacks"] = fallbacks
    a["budget"] = {"monthly_usd": monthly}
    return a


@pytest.mark.usefixtures("paid_allowed")
class TestFallbacks:
    def test_chain_keeps_order_and_shares_the_budget(self):
        chain = mg.fallback_chain(chained([{"model": "anthropic/claude-haiku-5-5", "monthly_usd": 4},
                                           "openrouter/google/gemini-2.5-flash-lite"]), PAID)
        assert chain == [{"provider": "anthropic", "model": "claude-haiku-5-5", "monthly_usd": 4.0},
                         {"provider": "openrouter", "model": "google/gemini-2.5-flash-lite", "monthly_usd": 1.0}]
        equal = mg.fallback_chain(chained(["anthropic/a", "openrouter/b/c"], monthly=3), PAID)
        assert [c["monthly_usd"] for c in equal] == [1.5, 1.5]

    def test_a_provider_without_a_key_is_left_out(self):
        chain = mg.fallback_chain(chained(["anthropic/a", "openrouter/b"]), {"openrouter": "OPENROUTER_API_KEY"})
        assert chain == [{"provider": "openrouter", "model": "b", "monthly_usd": 5.0}]
        assert mg.fallback_chain(chained(["anthropic/a"]), {}) == []
        assert mg.fallback_chain(agent(), PAID) == []

    def test_amounts_must_fit_inside_the_agents_total(self):
        with pytest.raises(ValueError, match="add up to more"):
            mg.fallback_chain(chained([{"model": "anthropic/a", "monthly_usd": 4},
                                       {"model": "openrouter/b", "monthly_usd": 2}]), PAID)
        with pytest.raises(ValueError, match="add up to more"):
            mg.fallback_chain(chained([{"model": "anthropic/a", "monthly_usd": 5}, "openrouter/b"]), PAID)
        with pytest.raises(ValueError, match="must be <provider>/<model>"):
            mg.fallback_chain(chained(["mystery/model"]), PAID)
        with pytest.raises(ValueError, match="must be <provider>/<model>"):
            mg.fallback_chain(chained(["anthropic"]), PAID)

    def test_config_puts_the_limit_on_each_paid_provider_never_on_the_key(self):
        cfg = mg.build_config({"greg": chained([{"model": "anthropic/claude-haiku-5-5", "monthly_usd": 4},
                                                "openrouter/google/gemini-2.5-flash-lite"])},
                              {"greg": "sk-bf-x"}, PAID)
        gov = cfg["governance"]
        assert gov["budgets"] == []                      # a key-wide limit would also refuse the free local model
        local, anthropic, openrouter = gov["virtual_keys"][0]["provider_configs"]
        assert local == {"provider": "lm-studio", "weight": 1.0, "allowed_models": ["*"], "key_ids": ["*"]}
        assert anthropic["allowed_models"] == ["claude-haiku-5-5"]
        assert anthropic["budgets"] == [{"id": "budget-greg-anthropic", "max_limit": 4.0, "reset_duration": "1M"}]
        assert openrouter["budgets"] == [{"id": "budget-greg-openrouter", "max_limit": 1.0, "reset_duration": "1M"}]
        rule = gov["routing_rules"][0]
        assert rule["fallbacks"] == ["anthropic/claude-haiku-5-5", "openrouter/google/gemini-2.5-flash-lite"]
        assert (rule["scope"], rule["scope_id"], rule["targets"]) == ("virtual_key", "vk-greg", [{"weight": 1.0}])
        assert rule["cel_expression"] == 'provider == "lm-studio"'
        assert mg.budget_ids(cfg) == ["budget-greg-anthropic", "budget-greg-openrouter"]

    def test_two_models_from_one_provider_share_one_limit(self):
        cfg = mg.build_config({"greg": chained(["anthropic/small", "anthropic/large"], monthly=4)},
                              {"greg": "sk-bf-x"}, PAID)
        _local, anthropic = cfg["governance"]["virtual_keys"][0]["provider_configs"]
        assert anthropic["allowed_models"] == ["small", "large"]
        assert anthropic["budgets"][0]["max_limit"] == 4.0

    def test_without_fallbacks_the_config_is_as_before(self):
        cfg = mg.build_config({"greg": agent()}, {"greg": "sk-bf-x"}, PAID)
        assert "routing_rules" not in cfg["governance"]
        assert [b["id"] for b in cfg["governance"]["budgets"]] == ["budget-greg"]
        assert mg.budget_ids(cfg) == ["budget-greg"]


class TestBrainStaysLocal:
    def test_every_kind_of_way_out_is_refused(self):
        assert mg.BRAIN_STAYS_LOCAL is True
        assert mg.local_only_problems("greg", agent()) == []
        cases = {
            "model.fallbacks": agent(fallbacks=["anthropic/claude-haiku-5-5"]),
            "model.allow": agent(allow=["anthropic"]),
            "model provider key": {**agent(), "providers": ["anthropic"]},
            "not a local model server": agent(provider="anthropic"),
            "consolidation.model": {**agent(), "consolidation": {"model": "anthropic/claude-sonnet-5-5"}},
            "digest.provider": {**agent(), "digest": {"provider": "openai"}},
        }
        for words, definition in cases.items():
            problems = " ".join(mg.local_only_problems("greg", definition))
            assert words in problems, words
            with pytest.raises(ValueError, match="brain stays local"):
                mg.build_config({"greg": definition}, {"greg": "k"}, {"anthropic": "ANTHROPIC_API_KEY"})

    def test_tool_credentials_are_not_model_keys(self):
        assert mg.local_only_problems("greg", {**agent(), "providers": ["github"]}) == []

    def test_a_key_in_models_env_configures_no_paid_provider(self):
        cfg = mg.build_config({"greg": agent()}, {"greg": "k"}, {"anthropic": "ANTHROPIC_API_KEY"})
        assert set(cfg["providers"]) == {mg.LOCAL_PROVIDER}
        assert "routing_rules" not in cfg["governance"]
        configs = cfg["governance"]["virtual_keys"][0]["provider_configs"]
        assert [c["provider"] for c in configs] == [mg.LOCAL_PROVIDER]

    def test_the_gateway_container_never_gets_the_keys(self, tmp_path):
        rt = FakeRuntime(tmp_path, {"greg": agent()})
        (tmp_path / "models.env").write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        assert mg.ModelGateway(rt).up() is True
        start = next(c for c in rt.runner.calls if c[:3] == ["docker", "run", "-d"])
        assert "--env-file" not in start and "sk-ant-test" not in " ".join(start)


def test_every_agent_in_the_repo_is_local_only():
    import yaml
    for path in sorted((REPO / "agents").glob("*.yaml")):
        if path.stem.endswith(".local"):
            continue
        definition = yaml.safe_load(path.read_text())
        if definition.get("sandbox") is False:
            continue
        assert mg.local_only_problems(path.stem, definition) == [], path.name
