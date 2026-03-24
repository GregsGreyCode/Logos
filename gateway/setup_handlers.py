"""Setup wizard API handlers.

Endpoints:
  GET  /api/setup/probe    — probe a model server (Ollama or LM Studio / OpenAI-compatible)
  GET  /api/setup/scan     — sweep local subnet for model servers on :11434 and :1234
  POST /api/setup/pull     — SSE: stream Ollama model pull progress
  POST /api/setup/compare  — SSE: quick-benchmark candidates, recommend best model
  POST /api/setup/test     — SSE: full benchmark of selected model
  POST /api/setup/complete — save machine config and mark setup done
  POST /api/setup/test-k8s   — test Kubernetes connectivity

Probe query params:
  url      — base URL to probe, e.g. http://192.168.1.50:11434  (omit for localhost auto-scan)
  api_key  — optional Bearer token (for LM Studio with auth enabled)
"""

import asyncio
import json
import logging
import os
import pathlib
import re
import socket
import time

import aiohttp
from aiohttp import web

import gateway.auth.db as auth_db
from gateway import seed as _seed

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=4)
_SCAN_TIMEOUT  = aiohttp.ClientTimeout(total=1)   # aggressive — we're sweeping 254 hosts
_SCAN_PORTS    = [11434, 1234, 8080]   # Ollama, LM Studio, llama.cpp/vLLM default
_SCAN_CONCURRENCY = 40


