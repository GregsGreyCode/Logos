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
_SCAN_PORTS    = [11434, 1234]
_SCAN_CONCURRENCY = 40


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
        # Deduplicate by endpoint (localhost and NODE_IP may resolve to same machine)
        seen: set[str] = set()
        unique = []
        for r in results:
            if r["endpoint"] not in seen:
                seen.add(r["endpoint"])
                unique.append(r)
    return web.json_response({"servers": unique})


async def handle_setup_scan(request: web.Request) -> web.Response:
    """Sweep the local /24 subnet for model servers on :11434 and :1234.

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

    # Deduplicate by endpoint
    seen: set[str] = set()
    unique = []
    for r in results:
        if r["endpoint"] not in seen:
            seen.add(r["endpoint"])
            unique.append(r)

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


_BENCH_PROMPTS = [
    "Say hello in one sentence.",
    "What is 17 multiplied by 6? Answer with just the number.",
    "Name the capital of France in one word.",
]
# ── Benchmark prompts ────────────────────────────────────────────────────────
# Speed: generates ~100 tokens of coherent prose — good throughput signal
_BENCH_PROMPT = (
    "Briefly explain three steps you would take to debug a program that crashes unexpectedly. "
    "Be concise."
)

# Test 1 — Instruction following: multi-step, each step is checkable
_INSTRUCT_PROMPT = (
    "Follow these instructions exactly, in order:\n"
    "1. Write the word ALPHA\n"
    "2. Write the result of 15 + 27\n"
    "3. Write the word BETA"
)
_INSTRUCT_CHECKS = ["ALPHA", "42", "BETA"]

# Test 2 — Reasoning: requires a calculation step
_REASON_PROMPT  = "A train travels 60 km in 45 minutes. What is its speed in km/h? Reply with only the number."
_REASON_EXPECTED = "80"

# Test 3 — Format compliance: critical for tool-calling / structured output
_FORMAT_PROMPT = 'Reply with ONLY a valid JSON object, no other text: {"status": "ok", "value": 99}'

# Test 4 — Tool-call readiness: simulate a basic agent tool selection
_TOOL_PROMPT = (
    'You have two tools: "search_web" (for looking up current information) and '
    '"run_code" (for executing Python). '
    'A user asks: "What is the current Bitcoin price?" '
    'Reply with ONLY a JSON object: {"tool": "<tool_name>", "reason": "<one sentence>"}'
)


def _eval_tool_call(text: str) -> bool:
    """Return True if response contains valid JSON selecting search_web."""
    import re as _re
    cleaned = _re.sub(r"```[a-z]*\n?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        return obj.get("tool") == "search_web"
    except Exception:
        m = _re.search(r'\{[^}]+\}', cleaned)
        if m:
            try:
                return json.loads(m.group()).get("tool") == "search_web"
            except Exception:
                pass
        return False

_COMPARE_TIMEOUT  = aiohttp.ClientTimeout(total=120)   # per-model; handles cold-start


def _eval_format(text: str) -> bool:
    """Return True if the response contains parseable JSON with status=ok and value=99."""
    import re as _re
    # Strip markdown fences if present
    cleaned = _re.sub(r"```[a-z]*\n?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        return obj.get("status") == "ok" and obj.get("value") == 99
    except Exception:
        # Try to find JSON object anywhere in the text
        m = _re.search(r'\{[^}]+\}', cleaned)
        if m:
            try:
                obj = json.loads(m.group())
                return obj.get("status") == "ok" and obj.get("value") == 99
            except Exception:
                pass
        return False


def _parse_model_size_b(model_id: str) -> float:
    """Extract parameter count in billions from a model ID string. Returns 0 if unknown."""
    m = re.search(r'(\d+(?:\.\d+)?)\s*b(?:[^a-z]|$)', model_id.lower())
    return float(m.group(1)) if m else 0.0


def _pick_compare_candidates(model_ids: list[str], max_n: int = 4) -> list[str]:
    """Pick up to max_n diverse candidates: sweet-spot (7-13B) first, then smaller, then larger."""
    SWEET = 8.0

    def priority(mid: str) -> float:
        sz = _parse_model_size_b(mid)
        if sz == 0:
            return 50.0   # unknown size: deprioritise
        dist = abs(sz - SWEET)
        bias = 0.5 if sz > SWEET else 0.0  # slight penalty for going over sweet spot
        return dist + bias

    return sorted(model_ids, key=priority)[:max_n]


def _compare_score(r: dict) -> float:
    if r.get("error") or r.get("tok_s", 0) == 0:
        return -1.0
    eval_score = r.get("eval", {}).get("score", 1 if r.get("quality_pass") else 0)
    eval_frac  = eval_score / 4                  # 0.0 – 1.0
    speed      = min(r["tok_s"], 40) / 40        # normalise; cap at 40 tok/s
    sz         = _parse_model_size_b(r["model"])
    size_b     = min(sz, 13) / 13 * 0.2 if sz > 0 else 0  # prefer bigger up to 13B
    # Weights: eval quality 45%, speed 35%, model size 20%
    return 0.45 * eval_frac + 0.35 * speed + size_b


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

    if score == 4 and best["tok_s"] >= 15:
        return (f"{label} at {best['tok_s']} tok/s — passes all 4 eval tests "
                f"({eval_str}). Strong default agent model for your hardware.")
    if score >= 3 and best["tok_s"] >= 10:
        return (f"{label} at {best['tok_s']} tok/s — passes {score}/4 eval tests "
                f"({eval_str}). Solid baseline agent model.")
    if score >= 3:
        return (f"Passes {score}/4 eval tests but slow at {best['tok_s']} tok/s. "
                f"Consider a smaller quantised model for better responsiveness.")
    return (f"{label} at {best['tok_s']} tok/s but only {score}/4 eval tests passed. "
            f"A general-purpose model (e.g. Qwen3-8B, Gemma3-9B) will work better.")


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
        async for raw in r.content:
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
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

    candidate_ids = _pick_compare_candidates([s["id"] for s in model_specs])
    candidates    = [s for s in model_specs if s["id"] in candidate_ids]

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    async def send(data: dict) -> None:
        await response.write(f"data: {json.dumps(data)}\n\n".encode())

    # Group candidates by server endpoint so we can test each server in parallel
    from collections import defaultdict as _dd
    server_groups: dict[str, list[dict]] = _dd(list)
    for c in candidates:
        server_groups[c["endpoint"]].append(c)

    n_servers = len(server_groups)
    await send({"targets": [c["id"] for c in candidates]})
    await send({"log": (
        f"Testing {len(candidates)} model{'s' if len(candidates) != 1 else ''} "
        f"across {n_servers} server{'s' if n_servers != 1 else ''}"
        + (" — running in parallel" if n_servers > 1 else "")
    )})

    results: list[dict] = []
    results_lock = asyncio.Lock()

    async def _test_server_group(group: list[dict], http: aiohttp.ClientSession) -> None:
        """Test all models for one server sequentially (protects that server's VRAM)."""
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

            async def _bench_run(pass_num: int) -> float:
                nonlocal ttft_s
                t_start    = time.time()
                tok_count  = 0
                char_count = 0
                t_first: float | None  = None
                usage_toks: int | None = None

                async with http.post(
                    f"{endpoint}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": _BENCH_PROMPT}],
                        "stream": True,
                        "max_tokens": 120,
                        "stream_options": {"include_usage": True},
                    },
                    timeout=_COMPARE_TIMEOUT,
                ) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    async for raw in r.content:
                        line = raw.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        chunk_data = line[6:]
                        if chunk_data == "[DONE]":
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

                end_t = time.time()
                gen_s = (end_t - t_first) if t_first else (end_t - t_start)
                if gen_s <= 0:
                    return 0.0
                actual_toks = usage_toks or max(tok_count, round(char_count / 4))
                return actual_toks / gen_s

            try:
                r1 = await _bench_run(1)
                await send({"log": f"  Pass 1: {r1:.1f} tok/s"})
                r2 = await _bench_run(2)
                await send({"log": f"  Pass 2: {r2:.1f} tok/s"})

                tok_s   = round((r1 + r2) / 2)
                ttft_ms = round(ttft_s * 1000) if ttft_s is not None else None
                await send({"log": f"  Avg: {tok_s} tok/s"})

                # ── 3-part eval ──────────────────────────────────────────────
                await send({"log": "  Eval 1/3: instruction following…"})
                instruct_pass = False
                try:
                    itext, _, _, _ = await _stream_chat(http, endpoint, model_id, api_key, _INSTRUCT_PROMPT, max_tokens=40)
                    instruct_pass = all(c in itext for c in _INSTRUCT_CHECKS)
                except Exception:
                    pass
                await send({"log": f"    {'✓' if instruct_pass else '✗'} instruction following"})

                await send({"log": "  Eval 2/3: reasoning…"})
                reason_pass = False
                try:
                    rtext, _, _, _ = await _stream_chat(http, endpoint, model_id, api_key, _REASON_PROMPT, max_tokens=20)
                    reason_pass = _REASON_EXPECTED in rtext.strip()
                except Exception:
                    pass
                await send({"log": f"    {'✓' if reason_pass else '✗'} reasoning"})

                await send({"log": "  Eval 3/4: format compliance…"})
                format_pass = False
                try:
                    ftext, _, _, _ = await _stream_chat(http, endpoint, model_id, api_key, _FORMAT_PROMPT, max_tokens=40)
                    format_pass = _eval_format(ftext)
                except Exception:
                    pass
                await send({"log": f"    {'✓' if format_pass else '✗'} JSON format"})

                await send({"log": "  Eval 4/4: tool-call selection…"})
                tool_pass = False
                try:
                    ttext, _, _, _ = await _stream_chat(http, endpoint, model_id, api_key, _TOOL_PROMPT, max_tokens=60)
                    tool_pass = _eval_tool_call(ttext)
                except Exception:
                    pass
                await send({"log": f"    {'✓' if tool_pass else '✗'} tool selection"})

                tests_passed = sum([instruct_pass, reason_pass, format_pass, tool_pass])
                quality_pass = tests_passed >= 3  # pass if ≥3/4 tests pass
                await send({"log": f"  {'✓' if quality_pass else '⚠'} {tok_s} tok/s · {tests_passed}/4 eval tests passed"})
                result: dict = {
                    "model": model_id, "tok_s": tok_s, "quality_pass": quality_pass,
                    "ttft_ms": ttft_ms, "eval": {
                        "instruction": instruct_pass, "reasoning": reason_pass,
                        "format": format_pass, "tool_call": tool_pass, "score": tests_passed,
                    },
                }
            except Exception as exc:
                await send({"log": f"  ✗ Error: {str(exc)[:80]}"})
                result = {"model": model_id, "tok_s": 0, "quality_pass": False, "ttft_ms": None, "error": str(exc)[:120]}
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
    best  = max(valid, key=_compare_score) if valid else None

    if best:
        await send({"log": f"Recommendation: {best['model']}"})

    await send({
        "done":           True,
        "recommendation": best["model"] if best else None,
        "reason":         _compare_reason(best) if best else "Could not benchmark any models.",
        "results":        results,
    })
    return response


