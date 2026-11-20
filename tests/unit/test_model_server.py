"""gateway/model_server.py: the dashboard's view of the local model server (LM Studio)."""
import json

import pytest

from gateway import model_server as ms

BASE = "http://192.0.2.117:1234"


def lm_model(key, size=22.1e9, instances=(), params="35B"):
    return {"key": key, "display_name": key, "quantization": {"name": "Q4_K_M"}, "size_bytes": size,
            "params_string": params, "max_context_length": 262144,
            "loaded_instances": [{"id": i.get("id", key), "config": i} for i in instances]}


QWEN = "qwen/qwen3.6-35b-a3b"
LOADED = {"context_length": 96512, "parallel": 4, "flash_attention": True, "offload_kv_cache_to_gpu": True,
          "num_experts": 8, "eval_batch_size": 2048}


def defs(load=96512):
    m = {"provider": "lmstudio", "base_url": BASE + "/v1", "default": QWEN, "context_length": 65536}
    return {"greg": {"model": {**m, "load_context_length": load}}, "bere": {"model": dict(m)},
            "other": {"model": {"provider": "anthropic", "default": "x"}}}


def test_wanted_names_the_agents_and_both_windows():
    assert ms.wanted(defs()) == {BASE: {QWEN: {"agents": ["bere", "greg"], "uses": 65536, "load": 96512}}}


def levels(out):
    return [c["level"] for c in out], " ".join(c["text"] for c in out)


def test_a_good_setup_says_so():
    listed = [ms.describe(lm_model(QWEN, instances=[LOADED]))]
    lv, text = levels(ms.checks(ms.wanted(defs())[BASE], listed, vram=16))
    assert "warn" not in lv and "enough for the agents" in text
    assert "parallel slots share" in text and "mixture-of-experts" in text          # 22.1 GB on a 16 GB GPU


def test_problems_are_named():
    small = {**LOADED, "context_length": 32768, "flash_attention": False, "offload_kv_cache_to_gpu": False}
    listed = [ms.describe(lm_model(QWEN, instances=[small, {**LOADED, "id": QWEN + ":2"}])),
              ms.describe(lm_model("google/gemma-4-31b", 19.9e9, instances=[LOADED]))]
    lv, text = levels(ms.checks(ms.wanted(defs())[BASE], listed))
    assert lv.count("warn") == 5
    assert "loaded 2 times" in text and "smaller than the 65,536" in text and "Flash attention is off" in text
    assert "KV cache is kept in RAM" in text and "gemma-4-31b is loaded but no agent uses it" in text


def test_not_loaded_or_not_downloaded():
    _, text = levels(ms.checks(ms.wanted(defs())[BASE], [ms.describe(lm_model(QWEN))]))
    assert "is not loaded" in text and "96,512-token window" in text
    lv, text = levels(ms.checks(ms.wanted(defs())[BASE], []))
    assert lv == ["warn"] and "not downloaded" in text


def test_a_dense_model_too_big_for_the_gpu_is_slow():
    listed = [ms.describe(lm_model("dense/27b", 17.7e9, instances=[{**LOADED, "num_experts": None}], params="27B"))]
    d = {"a": {"model": {"provider": "lmstudio", "base_url": BASE + "/v1", "default": "dense/27b",
                         "context_length": 65536}}}
    _, text = levels(ms.checks(ms.wanted(d)[BASE], listed, vram=16))
    assert "smaller quantisation would fit" in text


def test_speed_from_the_guard_log():
    lines = ["model guard: 8094 -> x", "not json",
             json.dumps({"event": "chat", "at": "2026-11-20 14:00:00", "status": 200, "seconds": 20, "alongside": 0,
                         "waited_s": 0, "completion_tokens": 600}),
             json.dumps({"event": "chat", "at": "2026-11-20 14:01:00", "status": 200, "seconds": 40, "alongside": 0,
                         "waited_s": 0, "completion_tokens": 1000}),
             json.dumps({"event": "chat", "at": "2026-11-20 14:02:00", "status": 200, "seconds": 50, "alongside": 1,
                         "waited_s": 12.5, "completion_tokens": 1000}),
             json.dumps({"event": "chat", "status": "model_off", "seconds": 1})]
    s = ms.speed(lines)
    assert s["alone"] == {"replies": 2, "median_seconds": 30.0, "median_tokens_per_s": 27.5}
    assert s["shared"]["median_tokens_per_s"] == 20.0 and s["queued"] == 1 and s["longest_wait_s"] == 12.5
    assert s["since"] == "2026-11-20 14:00:00"
    assert ms.speed([])["alone"]["replies"] == 0


class FakeLM:
    def __init__(self, listed):
        self.listed, self.calls = listed, []

    def __call__(self, url, data=None, headers=None, timeout=10, method=None):
        self.calls.append((url.replace(BASE, ""), json.loads(data) if data else None))
        if url.endswith("/api/v1/models"):
            return json.dumps({"models": self.listed}).encode()
        if url.endswith("/load"):
            return json.dumps({"status": "loaded", "load_config": {"context_length": 96512}}).encode()
        return b"{}"


def test_reload_unloads_every_copy_then_loads_once():
    lm = FakeLM([lm_model(QWEN, instances=[LOADED, {**LOADED, "id": QWEN + ":2"}])])
    out = ms.reload(lm, BASE, QWEN, 96512)
    assert [c[0] for c in lm.calls] == ["/api/v1/models", "/api/v1/models/unload", "/api/v1/models/unload",
                                        "/api/v1/models/load"]
    assert lm.calls[-1][1] == {"model": QWEN, "context_length": 96512, "flash_attention": True,
                               "offload_kv_cache_to_gpu": True, "echo_load_config": True}
    assert out["load_config"]["context_length"] == 96512


def test_unload_never_touches_the_agents_model():
    lm = FakeLM([])
    with pytest.raises(ValueError):
        ms.unload(lm, BASE, QWEN, [QWEN])
    with pytest.raises(ValueError):
        ms.unload(lm, BASE, QWEN + ":2", [QWEN])
    ms.unload(lm, BASE, "google/gemma-4-31b", [QWEN])
    assert lm.calls == [("/api/v1/models/unload", {"instance_id": "google/gemma-4-31b"})]


def test_vram_from_the_settings_file(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert ms.vram_gb(env) == 0.0
    (tmp_path / "logos").mkdir()
    (tmp_path / "logos" / "model-server").write_text("# the model PC\nGPU_VRAM_GB=16\n")
    assert ms.vram_gb(env) == 16.0
