"""The model server's own numbers, for Prometheus/Grafana and the Models page (28 Nov).

Two sources, both read-only, and only while the Prometheus endpoint is on or the Models page is open:

- **The model PC** (llama-swap in front of llama.cpp's ``llama-server``, docs/llama-swap.md):
  ``/running`` (what is loaded, with its command line: window, slots, experts on the CPU, KV-cache type),
  ``/upstream/<model>/metrics`` (llama-server's Prometheus text: tokens, seconds, queue; only when its
  command has ``--metrics``) and llama-swap's own ``/metrics`` (GPU memory, use, temperature, power, read
  with nvidia-smi). ``/upstream/…`` is only asked for a model that is already ``ready``: asking it for any
  other would load it.
- **The model guard's log** (``docker logs logos-model-guard``): one ``{"event": "chat", …}`` line per
  request, turned into counters and histograms by ``who`` (person, speaker, scheduled).

The PC is asked at most every :data:`EVERY` seconds with short timeouts, and the endpoint never waits
for it longer than :data:`WAIT`: a slow answer is used by the next scrape. When the PC is off, only
``logos_model_server_up 0`` is exported, never an old number. Stdlib only.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
import urllib.error
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

EVERY = 15.0           # seconds between two looks at the model PC
TIMEOUT = 2.5          # each request to it
WAIT = 4.0             # the most /metrics waits for a fresh look
STALE = 60.0           # an older look is not exported (up=0 instead)

# ── Prometheus text ──────────────────────────────────────────────────────────────────────────

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)(\s+\S+)?\s*$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


def parse(text: str) -> List[Tuple[str, Dict[str, str], float]]:
    """``[(name, labels, value)]`` from Prometheus text. Lines it cannot read are skipped."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(4))
        except ValueError:
            continue
        labels = {k: v.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
                  for k, v in _LABEL.findall(m.group(3) or "")}
        out.append((m.group(1), labels, value))
    return out


def llama(text: str) -> Dict[str, float]:
    """llama-server's numbers by short name (``llamacpp:prompt_tokens_total`` -> ``prompt_tokens_total``).
    Unlabelled samples only (per-position speculative counters are left out)."""
    return {name.split(":", 1)[1]: v for name, labels, v in parse(text)
            if name.startswith("llamacpp:") and not labels and math.isfinite(v)}


GPU_FIELDS = {"llamaswap_gpu_memory_used_bytes": "memory_used_bytes", "llamaswap_gpu_memory_total_bytes": "memory_total_bytes",
              "llamaswap_gpu_util_percent": "util_percent", "llamaswap_gpu_memory_util_percent": "memory_util_percent",
              "llamaswap_gpu_temperature_celsius": "temperature_celsius", "llamaswap_gpu_power_draw_watts": "power_watts",
              "llamaswap_gpu_fan_speed_percent": "fan_percent"}


def gpus(text: str) -> List[dict]:
    """Each GPU llama-swap reports (its ``/metrics``, llama-swap 212+; Windows nvidia-smi 217+)."""
    by_id: Dict[str, dict] = {}
    for name, labels, v in parse(text):
        field = GPU_FIELDS.get(name)
        if field and math.isfinite(v):
            g = by_id.setdefault(labels.get("id", "0"), {"id": labels.get("id", "0"), "name": labels.get("name", "")})
            g[field] = v
    return [by_id[k] for k in sorted(by_id)]


# ── one look at the model PC ─────────────────────────────────────────────────────────────────

def _int(v: str, default: int) -> int:
    return int(v) if v.isdigit() else default


def _settings(cmd: str) -> dict:
    """Window, slots, experts on the CPU, KV-cache type and flash attention from llama-swap's ``cmd``."""
    from gateway.local_models import _flag
    words = cmd.split()
    fa = _flag(cmd, "-fa", "--flash-attn")
    return {"window": _int(_flag(cmd, "-c", "--ctx-size"), 0),
            "parallel": _int(_flag(cmd, "-np", "--parallel"), 1),
            "n_cpu_moe": "all" if "--cpu-moe" in words else str(_int(_flag(cmd, "--n-cpu-moe", "-ncmoe"), 0)),
            "kv_cache_type": _flag(cmd, "-ctk", "--cache-type-k") or "f16",
            "flash_attention": fa.lower() if fa else "auto"}


