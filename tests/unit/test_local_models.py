"""gateway/local_models.py: what should be loaded on the local model server, and no duplicates."""
import json

from gateway import local_models as lm

BASE = "http://10.0.0.5:1234"


class Server:
    """A stand-in for LM Studio: remembers what is loaded and every call made."""

    def __init__(self, loaded=None):
        self.loaded = dict(loaded or {})
        self.calls = []

    def http(self, url, *, data=None, headers=None, timeout=10):
        path = url[len(BASE):]
        body = json.loads(data) if data else None
        self.calls.append((path, body))
        if path == "/api/v0/models":
            rows = [{"id": k, "state": "loaded", "loaded_context_length": v} for k, v in self.loaded.items()]
            rows.append({"id": "other/unloaded", "state": "not-loaded"})
            return json.dumps({"data": rows}).encode()
        if path == "/api/v1/models/load":
            self.loaded[body["model"]] = body["context_length"]
        if path == "/api/v1/models/unload":
            self.loaded.pop(body["instance_id"])
        return b"{}"


def agent(model_id="qwen/q", ctx=65536, provider="lmstudio"):
    return {"model": {"provider": provider, "base_url": BASE + "/v1", "default": model_id, "context_length": ctx}}


def test_wanted_takes_the_largest_context_and_skips_other_providers():
    agents = {"a": agent(ctx=65536), "b": agent(ctx=131072), "c": agent(provider="anthropic"), "d": {}}
    assert lm.wanted(agents) == {BASE: {"qwen/q": 131072}}


def test_ensure_leaves_a_large_enough_model_alone():
    s = Server({"qwen/q": 262144})
    assert lm.ensure(s.http, BASE, "qwen/q", 65536) is False
    assert [c[0] for c in s.calls] == ["/api/v0/models"]


def test_ensure_loads_a_missing_model_with_the_agents_context():
    s = Server()
    assert lm.ensure(s.http, BASE, "qwen/q", 65536) is True
    assert s.loaded == {"qwen/q": 65536}


def test_ensure_replaces_a_too_small_model_instead_of_stacking_a_second():
    s = Server({"qwen/q": 4096})
    assert lm.ensure(s.http, BASE, "qwen/q", 65536) is True
    assert [c[0] for c in s.calls] == ["/api/v0/models", "/api/v1/models/unload", "/api/v1/models/load"]
    assert s.loaded == {"qwen/q": 65536}


def test_a_second_download_of_the_same_model_is_a_stray():
    have = {"qwen/q": 65536, "q": 262144}
    assert lm.strays(have, {"qwen/q": 65536}) == ["q"]


def test_report_names_missing_small_and_stray_models():
    s = Server({"q": 262144})
    text = lm.report(s.http, {"a": agent()})
    assert "qwen/q: not loaded" in text and "q: loaded, but no agent uses it" in text
    s = Server({"qwen/q": 4096})
    assert "loaded too small" in lm.report(s.http, {"a": agent()})
