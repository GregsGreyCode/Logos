"""Setup wizard API handlers.

Endpoints:
  GET  /api/setup/probe    — probe a model server (Ollama or LM Studio / OpenAI-compatible)
  GET  /api/setup/scan     — sweep local subnet for model servers on :11434 and :1234
  POST /api/setup/pull     — SSE: stream Ollama model pull progress
  POST /api/setup/compare  — SSE: quick-benchmark candidates, recommend best model
  POST /api/setup/test     — SSE: full benchmark of selected model
  POST /api/setup/complete — save machine config and mark setup done

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
_QUALITY_PROMPT   = "What is 2+2? Reply with only the number."
_QUALITY_EXPECTED = "4"
_COMPARE_PROMPT   = "List three key traits of a helpful AI assistant."
_COMPARE_TIMEOUT  = aiohttp.ClientTimeout(total=90)   # per-model; handles cold-start


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
    q      = 1.5 if r.get("quality_pass") else 1.0
    speed  = min(r["tok_s"], 30) / 30            # normalise; cap at "Fast"
    sz     = _parse_model_size_b(r["model"])
    size_b = min(sz, 13) / 13 * 0.3 if sz > 0 else 0  # prefer bigger up to 13B
    return q * (0.6 * speed + 0.4) + size_b


def _compare_reason(best: dict) -> str:
    label, _ = _bench_score(best["tok_s"])
    q = best.get("quality_pass", False)
    if q and best["tok_s"] >= 15:
        return (f"{label} at {best['tok_s']} tok/s with reasoning confirmed — "
                f"this is the best fit for your hardware.")
    if q:
        return (f"Slowest option tested, but reasoning confirmed. "
                f"Consider a smaller quantised model for better speed.")
    return (f"{label} at {best['tok_s']} tok/s but reasoning check failed. "
            f"A general-purpose chat model (e.g. Qwen3-8B) will work better.")


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
      {"targets": [model_id, ...]}           — candidates selected for testing
      {"testing": model_id}                  — about to test this model
      {"loading_model": model_id}            — model is cold-starting (no tokens yet after 8s)
      {"result": {model, tok_s, quality_pass, error?}}
      {"done": true, "recommendation": model_id|null, "reason": str, "results": [...]}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    endpoint  = (body.get("endpoint") or "").rstrip("/")
    api_key   = body.get("api_key") or "ollama"
    model_ids = body.get("models") or []

    if not endpoint or not model_ids:
        return web.json_response({"error": "endpoint and models required"}, status=400)

    candidates = _pick_compare_candidates(model_ids)

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    async def send(data: dict) -> None:
        await response.write(f"data: {json.dumps(data)}\n\n".encode())

    await send({"targets": candidates})

    results: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for model_id in candidates:
            await send({"testing": model_id})
            t0             = time.time()
            tok_count      = 0
            loading_emitted = False
            try:
                async with session.post(
                    f"{endpoint}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": _COMPARE_PROMPT}],
                        "stream": True,
                        "max_tokens": 30,
                    },
                    timeout=_COMPARE_TIMEOUT,
                ) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    while True:
                        try:
                            raw = await asyncio.wait_for(r.content.readline(), timeout=3.0)
                        except asyncio.TimeoutError:
                            if not loading_emitted and (time.time() - t0) >= 8.0:
                                await send({"loading_model": model_id})
                                loading_emitted = True
                            continue
                        if not raw:
                            break
                        line = raw.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        chunk_data = line[6:]
                        if chunk_data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_data)
                            if chunk["choices"][0]["delta"].get("content"):
                                tok_count += 1
                        except Exception:
                            pass

                total_s = time.time() - t0
                tok_s   = round(tok_count / total_s) if total_s > 0 else 0

                # Quick quality check (model already warm at this point)
                quality_pass = False
                try:
                    qtext, _, _, _ = await _stream_chat(
                        session, endpoint, model_id, api_key, _QUALITY_PROMPT, max_tokens=10
                    )
                    quality_pass = _QUALITY_EXPECTED in qtext.strip()
                except Exception:
                    pass

                result: dict = {"model": model_id, "tok_s": tok_s, "quality_pass": quality_pass}
            except Exception as exc:
                result = {"model": model_id, "tok_s": 0, "quality_pass": False, "error": str(exc)[:120]}

            results.append(result)
            await send({"result": result})

    valid = [r for r in results if not r.get("error") and r.get("tok_s", 0) > 0]
    best  = max(valid, key=_compare_score) if valid else None

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
            loading_emitted = False
            _LOADING_HINT_AFTER = 8.0  # emit loading_model hint after this many seconds
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
                    text = await r.text()
                    await send({"error": f"Model server returned {r.status}: {text[:200]}"})
                    return response
                while True:
                    try:
                        raw = await asyncio.wait_for(r.content.readline(), timeout=3.0)
                    except asyncio.TimeoutError:
                        if not loading_emitted and (time.time() - t0) >= _LOADING_HINT_AFTER:
                            await send({"status": "loading_model"})
                            loading_emitted = True
                        continue
                    if not raw:
                        break
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
                            tok_count += 1
                            await send({"token": delta})
                    except Exception:
                        pass
            run1_s = time.time() - t0

            # ── Phase 2: two more silent runs for throughput ────────────────
            await send({"status": "benchmarking"})
            run_times: list[float] = [run1_s]
            run_toks:  list[int]   = [tok_count]
            for prompt in _BENCH_PROMPTS[1:]:
                try:
                    _, _, t, n = await _stream_chat(session, endpoint, model, api_key, prompt)
                    run_times.append(t)
                    run_toks.append(n)
                except Exception:
                    pass

            total_toks = sum(run_toks)
            total_s    = sum(run_times)
            avg_tok_s  = round(total_toks / total_s) if total_s > 0 else 0
            avg_ms     = round(total_s / len(run_times) * 1000)

            # ── Phase 3: quality sanity check ──────────────────────────────
            quality_pass = False
            try:
                qtext, _, _, _ = await _stream_chat(
                    session, endpoint, model, api_key, _QUALITY_PROMPT, max_tokens=10
                )
                quality_pass = _QUALITY_EXPECTED in qtext.strip()
            except Exception:
                pass

            label, colour = _bench_score(avg_tok_s)
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
    auth_db.ensure_user_settings(user_id)
    auth_db.update_user_settings(user_id, default_model=model)

    # Mark setup complete
    auth_db.mark_setup_completed()

    auth_db.write_audit_log(
        user_id, "setup_completed",
        metadata={"endpoint": endpoint, "model": model},
        ip_address=request.remote,
    )
    logger.info("setup completed: endpoint=%s model=%s by %s", endpoint, model, user_id)
    return web.json_response({"ok": True})