def look(http: Callable[..., bytes], base: str, timeout: float = TIMEOUT, now: Optional[float] = None) -> dict:
    """What the model server at ``base`` says now. ``up`` False when it does not answer.
    ``kind`` is llama-swap or lmstudio (LM Studio has none of these numbers: only ``up``)."""
    snap: Dict[str, Any] = {"at": time.time() if now is None else now, "up": False, "kind": "", "models": [], "gpus": []}
    try:
        running = json.loads(http(base + "/running", timeout=timeout))
    except urllib.error.HTTPError:
        snap.update(up=True, kind="lmstudio")          # it answered, without /running: LM Studio
        return snap
    except Exception:
        return snap
    if not isinstance(running, dict) or "running" not in running:
        snap.update(up=True, kind="lmstudio")
        return snap
    snap.update(up=True, kind="llama-swap")
    for r in running.get("running") or []:
        if not isinstance(r, dict) or not r.get("model"):
            continue
        m = {"model": str(r["model"]), "state": str(r.get("state") or ""), **_settings(str(r.get("cmd") or "")), "llama": None}
        if m["state"] == "ready":                      # never ask a model that is not loaded: it would load
            try:
                m["llama"] = llama(http(f"{base}/upstream/{quote(m['model'], safe='/')}/metrics", timeout=timeout).decode("utf-8", "replace"))
            except Exception:
                m["llama"] = None                      # no --metrics (501), or it went away
        snap["models"].append(m)
    try:
        snap["gpus"] = gpus(http(base + "/metrics", timeout=timeout).decode("utf-8", "replace"))
    except Exception:
        snap["gpus"] = []                              # older llama-swap, or no GPU monitor
    return snap


class Scraper:
    """The last look at each model server, at most one every :data:`EVERY` seconds."""

    def __init__(self, every: float = EVERY, clock: Callable[[], float] = time.monotonic):
        self.every, self.clock = every, clock
        self.cache: Dict[str, Tuple[float, dict]] = {}
        self.locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, base: str) -> threading.Lock:
        with self._guard:
            return self.locks.setdefault(base, threading.Lock())

    def cached(self, base: str, max_age: float = STALE) -> Optional[dict]:
        hit = self.cache.get(base)
        return hit[1] if hit and self.clock() - hit[0] <= max_age else None

    def get(self, http: Callable[..., bytes], base: str, wait: float = WAIT) -> Optional[dict]:
        """A look no older than ``every`` (blocking, at most about ``wait`` for the lock). None when
        another look is still under way and the last one is too old to show."""
        fresh = self.cached(base, self.every)
        if fresh is not None:
            return fresh
        lock = self._lock(base)
        if not lock.acquire(timeout=wait):
            return self.cached(base)
        try:
            fresh = self.cached(base, self.every)      # another thread may have looked meanwhile
            if fresh is None:
                fresh = look(http, base)
                self.cache[base] = (self.clock(), fresh)
            return fresh
        finally:
            lock.release()


SCRAPER = Scraper()


def live(snap: Optional[Mapping[str, Any]], model: str) -> Optional[dict]:
    """The Models card's live line for ``model``: GPU, tokens per second, cache. None without numbers."""
    if not snap or not snap.get("up") or snap.get("kind") != "llama-swap":
        return None
    out: Dict[str, Any] = {}
    g = (snap.get("gpus") or [None])[0]
    if g:
        out["gpu"] = {k: g[k] for k in ("name", "memory_used_bytes", "memory_total_bytes", "util_percent", "temperature_celsius", "power_watts") if k in g}
    m = next((x for x in snap.get("models") or [] if x.get("model") == model), None)
    L = (m or {}).get("llama")
    if m is not None:
        out["metrics_on"] = L is not None
    if L:
        def per(tokens: str, seconds: str) -> Optional[float]:
            t, s = L.get(tokens), L.get(seconds)
            return round(t / s, 1) if t and s else None
        out.update(prompt_tokens_per_s=per("prompt_tokens_total", "prompt_seconds_total"),
                   generation_tokens_per_s=per("tokens_predicted_total", "tokens_predicted_seconds_total"),
                   requests_processing=int(L.get("requests_processing", 0)), requests_deferred=int(L.get("requests_deferred", 0)))
        if L.get("prompt_tokens_total") and "prompt_tokens_cached_total" in L:
            # llama-server counts reused tokens apart from those it read; the hit rate is their share
            cached = L["prompt_tokens_cached_total"]
            out["prompt_cache_hit_ratio"] = round(cached / (cached + L["prompt_tokens_total"]), 3)
        if "kv_cache_usage_ratio" in L:
            out["kv_cache_usage_ratio"] = round(L["kv_cache_usage_ratio"], 3)
    return out or None