def _own_ips() -> set[str]:
    """Return all IPv4 addresses that refer to this machine (for dedup)."""
    ips: set[str] = {"127.0.0.1", "localhost"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def _dedup_servers(results: list[dict]) -> list[dict]:
    """Deduplicate server list, collapsing LAN-IP entries that duplicate a
    localhost entry on the same port (same physical machine, different address)."""
    own = _own_ips()
    # Build a set of ports already covered by a localhost/loopback entry
    local_ports: set[int] = set()
    for r in results:
        if r.get("status") not in ("up", "auth_required"):
            continue
        try:
            from urllib.parse import urlparse
            p = urlparse(r["endpoint"])
            if p.hostname in ("localhost", "127.0.0.1"):
                local_ports.add(p.port)
        except Exception:
            pass

    seen: set[str] = set()
    unique: list[dict] = []
    for r in results:
        ep = r["endpoint"]
        if ep in seen:
            continue
        # Skip a LAN-IP entry for this machine if localhost already covers the same port
        try:
            from urllib.parse import urlparse
            p = urlparse(ep)
            if p.hostname in own and p.hostname not in ("localhost", "127.0.0.1") and p.port in local_ports:
                continue  # already represented by localhost:PORT
        except Exception:
            pass
        seen.add(ep)
        unique.append(r)
    return unique


# ── Probe helpers ──────────────────────────────────────────────────────────────

async def _probe_server(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str | None = None,
    prefer: str = "ollama",   # kept for API compat but no longer used for typing
    timeout: aiohttp.ClientTimeout = _PROBE_TIMEOUT,
) -> dict:
    """Probe a single base URL and return its type, models, and status.

    Detection strategy (port-agnostic):
      1. GET /api/version  — returns {"version":"x.y.z"} on Ollama only.
         LM Studio does NOT expose this endpoint.  Definitive Ollama signal.
      2. GET /v1/models    — OpenAI-compatible; present on both LM Studio and
         Ollama.  If we reach here without step 1 succeeding → LM Studio
         (or another OpenAI-compat server).
    """
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # ── Step 1: /api/version — definitive Ollama fingerprint ─────────────
    try:
        async with session.get(f"{base}/api/version", timeout=timeout) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                if "version" in data:
                    # Confirmed Ollama — fetch model list via /api/tags
                    models: list[dict] = []
                    try:
                        async with session.get(f"{base}/api/tags", timeout=timeout) as tr:
                            if tr.status == 200:
                                td = await tr.json(content_type=None)
                                models = [
                                    {"id": m["name"], "name": m["name"], "size": m.get("size", 0)}
                                    for m in td.get("models", [])
                                ]
                    except Exception:
                        pass
                    return {"type": "ollama", "endpoint": f"{base}/v1", "status": "up", "models": models}
    except Exception:
        pass

    # ── Step 2: /v1/models — LM Studio / OpenAI-compat ───────────────────
    try:
        async with session.get(f"{base}/v1/models", headers=headers, timeout=timeout) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                models = [
                    {"id": m["id"], "name": m["id"], "size": 0}
                    for m in data.get("data", [])
                ]
                return {"type": "lmstudio", "endpoint": f"{base}/v1", "status": "up", "models": models}
            if r.status == 401:
                return {"type": "lmstudio", "endpoint": f"{base}/v1", "status": "auth_required", "models": []}
    except Exception:
        pass

    return {"type": "unknown", "endpoint": f"{base}/v1", "status": "down", "models": []}


def _local_subnet() -> str | None:
    """Return the /24 subnet to scan, e.g. '192.168.1'.

    In Kubernetes, the pod's CNI IP (10.244.x.x) is the wrong subnet —
    we want the node's LAN IP instead.  The deployment injects NODE_IP
    via the downward API (status.hostIP) for exactly this case.
    """
    # K8s: prefer the node's LAN IP injected via downward API
    node_ip = os.environ.get("NODE_IP", "").strip()
    if node_ip and not node_ip.startswith("127."):
        parts = node_ip.split(".")
        if len(parts) == 4:
            logger.debug("scan subnet from NODE_IP: %s", node_ip)
            return ".".join(parts[:3])

    # Bare-metal / Docker: use outbound interface IP (no packet sent)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        parts = ip.split(".")
        if len(parts) == 4 and not ip.startswith("127."):
            return ".".join(parts[:3])
    except Exception:
        pass
    return None


async def _scan_host(session: aiohttp.ClientSession, ip: str) -> list[dict]:
    """Check both model-server ports on a single IP; return found servers."""
    found = []
    for port in _SCAN_PORTS:
        result = await _probe_server(
            session, f"http://{ip}:{port}", api_key=None, timeout=_SCAN_TIMEOUT
        )
        if result["status"] in ("up", "auth_required"):
            found.append(result)
    return found


# ── Route handlers ─────────────────────────────────────────────────────────────

async def handle_setup_probe(request: web.Request) -> web.Response:
    """Probe a model server.

    With ?url=...: probe that specific address.
    Without url:   scan localhost defaults (Ollama :11434, LM Studio :1234).
    Optional: ?api_key=... for servers with auth enabled.
    Optional: ?prefer=ollama|lmstudio — controls which probe path is tried first;
              defaults to 'ollama'. Pass 'lmstudio' when probing an LM Studio server
              to skip the /api/tags check (LM Studio now responds to that endpoint
              in Ollama-compat mode, which would cause misclassification).
    """
    raw_url = (request.query.get("url") or "").strip()
    api_key = request.query.get("api_key") or None
    prefer  = request.query.get("prefer") or "ollama"

    async with aiohttp.ClientSession() as session:
        if raw_url:
            result = await _probe_server(session, raw_url, api_key, prefer=prefer)
            return web.json_response({"servers": [result]})

        # Auto-detect: probe localhost AND node IP (K8s pods see pod CNI, not node LAN)
        targets = [
            ("http://localhost:11434", None,    "ollama"),
            ("http://localhost:1234",  api_key, "lmstudio"),
        ]
        node_ip = os.environ.get("NODE_IP", "").strip()
        if node_ip and not node_ip.startswith("127."):
            targets += [
                (f"http://{node_ip}:11434", None,    "ollama"),
                (f"http://{node_ip}:1234",  api_key, "lmstudio"),
            ]
        results = await asyncio.gather(*[
            _probe_server(session, url, key, prefer=prefer_type)
            for url, key, prefer_type in targets
        ])
    return web.json_response({"servers": _dedup_servers(results)})


async def handle_setup_scan(request: web.Request) -> web.Response:
    """Sweep the local /24 subnet for model servers on :11434, :1234, and :8080.

    Returns all discovered servers sorted by IP.  Localhost is always
    checked first and prepended to the results so it ranks highest.
    """
    subnet = _local_subnet()
    results: list[dict] = []

    connector = aiohttp.TCPConnector(limit=_SCAN_CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Always check localhost first (covers Docker / WSL scenarios)
        local_results = await asyncio.gather(
            _probe_server(session, "http://localhost:11434", None),
            _probe_server(session, "http://localhost:1234", None),
            _probe_server(session, "http://localhost:8080", None),
        )
        for r in local_results:
            if r["status"] in ("up", "auth_required"):
                results.append(r)

        if subnet:
            hosts = [f"{subnet}.{i}" for i in range(1, 255)]
            sem = asyncio.Semaphore(_SCAN_CONCURRENCY)

            async def probe_with_sem(ip: str) -> list[dict]:
                async with sem:
                    return await _scan_host(session, ip)

            batches = await asyncio.gather(*[probe_with_sem(h) for h in hosts])
            for batch in batches:
                results.extend(batch)

    unique = _dedup_servers(results)
    logger.info("setup scan: subnet=%s found=%d", subnet or "localhost-only", len(unique))
    return web.json_response({"servers": unique, "subnet": subnet})


async def handle_setup_pull(request: web.Request) -> web.Response:
    """Stream Ollama model pull progress via SSE."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    base_url = (body.get("base_url") or "http://localhost:11434").rstrip("/")
    model = (body.get("model") or "llama3.2:3b").strip()

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    async def send(data: dict) -> None:
        await response.write(f"data: {json.dumps(data)}\n\n".encode())

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/api/pull",
                json={"name": model, "stream": True},
                timeout=aiohttp.ClientTimeout(total=600),
            ) as r:
                async for raw in r.content:
                    line = raw.decode().strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        status = chunk.get("status", "")
                        total = chunk.get("total") or 0
                        completed = chunk.get("completed") or 0
                        pct = int(completed / total * 100) if total > 0 else 0
                        await send({"status": status, "pct": pct, "done": status == "success"})
                        if status == "success":
                            break
                    except Exception:
                        pass
    except Exception as e:
        await send({"error": str(e), "done": False})

    return response


# ── Benchmark prompts ────────────────────────────────────────────────────────
# Pass 1: prose — baseline throughput under natural language generation
_BENCH_PROMPT = (
    "Briefly explain three steps you would take to debug a program that crashes unexpectedly. "
    "Be concise."
)
# Pass 2: structured — throughput under formatting constraints (some models slow here)
_BENCH_PROMPT_STRUCT = (
    'Output exactly this JSON and nothing else: '
    '{"steps": ["reproduce error", "add logging", "fix and test"]}'
)

# Test 1 — Instruction following: 4-step ordered task, all string outputs must be present
# Note: all expected outputs are unambiguous strings/numbers — avoids word/digit variance
_INSTRUCT_PROMPT = (
    "Follow these instructions exactly, one item per line:\n"
    "1. Write the word ALPHA\n"
    "2. Write the result of 15 + 27\n"
    "3. Write the word BETA\n"
    "4. Write the word GAMMA"
)
_INSTRUCT_CHECKS = ["ALPHA", "42", "BETA", "GAMMA"]

# Test 2 — Reasoning: two-part arithmetic, both answers must be present
_REASON_PROMPT = (
    "Answer both with only numbers, one per line:\n"
    "1. A car covers 150 km in 2.5 hours. Speed in km/h?\n"
    "2. What is 17 × 6 − 14?"
)
_REASON_CHECKS = ["60", "88"]  # 150/2.5=60, 102-14=88

# Test 3 — Strict format compliance: exact JSON only, no surrounding prose accepted
_FORMAT_PROMPT = 'Reply with ONLY a valid JSON object, no other text: {"status": "ok", "value": 99}'

# Test 4 — Tool selection: two scenarios, must route both correctly
_TOOL_PROMPT = (
    'You have two tools: "search_web" (for current information) and "run_code" (for Python execution). '
    'For each request, choose the correct tool:\n'
    'A: "What is the current Bitcoin price?"\n'
    'B: "Write a Python function to reverse a list."\n'
    'Reply with ONLY valid JSON: {"A": "<tool_name>", "B": "<tool_name>"}'
)

# Test 5 — Nested JSON schema: requires correct nesting, array, mixed types
# Harder than flat JSON (test 3) — small models that pass flat JSON often fail nesting
_NESTED_JSON_PROMPT = (
    'Reply with ONLY valid JSON, no other text:\n'
    '{"id": 7, "tags": ["agent", "llm"], "meta": {"active": true}}'
)

# Test 6 — Multi-step word problem: three chained multiplications
# Tests chained arithmetic reasoning beyond the 2-step test 2
_MULTIHOP_PROMPT = (
    "Answer with a single number only:\n"
    "A box has 5 layers. Each layer has 4 rows of oranges. Each row has 6 oranges. "
    "How many oranges are in the box in total?"
)
_MULTIHOP_CHECK = "120"   # 5 × 4 × 6 = 120


def _strip_think(text: str) -> str:
    """Strip <think>...</think> (and <thinking>) blocks produced by reasoning models.

    Qwen3 and similar models emit a chain-of-thought block before the real answer.
    Removing it lets the eval checks see only the final response.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _eval_tool_call(text: str) -> bool:
    """Return True if response is valid JSON routing A→search_web and B→run_code."""
    cleaned = re.sub(r"```[a-z]*\n?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        return obj.get("A") == "search_web" and obj.get("B") == "run_code"
    except Exception:
        return False


def _eval_nested_json(text: str) -> bool:
    """Return True if response is valid JSON with correct nested structure and values."""
    cleaned = re.sub(r"```[a-z]*\n?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        return (
            obj.get("id") == 7
            and isinstance(obj.get("tags"), list)
            and "agent" in obj["tags"]
            and "llm" in obj["tags"]
            and isinstance(obj.get("meta"), dict)
            and obj["meta"].get("active") is True
        )
    except Exception:
        return False


def _eval_multihop(text: str) -> bool:
    """Return True if the response contains the correct multi-step answer."""
    return _MULTIHOP_CHECK in text

_COMPARE_TIMEOUT  = aiohttp.ClientTimeout(total=120)   # per-model; handles cold-start


def _eval_format(text: str) -> bool:
    """Return True if the response is exactly valid JSON with status=ok and value=99.

    Strict: no prose before or after the JSON object is accepted.
    Models that wrap in markdown fences are tolerated (fences stripped first).
    """
    cleaned = re.sub(r"```[a-z]*\n?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        return obj.get("status") == "ok" and obj.get("value") == 99
    except Exception:
        return False


def _parse_model_size_b(model_id: str) -> float:
    """Extract parameter count in billions from a model ID string. Returns 0 if unknown."""
    m = re.search(r'(\d+(?:\.\d+)?)\s*b(?:[^a-z]|$)', model_id.lower())
    return float(m.group(1)) if m else 0.0


_INSTRUCT_KEYWORDS = {"instruct", "chat", "it", "tool", "assistant", "hermes", "qwen", "gemma", "llama"}


def _pick_compare_candidates(model_ids: list[str], max_n: int = 4) -> list[str]:
    """Sample one representative from each size bucket, then fill remaining slots.

    Buckets: small (<5B), mid (5–13B), large (>13B), unknown (no size in name).
    Within each bucket models are ranked by quality heuristics:
      - mid/large: prefer closer to 9B sweet spot; larger wins ties
      - small: prefer larger (closer to 4–5B)
      - unknown: prefer names containing instruct/chat/tool keywords
    This avoids the old approach of hard-suppressing unknowns or large quants.
    """
    def _bucket(mid: str) -> str:
        sz = _parse_model_size_b(mid)
        if sz == 0:   return "unknown"
        if sz < 5:    return "small"
        if sz <= 13:  return "mid"
        return "large"

    def _rank(mid: str, bucket: str) -> float:
        sz = _parse_model_size_b(mid)
        if bucket == "mid":    return abs(sz - 9.0)
        if bucket == "small":  return -sz                 # larger small = better
        if bucket == "large":  return sz                  # smaller large = better
        # unknown: prefer instruct-hinted names
        name = mid.lower()
        return 0.0 if any(kw in name for kw in _INSTRUCT_KEYWORDS) else 1.0

    buckets: dict[str, list[str]] = {"mid": [], "small": [], "large": [], "unknown": []}
    for mid in model_ids:
        b = _bucket(mid)
        buckets[b].append(mid)
    for b, items in buckets.items():
        items.sort(key=lambda m: _rank(m, b))

    # One slot per bucket (priority: mid → small → large → unknown), then fill
    selected: list[str] = []
    for b in ("mid", "small", "large", "unknown"):
        for mid in buckets[b]:
            if mid not in selected:
                selected.append(mid)
                break
    for b in ("mid", "small", "large", "unknown"):
        for mid in buckets[b]:
            if mid not in selected:
                selected.append(mid)
                if len(selected) >= max_n:
                    return selected
    return selected[:max_n]


def _compare_score(r: dict) -> float:
    """Composite score for ranking models within a gated candidate pool.

    Weights: eval capability 60%, throughput 20%, TTFT 15%, model size 5%.
    JSON format and tool-call are handled as mandatory gates before ranking
    (see handle_setup_compare), not as score components here.
    """
    if r.get("error") or r.get("tok_s", 0) == 0:
        return -1.0
    eval_score = r.get("eval", {}).get("score", 1 if r.get("quality_pass") else 0)
    eval_frac  = eval_score / 6                      # 0.0 – 1.0
    speed      = min(r["tok_s"], 40) / 40            # cap at 40 tok/s
    sz         = _parse_model_size_b(r["model"])
    size_b     = min(sz, 13) / 13 * 0.05 if sz > 0 else 0
    ttft_ms    = r.get("ttft_ms")
    # TTFT score: ≤500ms→1.0, ≥4000ms→0.0
    ttft_score = max(0.0, min(1.0, (4000 - ttft_ms) / 3500)) if ttft_ms is not None else 0.5
    return 0.60 * eval_frac + 0.20 * speed + 0.15 * ttft_score + size_b


def _compare_reason(best: dict) -> str:
    label, _ = _bench_score(best["tok_s"])
    ev        = best.get("eval", {})
    score     = ev.get("score", 1 if best.get("quality_pass") else 0)
    parts: list[str] = []
    if ev.get("instruction"): parts.append("follows instructions")
    if ev.get("reasoning"):   parts.append("reasons correctly")
    if ev.get("format"):      parts.append("structured output")
    if ev.get("tool_call"):   parts.append("tool selection")
    eval_str  = ", ".join(parts) if parts else "limited capability confirmed"
    ttft_ms   = best.get("ttft_ms")
    ttft_note = f", {ttft_ms}ms TTFT" if ttft_ms is not None else ""

    if score == 6 and best["tok_s"] >= 15:
        return (f"{label} at {best['tok_s']} tok/s{ttft_note} — passes all 6 eval tests "
                f"({eval_str}). Strong default agent model for your hardware.")
    if score >= 4 and best["tok_s"] >= 10:
        return (f"{label} at {best['tok_s']} tok/s{ttft_note} — passes {score}/6 eval tests "
                f"({eval_str}). Solid baseline agent model.")
    if score >= 4:
        return (f"Passes {score}/6 eval tests but slow at {best['tok_s']} tok/s. "
                f"Consider a smaller quantised model for better responsiveness.")
    return (f"{label} at {best['tok_s']} tok/s but only {score}/6 eval tests passed. "
            f"A general-purpose model (e.g. Qwen3-8B, Gemma3-9B) will work better.")


def _fast_reason(r: dict) -> str:
    """Short reason string for the fastest-acceptable recommendation."""
    label, _ = _bench_score(r["tok_s"])
    ttft_ms   = r.get("ttft_ms")
    ttft_note = f", {ttft_ms}ms TTFT" if ttft_ms is not None else ""
    score     = r.get("eval", {}).get("score", 0)
    return (f"{label} at {r['tok_s']} tok/s{ttft_note} — {score}/6 evals. "
            f"Best speed choice that passes the format and tool-call gates.")


def _bench_score(tok_s: float) -> tuple[str, str]:
    """Return (label, colour) based on tokens/sec."""
    if tok_s >= 30:  return ("Fast",   "green")
    if tok_s >= 15:  return ("Good",   "indigo")
    if tok_s >= 6:   return ("Usable", "yellow")
    return ("Slow", "red")


async def _stream_chat(
    session: aiohttp.ClientSession,
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int = 80,
) -> tuple[str, float, float, int]:
    """Return (response_text, ttft_s, total_s, token_count)."""
    t0 = time.time()
    ttft: float | None = None
    text = ""
    tok_count = 0
    try:
        async with session.post(
            f"{endpoint}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "max_tokens": max_tokens,
            },
            timeout=aiohttp.ClientTimeout(total=45),
        ) as r:
            if r.status != 200:
                body = await r.text()
                raise RuntimeError(f"HTTP {r.status}: {body[:200]}")
            _buf = ""
            _done = False
            async for raw in r.content:
                if _done:
                    continue
                _buf += raw.decode(errors="replace")
                while "\n" in _buf:
                    _line, _buf = _buf.split("\n", 1)
                    _line = _line.strip()
                    if not _line.startswith("data: "):
                        continue
                    data = _line[6:]
                    if data == "[DONE]":
                        _done = True
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            if ttft is None:
                                ttft = time.time() - t0
                            text += delta
                            tok_count += 1
                    except Exception:
                        pass
    except aiohttp.ServerDisconnectedError:
        if not text:
            raise
    return text, ttft or 0.0, time.time() - t0, tok_count


async def handle_setup_compare(request: web.Request) -> web.Response:
    """SSE: quick-benchmark up to 4 candidate models and recommend the best one.

    Events emitted:
      {"targets": [model_id, ...]}
      {"testing": model_id}
      {"loading_model": model_id}
      {"log": "message"}
      {"result": {model, tok_s, quality_pass, ttft_ms, error?}}
      {"done": true, "recommendation": model_id|null, "reason": str, "results": [...]}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    fallback_endpoint = (body.get("endpoint") or "").rstrip("/")
    fallback_key      = body.get("api_key") or "ollama"
    fallback_type     = body.get("server_type") or "unknown"
    raw_models        = body.get("models") or []

    if not fallback_endpoint or not raw_models:
        return web.json_response({"error": "endpoint and models required"}, status=400)

    # Accept per-model specs [{id, endpoint, api_key, server_type}] or plain strings
    model_specs: list[dict] = []
    for m in raw_models:
        if isinstance(m, str):
            model_specs.append({"id": m, "endpoint": fallback_endpoint, "api_key": fallback_key, "server_type": fallback_type})
        else:
            model_specs.append({
                "id":          m["id"],
                "endpoint":    (m.get("endpoint") or fallback_endpoint).rstrip("/"),
                "api_key":     m.get("api_key") or fallback_key,
                "server_type": m.get("server_type") or fallback_type,
            })

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    async def send(data: dict) -> None:
        await response.write(f"data: {json.dumps(data)}\n\n".encode())

    # Pick candidates per-server so each machine gets its own benchmark pool
    # (up to 4 candidates from each server's model list independently)
    from collections import defaultdict as _dd
    per_server_specs: dict[str, list[dict]] = _dd(list)
    for s in model_specs:
        per_server_specs[s["endpoint"]].append(s)

    server_groups: dict[str, list[dict]] = _dd(list)
    for ep, specs in per_server_specs.items():
        ep_candidate_ids = set(_pick_compare_candidates([s["id"] for s in specs]))
        for s in specs:
            if s["id"] in ep_candidate_ids:
                server_groups[ep].append(s)

    candidates = [m for group in server_groups.values() for m in group]
    n_servers = len(server_groups)
    await send({"targets": [c["id"] for c in candidates]})
    await send({"log": (
        f"Testing {len(candidates)} model{'s' if len(candidates) != 1 else ''} "
        f"across {n_servers} server{'s' if n_servers != 1 else ''}"
        + (" — each server benchmarked independently in parallel" if n_servers > 1 else "")
    )})

    results: list[dict] = []
    results_lock = asyncio.Lock()

    async def _flush_lmstudio_vram(base_url: str, http: aiohttp.ClientSession) -> None:
        """Unload every currently-loaded model from LM Studio before benchmarking.

        Models loaded outside our session (e.g. olmocr left loaded by the user)
        will consume VRAM and throttle throughput for every model we test.
        """
        try:
            async with http.get(
                f"{base_url}/api/v1/models",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    return
                data = await r.json(content_type=None)
                loaded = [m["id"] for m in data.get("data", []) if m.get("state") in ("loaded", "loading", None)]
        except Exception:
            return
        for mid in loaded:
            try:
                async with http.post(
                    f"{base_url}/api/v1/models/unload",
                    json={"instance_id": mid},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as ur:
                    await send({"log": f"  Pre-flush: unloaded {mid} (HTTP {ur.status})"})
            except Exception as e:
                await send({"log": f"  Pre-flush: could not unload {mid}: {str(e)[:60]}"})
        if loaded:
            await asyncio.sleep(1.5)   # give LM Studio time to actually free VRAM

    async def _test_server_group(group: list[dict], http: aiohttp.ClientSession) -> None:
        """Test all models for one server sequentially (protects that server's VRAM)."""
        # Flush any pre-existing models from VRAM before we start
        first_spec = group[0]
        if first_spec["server_type"] == "lmstudio":
            base_url_flush = re.sub(r"/v1/?$", "", first_spec["endpoint"])
            await send({"log": f"Clearing VRAM on {base_url_flush}…"})
            await _flush_lmstudio_vram(base_url_flush, http)

        for spec in group:
            model_id    = spec["id"]
            endpoint    = spec["endpoint"]
            api_key     = spec["api_key"]
            server_type = spec["server_type"]
            base_url    = re.sub(r"/v1/?$", "", endpoint)

            await send({"testing": model_id})
            await send({"log": f"→ {model_id}  ({base_url})"})

            # LM Studio: try native load API before benchmarking (best-effort)
            if server_type == "lmstudio":
                try:
                    async with http.post(
                        f"{base_url}/api/v1/models/load",
                        json={"model": model_id, "context_length": 4096},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as lr:
                        if lr.status == 200:
                            await send({"log": f"  Load request sent for {model_id}"})
                except Exception:
                    pass

            t0                   = time.time()
            ttft_s: float | None = None

            async def _model_hint(mid: str = model_id) -> None:
                await asyncio.sleep(8.0)
                await send({"loading_model": mid})
                await send({"log": f"  Loading {mid} into memory…"})

            hint_task = asyncio.create_task(_model_hint())

            async def _bench_run(pass_num: int, prompt: str, max_tokens: int = 120) -> tuple[float, bool]:
                """Return (tok_s, approx) — approx=True when usage.completion_tokens unavailable.

                pass_num == 0 is a warmup: loads the model into memory, result discarded.
                TTFT is only captured on pass_num == 1 (first real pass, model already hot).
                """
                nonlocal ttft_s
                t_start    = time.time()
                tok_count  = 0
                char_count = 0
                t_first: float | None  = None
                usage_toks: int | None = None

                _stream_done = False
                _status_code = None
                try:
                    async with http.post(
                        f"{endpoint}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model_id,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": True,
                            "max_tokens": max_tokens,
                            "stream_options": {"include_usage": True},
                        },
                        timeout=_COMPARE_TIMEOUT,
                    ) as r:
                        _status_code = r.status
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}")
                        _buf = ""
                        async for raw in r.content:
                            if _stream_done:
                                continue
                            # Split chunk by newlines — servers may batch multiple SSE events
                            _buf += raw.decode(errors="replace")
                            while "\n" in _buf:
                                _line, _buf = _buf.split("\n", 1)
                                _line = _line.strip()
                                if not _line.startswith("data: "):
                                    continue
                                chunk_data = _line[6:]
                                if chunk_data == "[DONE]":
                                    _stream_done = True
                                    break
                                try:
                                    chunk = json.loads(chunk_data)
                                    if chunk.get("usage", {}).get("completion_tokens"):
                                        usage_toks = chunk["usage"]["completion_tokens"]
                                    content = chunk["choices"][0]["delta"].get("content") or ""
                                    if content:
                                        if t_first is None:
                                            t_first = time.time()
                                            if pass_num == 1:
                                                ttft_s = t_first - t0
                                                hint_task.cancel()
                                                await send({"log": f"  First token: {ttft_s:.1f}s TTFT"})
                                        tok_count  += 1
                                        char_count += len(content)
                                except Exception:
                                    pass
                except aiohttp.ServerDisconnectedError:
                    # llama.cpp (and some other servers) close the TCP connection
                    # immediately after the final [DONE] chunk rather than waiting
                    # for the client to drain.  Treat this as a normal end-of-stream
                    # if we received content; re-raise only if we got nothing at all.
                    if not t_first:
                        raise

                end_t  = time.time()
                gen_s  = (end_t - t_first) if t_first else (end_t - t_start)
                if gen_s <= 0:
                    return 0.0, True
                approx      = usage_toks is None
                actual_toks = usage_toks or max(tok_count, round(char_count / 4))
                return actual_toks / gen_s, approx

            try:
                # ── Warmup pass — ensures model is fully loaded before timing ──
                # Result is discarded; this prevents the cold-start penalty from
                # skewing pass 1 (seen as ~33% of real throughput on first model).
                await send({"log": "  Warmup (loading model into memory)…"})
                try:
                    await _bench_run(0, "Reply with one word.", max_tokens=10)
                    hint_task.cancel()
                    await send({"log": "  Model ready."})
                except Exception as _we:
                    await send({"log": f"  Warmup failed ({str(_we)[:80]}), continuing anyway…"})
                # Reset t0 so TTFT on pass 1 is measured from a hot-model start
                t0 = time.time()

                # ── 3-pass speed benchmark, take median (discards outliers) ──
                r1, approx1 = await _bench_run(1, _BENCH_PROMPT)
                await send({"log": f"  Pass 1 (prose): {r1:.1f} tok/s"})
                r2, approx2 = await _bench_run(2, _BENCH_PROMPT_STRUCT)
                await send({"log": f"  Pass 2 (structured): {r2:.1f} tok/s"})
                r3, approx3 = await _bench_run(3, _BENCH_PROMPT)
                await send({"log": f"  Pass 3 (prose): {r3:.1f} tok/s"})

                tok_s   = round(sorted([r1, r2, r3])[1])   # median of 3
                approx  = approx1 and approx2 and approx3
                ttft_ms = round(ttft_s * 1000) if ttft_s is not None else None
                approx_note = " (~approx)" if approx else ""
                await send({"log": f"  Median: {tok_s} tok/s{approx_note}"})

                # ── Capability evals — each retried once on failure ────────────
                async def _eval_once(prompt: str, check_fn, max_tokens: int) -> tuple[bool, str]:
                    """Run eval, retry once if it fails. Returns (passed, last_stripped_response).

                    max_tokens is set high enough to absorb <think> chains (Qwen3 etc.)
                    plus the actual answer. _strip_think removes the reasoning block
                    before the check function sees the text.
                    """
                    last_stripped = ""
                    for attempt in range(2):
                        try:
                            txt, _, _, _ = await _stream_chat(
                                http, endpoint, model_id, api_key, prompt, max_tokens=max_tokens
                            )
                            last_stripped = _strip_think(txt)
                            if check_fn(last_stripped):
                                return True, last_stripped
                        except Exception as e:
                            last_stripped = f"[error: {str(e)[:80]}]"
                        if attempt == 0:
                            await asyncio.sleep(0.5)   # brief pause before retry
                    return False, last_stripped

                async def _log_eval_detail(prompt: str, response: str, missing: list[str] | None = None) -> None:
                    """Emit detail lines (prompt snippet, response, missing checks) on eval failure."""
                    prompt_preview = (prompt[:120] + "…") if len(prompt) > 120 else prompt
                    resp_preview = (response[:300] + "…") if len(response) > 300 else (response or "(empty)")
                    await send({"log": f"      prompt: {prompt_preview}"})
                    await send({"log": f"      got:    {resp_preview}"})
                    if missing:
                        await send({"log": f"      missing: {', '.join(missing)}"})

                await send({"log": "  Eval 1/6: instruction following…"})
                instruct_pass, instruct_resp = await _eval_once(
                    _INSTRUCT_PROMPT,
                    lambda t: all(c in t for c in _INSTRUCT_CHECKS),
                    600,
                )
                await send({"log": f"    {'✓' if instruct_pass else '✗'} instruction following"})
                if not instruct_pass:
                    missing = [c for c in _INSTRUCT_CHECKS if c not in instruct_resp]
                    await _log_eval_detail(_INSTRUCT_PROMPT, instruct_resp, missing)

                await send({"log": "  Eval 2/6: reasoning (2-part)…"})
                reason_pass, reason_resp = await _eval_once(
                    _REASON_PROMPT,
                    lambda t: all(c in t for c in _REASON_CHECKS),
                    600,
                )
                await send({"log": f"    {'✓' if reason_pass else '✗'} reasoning"})
                if not reason_pass:
                    missing = [c for c in _REASON_CHECKS if c not in reason_resp]
                    await _log_eval_detail(_REASON_PROMPT, reason_resp, missing)

                await send({"log": "  Eval 3/6: strict JSON format…"})
                format_pass, format_resp = await _eval_once(_FORMAT_PROMPT, _eval_format, 600)
                await send({"log": f"    {'✓' if format_pass else '✗'} JSON format"})
                if not format_pass:
                    await _log_eval_detail(_FORMAT_PROMPT, format_resp)

                await send({"log": "  Eval 4/6: tool selection (2 scenarios)…"})
                tool_pass, tool_resp = await _eval_once(_TOOL_PROMPT, _eval_tool_call, 600)
                await send({"log": f"    {'✓' if tool_pass else '✗'} tool selection"})
                if not tool_pass:
                    await _log_eval_detail(_TOOL_PROMPT, tool_resp)

                await send({"log": "  Eval 5/6: nested JSON schema…"})
                nested_json_pass, nested_resp = await _eval_once(_NESTED_JSON_PROMPT, _eval_nested_json, 600)
                await send({"log": f"    {'✓' if nested_json_pass else '✗'} nested JSON"})
                if not nested_json_pass:
                    await _log_eval_detail(_NESTED_JSON_PROMPT, nested_resp)

                await send({"log": "  Eval 6/6: multi-step reasoning…"})
                multihop_pass, multihop_resp = await _eval_once(_MULTIHOP_PROMPT, _eval_multihop, 600)
                await send({"log": f"    {'✓' if multihop_pass else '✗'} multi-step reasoning"})
                if not multihop_pass:
                    expected = _MULTIHOP_CHECK
                    await _log_eval_detail(_MULTIHOP_PROMPT, multihop_resp, [f"expected '{expected}'"])

                tests_passed = sum([instruct_pass, reason_pass, format_pass, tool_pass, nested_json_pass, multihop_pass])
                quality_pass = tests_passed >= 4
                await send({"log": f"  {'✓' if quality_pass else '⚠'} {tok_s} tok/s{approx_note} · {tests_passed}/6 eval tests passed"})
                result: dict = {
                    "model": model_id, "tok_s": tok_s, "quality_pass": quality_pass,
                    "ttft_ms": ttft_ms, "approx": approx, "endpoint": endpoint, "eval": {
                        "instruction": instruct_pass, "reasoning": reason_pass,
                        "format": format_pass, "tool_call": tool_pass,
                        "nested_json": nested_json_pass, "multihop": multihop_pass,
                        "score": tests_passed,
                    },
                }
            except Exception as exc:
                err_msg = str(exc)
                await send({"log": f"  ✗ Error: {err_msg[:200]}"})
                result = {"model": model_id, "tok_s": 0, "quality_pass": False, "ttft_ms": None, "approx": False, "endpoint": endpoint, "error": err_msg[:300]}
            finally:
                hint_task.cancel()
                try:
                    if server_type == "lmstudio":
                        ur = await http.post(
                            f"{base_url}/api/v1/models/unload",
                            json={"instance_id": model_id},
                            timeout=aiohttp.ClientTimeout(total=5),
                        )
                        await send({"log": f"  Unloaded {model_id} (HTTP {ur.status})"})
                    elif server_type == "ollama":
                        ur = await http.post(
                            f"{base_url}/api/generate",
                            json={"model": model_id, "keep_alive": 0, "prompt": ""},
                            timeout=aiohttp.ClientTimeout(total=5),
                        )
                        await send({"log": f"  Unloaded {model_id} (HTTP {ur.status})"})
                except Exception as ue:
                    await send({"log": f"  Unload skipped: {str(ue)[:60]}"})

            async with results_lock:
                results.append(result)
            await send({"result": result})

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[
            _test_server_group(group, session)
            for group in server_groups.values()
        ])

    valid = [r for r in results if not r.get("error") and r.get("tok_s", 0) > 0]

    # Mandatory gates: JSON format + tool-call selection are critical for agent use.
    # Models that pass both are ranked first; if none pass, fall back to all valid models.
    gated = [r for r in valid if r.get("eval", {}).get("format") and r.get("eval", {}).get("tool_call")]
    pool  = gated if gated else valid
    if gated:
        await send({"log": f"{len(gated)}/{len(valid)} model(s) passed format+tool gates"})
    elif valid:
        await send({"log": "⚠ No model passed format+tool gates — ranking all valid models"})

    # Best balanced: highest composite score
    best = max(pool, key=_compare_score) if pool else None

    # Fastest acceptable: highest tok/s from gated pool (only surface if different from best)
    fast = max(pool, key=lambda r: r.get("tok_s", 0)) if pool else None
    fast_rec    = fast["model"]    if fast and fast is not best else None
    fast_reason = _fast_reason(fast) if fast and fast is not best else None

    # Per-server recommendations — best model on each individual inference machine
    per_server_recs: dict[str, dict] = {}
    for ep, group in server_groups.items():
        ep_valid = [r for r in valid if r.get("endpoint") == ep]
        ep_gated = [r for r in ep_valid if r.get("eval", {}).get("format") and r.get("eval", {}).get("tool_call")]
        ep_pool  = ep_gated if ep_gated else ep_valid
        if ep_pool:
            ep_best = max(ep_pool, key=_compare_score)
            per_server_recs[ep] = {"model": ep_best["model"], "reason": _compare_reason(ep_best)}

    if best:
        await send({"log": f"Recommendation: {best['model']}"})
    if fast_rec:
        await send({"log": f"Speed pick: {fast_rec}"})
    if len(per_server_recs) > 1:
        for ep, rec in per_server_recs.items():
            await send({"log": f"  Server {ep}: {rec['model']}"})

    await send({
        "done":                       True,
        "recommendation":             best["model"] if best else None,
        "reason":                     _compare_reason(best) if best else "Could not benchmark any models.",
        "fast_recommendation":        fast_rec,
        "fast_reason":                fast_reason,
        "per_server_recommendations": per_server_recs,
        "results":                    results,
    })
    return response


async def handle_setup_test(request: web.Request) -> web.Response:
    """SSE: stream one prompt live to verify the model is responding.

    Step 2 (compare) already ran the full benchmark and evals.
    This step is a connectivity confirmation only — one live completion
    measuring TTFT and rough tok/s, no redundant re-benchmarking.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    endpoint = (body.get("endpoint") or "http://localhost:11434/v1").rstrip("/")
    model    = body.get("model") or ""
    api_key  = body.get("api_key") or "ollama"

    if not model:
        return web.json_response({"error": "model required"}, status=400)

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    async def send(data: dict) -> None:
        await response.write(f"data: {json.dumps(data)}\n\n".encode())

    try:
        async with aiohttp.ClientSession() as session:
            t0 = time.time()
            ttft_ms: int | None = None
            t_first: float | None = None
            tok_count = 0
            char_count = 0
            usage_toks: int | None = None

            async def _loading_hint() -> None:
                await asyncio.sleep(8.0)
                await send({"status": "loading_model"})

            hint_task = asyncio.create_task(_loading_hint())
            try:
                try:
                    async with session.post(
                        f"{endpoint}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": _BENCH_PROMPT}],
                            "stream": True,
                            "max_tokens": 80,
                            "stream_options": {"include_usage": True},
                        },
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as r:
                        if r.status != 200:
                            hint_task.cancel()
                            text = await r.text()
                            await send({"error": f"Model server returned {r.status}: {text[:200]}"})
                            return response
                        _buf = ""
                        _done = False
                        async for raw in r.content:
                            if _done:
                                continue
                            _buf += raw.decode(errors="replace")
                            while "\n" in _buf:
                                _line, _buf = _buf.split("\n", 1)
                                _line = _line.strip()
                                if not _line.startswith("data: "):
                                    continue
                                chunk_data = _line[6:]
                                if chunk_data == "[DONE]":
                                    _done = True
                                    break
                                try:
                                    chunk = json.loads(chunk_data)
                                    if chunk.get("usage", {}).get("completion_tokens"):
                                        usage_toks = chunk["usage"]["completion_tokens"]
                                    delta = chunk["choices"][0]["delta"].get("content", "")
                                    if delta:
                                        if ttft_ms is None:
                                            ttft_ms = round((time.time() - t0) * 1000)
                                            t_first = time.time()
                                            hint_task.cancel()
                                        tok_count += 1
                                        char_count += len(delta)
                                        await send({"token": delta})
                                except Exception:
                                    pass
                except aiohttp.ServerDisconnectedError:
                    if not t_first:
                        raise
            finally:
                hint_task.cancel()

            gen_s = (time.time() - t_first) if t_first else max(time.time() - t0, 0.1)
            actual_toks = usage_toks or max(tok_count, round(char_count / 4))
            tok_s = round(actual_toks / gen_s)
            label, colour = _bench_score(tok_s)
            await send({
                "done": True,
                "ttft": ttft_ms,
                "tok_s": tok_s,
                "score_label": label,
                "score_colour": colour,
            })

    except asyncio.TimeoutError:
        await send({"error": "Timed out — the model may still be loading. Try again in a moment."})
    except Exception as e:
        await send({"error": str(e)})

    return response


async def handle_setup_test_k8s(request: web.Request) -> web.Response:
    """Test Kubernetes connectivity for the chosen execution environment."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    mode            = body.get("mode", "incluster")   # "incluster" | "kubeconfig"
    namespace       = body.get("namespace", "hermes")
    kubeconfig_text = body.get("kubeconfig", "")

    try:
        from kubernetes import client as _kc, config as _kcfg
        # Always use an explicit Configuration so we don't pollute (or inherit
        # from) the global in-cluster config that may already be loaded.
        cfg = _kc.Configuration()
        if mode == "incluster":
            _kcfg.load_incluster_config(client_configuration=cfg)
        else:
            if not kubeconfig_text.strip():
                return web.json_response({"ok": False, "error": "kubeconfig is empty"}, status=400)
            import tempfile, os as _os
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write(kubeconfig_text)
                tmp = f.name
            try:
                _kcfg.load_kube_config(config_file=tmp, client_configuration=cfg)
            finally:
                _os.unlink(tmp)

        with _kc.ApiClient(cfg) as api_client:
            v1 = _kc.CoreV1Api(api_client)
            pod_list = v1.list_namespaced_pod(namespace, limit=1)
            count = len(pod_list.items)
        return web.json_response({"ok": True, "namespace": namespace, "pod_count": count})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)[:300]}, status=200)