async def handle_setup_test(request: web.Request) -> web.Response:
    """Stream benchmark results via SSE.

    Phase 1: stream the first prompt response live (so the user sees output).
    Phase 2: run 2 more silent benchmark prompts to measure tok/s.
    Phase 3: run quality check prompt, emit pass/fail.
    Final:   emit score event with rating.
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
            # ── Phase 1: stream first prompt live ──────────────────────────
            t0 = time.time()
            ttft_ms: int | None = None
            tok_count = 0

            # Background task: emit loading hint after 8s with no first token
            async def _loading_hint() -> None:
                await asyncio.sleep(8.0)
                await send({"status": "loading_model"})
                await send({"log": "  Model loading into memory — this can take 30–60s on first use"})

            await send({"log": f"Phase 1/3 — connecting to {endpoint.replace('/v1', '')}…"})
            hint_task = asyncio.create_task(_loading_hint())
            try:
                async with session.post(
                    f"{endpoint}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": _BENCH_PROMPTS[0]}],
                        "stream": True, "max_tokens": 80,
                    },
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as r:
                    if r.status != 200:
                        hint_task.cancel()
                        text = await r.text()
                        await send({"error": f"Model server returned {r.status}: {text[:200]}"})
                        return response
                    async for raw in r.content:
                        line = raw.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                if ttft_ms is None:
                                    ttft_ms = round((time.time() - t0) * 1000)
                                    hint_task.cancel()  # got first token — no longer need hint
                                    await send({"log": f"  First token: {ttft_ms}ms TTFT \u2713"})
                                tok_count += 1
                                await send({"token": delta})
                        except Exception:
                            pass
            finally:
                hint_task.cancel()
            run1_s = time.time() - t0
            await send({"log": f"  Phase 1 done \u2014 {tok_count} tokens \u00b7 {run1_s:.1f}s"})

            # ── Phase 2: two more silent runs for throughput ────────────────
            await send({"status": "benchmarking"})
            await send({"log": "Phase 2/3 \u2014 measuring throughput\u2026"})
            run_times: list[float] = [run1_s]
            run_toks:  list[int]   = [tok_count]
            for i, prompt in enumerate(_BENCH_PROMPTS[1:], start=2):
                await send({"log": f"  Run {i}/{len(_BENCH_PROMPTS)}\u2026"})
                try:
                    _, _, t, n = await _stream_chat(session, endpoint, model, api_key, prompt)
                    run_times.append(t)
                    run_toks.append(n)
                    await send({"log": f"  Run {i} done: {n} tokens \u00b7 {t:.1f}s"})
                except Exception as exc:
                    await send({"log": f"  Run {i} failed: {str(exc)[:60]}"})

            total_toks = sum(run_toks)
            total_s    = sum(run_times)
            avg_tok_s  = round(total_toks / total_s) if total_s > 0 else 0
            avg_ms     = round(total_s / len(run_times) * 1000)

            # ── Phase 3: quality sanity check ──────────────────────────────
            await send({"log": "Phase 3/3 \u2014 quality check\u2026"})
            quality_pass = False
            try:
                qtext, _, _, _ = await _stream_chat(
                    session, endpoint, model, api_key, _QUALITY_PROMPT, max_tokens=10
                )
                quality_pass = _QUALITY_EXPECTED in qtext.strip()
                await send({"log": "  \u2713 Reasoning ok" if quality_pass else "  \u26a0 Reasoning check failed"})
            except Exception as exc:
                await send({"log": f"  Quality check error: {str(exc)[:60]}"})

            label, colour = _bench_score(avg_tok_s)
            await send({"log": f"Result: {avg_tok_s} tok/s \u00b7 {label}"})
            await send({
                "done": True,
                "latency": avg_ms,
                "ttft": ttft_ms,
                "tok_s": avg_tok_s,
                "score_label": label,
                "score_colour": colour,
                "quality_pass": quality_pass,
                "runs": len(run_times),
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
        if mode == "incluster":
            _kcfg.load_incluster_config()
        else:
            if not kubeconfig_text.strip():
                return web.json_response({"ok": False, "error": "kubeconfig is empty"}, status=400)
            import tempfile, os as _os
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write(kubeconfig_text)
                tmp = f.name
            try:
                _kcfg.load_kube_config(config_file=tmp)
            finally:
                _os.unlink(tmp)

        v1 = _kc.CoreV1Api()
        # Try reading the namespace — lightweight existence check
        ns_obj = v1.read_namespace(namespace)
        return web.json_response({"ok": True, "namespace": namespace, "uid": ns_obj.metadata.uid})
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

    # Clear any example-placeholder machines and their auto-generated profiles
    for m in auth_db.list_machines():
        if (m.get("description") or "").startswith("Example"):
            auth_db.delete_machine(m["id"])
    for p in auth_db.list_policies():
        if (p.get("description") or "").startswith(("Auto-generated", "Auto-created")):
            auth_db.delete_policy(p["id"])

    # Create the real machine (skip if a non-example one already exists)
    result = _seed.apply_single_machine_setup(endpoint)
    if "error" in result and result["error"] != "machines_already_exist":
        return web.json_response(result, status=409)

    # Save model preference on the admin user
    user_id = request["current_user"]["sub"]
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

    auth_db.write_audit_log(
        user_id, "setup_completed",
        metadata={"endpoint": endpoint, "model": model},
        ip_address=request.remote,
    )
    logger.info("setup completed: endpoint=%s model=%s by %s", endpoint, model, user_id)
    return web.json_response({"ok": True})
