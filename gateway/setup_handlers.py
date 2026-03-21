"""Setup wizard API handlers.

Endpoints:
  GET  /api/setup/probe    — probe Ollama (:11434) and LM Studio (:1234)
  POST /api/setup/pull     — SSE: stream Ollama model pull progress
  POST /api/setup/test     — SSE: stream a test prompt response
  POST /api/setup/complete — save machine config and mark setup done
"""

import asyncio
import json
import logging
import time

import aiohttp
from aiohttp import web

import gateway.auth.db as auth_db
from gateway import seed as _seed

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=3)


# ── Probe helpers ──────────────────────────────────────────────────────────────

async def _probe_ollama(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(
            "http://localhost:11434/api/tags", timeout=_PROBE_TIMEOUT
        ) as r:
            if r.status == 200:
                data = await r.json()
                models = [
                    {"id": m["name"], "name": m["name"], "size": m.get("size", 0)}
                    for m in data.get("models", [])
                ]
                return {
                    "type": "ollama",
                    "endpoint": "http://localhost:11434/v1",
                    "status": "up",
                    "models": models,
                }
    except Exception:
        pass
    return {"type": "ollama", "endpoint": "http://localhost:11434/v1", "status": "down", "models": []}


async def _probe_lmstudio(session: aiohttp.ClientSession, api_key: str | None = None) -> dict:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with session.get(
            "http://localhost:1234/v1/models", headers=headers, timeout=_PROBE_TIMEOUT
        ) as r:
            if r.status == 200:
                data = await r.json()
                models = [
                    {"id": m["id"], "name": m["id"], "size": 0}
                    for m in data.get("data", [])
                ]
                return {
                    "type": "lmstudio",
                    "endpoint": "http://localhost:1234/v1",
                    "status": "up",
                    "models": models,
                }
            if r.status == 401:
                return {
                    "type": "lmstudio",
                    "endpoint": "http://localhost:1234/v1",
                    "status": "auth_required",
                    "models": [],
                }
    except Exception:
        pass
    return {"type": "lmstudio", "endpoint": "http://localhost:1234/v1", "status": "down", "models": []}


# ── Route handlers ─────────────────────────────────────────────────────────────

async def handle_setup_probe(request: web.Request) -> web.Response:
    """Probe local model servers. Query param: lmstudio_key (optional)."""
    lmstudio_key = request.query.get("lmstudio_key") or None
    async with aiohttp.ClientSession() as session:
        ollama, lmstudio = await asyncio.gather(
            _probe_ollama(session),
            _probe_lmstudio(session, lmstudio_key),
        )
    return web.json_response({"servers": [ollama, lmstudio]})


async def handle_setup_pull(request: web.Request) -> web.Response:
    """Stream Ollama model pull progress via SSE."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

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
                "http://localhost:11434/api/pull",
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
        await send({"error": "Request timed out — model may be too large or still loading."})
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