async def handle_setup_complete(request: web.Request) -> web.Response:
    """Save machine config and mark setup complete."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    endpoint = (body.get("endpoint") or "").strip()
    model = (body.get("model") or "").strip()

    if not endpoint or not model:
        return web.json_response({"error": "endpoint and model required"}, status=400)

    try:
        # Clear any example-placeholder machines and their auto-generated profiles
        for m in auth_db.list_machines():
            if (m.get("description") or "").startswith("Example"):
                auth_db.delete_machine(m["id"])
        for p in auth_db.list_policies():
            if (p.get("description") or "").startswith(("Auto-generated", "Auto-created")):
                auth_db.delete_policy(p["id"])

        # Register inference machines — one per selected server.
        # Fall back to single-server path if no servers list provided.
        servers = body.get("servers") or []
        if not auth_db.list_machines():
            if servers and len(servers) > 1:
                # Multi-server: create a machine for each server, one catch-all policy.
                machine_ids = []
                for idx, srv in enumerate(servers):
                    ep   = (srv.get("endpoint") or "").strip().rstrip("/")
                    name = (srv.get("name") or "").strip() or f"Inference Node {idx + 1}"
                    desc = f"Auto-registered by setup wizard ({srv.get('type','unknown')})."
                    m_obj = auth_db.create_machine(name=name, endpoint_url=ep or endpoint, description=desc)
                    auth_db.set_machine_capabilities(
                        m_obj["id"],
                        ["lightweight", "general", "coding", "reasoning", "vision", "embedding"],
                    )
                    machine_ids.append(m_obj["id"])
                # Catch-all policy routing to first machine (user can tune later)
                policy = auth_db.create_policy(
                    name="default",
                    description="Auto-created by setup wizard.",
                    fallback="any_available",
                )
                auth_db.set_policy_rules(policy["id"], [
                    {"model_class": "*", "machine_id": machine_ids[0], "rank": 1},
                ])
                logger.info("setup wizard: %d machines registered", len(machine_ids))
            else:
                # Single-server path
                result = _seed.apply_single_machine_setup(endpoint)
                if "error" in result and result["error"] != "machines_already_exist":
                    return web.json_response(result, status=409)

        # Write chosen model + endpoint to config.yaml so the agent actually uses them.
        # Keys are bridged to env vars by run.py on startup (only if not already in env,
        # so pre-configured k8s deployments with explicit env vars are not overridden).
        _hermes_home = pathlib.Path(os.environ.get("HERMES_HOME") or (pathlib.Path.home() / ".logos"))
        _config_path = _hermes_home / "config.yaml"
        try:
            import yaml as _yaml
            _cfg: dict = {}
            if _config_path.exists():
                with open(_config_path, encoding="utf-8") as _f:
                    _cfg = _yaml.safe_load(_f) or {}
            if not os.getenv("HERMES_MODEL"):
                _cfg["HERMES_MODEL"] = model
            if not os.getenv("OPENAI_BASE_URL"):
                _cfg["OPENAI_BASE_URL"] = endpoint
            _config_path.write_text(_yaml.dump(_cfg, default_flow_style=False, allow_unicode=True))
            logger.info("setup: wrote HERMES_MODEL=%s to config.yaml", model)
        except Exception as _cfg_err:
            logger.warning("setup: could not write model to config.yaml: %s", _cfg_err)

        # Save model preference on the admin user.
        # During first-run setup the browser has no session yet, so we look up
        # the seeded admin account directly rather than reading current_user.
        admin_users, _ = auth_db.list_users(page=1, limit=1, role="admin")
        if not admin_users:
            return web.json_response({"error": "no_admin_user"}, status=500)
        user_id = admin_users[0]["id"]
        agent_type   = (body.get("agent_type") or "general").strip()
        exec_env     = (body.get("exec_env") or "local").strip()
        k8s_ns       = (body.get("k8s_namespace") or "hermes").strip()
        kubeconfig   = (body.get("kubeconfig") or "").strip()

        auth_db.ensure_user_settings(user_id)
        auth_db.update_user_settings(user_id, default_model=model, default_soul=agent_type)
        auth_db.set_platform_feature_flag("exec_env", exec_env)
        auth_db.set_platform_feature_flag("k8s_namespace", k8s_ns)
        if kubeconfig:
            auth_db.set_platform_feature_flag("k8s_kubeconfig", kubeconfig)
        auth_db.mark_setup_completed()

        # Update admin account credentials if the user customised them
        setup_email    = (body.get("setup_email") or "").strip()
        setup_username = (body.get("setup_username") or "").strip()
        setup_password = (body.get("setup_password") or "").strip()
        if setup_email or setup_username or setup_password:
            from gateway.auth.password import hash_password as _hp
            updates = {}
            if setup_email:    updates["email"]         = setup_email
            if setup_username: updates["username"]       = setup_username
            if setup_password: updates["password_hash"]  = _hp(setup_password)
            if updates:
                auth_db.update_user(user_id, **updates)
                logger.info("setup: updated admin credentials for %s", user_id)

        auth_db.write_audit_log(
            user_id, "setup_completed",
            metadata={"endpoint": endpoint, "model": model},
            ip_address=request.remote,
        )
        logger.info("setup completed: endpoint=%s model=%s by %s", endpoint, model, user_id)

        # Warn if the model endpoint isn't reachable from this host
        # (common when user selected localhost but runs Logos in k8s)
        warning = None
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _s:
                async with _s.get(
                    endpoint.replace("/v1", "") + "/api/tags",
                    timeout=_aio.ClientTimeout(total=3),
                ) as _r:
                    pass  # reachable
        except Exception as _reach_err:
            warning = (
                f"Model server at {endpoint} is not reachable from this host "
                f"({type(_reach_err).__name__}: {str(_reach_err)[:120]}). "
                "If Logos runs in Kubernetes, use the node's LAN IP instead of localhost."
            )

        # Auto-spawn first agent instance for local mode
        if exec_env == "local":
            import asyncio as _asyncio
            _asyncio.get_event_loop().run_in_executor(
                None, _auto_spawn_first_instance, agent_type, model
            )

        if warning:
            return web.json_response({"ok": True, "warning": warning})
        return web.json_response({"ok": True})
    except Exception as exc:
        logger.exception("setup/complete failed: %s", exc)
        return web.json_response({"error": "internal_error", "detail": str(exc)[:300]}, status=500)


def _auto_spawn_first_instance(soul_name: str, model: str) -> None:
    """Spawn a single default agent instance after local-mode setup completes."""
    try:
        from gateway.executors.local import LocalProcessExecutor
        from gateway.executors.base import InstanceConfig
        executor = LocalProcessExecutor()
        resources = executor.get_resources()
        if not resources.get("can_spawn", True):
            logger.warning("setup: skipping auto-spawn — %s", resources.get("reason", "low resources"))
            return
        cfg = InstanceConfig(name="default", soul_name=soul_name or "general", model=model)
        instance = executor.spawn(cfg)
        logger.info("setup: auto-spawned first instance %s on port %d (healthy=%s)",
                    instance.name, instance.port, instance.healthy)
    except Exception as exc:
        logger.warning("setup: auto-spawn failed: %s", exc)


# ---------------------------------------------------------------------------
# LAN discovery — find existing Logos instances on the local network
# ---------------------------------------------------------------------------

_LOGOS_PORTS = [8080, 7860, 8000]   # ports to probe for Logos instances
_DISCOVER_TIMEOUT = aiohttp.ClientTimeout(total=1.5)
_DISCOVER_CONCURRENCY = 30
_CONNECT_JSON = pathlib.Path(os.environ.get("HERMES_HOME", pathlib.Path.home() / ".logos")) / "connect.json"


async def _probe_logos(session: aiohttp.ClientSession, url: str) -> dict | None:
    """Return instance info if url hosts a Logos instance, else None."""
    try:
        async with session.get(f"{url}/health", timeout=_DISCOVER_TIMEOUT) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            if data.get("product") != "logos":
                return None
            return {
                "url": url,
                "setup_completed": data.get("setup_completed", False),
                "uptime_s": data.get("uptime_s"),
            }
    except Exception:
        return None


async def handle_setup_discover(request: web.Request) -> web.Response:
    """Scan local network for running Logos instances.

    Returns a list of discovered instances. Scans:
      - localhost on all LOGOS_PORTS
      - local /24 subnet on port 8080
    """
    own_ips = _own_ips()
    targets: list[str] = []

    # Always check localhost variants first
    for port in _LOGOS_PORTS:
        for host in ("127.0.0.1", "localhost"):
            targets.append(f"http://{host}:{port}")

    # Scan local /24 subnet
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            own_ip = s.getsockname()[0]
        prefix = ".".join(own_ip.split(".")[:3])
        for i in range(1, 255):
            host = f"{prefix}.{i}"
            if host not in own_ips:
                targets.append(f"http://{host}:8080")
    except Exception:
        pass

    results = []
    sem = asyncio.Semaphore(_DISCOVER_CONCURRENCY)

    async def _bounded(url: str):
        async with sem:
            return await _probe_logos(session, url)

    async with aiohttp.ClientSession() as session:
        tasks = [_bounded(url) for url in targets]
        for coro in asyncio.as_completed(tasks):
            found = await coro
            if found:
                results.append(found)

    return web.json_response({"instances": results})


async def handle_setup_set_remote(request: web.Request) -> web.Response:
    """Save a remote Logos URL so the launcher opens it instead of local mode.

    Writes ~/.logos/connect.json with {"url": "..."}.
    The launcher reads this on next start and skips the local gateway.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    url = (body.get("url") or "").strip().rstrip("/")
    if not url:
        return web.json_response({"error": "url required"}, status=400)

    # Validate it's actually a Logos instance before saving
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/health", timeout=_DISCOVER_TIMEOUT) as r:
                if r.status != 200:
                    return web.json_response({"error": "unreachable"}, status=400)
                data = await r.json(content_type=None)
                if data.get("product") != "logos":
                    return web.json_response({"error": "not_logos"}, status=400)
    except Exception as exc:
        return web.json_response({"error": "unreachable", "detail": str(exc)[:200]}, status=400)

    _CONNECT_JSON.parent.mkdir(parents=True, exist_ok=True)
    _CONNECT_JSON.write_text(json.dumps({"url": url}, indent=2), encoding="utf-8")
    logger.info("setup: saved remote connect URL: %s", url)
    return web.json_response({"ok": True, "url": url})