# ── the model guard's log ────────────────────────────────────────────────────────────────────

REQUEST_BUCKETS = (1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200)
WAIT_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120)
WHO = ("person", "speaker", "scheduled")
_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z\s+(.*)$")


def _stamp(ts: str, frac: Optional[str]) -> Tuple[int, int]:
    sec = int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    return sec, int(((frac or "") + "000000000")[:9])


def outcome(status: Any) -> str:
    if status == 200:
        return "ok"
    if status in ("loop_cut", "model_off", "dropped"):
        return str(status)
    return "error"


class GuardLog:
    """Counters and histograms over the guard's ``chat`` lines since the dashboard started.
    :meth:`read` asks Docker only for the lines after the last one seen (``--since``, ``--timestamps``)."""

    def __init__(self, since: Optional[float] = None):
        self.since = since if since is not None else time.time()
        self.last: Tuple[int, int] = (0, 0)
        self.requests: Dict[Tuple[str, str], int] = {}
        self.seconds: Dict[str, List[float]] = {}
        self.waits: Dict[str, List[float]] = {}
        self.tokens: Dict[Tuple[str, str], int] = {}
        self.loop_cuts: Dict[str, int] = {}
        self.read_ok: Optional[bool] = None

    @staticmethod
    def _hist(store: Dict[str, List[float]], who: str, buckets: tuple, v: float) -> None:
        row = store.setdefault(who, [0] * len(buckets) + [0, 0.0])     # buckets…, count, sum
        for i, b in enumerate(buckets):
            if v <= b:
                row[i] += 1
        row[-2] += 1
        row[-1] += v

    def add(self, r: Mapping[str, Any]) -> None:
        who = r.get("who") if r.get("who") in WHO else "person"
        out = outcome(r.get("status"))
        self.requests[(who, out)] = self.requests.get((who, out), 0) + 1
        if out == "loop_cut":
            self.loop_cuts[who] = self.loop_cuts.get(who, 0) + 1
        try:
            self._hist(self.seconds, who, REQUEST_BUCKETS, max(float(r.get("seconds") or 0), 0.0))
            self._hist(self.waits, who, WAIT_BUCKETS, max(float(r.get("waited_s") or 0), 0.0))
        except (TypeError, ValueError):
            pass
        for k in ("prompt_tokens", "completion_tokens"):
            if isinstance(r.get(k), int) and r[k] >= 0:
                self.tokens[(who, k)] = self.tokens.get((who, k), 0) + r[k]

    def feed(self, lines: List[str]) -> int:
        """Lines of ``docker logs --timestamps``; each newer than the last seen counts once. Returns how many."""
        n = 0
        for line in lines:
            m = _TS.match(line.strip())
            if not m or not m.group(3).startswith("{"):
                continue
            stamp = _stamp(m.group(1), m.group(2))
            if stamp <= self.last:
                continue
            self.last = stamp
            try:
                r = json.loads(m.group(3))
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("event") == "chat":
                self.add(r)
                n += 1
        return n

    def read(self, run: Callable[[List[str]], Any], container: str) -> None:
        if self.last > (0, 0):
            since = datetime.fromtimestamp(self.last[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{self.last[1]:09d}Z"
        else:
            since = str(int(self.since))
        try:
            p = run(["docker", "logs", "--timestamps", "--since", since, container])
            self.read_ok = p.returncode == 0
            if self.read_ok:
                self.feed(((p.stdout or "") + "\n" + (p.stderr or "")).splitlines())
        except Exception:
            self.read_ok = False


# ── the exposition ───────────────────────────────────────────────────────────────────────────

def _metric(out: List[str], name: str, kind: str, help_: str, samples: List[tuple]) -> None:
    from gateway.metrics import esc
    if not samples:
        return
    out.append(f"# HELP {name} {help_}")
    out.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
        v = str(int(value)) if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53 else repr(value)
        out.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")


# llama-server's numbers, re-published: (our name, its short name, type, help)
LLAMA = (
    ("logos_model_prompt_tokens_per_second", "prompt_tokens_seconds", "gauge", "llama-server: prompt reading speed since its last scrape (0 when idle; graph rate(prompt_tokens_processed_total)/rate(prompt_seconds_total) instead)."),
    ("logos_model_generation_tokens_per_second", "predicted_tokens_seconds", "gauge", "llama-server: writing speed since its last scrape (0 when idle)."),
    ("logos_model_server_requests_processing", "requests_processing", "gauge", "llama-server: requests being worked on now."),
    ("logos_model_server_requests_deferred", "requests_deferred", "gauge", "llama-server: requests waiting for a free slot."),
    ("logos_model_kv_cache_usage_ratio", "kv_cache_usage_ratio", "gauge", "llama-server: share of the KV cache in use (older builds only)."),
    ("logos_model_kv_cache_tokens", "kv_cache_tokens", "gauge", "llama-server: tokens in the KV cache (older builds only)."),
    ("logos_model_busy_slots_per_decode", "n_busy_slots_per_decode", "gauge", "llama-server: average busy slots per decode call."),
    ("logos_model_context_high_watermark_tokens", "n_tokens_max", "gauge", "llama-server: the most context tokens one request used."),
    ("logos_model_prompt_tokens_processed_total", "prompt_tokens_total", "counter", "llama-server: prompt tokens read (not reused from cache)."),
    ("logos_model_prompt_tokens_cached_total", "prompt_tokens_cached_total", "counter", "llama-server: prompt tokens reused from the cache."),
    ("logos_model_prompt_seconds_total", "prompt_seconds_total", "counter", "llama-server: seconds spent reading prompts."),
    ("logos_model_generated_tokens_total", "tokens_predicted_total", "counter", "llama-server: tokens written."),
    ("logos_model_generation_seconds_total", "tokens_predicted_seconds_total", "counter", "llama-server: seconds spent writing."),
    ("logos_model_decode_calls_total", "n_decode_total", "counter", "llama-server: llama_decode() calls."),
    ("logos_model_spec_draft_tokens_total", "spec_decode_num_draft_tokens_total", "counter", "llama-server: speculative draft tokens."),
    ("logos_model_spec_accepted_tokens_total", "spec_decode_num_accepted_tokens_total", "counter", "llama-server: speculative draft tokens accepted."),
)
GPU = (
    ("logos_model_gpu_memory_used_bytes", "memory_used_bytes", "GPU memory in use on the model PC."),
    ("logos_model_gpu_memory_total_bytes", "memory_total_bytes", "GPU memory on the model PC."),
    ("logos_model_gpu_utilization_percent", "util_percent", "GPU use on the model PC (0-100)."),
    ("logos_model_gpu_memory_utilization_percent", "memory_util_percent", "GPU memory bandwidth use (0-100)."),
    ("logos_model_gpu_temperature_celsius", "temperature_celsius", "GPU temperature on the model PC."),
    ("logos_model_gpu_power_watts", "power_watts", "GPU power draw on the model PC."),
)


def render_servers(snaps: Mapping[str, Optional[Mapping[str, Any]]]) -> str:
    """``{server: look or None}`` as Prometheus text. None (or not up): ``logos_model_server_up 0`` only."""
    out: List[str] = []
    up = {s: bool(v and v.get("up")) for s, v in snaps.items()}
    _metric(out, "logos_model_server_up", "gauge", "1 when the model server answers (from Logos's last look, at most 15 s old).",
            [({"server": s}, int(u)) for s, u in sorted(up.items())])
    swap = {s: v for s, v in snaps.items() if up[s] and v.get("kind") == "llama-swap"}
    models = [(s, m) for s, v in sorted(swap.items()) for m in v.get("models") or []]
    lab = lambda s, m: {"server": s, "model": m["model"]}
    _metric(out, "logos_model_loaded", "gauge", "1 when llama-swap has the model ready (0 while it starts or stops).",
            [(lab(s, m), int(m["state"] == "ready")) for s, m in models])
    _metric(out, "logos_model_info", "gauge", "The running model's settings from llama-swap's command line (always 1).",
            [({**lab(s, m), "state": m["state"], "kv_cache_type": m["kv_cache_type"], "n_cpu_moe": m["n_cpu_moe"],
               "flash_attention": m["flash_attention"]}, 1) for s, m in models])
    _metric(out, "logos_model_load_window_tokens", "gauge", "The model's window (-c), shared by its slots (0: not set).",
            [(lab(s, m), m["window"]) for s, m in models])
    _metric(out, "logos_model_parallel_slots", "gauge", "Requests llama-server runs at once (--parallel).",
            [(lab(s, m), m["parallel"]) for s, m in models])
    _metric(out, "logos_model_n_cpu_moe_layers", "gauge", "Layers whose experts stay on the CPU (--n-cpu-moe; -1: all).",
            [(lab(s, m), -1 if m["n_cpu_moe"] == "all" else int(m["n_cpu_moe"])) for s, m in models])
    ready = [(s, m) for s, m in models if m["state"] == "ready"]
    _metric(out, "logos_model_metrics_up", "gauge", "1 when llama-server answers /metrics (its command needs --metrics).",
            [(lab(s, m), int(m["llama"] is not None)) for s, m in ready])
    for name, short, kind, help_ in LLAMA:
        _metric(out, name, kind, help_, [(lab(s, m), float(m["llama"][short])) for s, m in ready if m["llama"] and short in m["llama"]])
    gs = [(s, g) for s, v in sorted(swap.items()) for g in v.get("gpus") or []]
    for name, field, help_ in GPU:
        _metric(out, name, "gauge", help_ + " From llama-swap (nvidia-smi).",
                [({"server": s, "gpu": g["id"], "name": g.get("name", "")}, float(g[field])) for s, g in gs if field in g])
    return "\n".join(out) + ("\n" if out else "")


def render_guard(g: GuardLog) -> str:
    out: List[str] = []
    if g.read_ok is not None:
        _metric(out, "logos_model_guard_log_up", "gauge", "1 when Logos could read the model guard's log.", [({}, int(g.read_ok))])
    _metric(out, "logos_model_requests_total", "counter", "Model requests through the guard by who asked and how they ended (ok, loop_cut, model_off, dropped, error).",
            [({"who": w, "outcome": o}, n) for (w, o), n in sorted(g.requests.items())])
    _metric(out, "logos_model_loop_cuts_total", "counter", "Replies the guard cut because their thinking went round in circles.",
            [({"who": w}, n) for w, n in sorted(g.loop_cuts.items())])
    _metric(out, "logos_model_prompt_tokens_total", "counter", "Prompt tokens the model server reported, by who asked.",
            [({"who": w}, n) for (w, k), n in sorted(g.tokens.items()) if k == "prompt_tokens"])
    _metric(out, "logos_model_completion_tokens_total", "counter", "Tokens the model wrote, by who asked.",
            [({"who": w}, n) for (w, k), n in sorted(g.tokens.items()) if k == "completion_tokens"])
    for name, store, buckets, help_ in (
            ("logos_model_request_seconds", g.seconds, REQUEST_BUCKETS, "How long a model request took, from its turn to its last token, by who asked."),
            ("logos_model_request_wait_seconds", g.waits, WAIT_BUCKETS, "How long a model request waited in the guard's queue, by who asked.")):
        if not store:
            continue
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} histogram")
        for who, row in sorted(store.items()):
            for i, b in enumerate(buckets):
                out.append(f'{name}_bucket{{who="{who}",le="{b}"}} {row[i]}')
            out.append(f'{name}_bucket{{who="{who}",le="+Inf"}} {row[-2]}')
            out.append(f'{name}_sum{{who="{who}"}} {round(row[-1], 3)}')
            out.append(f'{name}_count{{who="{who}"}} {row[-2]}')
    return "\n".join(out) + ("\n" if out else "")


# ── for gateway/metrics.py ───────────────────────────────────────────────────────────────────

class Collector:
    """What /metrics adds: the servers' looks and the guard's log, gathered off the event loop."""

    def __init__(self, scraper: Scraper = SCRAPER, guard: Optional[GuardLog] = None, every: float = EVERY,
                 clock: Callable[[], float] = time.monotonic):
        self.scraper, self.guard, self.every, self.clock = scraper, guard or GuardLog(), every, clock
        self.bases: Dict[str, str] = {}               # server key -> base URL
        self.log_at = -math.inf
        self.task: Optional[asyncio.Future] = None

    def gather(self, runtime: Any) -> None:
        """Blocking: every local model server an agent uses, and the guard's new log lines."""
        from gateway import dashboard_api, model_gateway, model_server
        bases = {model_server.server_key(b): b for b in model_server.wanted(dashboard_api._local_definitions(runtime))}
        self.bases = bases
        for b in bases.values():
            self.scraper.get(runtime.runner.http, b)
        if self.clock() - self.log_at >= self.every:
            self.log_at = self.clock()
            self.guard.read(runtime.runner.run, model_gateway.GUARD_CONTAINER)

    async def render(self, runtime: Any, wait: float = WAIT) -> str:
        if runtime is None:
            return ""
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(asyncio.to_thread(self.gather, runtime))
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=wait)
        except asyncio.TimeoutError:
            pass                                      # still looking: this scrape shows what is fresh enough
        except Exception:
            pass
        snaps = {key: self.scraper.cached(base) for key, base in self.bases.items()}
        return render_servers(snaps) + render_guard(self.guard)
