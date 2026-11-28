"""gateway/model_server_metrics.py: llama-server and llama-swap numbers, and the guard's log, as Prometheus text."""
import asyncio
import json
import threading
import time
import urllib.error

from gateway import model_server_metrics as msm

BASE = "http://192.0.2.10:8080"
MODEL = "qwen/qwen3.6-35b-a3b"
CMD = ('C:\\llama\\llama-server.exe --port ${PORT} -m "C:\\models\\m.gguf" -c 135168 --parallel 3 --kv-unified '
       "-ngl 99 --n-cpu-moe 25 -b 2048 -ub 2048 -fa on -ctk q8_0 -ctv q8_0 --jinja --metrics")

# llama-server /metrics (llama.cpp, 28 Nov build: no kv_cache_* any more)
LLAMA = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed, excluding cached tokens
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 24500
llamacpp:prompt_tokens_cached_total 73500
llamacpp:prompt_seconds_total 10
llamacpp:tokens_predicted_total 6400
llamacpp:tokens_predicted_seconds_total 100
llamacpp:n_decode_total 7000
llamacpp:n_tokens_max 31000
llamacpp:spec_decode_num_draft_tokens_total 0
llamacpp:spec_decode_num_accepted_tokens_total 0
llamacpp:spec_decode_num_drafts_total 0
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds 2450.5
llamacpp:predicted_tokens_seconds 64
llamacpp:requests_processing 1
llamacpp:requests_deferred 0
llamacpp:n_busy_slots_per_decode 1.2
llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 3
this line is not prometheus
llamacpp:bad_value abc
"""

# llama-swap /metrics (internal/perf/prometheus.go)
SWAP = """# HELP llamaswap_cpu_util_percent CPU utilization per core (0-100)
# TYPE llamaswap_cpu_util_percent gauge
llamaswap_cpu_util_percent{core="0"} 12.5
llamaswap_memory_total_bytes 68719476736
# TYPE llamaswap_gpu_temperature_celsius gauge
llamaswap_gpu_temperature_celsius{id="0",name="NVIDIA GeForce RTX 4080 SUPER",uuid="GPU-x"} 61
llamaswap_gpu_util_percent{id="0",name="NVIDIA GeForce RTX 4080 SUPER",uuid="GPU-x"} 35
llamaswap_gpu_memory_util_percent{id="0",name="NVIDIA GeForce RTX 4080 SUPER",uuid="GPU-x"} 20
llamaswap_gpu_memory_used_bytes{id="0",name="NVIDIA GeForce RTX 4080 SUPER",uuid="GPU-x"} 15891169280
llamaswap_gpu_memory_total_bytes{id="0",name="NVIDIA GeForce RTX 4080 SUPER",uuid="GPU-x"} 17171480576
llamaswap_gpu_power_draw_watts{id="0",name="NVIDIA GeForce RTX 4080 SUPER",uuid="GPU-x"} 180.5
"""

RUNNING = {"running": [{"model": MODEL, "state": "ready", "cmd": CMD, "proxy": "http://127.0.0.1:5800", "ttl": 0},
                       {"model": "small", "state": "starting", "cmd": "llama-server -c 4096"}]}


def fake_http(pages, calls=None):
    def http(url, timeout=10, **kw):
        if calls is not None:
            calls.append(url)
        if url not in pages:
            raise urllib.error.HTTPError(url, 404, "no", {}, None)
        v = pages[url]
        if isinstance(v, Exception):
            raise v
        return v if isinstance(v, bytes) else (json.dumps(v) if not isinstance(v, str) else v).encode()
    return http


PAGES = {BASE + "/running": RUNNING, f"{BASE}/upstream/{MODEL}/metrics": LLAMA, BASE + "/metrics": SWAP}


def test_parse_is_tolerant():
    rows = msm.parse(LLAMA)
    names = [n for n, _, _ in rows]
    assert "llamacpp:prompt_tokens_total" in names and "llamacpp:bad_value" not in names
    assert ("llamacpp:spec_decode_num_accepted_tokens_per_pos_total", {"position": "0"}, 3.0) in rows
    assert msm.parse('a{b="x\\"y",c="z"} 1 1700000000') == [("a", {"b": 'x"y', "c": "z"}, 1.0)]
    L = msm.llama(LLAMA)
    assert L["prompt_tokens_seconds"] == 2450.5 and "spec_decode_num_accepted_tokens_per_pos_total" not in L
    assert "kv_cache_usage_ratio" not in L


def test_gpus_from_llama_swap():
    (g,) = msm.gpus(SWAP)
    assert g["id"] == "0" and g["name"].startswith("NVIDIA") and g["temperature_celsius"] == 61
    assert g["memory_used_bytes"] == 15891169280 and g["power_watts"] == 180.5
    assert msm.gpus("llamaswap_memory_total_bytes 1\n") == []


def test_look_reads_running_models_only():
    calls = []
    snap = msm.look(fake_http(PAGES, calls), BASE, now=1.0)
    assert snap["up"] and snap["kind"] == "llama-swap"
    ready, starting = snap["models"]
    assert ready["window"] == 135168 and ready["parallel"] == 3 and ready["n_cpu_moe"] == "25"
    assert ready["kv_cache_type"] == "q8_0" and ready["flash_attention"] == "on"
    assert ready["llama"]["tokens_predicted_total"] == 6400
    assert starting["state"] == "starting" and starting["llama"] is None
    # asking /upstream/<model> loads a model that is not running: never for one that is not ready
    assert not any("/upstream/small" in c for c in calls)
    assert snap["gpus"][0]["util_percent"] == 35


def test_look_without_metrics_flag_or_gpu_monitor():
    pages = {BASE + "/running": RUNNING, f"{BASE}/upstream/{MODEL}/metrics": urllib.error.HTTPError("u", 501, "x", {}, None)}
    snap = msm.look(fake_http(pages), BASE)
    assert snap["up"] and snap["models"][0]["llama"] is None and snap["gpus"] == []
    text = msm.render_servers({"192.0.2.10:8080": snap})
    assert 'logos_model_metrics_up{server="192.0.2.10:8080",model="qwen/qwen3.6-35b-a3b"} 0' in text
    assert "logos_model_generated_tokens_total" not in text and "logos_model_gpu" not in text


def test_off_pc_and_lm_studio():
    off = msm.look(fake_http({BASE + "/running": OSError("timed out")}), BASE)
    assert off["up"] is False
    lms = msm.look(fake_http({}), BASE)                       # answers 404 to /running: LM Studio
    assert lms["up"] and lms["kind"] == "lmstudio"
    text = msm.render_servers({"a:1": off, "b:2": lms, "c:3": None})
    assert 'logos_model_server_up{server="a:1"} 0' in text and 'logos_model_server_up{server="b:2"} 1' in text
    assert 'logos_model_server_up{server="c:3"} 0' in text
    assert "logos_model_loaded" not in text and "logos_model_gpu" not in text


def test_render_servers():
    text = msm.render_servers({"192.0.2.10:8080": msm.look(fake_http(PAGES), BASE)})
    lab = 'server="192.0.2.10:8080",model="qwen/qwen3.6-35b-a3b"'
    for line in ('logos_model_server_up{server="192.0.2.10:8080"} 1',
                 f"logos_model_loaded{{{lab}}} 1",
                 'logos_model_loaded{server="192.0.2.10:8080",model="small"} 0',
                 f'logos_model_info{{{lab},state="ready",kv_cache_type="q8_0",n_cpu_moe="25",flash_attention="on"}} 1',
                 f"logos_model_load_window_tokens{{{lab}}} 135168",
                 f"logos_model_parallel_slots{{{lab}}} 3",
                 f"logos_model_n_cpu_moe_layers{{{lab}}} 25",
                 f"logos_model_metrics_up{{{lab}}} 1",
                 f"logos_model_prompt_tokens_per_second{{{lab}}} 2450.5",
                 f"logos_model_generation_tokens_per_second{{{lab}}} 64",
                 f"logos_model_server_requests_processing{{{lab}}} 1",
                 f"logos_model_prompt_tokens_cached_total{{{lab}}} 73500",
                 f"logos_model_generated_tokens_total{{{lab}}} 6400",
                 'logos_model_gpu_memory_used_bytes{server="192.0.2.10:8080",gpu="0",name="NVIDIA GeForce RTX 4080 SUPER"} 15891169280',
                 'logos_model_gpu_temperature_celsius{server="192.0.2.10:8080",gpu="0",name="NVIDIA GeForce RTX 4080 SUPER"} 61'):
        assert line in text, line
    assert "# TYPE logos_model_generated_tokens_total counter" in text
    assert "logos_model_kv_cache_usage_ratio" not in text     # not in this llama.cpp build
    # every family once, and every line parses back
    assert len([l for l in text.splitlines() if l.startswith("# TYPE logos_model_loaded ")]) == 1
    assert len(msm.parse(text)) == len([l for l in text.splitlines() if l and not l.startswith("#")])


def test_gpu_memory_exact():
    rows = {(n, l.get("gpu")): v for n, l, v in msm.parse(msm.render_servers({"s": msm.look(fake_http(PAGES), BASE)}))}
    assert rows[("logos_model_gpu_memory_used_bytes", "0")] == 15891169280        # bytes and counters keep every digit
    assert rows[("logos_model_gpu_power_watts", "0")] == 180.5


def test_live_for_the_models_page():
    lv = msm.live(msm.look(fake_http(PAGES), BASE), MODEL)
    assert lv["metrics_on"] and lv["prompt_tokens_per_s"] == 2450.0 and lv["generation_tokens_per_s"] == 64.0
    assert lv["prompt_cache_hit_ratio"] == 0.75                # 73,500 reused of 98,000
    assert lv["requests_processing"] == 1 and lv["requests_deferred"] == 0
    assert lv["gpu"]["temperature_celsius"] == 61 and "kv_cache_usage_ratio" not in lv
    assert msm.live({"up": False}, MODEL) is None and msm.live(None, MODEL) is None
    assert msm.live({"up": True, "kind": "llama-swap", "models": [], "gpus": []}, MODEL) is None


def test_scraper_caches_and_drops_stale():
    t = [100.0]
    calls = []
    s = msm.Scraper(every=15, clock=lambda: t[0])
    http = fake_http(PAGES, calls)
    s.get(http, BASE)
    n = len(calls)
    t[0] += 10
    s.get(http, BASE)
    assert len(calls) == n                                     # within 15 s: no new look
    t[0] += 6
    s.get(http, BASE)
    assert len(calls) == 2 * n
    t[0] += 61
    assert s.cached(BASE) is None                              # too old to show: up=0, not old numbers


def test_scraper_does_not_wait_for_a_slow_look():
    s = msm.Scraper(every=15)
    lock = s._lock(BASE)
    lock.acquire()                                             # another look is under way
    try:
        began = time.monotonic()
        assert s.get(fake_http(PAGES), BASE, wait=0.2) is None
        assert time.monotonic() - began < 1
    finally:
        lock.release()


def _line(ts, **r):
    return f"{ts} " + json.dumps({"event": "chat", "at": "x", **r})


LOG = [
    _line("2026-11-28T10:00:00.100000000Z", who="person", waited_s=0.0, seconds=12.3, status=200, prompt_tokens=9000, completion_tokens=400),
    _line("2026-11-28T10:00:05.5Z", who="speaker", waited_s=1.5, seconds=3.2, status=200, prompt_tokens=24000, completion_tokens=40),
    _line("2026-11-28T10:01:00Z", who="scheduled", waited_s=40, seconds=700, status="loop_cut", loop_ratio=30.1),
    _line("2026-11-28T10:02:00Z", who="person", waited_s=60, seconds=60, status="model_off"),
    "2026-11-28T10:03:00Z model server off: POST /v1/chat/completions refused",
    "2026-11-28T10:03:01Z " + json.dumps({"event": "warm", "seconds": 1}),
]


def test_guard_log_counts_each_line_once():
    g = msm.GuardLog(since=0)
    assert g.feed(LOG) == 4
    assert g.feed(LOG) == 0                                    # docker --since repeats the boundary line
    assert g.requests == {("person", "ok"): 1, ("speaker", "ok"): 1, ("scheduled", "loop_cut"): 1, ("person", "model_off"): 1}
    assert g.loop_cuts == {"scheduled": 1}
    assert g.tokens[("speaker", "prompt_tokens")] == 24000
    text = msm.render_guard(g)
    for line in ('logos_model_requests_total{who="person",outcome="ok"} 1',
                 'logos_model_loop_cuts_total{who="scheduled"} 1',
                 'logos_model_prompt_tokens_total{who="person"} 9000',
                 'logos_model_completion_tokens_total{who="speaker"} 40',
                 'logos_model_request_seconds_bucket{who="person",le="10"} 0',
                 'logos_model_request_seconds_bucket{who="person",le="20"} 1',
                 'logos_model_request_seconds_bucket{who="person",le="+Inf"} 2',
                 'logos_model_request_seconds_count{who="person"} 2',
                 'logos_model_request_seconds_sum{who="person"} 72.3',
                 'logos_model_request_wait_seconds_bucket{who="speaker",le="2"} 1',
                 'logos_model_request_wait_seconds_bucket{who="speaker",le="1"} 0'):
        assert line in text, line
    assert "# TYPE logos_model_request_seconds histogram" in text


class _P:
    def __init__(self, out, rc=0):
        self.stdout, self.stderr, self.returncode = out, "", rc


def test_guard_log_read_asks_docker_after_the_last_line():
    seen = []
    g = msm.GuardLog(since=1795000000)
    g.read(lambda argv: seen.append(argv) or _P("\n".join(LOG[:2])), "logos-model-guard")
    assert seen[0] == ["docker", "logs", "--timestamps", "--since", "1795000000", "logos-model-guard"]
    g.read(lambda argv: seen.append(argv) or _P("\n".join(LOG)), "logos-model-guard")
    assert seen[1][4] == "2026-11-28T10:00:05.500000000Z"
    assert sum(g.requests.values()) == 4 and g.read_ok
    g.read(lambda argv: _P("", rc=1), "logos-model-guard")
    assert g.read_ok is False and 'logos_model_guard_log_up 0' in msm.render_guard(g)


def test_collector_never_blocks_the_endpoint():
    gate = threading.Event()

    class Slow(msm.Collector):
        def gather(self, runtime):
            gate.wait(5)
            self.bases = {"192.0.2.10:8080": BASE}

    c = Slow(scraper=msm.Scraper(), guard=msm.GuardLog(since=0))

    async def go():
        began = time.monotonic()
        text = await c.render(object(), wait=0.2)
        took = time.monotonic() - began
        gate.set()
        await c.task
        return text, took

    text, took = asyncio.run(go())
    assert took < 1 and text == ""
    assert asyncio.run(msm.Collector().render(None)) == ""


def test_collector_gathers_the_agents_servers_and_the_guard_log():
    class Runner:
        http = staticmethod(fake_http(PAGES))
        argv = []

        def run(self, argv):
            self.argv.append(argv)
            return _P("\n".join(LOG))

    class Runtime:
        runner = Runner()

        def defined(self):
            return ["greg"]

        def load(self, name):
            return {"model": {"provider": "lmstudio", "base_url": BASE + "/v1", "default": MODEL, "context_length": 65536}}

    c = msm.Collector(scraper=msm.Scraper(), guard=msm.GuardLog(since=0))
    text = asyncio.run(c.render(Runtime()))
    assert 'logos_model_server_up{server="192.0.2.10:8080"} 1' in text
    assert 'logos_model_generated_tokens_total{server="192.0.2.10:8080",model="qwen/qwen3.6-35b-a3b"} 6400' in text
    assert 'logos_model_requests_total{who="speaker",outcome="ok"} 1' in text
    assert Runner.argv[0][:3] == ["docker", "logs", "--timestamps"] and Runner.argv[0][-1] == "logos-model-guard"


def test_metrics_endpoint_carries_them():
    from gateway import metrics
    from aiohttp import web
    app = web.Application()
    metrics.register(app, path=None)
    assert isinstance(app["model_server_metrics"], msm.Collector)


def test_grafana_dashboard_uses_only_metrics_logos_exports():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2]
    dash = json.loads((root / "docs/grafana/logos-model.json").read_text())
    exprs = [t["expr"] for p in dash["panels"] for t in p["targets"]]
    names = {re.sub(r"_(bucket|sum|count)$", "", n) for e in exprs for n in re.findall(r"\blogos_[a-z0-9_]+", e)}
    source = "".join((root / f).read_text() for f in ("gateway/model_server_metrics.py", "gateway/metrics.py", "gateway/voice_converse.py"))
    missing = sorted(n for n in names if f'"{n}' not in source and f"'{n}" not in source and f"{n}_bucket" not in source)
    assert names and not missing, missing
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", json.dumps(dash))       # no addresses in a public file
