"""Setup wizard API handlers.

Endpoints:
  GET  /api/setup/probe    — probe a model server (Ollama or LM Studio / OpenAI-compatible)
  GET  /api/setup/scan     — sweep local subnet for model servers on :11434 and :1234
  POST /api/setup/pull     — SSE: stream Ollama model pull progress
  POST /api/setup/test     — SSE: stream a test prompt response
  POST /api/setup/complete — save machine config and mark setup done

Probe query params:
  url      — base URL to probe, e.g. http://192.168.1.50:11434  (omit for localhost auto-scan)
  api_key  — optional Bearer token (for LM Studio with auth enabled)
"""

import asyncio
import ipaddress
import json
import logging
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
) -> dict:
    """Probe a single base URL.

    Tries Ollama's native /api/tags first (unique to Ollama).
    Falls back to OpenAI-compatible /v1/models (LM Studio, vLLM, etc.).
    Returns a result dict with keys: type, endpoint, status, models.
    """
    base = base_url.rstrip("/")

    # ── Ollama: unique /api/tags endpoint ──────────────────────────────────
    try:
        async with session.get(f"{base}/api/tags", timeout=_PROBE_TIMEOUT) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                models = [
                    {"id": m["name"], "name": m["name"], "size": m.get("size", 0)}
                    for m in data.get("models", [])
                ]
                return {"type": "ollama", "endpoint": f"{base}/v1", "status": "up", "models": models}
    except Exception:
        pass

    # ── OpenAI-compatible: /v1/models (LM Studio, etc.) ───────────────────
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with session.get(f"{base}/v1/models", headers=headers, timeout=_PROBE_TIMEOUT) as r:
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
    """Return the /24 subnet of this machine's primary LAN interface, e.g. '192.168.1'."""
    try:
        # Connect to a public address (no packet sent) to find the outbound interface IP.
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
        url = f"http://{ip}:{port}"
        result = await _probe_server(session, url, api_key=None)
        if result["status"] in ("up", "auth_required"):
            found.append(result)
    return found


# ── Route handlers ─────────────────────────────────────────────────────────────

async def handle_setup_probe(request: web.Request) -> web.Response:
    """Probe a model server.

    With ?url=...: probe that specific address.
    Without url:   scan localhost defaults (Ollama :11434, LM Studio :1234).
    Optional: ?api_key=... for servers with auth enabled.
    """
    raw_url = (request.query.get("url") or "").strip()
    api_key = request.query.get("api_key") or None

    async with aiohttp.ClientSession() as session:
        if raw_url:
            result = await _probe_server(session, raw_url, api_key)
            return web.json_response({"servers": [result]})

        # Auto-scan both localhost defaults in parallel
        ollama, lmstudio = await asyncio.gather(
            _probe_server(session, "http://localhost:11434", None),
            _probe_server(session, "http://localhost:1234", api_key),
        )
    return web.json_response({"servers": [ollama, lmstudio]})


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


async def handle_setup_test(request: web.Request) -> web.Response:
    """Stream a test-prompt response via SSE."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    endpoint = (body.get("endpoint") or "http://localhost:11434/v1").rstrip("/")
    model = body.get("model") or ""
    api_key = body.get("api_key") or "ollama"

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

    start = time.time()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{endpoint}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say hello briefly."}],
                    "stream": True,
                    "max_tokens": 60,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    text = await r.text()
                    await send({"error": f"Model server returned {r.status}: {text[:200]}"})
                    return response

                async for raw in r.content:
                    line = raw.decode().strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        latency = round((time.time() - start) * 1000)
                        await send({"done": True, "latency": latency})
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            await send({"token": delta})
                    except Exception:
                        pass
    except asyncio.TimeoutError:
        await send({"error": "Request timed out — the model may still be loading. Try again in a moment."})
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
