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


def test_the_load_window_can_be_larger_than_what_an_agent_uses():
    shared = {"model": {**agent()["model"], "load_context_length": 96512}}
    assert lm.load_window(shared["model"]) == 96512
    assert lm.load_window({"context_length": 65536, "load_context_length": 32768}) == 65536   # never smaller
    assert lm.wanted({"a": shared, "b": agent()}) == {BASE: {"qwen/q": 96512}}


def test_ensure_leaves_a_large_enough_model_alone():
    s = Server({"qwen/q": 262144})
    assert lm.ensure(s.http, BASE, "qwen/q", 65536) is False
    assert [c[0] for c in s.calls if c[0] != "/running"] == ["/api/v0/models"]


def test_ensure_loads_a_missing_model_with_the_agents_context():
    s = Server()
    assert lm.ensure(s.http, BASE, "qwen/q", 65536) is True
    assert s.loaded == {"qwen/q": 65536}


def test_ensure_replaces_a_too_small_model_instead_of_stacking_a_second():
    s = Server({"qwen/q": 4096})
    assert lm.ensure(s.http, BASE, "qwen/q", 65536) is True
    assert [c[0] for c in s.calls if c[0] != "/running"] == ["/api/v0/models", "/api/v1/models/unload", "/api/v1/models/load"]
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


def test_reachable_is_true_only_when_the_server_answers():
    assert lm.reachable(Server().http, BASE) is True

    def dead(url, **kw):
        raise TimeoutError("timed out")

    assert lm.reachable(dead, BASE) is False


# ── llama-swap (docs/llama-swap.md) ─────────────────────────────────────────

SWAP = "http://10.0.0.6:8080"
CMD = ('C:\\llama\\llama-server.exe --port 5800 -m "C:\\models\\Qwen3.6-35B-A3B-Q4_K_M.gguf" -c 135168 --parallel 3 '
       '--kv-unified\n -ngl 99 --n-cpu-moe 25 -b 2048 -fa on -ctk q8_0 -ctv q8_0 --jinja\n')


class Swap:
    """A stand-in for llama-swap: one model in its config, loaded or not."""

    def __init__(self, running=True):
        self.running, self.calls = running, []

    def http(self, url, *, data=None, headers=None, timeout=10, method=None):
        path = url[len(SWAP):]
        self.calls.append(path)
        if path == "/running":
            return json.dumps({"running": [{"model": "qwen/q", "state": "ready", "cmd": CMD}] if self.running else []}).encode()
        if path == "/v1/models":
            return json.dumps({"data": [{"id": "qwen/q", "context_length": 135168,
                                         "capabilities": {"vision": True}}]}).encode()
        if path == "/upstream/qwen/q/health":
            self.running = True
            return b'{"status":"ok"}'
        if path == "/upstream/qwen/q/v1/models":
            return json.dumps({"data": [{"meta": {"size": 21155768832}}]}).encode()
        if path == "/api/models/unload/qwen/q":
            self.running = False
            return b"OK"
        raise AssertionError(path)


def test_llama_swap_is_told_apart_from_lm_studio():
    assert lm.kind(Swap().http, SWAP) == lm.LLAMA_SWAP
    assert lm.kind(Server().http, BASE) == lm.LM_STUDIO


def test_llama_swap_lists_its_model_in_lm_studios_shape():
    s = Swap()
    assert lm.loaded(s.http, SWAP) == {"qwen/q": 135168}
    assert lm.listing_v0(s.http, SWAP)["data"][0]["type"] == "vlm"
    (m,) = lm.listing_v1(s.http, SWAP)
    assert m["quantization"]["name"] == "Q4_K_M" and m["size_bytes"] == 21155768832
    assert m["loaded_instances"][0]["config"] == {
        "context_length": 135168, "parallel": 3, "flash_attention": True, "offload_kv_cache_to_gpu": True,
        "kv_cache_type": "q8_0", "n_cpu_moe": 25, "eval_batch_size": 2048}


def test_llama_swap_loads_with_its_own_settings_and_is_left_alone_when_loaded():
    s = Swap(running=False)
    assert lm.loaded(s.http, SWAP) == {}
    assert lm.ensure(s.http, SWAP, "qwen/q", 96512) is True
    assert "/upstream/qwen/q/health" in s.calls and not any("load" in c and "health" not in c for c in s.calls)
    assert lm.ensure(s.http, SWAP, "qwen/q", 96512) is False
    lm.unload(s.http, SWAP, "qwen/q")
    assert lm.loaded(s.http, SWAP) == {}
