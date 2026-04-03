"""Setup wizard API handlers.

Endpoints:
  GET  /api/setup/probe    — probe a model server (Ollama or LM Studio / OpenAI-compatible)
  GET  /api/setup/scan     — sweep local subnet for model servers on :11434 and :1234
  POST /api/setup/pull     — SSE: stream Ollama model pull progress
  POST /api/setup/compare  — SSE: quick-benchmark candidates, recommend best model
  POST /api/setup/test     — SSE: full benchmark of selected model
  POST /api/setup/complete — save machine config and mark setup done
  POST /api/setup/test-k8s   — test Kubernetes connectivity
  POST /api/setup/k3s-install — SSE: install k3s on bare Linux host

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
import uuid

import aiohttp
from aiohttp import web

import gateway.auth.db as auth_db
from gateway import seed as _seed
from gateway.auth.tokens import (
    REFRESH_TOKEN_TTL,
    issue_access_token,
    issue_refresh_token,
    set_auth_cookies,
)

logger = logging.getLogger(__name__)

# bench_id → set of endpoints the user has requested to skip mid-benchmark
_bench_cancels: dict[str, set[str]] = {}
# bench_id → {endpoint: {model, server_type, api_key, base_url}} tracking currently-loaded model
_bench_active: dict[str, dict[str, dict]] = {}

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=4)
_SCAN_TIMEOUT  = aiohttp.ClientTimeout(total=1)   # aggressive — we're sweeping 254 hosts
_SCAN_PORTS    = [11434, 1234, 8080]   # Ollama, LM Studio, llama.cpp/vLLM default
_SCAN_CONCURRENCY = 40


def _own_ips() -> set[str]:
    """Return all IPv4 addresses that refer to this machine (for dedup).

    In Kubernetes the pod's cluster IP differs from the node's LAN IP, but
    NODE_IP is injected via the downward API so we include it here — otherwise
    port 8080 on the host node would not be skipped and Logos itself (or any
    service on that port) would appear as a discovered model server.

    On Windows with multiple NICs (VPN, Docker bridge, etc.) gethostbyname_ex
    returns all bound addresses, which is more complete than getaddrinfo alone.
    """
    ips: set[str] = {"127.0.0.1", "localhost"}
    # K8s: include the host node's LAN IP so own_port is skipped there too
    node_ip = os.environ.get("NODE_IP", "").strip()
    if node_ip:
        ips.add(node_ip)
    # Primary route IP — reliable for single-NIC machines
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    # All addresses bound to this hostname — catches multi-NIC, VPN, Docker
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.update(addrs)
    except Exception:
        pass
    # getaddrinfo as a final sweep
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def _dedup_servers(results: list[dict]) -> list[dict]:
    """Deduplicate server list, collapsing LAN-IP entries that duplicate a
    localhost entry on the same port (same physical machine, different address).

    Also applies a port-preference rule: if the same host is found on both
    port 8080 and port 1234, only 1234 is kept (LM Studio's canonical port).
    Port 8080 on a host that already has 1234 is almost certainly the same
    LM Studio instance responding on its secondary/proxy port.
    """
    from urllib.parse import urlparse as _up

    own = _own_ips()
    # Build a set of ports already covered by a localhost/loopback entry
    local_ports: set[int] = set()
    for r in results:
        if r.get("status") not in ("up", "auth_required"):
            continue
        try:
            p = _up(r["endpoint"])
            if p.hostname in ("localhost", "127.0.0.1"):
                local_ports.add(p.port)
        except Exception:
            pass

    # Build per-host port set so we can apply the 8080→1234 preference rule
    host_ports: dict[str, set[int]] = {}
    for r in results:
        if r.get("status") not in ("up", "auth_required"):
            continue
        try:
            p = _up(r["endpoint"])
            host_ports.setdefault(p.hostname, set()).add(p.port)
        except Exception:
            pass

    seen: set[str] = set()
    unique: list[dict] = []
    for r in results:
        ep = r["endpoint"]
        if ep in seen:
            continue
        try:
            p = _up(ep)
            # Skip a LAN-IP entry for this machine if localhost already covers the same port
            if p.hostname in own and p.hostname not in ("localhost", "127.0.0.1") and p.port in local_ports:
                continue
            # If this host is on both 8080 and 1234, drop the 8080 entry
            if p.port == 8080 and 1234 in host_ports.get(p.hostname, set()):
                continue
            # If this host is on both 11434 and 1234, drop the 11434 entry.
            # LM Studio now exposes an Ollama-compat endpoint on :11434; prefer
            # its native :1234 API so the same server isn't benchmarked twice.
            if p.port == 11434 and 1234 in host_ports.get(p.hostname, set()):
                continue
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
    # Strip trailing /v1 so callers can pass either http://host:1234 or http://host:1234/v1
    base = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
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

    # ── Step 2: /v1/models — LM Studio / llama.cpp / OpenAI-compat ──────
    try:
        async with session.get(f"{base}/v1/models", headers=headers, timeout=timeout) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                models = [
                    {"id": m["id"], "name": m["id"], "size": 0}
                    for m in data.get("data", [])
                ]
                # ── Step 3: Distinguish llama.cpp from LM Studio ──────────
                # llama.cpp exposes /health returning {"status": "ok"|"loading"}.
                # LM Studio does not have this endpoint.
                server_type = "lmstudio"
                try:
                    async with session.get(
                        f"{base}/health",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as hr:
                        if hr.status in (200, 503):
                            hd = await hr.json(content_type=None)
                            if "status" in hd:
                                server_type = "llamacpp"
                except Exception:
                    pass
                return {"type": server_type, "endpoint": f"{base}/v1", "status": "up", "models": models}
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


async def _scan_host(
    session: aiohttp.ClientSession,
    ip: str,
    skip_ports: set[int] | None = None,
) -> list[dict]:
    """Check model-server ports on a single IP; return found servers."""
    found = []
    for port in _SCAN_PORTS:
        if skip_ports and port in skip_ports:
            continue
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
    raw_url    = (request.query.get("url") or "").strip()
    api_key    = request.query.get("api_key") or None
    prefer     = request.query.get("prefer") or "ollama"
    machine_id = request.query.get("machine_id") or None

    # If a machine_id is supplied, look up the stored key from the DB
    if machine_id and not api_key:
        m = auth_db.get_machine(machine_id)
        if m:
            api_key = m.get("api_key") or None

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
    # The gateway itself listens on HERMES_PORT (default 8080).  Exclude it from
    # the scan so Logos is never mistakenly identified as an "LM Studio (auth required)"
    # server — the /v1/models endpoint is present on the gateway too (OpenAI-compat),
    # and it returns 401 without a JWT, which looks identical to LM Studio with auth on.
    own_port = int(os.environ.get("HERMES_PORT", request.url.port or 8080))
    localhost_ports = [p for p in _SCAN_PORTS if p != own_port]

    subnet = _local_subnet()
    results: list[dict] = []

    connector = aiohttp.TCPConnector(limit=_SCAN_CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Always check localhost first (covers Docker / WSL scenarios)
        local_results = await asyncio.gather(
            *[_probe_server(session, f"http://localhost:{p}", None) for p in localhost_ports]
        )
        for r in local_results:
            if r["status"] in ("up", "auth_required"):
                results.append(r)

        if subnet:
            own_ips = _own_ips()
            hosts = [f"{subnet}.{i}" for i in range(1, 255)]
            sem = asyncio.Semaphore(_SCAN_CONCURRENCY)

            async def probe_with_sem(ip: str) -> list[dict]:
                async with sem:
                    # Skip the gateway's own port on this machine's LAN IPs
                    skip = {own_port} if ip in own_ips else None
                    return await _scan_host(session, ip, skip_ports=skip)

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

# ── Secondary (hard) evals — run when a model passes ≥5/6 standard evals ──────
# These test capabilities that matter most for real agent workflows.

# Hard 1 — 4-scenario routing across 5 tools
_HARD_TOOL_PROMPT = (
    'You have 5 tools: "search_web", "run_code", "read_file", "send_email", "query_db".\n'
    'For each request choose the single correct tool:\n'
    'A: "What are the latest AI research papers published today?"\n'
    'B: "Calculate the median of this list: [7, 2, 9, 4, 1]"\n'
    'C: "Load the contents of config.yaml"\n'
    'D: "Tell alice@company.com that the nightly job completed"\n'
    'Reply with ONLY valid JSON: {"A":"<tool>","B":"<tool>","C":"<tool>","D":"<tool>"}'
)


def _eval_hard_tool(text: str) -> bool:
    cleaned = re.sub(r"```[a-z]*\n?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        return (
            obj.get("A") == "search_web"
            and obj.get("B") == "run_code"
            and obj.get("C") == "read_file"
            and obj.get("D") == "send_email"
        )
    except Exception:
        return False


# Hard 2 — Deep nested JSON: array of objects + mixed types
_HARD_JSON_PROMPT = (
    'Reply with ONLY valid JSON, no other text:\n'
    '{"status":"ok","results":[{"id":1,"score":9.5,"tags":["pass","verified"]},'
    '{"id":2,"score":7.0,"tags":["fail"]}],"meta":{"total":2,"checked":true}}'
)


def _eval_hard_json(text: str) -> bool:
    cleaned = re.sub(r"```[a-z]*\n?", "", text).strip()
    try:
        obj = json.loads(cleaned)
        results = obj.get("results", [])
        return (
            obj.get("status") == "ok"
            and len(results) == 2
            and results[0].get("id") == 1
            and results[0].get("score") == 9.5
            and "pass" in results[0].get("tags", [])
            and results[1].get("id") == 2
            and obj.get("meta", {}).get("total") == 2
            and obj.get("meta", {}).get("checked") is True
        )
    except Exception:
        return False


# Hard 3 — 5-operation word problem (multiplication + subtraction)
_HARD_REASON_PROMPT = (
    "Answer with a single integer only:\n"
    "A factory runs 3 shifts per day. Each shift produces 8 crates. "
    "Each crate holds 12 bottles. After 5 days, 200 bottles are set aside for QA. "
    "How many bottles remain?"
)
_HARD_REASON_CHECK = "1240"   # 3 × 8 × 12 × 5 − 200 = 1440 − 200 = 1240


# Hard 4 — Constrained instruction following (must include AND exclude terms)
_HARD_INSTRUCT_PROMPT = (
    "Follow these rules exactly:\n"
    "1. Write the word DELTA\n"
    "2. Write the result of 256 ÷ 4\n"
    "3. Write the word ECHO\n"
    "4. Do NOT write the word ALPHA anywhere in your response\n"
    "5. Write the word FOXTROT"
)
_HARD_INSTRUCT_PRESENT = ["DELTA", "64", "ECHO", "FOXTROT"]
_HARD_INSTRUCT_ABSENT  = ["ALPHA"]


def _eval_hard_instruct(text: str) -> bool:
    return (
        all(c in text for c in _HARD_INSTRUCT_PRESENT)
        and all(c not in text for c in _HARD_INSTRUCT_ABSENT)
    )


def _strip_think(text: str) -> str:
    """Strip <think>...</think> (and <thinking>) blocks produced by reasoning models.

    Qwen3 and similar models emit a chain-of-thought block before the real answer.
    Removing it lets the eval checks see only the final response.

    Also handles *truncated* think blocks (no closing tag) — if max_tokens cut the
    stream off mid-think, everything from <think> onwards is reasoning noise, not
    the answer. Strip it so the caller sees an empty string and retries.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Unclosed blocks — truncated mid-think
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thinking>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
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


def _parse_model_size_b(model_id: str, hint_size_b: float = 0.0) -> float:
    """Extract parameter count in billions from a model ID string.

    If hint_size_b is provided (e.g. from Ollama /api/show), it takes priority.
    Returns 0 if unknown.
    """
    if hint_size_b > 0:
        return hint_size_b
    m = re.search(r'(\d+(?:\.\d+)?)\s*b(?:[^a-z]|$)', model_id.lower())
    return float(m.group(1)) if m else 0.0


async def _ollama_fetch_sizes(
    base_url: str, model_ids: list[str], http: aiohttp.ClientSession
) -> dict[str, float]:
    """Call Ollama /api/show for each model to get the real parameter count.

    Returns {model_id: size_in_billions}. Missing/failed entries are omitted.
    """
    sizes: dict[str, float] = {}
    for mid in model_ids:
        try:
            async with http.post(
                f"{base_url}/api/show",
                json={"name": mid},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    param_size = data.get("details", {}).get("parameter_size", "")
                    m = re.match(r"(\d+(?:\.\d+)?)", param_size.upper().replace("B", ""))
                    if m:
                        sizes[mid] = float(m.group(1))
        except Exception:
            pass
    return sizes


_INSTRUCT_KEYWORDS = {"instruct", "chat", "it", "tool", "assistant", "hermes", "qwen", "gemma", "llama"}
# Models specialised for one domain — not ideal as everyday agent defaults
_SPECIALIZED_KEYWORDS = {"coder", "code", "math", "vision", "ocr", "embed", "search"}


def _pick_compare_candidates(
    model_ids: list[str], max_n: int = 4, sizes: dict[str, float] | None = None
) -> list[str]:
    """Sample one representative from each size bucket, then fill remaining slots.

    Buckets: small (<5B), mid (5–13B), large (>13B), unknown (no size in name).
    Within each bucket models are ranked by quality heuristics:
      - mid/large: prefer closer to 9B sweet spot; larger wins ties
      - small: prefer larger (closer to 4–5B)
      - unknown: prefer instruct-hinted names; deprioritise coder/math/vision
    This avoids the old approach of hard-suppressing unknowns or large quants.
    """
    _sizes = sizes or {}

    # Deduplicate by base model name — strip spawned-instance suffixes like `:2`, `:3`
    # so that a second Logos instance running the same model isn't benchmarked twice.
    seen_base: set[str] = set()
    deduped: list[str] = []
    for mid in model_ids:
        base = re.sub(r":\d+$", "", mid)
        if base not in seen_base:
            seen_base.add(base)
            deduped.append(mid)
    model_ids = deduped

    # Exclude embedding-only models — they can't do chat completions.
    _EMBED_KEYWORDS = {"embed", "embedding", "nomic-embed", "bge-", "e5-", "gte-"}
    model_ids = [
        mid for mid in model_ids
        if not any(kw in mid.lower() for kw in _EMBED_KEYWORDS)
    ]

    def _bucket(mid: str) -> str:
        sz = _parse_model_size_b(mid, _sizes.get(mid, 0.0))
        if sz == 0:   return "unknown"
        if sz < 5:    return "small"
        if sz <= 13:  return "mid"
        return "large"

    def _rank(mid: str, bucket: str) -> float:
        sz = _parse_model_size_b(mid, _sizes.get(mid, 0.0))
        if bucket == "mid":    return abs(sz - 9.0)
        if bucket == "small":  return -sz                 # larger small = better
        if bucket == "large":  return sz                  # smaller large = better
        # unknown: prefer instruct-hinted, deprioritise specialised (coder/math/vision)
        name = mid.lower()
        if any(kw in name for kw in _SPECIALIZED_KEYWORDS): return 2.0
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


def _sanitize_for_json(obj: object) -> object:
    """Replace non-finite floats (NaN/Infinity) with None so json.dumps produces
    valid JSON.  Python's json module outputs bare NaN/Infinity by default which
    JSON.parse() in browsers rejects, silently dropping the done event."""
    import math as _math
    if isinstance(obj, float):
        return None if (_math.isnan(obj) or _math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


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
    sz         = _parse_model_size_b(r["model"], r.get("size_b", 0.0))
    size_b     = min(sz, 13) / 13 * 0.05 if sz > 0 else 0
    ttft_ms    = r.get("ttft_ms")
    # TTFT score: ≤500ms→1.0, ≥4000ms→0.0
    ttft_score = max(0.0, min(1.0, (4000 - ttft_ms) / 3500)) if ttft_ms is not None else 0.5
    # Penalise domain-specific models (coder, math, vision) — not good everyday agent defaults
    name_lower = r["model"].lower()
    specialized_penalty = -0.15 if any(kw in name_lower for kw in _SPECIALIZED_KEYWORDS) else 0.0
    return 0.60 * eval_frac + 0.20 * speed + 0.15 * ttft_score + size_b + specialized_penalty


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


async def _unload_model(base_url: str, model_id: str, server_type: str, api_key: str) -> None:
    """Best-effort unload a model from an inference server to free VRAM."""
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "ollama" else {}
        async with aiohttp.ClientSession() as sess:
            if server_type == "lmstudio":
                async with sess.post(
                    f"{base_url}/api/v1/models/unload",
                    headers=headers,
                    json={"instance_id": model_id},
                    timeout=aiohttp.ClientTimeout(total=8),
                ):
                    pass
            elif server_type == "ollama":
                async with sess.post(
                    f"{base_url}/api/generate",
                    json={"model": model_id, "keep_alive": 0, "prompt": ""},
                    timeout=aiohttp.ClientTimeout(total=8),
                ):
                    pass
    except Exception:
        pass  # best-effort — don't propagate errors


async def handle_setup_compare(request: web.Request) -> web.Response:
    """SSE: quick-benchmark up to 4 candidate models and recommend the best one.

    Events emitted:
      {"targets": [model_id, ...]}
      {"testing": model_id, "testing_endpoint": endpoint}
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

    # Accept per-model specs [{id, endpoint, api_key, server_type, machine_id?}] or plain strings
    model_specs: list[dict] = []
    for m in raw_models:
        if isinstance(m, str):
            model_specs.append({"id": m, "endpoint": fallback_endpoint, "api_key": fallback_key, "server_type": fallback_type})
        else:
            raw_key = m.get("api_key") or fallback_key
            # Resolve '__stored__' placeholder — look up the api_key from the machine DB record
            if raw_key == "__stored__":
                machine_rec = auth_db.get_machine(m.get("machine_id") or "")
                raw_key = (machine_rec or {}).get("api_key") or "ollama"
            model_specs.append({
                "id":          m["id"],
                "endpoint":    (m.get("endpoint") or fallback_endpoint).rstrip("/"),
                "api_key":     raw_key,
                "server_type": m.get("server_type") or fallback_type,
            })

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    bench_id = str(uuid.uuid4())
    _bench_cancels[bench_id] = set()
    _bench_active[bench_id] = {}

    async def send(data: dict) -> None:
        await response.write(f"data: {json.dumps(data)}\n\n".encode())

    # Pick candidates per-server so each machine gets its own benchmark pool
    # (up to 4 candidates from each server's model list independently)
    from collections import defaultdict as _dd
    per_server_specs: dict[str, list[dict]] = _dd(list)
    for s in model_specs:
        per_server_specs[s["endpoint"]].append(s)

    # Enrich Ollama specs with actual parameter counts from /api/show
    # so that models without a size in their name are bucketed correctly.
    _ollama_sizes: dict[str, dict[str, float]] = {}   # ep → {model_id: size_b}
    async with aiohttp.ClientSession() as _size_http:
        for ep, specs in per_server_specs.items():
            if specs and specs[0]["server_type"] == "ollama":
                base = re.sub(r"/v1/?$", "", ep)
                _ollama_sizes[ep] = await _ollama_fetch_sizes(base, [s["id"] for s in specs], _size_http)
                # Store enriched size_b back on spec for downstream use
                for spec in specs:
                    sz = _ollama_sizes[ep].get(spec["id"], 0.0)
                    if sz > 0:
                        spec["size_b"] = sz

    server_groups: dict[str, list[dict]] = _dd(list)
    for ep, specs in per_server_specs.items():
        ep_sizes = _ollama_sizes.get(ep, {})
        ep_candidate_ids = set(_pick_compare_candidates([s["id"] for s in specs], sizes=ep_sizes))
        for s in specs:
            if s["id"] in ep_candidate_ids:
                server_groups[ep].append(s)

    candidates = [m for group in server_groups.values() for m in group]
    n_servers = len(server_groups)
    await send({"bench_id": bench_id, "targets": [c["id"] for c in candidates]})
    await send({"log": (
        f"Testing {len(candidates)} model{'s' if len(candidates) != 1 else ''} "
        f"across {n_servers} server{'s' if n_servers != 1 else ''}"
        + (" — each server benchmarked independently in parallel" if n_servers > 1 else "")
    )})

    results: list[dict] = []
    results_lock = asyncio.Lock()

    async def _flush_lmstudio_vram(base_url: str, http: aiohttp.ClientSession, api_key: str = "") -> None:
        """Unload every currently-loaded model from LM Studio before benchmarking.

        Models loaded outside our session (e.g. olmocr left loaded by the user)
        will consume VRAM and throttle throughput for every model we test.
        """
        _headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "ollama" else {}
        try:
            async with http.get(
                f"{base_url}/api/v1/models",
                headers=_headers,
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
                    headers=_headers,
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
            await _flush_lmstudio_vram(base_url_flush, http, first_spec.get("api_key", ""))

        for spec in group:
            model_id    = spec["id"]
            endpoint    = spec["endpoint"]
            api_key     = spec["api_key"]
            server_type = spec["server_type"]
            base_url    = re.sub(r"/v1/?$", "", endpoint)

            # Check if user clicked Stop on this server mid-benchmark
            if endpoint in _bench_cancels.get(bench_id, set()):
                result = {"model": model_id, "endpoint": endpoint, "skipped": True, "error": "Stopped by user"}
                async with results_lock:
                    results.append(result)
                await send({"result": result})
                continue

            await send({"testing": model_id, "testing_endpoint": endpoint})
            # Track which model is currently loaded on this endpoint so cancel can unload it
            if bench_id in _bench_active:
                _bench_active[bench_id][endpoint] = {
                    "model": model_id, "server_type": server_type,
                    "api_key": api_key, "base_url": base_url,
                }
            type_label = {"lmstudio": "LM Studio", "ollama": "Ollama", "llamacpp": "llama.cpp"}.get(server_type, server_type)
            await send({"log": f"→ {model_id}  ({base_url})  [{type_label}]"})

            # LM Studio: try native load API before benchmarking (best-effort)
            if server_type == "lmstudio":
                _auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "ollama" else {}
                try:
                    async with http.post(
                        f"{base_url}/api/v1/models/load",
                        headers=_auth_headers,
                        json={"model": model_id, "context_length": 4096, "n_parallel": 1},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as lr:
                        if lr.status == 200:
                            await send({"log": f"  Load request sent for {model_id}"})
                        else:
                            await send({"log": f"  Load request HTTP {lr.status} (model may already be loaded)"})
                except Exception as _le:
                    await send({"log": f"  Load request failed: {str(_le)[:80]}"})


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
                # ── llama.cpp: wait for server ready before first request ─────
                # llama.cpp disconnects mid-stream if the model is still loading.
                # Poll /health until {"status":"ok"} or 45s timeout.
                if server_type == "llamacpp":
                    health_url = f"{base_url}/health"
                    deadline   = time.time() + 45
                    while time.time() < deadline:
                        try:
                            async with http.get(health_url, timeout=aiohttp.ClientTimeout(total=3)) as _hr:
                                if _hr.status == 200:
                                    _hd = await _hr.json(content_type=None)
                                    if _hd.get("status") == "ok":
                                        break
                                    await send({"log": f"  Server status: {_hd.get('status','?')} — waiting…"})
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                    else:
                        await send({"log": "  ⚠ Server not ready after 45s, attempting anyway…"})

                # ── Warmup pass — ensures model is fully loaded before timing ──
                # Result is discarded; this prevents the cold-start penalty from
                # skewing pass 1 (seen as ~33% of real throughput on first model).
                # llama.cpp with --sleep-idle-seconds wakes on the first real request
                # and drops the connection while loading.  Retry up to 3× with a
                # /health re-poll so we wait out the reload rather than giving up.
                await send({"log": "  Warmup (loading model into memory)…"})
                _warmup_ok = False
                _max_warmup = 3 if server_type == "llamacpp" else 1
                for _wa in range(_max_warmup):
                    try:
                        await _bench_run(0, "Reply with one word.", max_tokens=10)
                        hint_task.cancel()
                        await send({"log": "  Model ready."})
                        _warmup_ok = True
                        break
                    except Exception as _we:
                        if server_type == "llamacpp" and _wa < _max_warmup - 1:
                            await send({"log": f"  Connection dropped (attempt {_wa+1}/{_max_warmup}) — model may be loading from sleep, re-checking…"})
                            # Re-poll /health until loading is done before retry
                            _hp_deadline = time.time() + 30
                            while time.time() < _hp_deadline:
                                try:
                                    async with http.get(f"{base_url}/health", timeout=aiohttp.ClientTimeout(total=3)) as _hr2:
                                        if _hr2.status == 200:
                                            _hd2 = await _hr2.json(content_type=None)
                                            if _hd2.get("status") == "ok":
                                                break
                                except Exception:
                                    pass
                                await asyncio.sleep(2)
                        else:
                            await send({"log": f"  Warmup failed ({str(_we)[:80]}), continuing anyway…"})
                # Reset t0 so TTFT on pass 1 is measured from a hot-model start
                t0 = time.time()

                # ── Context window detection ───────────────────────────────────
                # llama.cpp : reads n_ctx from /props (fixed at server startup).
                # Ollama    : reads llama.context_length from /api/show GGUF metadata.
                # LM Studio : reads max_context_length from /api/v1/models metadata,
                #             then probes downward to find the VRAM-limited maximum.
                #             The metadata ceiling prevents asking LM Studio to load a
                #             model above its native context (which it accepts with 200
                #             but silently caps or misconfigures).
                max_context: int | None = None
                if server_type == "llamacpp":
                    # llama.cpp reports its configured --ctx-size via GET /props
                    try:
                        async with http.get(
                            f"{base_url}/props",
                            timeout=aiohttp.ClientTimeout(total=4),
                        ) as _pp:
                            if _pp.status == 200:
                                _pd = await _pp.json(content_type=None)
                                _nc = _pd.get("n_ctx") or _pd.get("total_slots_n_ctx")
                                if isinstance(_nc, int) and _nc > 0:
                                    max_context = _nc
                                    await send({"log": f"  Context window: {_nc:,} tokens (from /props)"})
                    except Exception:
                        pass
                elif server_type == "ollama":
                    # Ollama exposes the model's native context ceiling via /api/show,
                    # but defaults to 2048 at runtime.  We read the ceiling first, then
                    # probe downward to find the largest num_ctx the machine's VRAM can
                    # actually handle — same approach as LM Studio.
                    _ollama_native_ctx: int | None = None
                    try:
                        async with http.post(
                            f"{base_url}/api/show",
                            json={"name": model_id},
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as _sr:
                            if _sr.status == 200:
                                _sd = await _sr.json(content_type=None)
                                _nc = (_sd.get("model_info") or {}).get("llama.context_length")
                                if isinstance(_nc, int) and _nc > 0:
                                    _ollama_native_ctx = _nc
                                    await send({"log": f"  Native context (GGUF): {_nc:,} tokens"})
                    except Exception:
                        pass
                    # Probe downward from native ceiling to find the largest
                    # num_ctx the machine's VRAM can actually handle.  Ollama's
                    # native /api/generate accepts num_ctx as an option — use it
                    # instead of the OpenAI-compat endpoint which ignores the param.
                    await send({"log": "  Probing max loadable context window…"})
                    _ollama_probe_sizes = [262144, 131072, 65536, 32768, 16384, 8192, 4096]
                    _ollama_ceil = _ollama_native_ctx or 262144
                    for _ctx_probe in [s for s in _ollama_probe_sizes if s <= _ollama_ceil]:
                        try:
                            async with http.post(
                                f"{base_url}/api/generate",
                                json={
                                    "model": model_id,
                                    "prompt": "Say OK.",
                                    "options": {"num_ctx": _ctx_probe},
                                    "stream": False,
                                },
                                timeout=aiohttp.ClientTimeout(total=90),
                            ) as _vr:
                                if _vr.status == 200:
                                    max_context = _ctx_probe
                                    await send({"log": f"  Context probe: {_ctx_probe:,} tokens ✓"})
                                    break
                                else:
                                    await send({"log": f"  Context probe: {_ctx_probe:,} — HTTP {_vr.status}, trying smaller…"})
                        except Exception as _ce:
                            await send({"log": f"  Context probe: {_ctx_probe:,} — {str(_ce)[:60]}, trying smaller…"})
                    if max_context is None:
                        # All probes failed — fall back to metadata value
                        if _ollama_native_ctx:
                            max_context = _ollama_native_ctx
                            await send({"log": f"  Context probe: could not verify — using metadata ({_ollama_native_ctx:,})"})
                        else:
                            await send({"log": "  Context probe: could not determine context window"})
                    # Persist probed context so runtime can use it
                    if max_context:
                        try:
                            import yaml as _cp_yaml
                            _cp_path = pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".logos")) / "config.yaml"
                            _cp_cfg: dict = _cp_yaml.safe_load(_cp_path.read_text(encoding="utf-8")) if _cp_path.exists() else {}
                            _cp_cfg.setdefault("ollama_context_lengths", {})[model_id] = max_context
                            _cp_path.write_text(_cp_yaml.dump(_cp_cfg, default_flow_style=False, allow_unicode=True))
                        except Exception:
                            pass
                elif server_type == "lmstudio":
                    _ctx_probe_headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "ollama" else {}
                    # Step 1: read native max_context_length from LM Studio's model list.
                    # This comes from the GGUF file and is the authoritative ceiling —
                    # LM Studio will accept load requests above this value with HTTP 200
                    # but will internally cap or misconfigure the context.  Using the
                    # metadata ceiling means we never probe above what the model supports.
                    _native_ctx: int | None = None
                    try:
                        async with http.get(
                            f"{base_url}/api/v1/models",
                            headers=_ctx_probe_headers,
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as _mr:
                            await send({"log": f"  Native API status: HTTP {_mr.status}"})
                            if _mr.status == 200:
                                _md = await _mr.json(content_type=None)
                                # LM Studio may return a list or a dict with a "data" key.
                                _model_list = _md if isinstance(_md, list) else _md.get("data", [])
                                await send({"log": f"  Native API model count: {len(_model_list)}"})
                                _model_id_lower = model_id.lower()
                                for _m in _model_list:
                                    _mid = (_m.get("id") or "").lower()
                                    await send({"log": f"  Native entry: id={_m.get('id')!r} max_context_length={_m.get('max_context_length')!r} keys={list(_m.keys())[:6]}"})
                                    # LM Studio's native API IDs often include the
                                    # quantisation suffix (.gguf) that the OpenAI-compat
                                    # /v1/models endpoint strips.  Match on substring.
                                    if _mid == _model_id_lower or _model_id_lower in _mid or _mid in _model_id_lower:
                                        _nc = _m.get("max_context_length")
                                        if isinstance(_nc, int) and _nc > 0:
                                            _native_ctx = _nc
                                        break
                            else:
                                _raw = (await _mr.text())[:120]
                                await send({"log": f"  Native API error body: {_raw}"})
                    except Exception as _me:
                        await send({"log": f"  Could not read LM Studio model list: {str(_me)[:80]}"})
                    if _native_ctx:
                        await send({"log": f"  Native context (GGUF): {_native_ctx:,} tokens"})
                    else:
                        await send({"log": "  Native context: not found in model list — probing from 262144"})
                    # Step 2: probe downward from the native ceiling (or 65536 if unknown)
                    # to find the largest context this machine's VRAM can actually load.
                    await send({"log": "  Probing max loadable context window…"})
                    _all_probe_sizes = [262144, 131072, 65536, 32768, 16384, 8192, 4096]
                    _probe_ceil = _native_ctx if _native_ctx else 262144
                    for _ctx_probe in [s for s in _all_probe_sizes if s <= _probe_ceil]:
                        # Unload first so we start from a clean slot
                        try:
                            async with http.post(
                                f"{base_url}/api/v1/models/unload",
                                headers=_ctx_probe_headers,
                                json={"instance_id": model_id},
                                timeout=aiohttp.ClientTimeout(total=8),
                            ):
                                pass
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
                        try:
                            async with http.post(
                                f"{base_url}/api/v1/models/load",
                                headers=_ctx_probe_headers,
                                json={"model": model_id, "context_length": _ctx_probe, "n_parallel": 1},
                                timeout=aiohttp.ClientTimeout(total=30),
                            ) as _cl:
                                if _cl.status != 200:
                                    await send({"log": f"  Context probe: {_ctx_probe:,} — HTTP {_cl.status}, trying smaller…"})
                                    continue
                            # LM Studio can return HTTP 200 but silently load the model in a
                            # degraded state (red indicator in UI) when VRAM is insufficient.
                            # Short prompts still succeed because they never fill the KV cache.
                            # Verify by actually sending a payload sized to the probed context.
                            await asyncio.sleep(1.0)
                            _verify_ok = False
                            try:
                                _filler_unit = "The quick brown fox jumps over the lazy dog. "
                                _target_chars = (_ctx_probe - 200) * 3  # ~3 chars/token
                                _reps = max(1, _target_chars // len(_filler_unit))
                                _filler = _filler_unit * _reps
                                _v_headers = {**_ctx_probe_headers, "Content-Type": "application/json"}
                                async with http.post(
                                    f"{endpoint}/chat/completions",
                                    headers=_v_headers,
                                    json={
                                        "model": model_id,
                                        "messages": [{"role": "user", "content": _filler + "\n\nSay OK."}],
                                        "max_tokens": 1,
                                        "stream": False,
                                    },
                                    timeout=aiohttp.ClientTimeout(total=120),
                                ) as _vr:
                                    if _vr.status == 200:
                                        _verify_ok = True
                                        await send({"log": f"  Context verify: {_ctx_probe:,} tokens — full payload accepted ✓"})
                                    else:
                                        _vbody = (await _vr.text())[:120]
                                        await send({"log": f"  Context verify: {_ctx_probe:,} — HTTP {_vr.status}: {_vbody}, trying smaller…"})
                            except Exception as _ve:
                                await send({"log": f"  Context verify: {_ctx_probe:,} — {str(_ve)[:80]}, trying smaller…"})
                            if _verify_ok:
                                max_context = _ctx_probe
                                await send({"log": f"  Context probe: {_ctx_probe:,} tokens ✓"})
                                break
                        except Exception as _ce:
                            await send({"log": f"  Context probe: {_ctx_probe:,} — {str(_ce)[:60]}, trying smaller…"})
                    if max_context is None:
                        await send({"log": "  Context probe: could not determine (model may not support load API)"})
                    # Persist per-model result so runtime skips the probe
                    if max_context:
                        try:
                            import yaml as _cp_yaml
                            _cp_path = pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".logos")) / "config.yaml"
                            _cp_cfg: dict = _cp_yaml.safe_load(_cp_path.read_text(encoding="utf-8")) if _cp_path.exists() else {}
                            _cp_cfg.setdefault("lmstudio_context_lengths", {})[model_id] = max_context
                            _cp_path.write_text(_cp_yaml.dump(_cp_cfg, default_flow_style=False, allow_unicode=True))
                        except Exception:
                            pass

                # ── 3-pass speed benchmark, take median (discards outliers) ──
                # Retry once on ServerDisconnectedError — llama.cpp and some other
                # servers close the TCP connection immediately after each response.
                # aiohttp may return a stale pooled connection; a single retry with a
                # short pause is enough to get a fresh connection established.
                async def _bench_run_r(pass_num: int, prompt: str, max_tokens: int = 120) -> tuple[float, bool]:
                    try:
                        return await _bench_run(pass_num, prompt, max_tokens)
                    except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError):
                        await asyncio.sleep(0.5)
                        return await _bench_run(pass_num, prompt, max_tokens)

                r1, approx1 = await _bench_run_r(1, _BENCH_PROMPT)
                await send({"log": f"  Pass 1 (prose): {r1:.1f} tok/s"})

                # If TTFT looks anomalously high, re-measure once with a short
                # prompt to check for a one-off spike (GC pause, partial load, etc.)
                # Use the lower of the two values.
                _ttft_pass1 = ttft_s
                if _ttft_pass1 is not None and _ttft_pass1 > 2.0:
                    await send({"log": f"  TTFT {_ttft_pass1*1000:.0f}ms — re-measuring once to verify…"})
                    t0 = time.time()
                    try:
                        await _bench_run_r(1, "Hi", max_tokens=5)
                        if ttft_s is not None and ttft_s < _ttft_pass1 * 0.6:
                            await send({"log": f"  TTFT retry: {ttft_s*1000:.0f}ms — significantly lower, using this value"})
                        else:
                            _retry_ms = round(ttft_s * 1000) if ttft_s is not None else "?"
                            await send({"log": f"  TTFT retry: {_retry_ms}ms — consistent, keeping original"})
                            ttft_s = _ttft_pass1
                    except Exception:
                        ttft_s = _ttft_pass1

                r2, approx2 = await _bench_run_r(2, _BENCH_PROMPT_STRUCT)
                await send({"log": f"  Pass 2 (structured): {r2:.1f} tok/s"})
                r3, approx3 = await _bench_run_r(3, _BENCH_PROMPT)
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
                    2048,
                )
                await send({"log": f"    {'✓' if instruct_pass else '✗'} instruction following"})
                if not instruct_pass:
                    missing = [c for c in _INSTRUCT_CHECKS if c not in instruct_resp]
                    await _log_eval_detail(_INSTRUCT_PROMPT, instruct_resp, missing)

                await send({"log": "  Eval 2/6: reasoning (2-part)…"})
                reason_pass, reason_resp = await _eval_once(
                    _REASON_PROMPT,
                    lambda t: all(c in t for c in _REASON_CHECKS),
                    2048,
                )
                await send({"log": f"    {'✓' if reason_pass else '✗'} reasoning"})
                if not reason_pass:
                    missing = [c for c in _REASON_CHECKS if c not in reason_resp]
                    await _log_eval_detail(_REASON_PROMPT, reason_resp, missing)

                await send({"log": "  Eval 3/6: strict JSON format…"})
                format_pass, format_resp = await _eval_once(_FORMAT_PROMPT, _eval_format, 2048)
                await send({"log": f"    {'✓' if format_pass else '✗'} JSON format"})
                if not format_pass:
                    await _log_eval_detail(_FORMAT_PROMPT, format_resp)

                await send({"log": "  Eval 4/6: tool selection (2 scenarios)…"})
                tool_pass, tool_resp = await _eval_once(_TOOL_PROMPT, _eval_tool_call, 2048)
                await send({"log": f"    {'✓' if tool_pass else '✗'} tool selection"})
                if not tool_pass:
                    await _log_eval_detail(_TOOL_PROMPT, tool_resp)

                await send({"log": "  Eval 5/6: nested JSON schema…"})
                nested_json_pass, nested_resp = await _eval_once(_NESTED_JSON_PROMPT, _eval_nested_json, 2048)
                await send({"log": f"    {'✓' if nested_json_pass else '✗'} nested JSON"})
                if not nested_json_pass:
                    await _log_eval_detail(_NESTED_JSON_PROMPT, nested_resp)

                await send({"log": "  Eval 6/6: multi-step reasoning…"})
                multihop_pass, multihop_resp = await _eval_once(_MULTIHOP_PROMPT, _eval_multihop, 2048)
                await send({"log": f"    {'✓' if multihop_pass else '✗'} multi-step reasoning"})
                if not multihop_pass:
                    expected = _MULTIHOP_CHECK
                    await _log_eval_detail(_MULTIHOP_PROMPT, multihop_resp, [f"expected '{expected}'"])

                tests_passed = sum([instruct_pass, reason_pass, format_pass, tool_pass, nested_json_pass, multihop_pass])
                quality_pass = tests_passed >= 4
                await send({"log": f"  {'✓' if quality_pass else '⚠'} {tok_s} tok/s{approx_note} · {tests_passed}/6 eval tests passed"})

                # ── Secondary (hard) evals — only run if model passed ≥5/6 ──
                hard_eval: dict = {}
                if tests_passed >= 5:
                    await send({"log": "  ★ Hard evals (model passed ≥5/6 — running advanced tests)…"})
                    h1_pass, _ = await _eval_once(_HARD_TOOL_PROMPT, _eval_hard_tool, 2048)
                    await send({"log": f"    {'✓' if h1_pass else '✗'} hard tool routing (4 scenarios, 5 tools)"})

                    h2_pass, _ = await _eval_once(_HARD_JSON_PROMPT, _eval_hard_json, 2048)
                    await send({"log": f"    {'✓' if h2_pass else '✗'} deep nested JSON (array of objects)"})

                    h3_pass, h3_resp = await _eval_once(_HARD_REASON_PROMPT, lambda t: _HARD_REASON_CHECK in t, 2048)
                    await send({"log": f"    {'✓' if h3_pass else '✗'} 5-step arithmetic (expected {_HARD_REASON_CHECK})"})
                    if not h3_pass:
                        await _log_eval_detail(_HARD_REASON_PROMPT, h3_resp, [f"expected '{_HARD_REASON_CHECK}'"])

                    h4_pass, _ = await _eval_once(_HARD_INSTRUCT_PROMPT, _eval_hard_instruct, 2048)
                    await send({"log": f"    {'✓' if h4_pass else '✗'} constrained instructions (include+exclude)"})

                    hard_score = sum([h1_pass, h2_pass, h3_pass, h4_pass])
                    await send({"log": f"  ★ Hard eval score: {hard_score}/4"})
                    hard_eval = {
                        "hard_tool": h1_pass, "hard_json": h2_pass,
                        "hard_reasoning": h3_pass, "hard_instruct": h4_pass,
                        "score": hard_score,
                    }

                result: dict = {
                    "model": model_id, "tok_s": tok_s, "quality_pass": quality_pass,
                    "ttft_ms": ttft_ms, "approx": approx, "endpoint": endpoint,
                    **({"max_context": max_context} if max_context else {}),
                    **({"size_b": spec.get("size_b")} if spec.get("size_b") else {}),
                    "eval": {
                        "instruction": instruct_pass, "reasoning": reason_pass,
                        "format": format_pass, "tool_call": tool_pass,
                        "nested_json": nested_json_pass, "multihop": multihop_pass,
                        "score": tests_passed,
                        **({"hard": hard_eval} if hard_eval else {}),
                    },
                }
            except Exception as exc:
                if isinstance(exc, aiohttp.ServerDisconnectedError):
                    if server_type == "llamacpp":
                        err_msg = "Server disconnected — llama.cpp dropped the connection (model still loading or server ran out of memory). Check server logs."
                    else:
                        err_msg = "Server disconnected — connection closed unexpectedly. Is the model loaded?"
                else:
                    err_msg = str(exc)
                await send({"log": f"  ✗ Error: {err_msg[:300]}"})
                result = {"model": model_id, "tok_s": 0, "quality_pass": False, "ttft_ms": None, "approx": False, "endpoint": endpoint, "error": err_msg[:300]}
            finally:
                hint_task.cancel()
                try:
                    if server_type == "lmstudio":
                        _unload_headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "ollama" else {}
                        async with http.post(
                            f"{base_url}/api/v1/models/unload",
                            headers=_unload_headers,
                            json={"instance_id": model_id},
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as ur:
                            await send({"log": f"  Unloaded {model_id} (HTTP {ur.status})"})
                        await asyncio.sleep(1.5)   # give LM Studio time to free VRAM before next load
                    elif server_type == "ollama":
                        async with http.post(
                            f"{base_url}/api/generate",
                            json={"model": model_id, "keep_alive": 0, "prompt": ""},
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as ur:
                            await send({"log": f"  Unloaded {model_id} (HTTP {ur.status})"})
                except Exception as ue:
                    await send({"log": f"  Unload skipped: {str(ue)[:60]}"})
                # Clear active tracking — model is now unloaded
                if bench_id in _bench_active:
                    _bench_active[bench_id].pop(endpoint, None)

            async with results_lock:
                results.append(result)
            await send({"result": result})

    try:
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*[
                _test_server_group(group, session)
                for group in server_groups.values()
            ])
    finally:
        _bench_cancels.pop(bench_id, None)
        _bench_active.pop(bench_id, None)

    valid = [r for r in results if not r.get("error") and r.get("tok_s", 0) > 0]

    # Context window gate: agent system prompt is ~7 800 tokens; anything ≤ 8 192 ctx
    # leaves essentially no room for conversation and must not be recommended.
    # Models without a measured max_context (e.g. remote APIs) are allowed through.
    _MIN_CTX = 16384
    ctx_viable = [r for r in valid if r.get("max_context") is None or r["max_context"] >= _MIN_CTX]
    ctx_limited = [r for r in valid if r.get("max_context") is not None and r["max_context"] < _MIN_CTX]
    if ctx_limited:
        names = ", ".join(r["model"] for r in ctx_limited)
        await send({"log": f"⚠ Context too small for agent use (<{_MIN_CTX//1024}K): {names}"})
    # Fall back to all valid if nothing clears the ctx bar (e.g. all are known-small)
    ctx_pool = ctx_viable if ctx_viable else valid

    # Mandatory gates: JSON format + tool-call selection are critical for agent use.
    # Models that pass both are ranked first; if none pass, fall back to all ctx-viable models.
    gated = [r for r in ctx_pool if r.get("eval", {}).get("format") and r.get("eval", {}).get("tool_call")]
    pool  = gated if gated else ctx_pool
    if gated:
        await send({"log": f"{len(gated)}/{len(ctx_pool)} model(s) passed format+tool gates"})
    elif ctx_pool:
        await send({"log": "⚠ No model passed format+tool gates — ranking all viable models"})

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
        ep_ctx   = [r for r in ep_valid if r.get("max_context") is None or r["max_context"] >= _MIN_CTX]
        ep_ctx   = ep_ctx if ep_ctx else ep_valid
        ep_gated = [r for r in ep_ctx if r.get("eval", {}).get("format") and r.get("eval", {}).get("tool_call")]
        ep_pool  = ep_gated if ep_gated else ep_ctx
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

    # ── TTFT outlier detection — flag models whose TTFT is ≥3× median ──────
    # TTFT is measured after warmup (model already hot in VRAM/RAM), so an
    # unusually high value almost always means the inference machine is under
    # load, not that the model is cold.  We only flag when ≥2 models have TTFT
    # data AND the outlier is >3 s absolute (avoids false positives on
    # CPU-only boxes where all models are legitimately slow but similar).
    ttft_warnings: list[dict] = []
    ttft_data = [r for r in valid if r.get("ttft_ms") is not None]
    if len(ttft_data) >= 2:
        ttfts = sorted(r["ttft_ms"] for r in ttft_data)
        median_ttft = ttfts[len(ttfts) // 2]
        _TTFT_RATIO   = 3.0    # ≥3× median
        _TTFT_ABS_MS  = 3000   # AND >3 s absolute
        for r in ttft_data:
            if r["ttft_ms"] >= _TTFT_ABS_MS and r["ttft_ms"] >= _TTFT_RATIO * median_ttft:
                ttft_warnings.append({
                    "model":    r["model"],
                    "ttft_ms":  r["ttft_ms"],
                    "endpoint": r.get("endpoint", ""),
                })
                await send({"log": (
                    f"  ⚠ {r['model']} TTFT {r['ttft_ms']}ms vs median {median_ttft}ms"
                    f" — server may be under load"
                )})

    try:
        await send(_sanitize_for_json({
            "done":                       True,
            "recommendation":             best["model"] if best else None,
            "reason":                     _compare_reason(best) if best else "Could not benchmark any models.",
            "fast_recommendation":        fast_rec,
            "fast_reason":                fast_reason,
            "per_server_recommendations": per_server_recs,
            "results":                    results,
            "ttft_warnings":              ttft_warnings,
        }))
    except Exception as _done_err:
        logger.warning("handle_setup_compare: error sending done event: %s", _done_err)
        try:
            await send({"done": True, "recommendation": None,
                        "reason": f"Error finalising benchmark: {_done_err}",
                        "results": [], "per_server_recommendations": {}, "ttft_warnings": []})
        except Exception:
            pass
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
                    rec_model = (srv.get("recommended_model") or "").strip()
                    if rec_model:
                        auth_db.update_machine(m_obj["id"], default_model=rec_model)
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
                # Backfill default_model on the newly created machine
                _single_rec = (body.get("model") or "").strip()
                if _single_rec:
                    for _m in auth_db.list_machines():
                        if (_m.get("endpoint_url") or "").rstrip("/") == endpoint.rstrip("/"):
                            auth_db.update_machine(_m["id"], default_model=_single_rec)
                            break

        # Save model preference on the admin user.
        # During first-run setup the browser has no session yet, so we look up
        # the seeded admin account directly rather than reading current_user.
        # Use get_primary_admin() (oldest admin by created_at) so that re-runs
        # always target the original seeded account, not a newer admin that was
        # created as an "additional user" during a previous setup run.
        primary_admin = auth_db.get_primary_admin()
        if not primary_admin:
            return web.json_response({"error": "no_admin_user"}, status=500)
        user_id = primary_admin["id"]
        agent_type   = (body.get("agent_type") or "general").strip()
        exec_env     = (body.get("exec_env") or "local").strip()
        k8s_ns       = (body.get("k8s_namespace") or "hermes").strip()
        kubeconfig   = (body.get("kubeconfig") or "").strip()

        # Write chosen model + endpoint to config.yaml so the agent actually uses them.
        # Keys are bridged to env vars by run.py on startup (only if not already in env,
        # so pre-configured k8s deployments with explicit env vars are not overridden).
        _hermes_home = pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".logos"))
        _config_path = _hermes_home / "config.yaml"
        try:
            import yaml as _yaml
            _cfg: dict = {}
            if _config_path.exists():
                with open(_config_path, encoding="utf-8") as _f:
                    _cfg = _yaml.safe_load(_f) or {}
            _cfg["HERMES_MODEL"] = model
            _cfg["OPENAI_BASE_URL"] = endpoint
            # Persist the API key for the primary server so the agent authenticates.
            # Find it from the servers list; fall back to top-level api_key field.
            _primary_key = ""
            for _srv in (body.get("servers") or []):
                if (_srv.get("endpoint") or "").rstrip("/") == endpoint.rstrip("/"):
                    _primary_key = _srv.get("api_key") or ""
                    break
            if not _primary_key:
                _primary_key = (body.get("api_key") or "")
            # Always write the key from setup — overwrite any stale key left
            # from a previous session.  An empty key clears a stale wrong key
            # (important for LM Studio which returns 401 when auth is enabled
            # and the wrong key is sent).
            _cfg["OPENAI_API_KEY"] = _primary_key
            os.environ["OPENAI_API_KEY"] = _primary_key  # live process picks it up immediately
            # Always sync live so the agent uses the new values without a restart
            os.environ["OPENAI_BASE_URL"] = endpoint
            os.environ["HERMES_MODEL"] = model
            # Persist the primary server type so the gateway can pre-load the model
            # with a sufficient context window before the first chat turn.
            _primary_server_type = ""
            for _srv in (body.get("servers") or []):
                if (_srv.get("endpoint") or "").rstrip("/") == endpoint.rstrip("/"):
                    _primary_server_type = _srv.get("type") or ""
                    break
            if _primary_server_type:
                _cfg["HERMES_SERVER_TYPE"] = _primary_server_type
                os.environ["HERMES_SERVER_TYPE"] = _primary_server_type
            # Persist the chosen execution mode so restarts use the correct executor.
            # Only written if not already forced via env var (k8s deployments set
            # HERMES_RUNTIME_MODE explicitly and must not be overridden by setup).
            if not os.getenv("HERMES_RUNTIME_MODE"):
                if exec_env == "k8s":
                    _cfg["HERMES_RUNTIME_MODE"] = "kubernetes"
                elif exec_env == "openshell":
                    _cfg["HERMES_RUNTIME_MODE"] = "openshell"
                elif exec_env == "docker":
                    _cfg["HERMES_RUNTIME_MODE"] = "docker"
                else:
                    _cfg["HERMES_RUNTIME_MODE"] = "local"
            # For k8s-kubeconfig mode, also write the kubeconfig to a file so
            # k8s_clients() can pick it up via load_kube_config() on the next start.
            _kube_raw = kubeconfig if exec_env == "k8s" and kubeconfig else ""
            if _kube_raw and not os.getenv("KUBECONFIG"):
                _kube_path = _hermes_home / "kubeconfig.yaml"
                try:
                    _kube_path.write_text(_kube_raw, encoding="utf-8")
                    _kube_path.chmod(0o600)
                    _cfg["KUBECONFIG"] = str(_kube_path)
                    logger.info("setup: wrote kubeconfig to %s", _kube_path)
                except Exception as _kube_err:
                    logger.warning("setup: could not write kubeconfig: %s", _kube_err)
            _config_path.write_text(_yaml.dump(_cfg, default_flow_style=False, allow_unicode=True))
            logger.info("setup: wrote HERMES_MODEL=%s HERMES_RUNTIME_MODE=%s to config.yaml",
                        model, _cfg.get("HERMES_RUNTIME_MODE", "(env)"))
        except Exception as _cfg_err:
            logger.warning("setup: could not write model to config.yaml: %s", _cfg_err)

        auth_db.ensure_user_settings(user_id)
        auth_db.update_user_settings(user_id, default_model=model, default_soul=agent_type)
        auth_db.set_platform_feature_flag("exec_env", exec_env)
        auth_db.set_platform_feature_flag("k8s_namespace", k8s_ns)
        if kubeconfig:
            auth_db.set_platform_feature_flag("k8s_kubeconfig", kubeconfig)
        auth_db.mark_setup_completed()

        # Update admin account credentials if the user customised them.
        # Skip fields that are already set to the submitted value to avoid
        # spurious UNIQUE constraint errors when re-running setup unchanged.
        setup_email    = (body.get("setup_email") or "").strip()
        setup_username = (body.get("setup_username") or "").strip()
        setup_password = (body.get("setup_password") or "").strip()
        if setup_email or setup_username or setup_password:
            from gateway.auth.password import hash_password as _hp
            updates = {}
            if setup_email    and setup_email    != primary_admin.get("email", ""):
                updates["email"] = setup_email
            if setup_username and setup_username != primary_admin.get("username", ""):
                updates["username"] = setup_username
            if setup_password:
                updates["password_hash"] = _hp(setup_password)
            if updates:
                try:
                    auth_db.update_user(user_id, **updates)
                    logger.info("setup: updated admin credentials for %s", user_id)
                except Exception as _upd_err:
                    _err_str = str(_upd_err).lower()
                    if "unique" in _err_str and "username" in _err_str:
                        conflicting = auth_db.get_user_by_username(setup_username)
                        detail = (
                            f"Username '{setup_username}' is already used by account "
                            f"'{conflicting['email']}'. Remove that account in Admin → Users first, "
                            f"or choose a different username."
                        ) if conflicting else f"Username '{setup_username}' is already taken."
                        return web.json_response(
                            {"error": "username_taken", "detail": detail}, status=409,
                        )
                    if "unique" in _err_str and "email" in _err_str:
                        return web.json_response(
                            {"error": "email_taken",
                             "detail": f"Email '{setup_email}' is already registered to another account."},
                            status=409,
                        )
                    raise

        # Assign admin and any additional users to the default routing profile
        from gateway.auth.password import hash_password as _hp
        default_policy = next(
            (p for p in auth_db.list_policies() if p.get("name") == "default"),
            None,
        ) or (auth_db.list_policies() or [None])[0]
        if default_policy:
            auth_db.assign_user_policy(user_id, default_policy["id"])

        additional_users = body.get("additional_users") or []
        for u in additional_users:
            uname = (u.get("username") or "").strip()
            uemail = (u.get("email") or "").strip()
            upw = (u.get("password") or "").strip()
            urole = (u.get("role") or "user").strip()
            if not (uname and uemail and upw):
                continue
            try:
                new_user = auth_db.create_user(
                    email=uemail, username=uname,
                    password_hash=_hp(upw), role=urole,
                )
                if default_policy:
                    auth_db.assign_user_policy(new_user["id"], default_policy["id"])
                logger.info("setup: created user %s (%s)", uname, urole)
            except Exception as _ue:
                logger.warning("setup: could not create user %s: %s", uname, _ue)

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

        # Issue a session for the admin so the frontend navigates straight to the
        # main app.  This is best-effort: if token issuance fails for any reason
        # (missing secret, DB write error, etc.) we still return a successful
        # setup response and tell the frontend to fall back to the login page.
        autologin_ok = False
        access_token = raw_refresh = rtk_hash = None
        try:
            admin_email = primary_admin.get("email", "")
            admin_role  = primary_admin.get("role", "admin")
            # Re-fetch in case credentials were just updated above
            updated_user = auth_db.get_user_by_id(user_id)
            if updated_user:
                admin_email = updated_user.get("email", admin_email)
                admin_role  = updated_user.get("role", admin_role)
            access_token          = issue_access_token(user_id, admin_email, admin_role)
            raw_refresh, rtk_hash = issue_refresh_token()
            auth_db.store_refresh_token(
                user_id, rtk_hash,
                expires_at=int(time.time()) + REFRESH_TOKEN_TTL,
                ip=request.remote,
                ua=request.headers.get("User-Agent"),
            )
            auth_db.write_audit_log(user_id, "setup_autologin", ip_address=request.remote)
            autologin_ok = True
        except Exception as _tok_err:
            logger.warning("setup: auto-login token issuance failed (%s) — user will be sent to /login", _tok_err)

        payload: dict = {"ok": True}
        if not autologin_ok:
            payload["needs_login"] = True
        if warning:
            payload["warning"] = warning

        resp = web.json_response(payload)
        if autologin_ok:
            set_auth_cookies(resp, access_token, raw_refresh)
        return resp
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
_CONNECT_JSON = pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".logos")) / "connect.json"


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


async def handle_setup_compare_cancel_server(request: web.Request) -> web.Response:
    """Mark a server endpoint as cancelled for an in-progress benchmark.

    Also unloads the currently-loaded model on that endpoint (if any) so
    VRAM is freed immediately rather than waiting for the benchmark loop
    to reach the next iteration.

    Body: {"bench_id": "...", "endpoint": "http://..."}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    bench_id = body.get("bench_id", "")
    endpoint = (body.get("endpoint") or "").rstrip("/")
    if bench_id in _bench_cancels:
        _bench_cancels[bench_id].add(endpoint)

    # Best-effort unload of whatever model is currently loaded on this endpoint
    active = (_bench_active.get(bench_id) or {}).get(endpoint)
    if active:
        asyncio.ensure_future(_unload_model(
            active["base_url"], active["model"],
            active["server_type"], active.get("api_key", ""),
        ))

    return web.json_response({"ok": True})


# ── OpenShell / sandbox auto-detect and auto-setup ─────────────────────────

import platform as _platform
import shutil as _shutil
import subprocess as _subprocess
import sys as _sys
import urllib.request as _urllib_request


_OPENSHELL_IMAGE  = os.getenv("LOGOS_OPENSHELL_IMAGE", "logos-hermes-sandbox")
_HERMES_BIN_DIR   = pathlib.Path(os.getenv("LOGOS_HOME") or os.getenv("HERMES_HOME") or str(pathlib.Path.home() / ".logos")) / "bin"

# GitHub release asset naming (actual): openshell-{arch}-{os-triple}.tar.gz
# e.g. openshell-x86_64-unknown-linux-musl.tar.gz, openshell-aarch64-apple-darwin.tar.gz
_OS_MAP   = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}
_ARCH_MAP = {"AMD64": "x86_64", "x86_64": "x86_64", "ARM64": "aarch64", "aarch64": "aarch64"}
_OPENSHELL_RELEASES = "https://github.com/NVIDIA/OpenShell/releases/latest/download"
# Maps (platform.system(), arch) → GitHub release asset suffix (after "openshell-")
_OPENSHELL_ASSET_MAP = {
    ("Linux",  "x86_64"):  "x86_64-unknown-linux-musl",
    ("Linux",  "aarch64"): "aarch64-unknown-linux-musl",
    ("Darwin", "aarch64"): "aarch64-apple-darwin",
}


def _k3s_status() -> dict:
    """Return {installed: bool, running: bool} describing k3s state on this host."""
    installed = bool(_shutil.which("k3s"))
    running = False
    if installed:
        try:
            r = _subprocess.run(
                ["k3s", "kubectl", "get", "nodes"],
                capture_output=True, timeout=10,
            )
            running = r.returncode == 0
        except Exception:
            pass
    return {"installed": installed, "running": running}


def _openshell_exe() -> str:
    """Return path to the openshell binary, checking PATH and ~/.hermes/bin."""
    found = _shutil.which("openshell")
    if found:
        logger.debug("openshell_exe: found on PATH at %s", found)
        return found
    local = _HERMES_BIN_DIR / ("openshell.exe" if _sys.platform == "win32" else "openshell")
    if local.exists():
        logger.debug("openshell_exe: found in hermes bin at %s", local)
        return str(local)
    logger.debug("openshell_exe: not found (checked PATH and %s)", _HERMES_BIN_DIR)
    return ""


def _docker_running() -> dict:
    """
    Return {running, installed, desktop_path} describing Docker state.
    Tries the Docker socket/pipe first; falls back to ``docker info`` subprocess.
    """
    logger.debug("docker_running: probing Docker daemon (platform=%s)", _sys.platform)

    # Windows: Docker Desktop uses a named pipe, not a Unix socket.
    # pathlib.Path.exists() does NOT work for \\.\pipe\ device-namespace paths —
    # use ctypes CreateFileW to actually probe the pipe.  Set restype=c_void_p so
    # the full pointer-sized HANDLE is returned (default c_int truncates on 64-bit).
    # Also check ERROR_PIPE_BUSY (231): pipe exists but all slots occupied = running.
    if _sys.platform == "win32":
        # 1. Try to open the named pipe directly (most reliable)
        try:
            import ctypes as _ct
            _k32 = _ct.windll.kernel32
            _k32.CreateFileW.restype = _ct.c_void_p   # HANDLE is pointer-sized
            _INVALID = 2 ** (8 * _ct.sizeof(_ct.c_void_p)) - 1  # all-bits-set
            _h = _k32.CreateFileW(
                r"\\.\pipe\docker_engine",
                0x80000000,   # GENERIC_READ
                0, None, 3,   # OPEN_EXISTING
                0, None,
            )
            _last_err = _k32.GetLastError()
            logger.debug("docker_running: named pipe probe handle=%r GetLastError=%d", _h, _last_err)
            if _h is not None and _h not in (0, _INVALID):
                _k32.CloseHandle(_h)
                logger.info("docker_running: Docker detected via named pipe \\.\pipe\docker_engine")
                return {"running": True, "installed": True, "desktop_path": ""}
            if _last_err == 231:  # ERROR_PIPE_BUSY — pipe exists but all slots occupied
                logger.info("docker_running: Docker detected via ERROR_PIPE_BUSY on named pipe")
                return {"running": True, "installed": True, "desktop_path": ""}
            logger.debug("docker_running: named pipe not available (err=%d)", _last_err)
        except Exception as _e:
            logger.warning("docker_running: named pipe check raised %s: %s", type(_e).__name__, _e)

        # 2. Check if Docker daemon process is actually running via tasklist
        try:
            _tl = _subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq com.docker.proxy.exe",
                 "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            logger.debug("docker_running: tasklist stdout=%r", _tl.stdout[:200])
            if "com.docker.proxy.exe" in _tl.stdout.lower():
                logger.info("docker_running: Docker detected via com.docker.proxy.exe process")
                return {"running": True, "installed": True, "desktop_path": ""}
            logger.debug("docker_running: com.docker.proxy.exe not found in tasklist")
        except Exception as _e:
            logger.warning("docker_running: tasklist check raised %s: %s", type(_e).__name__, _e)

    # Try unix socket (Linux / macOS / Docker Desktop with socket proxy enabled)
    sock_paths = ["/var/run/docker.sock", "/run/docker.sock"]
    for sp in sock_paths:
        if pathlib.Path(sp).exists():
            logger.info("docker_running: Docker detected via unix socket %s", sp)
            return {"running": True, "installed": True, "desktop_path": ""}

    # Resolve docker executable — PATH first, then well-known Windows install locations
    docker_exe = _shutil.which("docker")
    logger.debug("docker_running: docker on PATH: %r", docker_exe)
    if not docker_exe and _sys.platform == "win32":
        _pf  = os.environ.get("PROGRAMFILES",  r"C:\Program Files")
        _pf86= os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        _lad = os.environ.get("LOCALAPPDATA",  "")
        _win_candidates = [
            # System-wide install (most common for Docker Desktop)
            pathlib.Path(_pf)   / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
            # Per-user install (Docker Desktop 4.x non-admin)
            pathlib.Path(_lad)  / "Programs" / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
            # Alternate system path
            pathlib.Path(_pf86) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
            # Standalone Docker CLI (without Desktop)
            pathlib.Path(_pf)   / "Docker" / "cli-plugins" / "docker.exe",
        ]
        for _c in _win_candidates:
            logger.debug("docker_running: checking candidate %s (exists=%s)", _c, _c.exists())
            if _c.exists():
                docker_exe = str(_c)
                logger.info("docker_running: found docker.exe at %s", docker_exe)
                break

    if docker_exe:
        try:
            r = _subprocess.run(
                [docker_exe, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=5,
            )
            logger.debug("docker_running: `docker info` rc=%d stdout=%r stderr=%r",
                         r.returncode, r.stdout[:100], r.stderr[:200])
            if r.returncode == 0:
                logger.info("docker_running: Docker daemon running (version=%s)", r.stdout.strip())
                return {"running": True, "installed": True, "desktop_path": ""}
            logger.warning("docker_running: `docker info` failed rc=%d stderr=%s",
                           r.returncode, r.stderr.strip()[:300])
        except Exception as _e:
            logger.warning("docker_running: `docker info` raised %s: %s", type(_e).__name__, _e)

        # Docker installed but daemon not running — try to find Desktop executable
        desktop = ""
        if _sys.platform == "win32":
            lad = os.environ.get("LOCALAPPDATA", "")
            candidate = pathlib.Path(lad) / "Docker" / "Docker Desktop.exe"
            logger.debug("docker_running: Desktop exe candidate %s (exists=%s)",
                         candidate, candidate.exists())
            if candidate.exists():
                desktop = str(candidate)
        elif _sys.platform == "darwin":
            candidate = pathlib.Path("/Applications/Docker.app/Contents/MacOS/Docker")
            if candidate.exists():
                desktop = str(candidate)
        logger.info("docker_running: Docker installed but not running (desktop_path=%r)", desktop)
        return {"running": False, "installed": True, "desktop_path": desktop}

    # Nothing found
    logger.warning("docker_running: Docker not found — no socket, no pipe, no docker executable")
    return {"running": False, "installed": False, "desktop_path": ""}


def _sandbox_image_exists() -> bool:
    """Return True if the logos-hermes-sandbox Docker image is present locally."""
    docker_exe = _shutil.which("docker")
    if not docker_exe:
        logger.debug("sandbox_image_exists: docker not on PATH, skipping image check")
        return False
    try:
        r = _subprocess.run(
            [docker_exe, "image", "inspect", _OPENSHELL_IMAGE, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            logger.debug("sandbox_image_exists: image %s found", _OPENSHELL_IMAGE)
            return True
        logger.debug("sandbox_image_exists: image %s not found (rc=%d)", _OPENSHELL_IMAGE, r.returncode)
        return False
    except Exception as _e:
        logger.warning("sandbox_image_exists: raised %s: %s", type(_e).__name__, _e)
        return False


async def handle_setup_launch_docker(request: web.Request) -> web.Response:
    """
    POST /api/setup/launch-docker

    Attempt to launch Docker Desktop on the host machine.
    Returns {ok, error} — non-blocking, the client should poll env-probe to
    detect when the daemon comes up.
    """
    info = _docker_running()
    desktop = info.get("desktop_path", "")
    if not desktop:
        return web.json_response({"ok": False, "error": "Docker Desktop executable not found"})
    try:
        if _sys.platform == "win32":
            _subprocess.Popen([desktop], creationflags=getattr(_subprocess, "DETACHED_PROCESS", 0x00000008))
        else:
            _subprocess.Popen([desktop])
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)})


async def handle_setup_env_probe(request: web.Request) -> web.Response:
    """
    GET /api/setup/env-probe

    Returns the current sandbox readiness state so the setup wizard can
    decide whether to recommend OpenShell or fall back to in-process.

    Response shape:
      {
        platform:          "windows" | "linux" | "darwin",
        docker_running:    bool,
        docker_installed:  bool,
        docker_desktop_path: str,   # non-empty = Docker Desktop found but not running
        openshell_present: bool,
        openshell_path:    str,
        image_present:     bool,
        sandbox_ready:     bool,    # true when all three are green
      }
    """
    loop = asyncio.get_event_loop()
    docker_info   = await loop.run_in_executor(None, _docker_running)
    openshell_exe = await loop.run_in_executor(None, _openshell_exe)
    k3s_info      = await loop.run_in_executor(None, _k3s_status)
    image_present = False
    if docker_info["running"]:
        image_present = await loop.run_in_executor(None, _sandbox_image_exists)

    # OpenShell only ships Linux and macOS binaries — not available on Windows
    openshell_supported = _platform.system() != "Windows"

    # Docker sandbox (container-only, no OpenShell) is available on any platform
    # where Docker is running and the sandbox image exists.
    docker_sandbox_ready = docker_info["running"] and image_present

    return web.json_response({
        "platform":              _OS_MAP.get(_platform.system(), _platform.system().lower()),
        "docker_running":        docker_info["running"],
        "docker_installed":      docker_info["installed"],
        "docker_desktop_path":   docker_info.get("desktop_path", ""),
        "openshell_present":     bool(openshell_exe),
        "openshell_path":        openshell_exe,
        "openshell_supported":   openshell_supported,
        "image_present":         image_present,
        "sandbox_ready":         docker_info["running"] and bool(openshell_exe) and image_present,
        "docker_sandbox_ready":  docker_sandbox_ready,
        "k3s_installed":         k3s_info["installed"],
        "k3s_running":           k3s_info["running"],
        # In-cluster detection: if KUBERNETES_SERVICE_HOST is set, we're a pod
        "in_cluster":            bool(os.environ.get("KUBERNETES_SERVICE_HOST")),
    })


async def handle_setup_k3s_install(request: web.Request) -> web.Response:
    """
    POST /api/setup/k3s-install   (SSE stream)

    Installs k3s on a bare Linux host, waits for it to become ready,
    creates the agent namespace, and returns the kubeconfig.

    Streams progress as SSE events:
      data: {"step": "k3s_install",      "status": "running"|"ok"|"error"|"skip", "msg": "..."}
      data: {"step": "k3s_wait",         "status": "running"|"ok"|"error",        "msg": "..."}
      data: {"step": "namespace_create", "status": "running"|"ok"|"error",        "msg": "..."}
      data: {"step": "done",             "status": "ok"|"error", "kubeconfig": "..."}
    """
    resp = web.StreamResponse(headers={
        "Content-Type":     "text/event-stream",
        "Cache-Control":    "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    async def emit(step: str, status: str, msg: str = "", **extra):
        payload = json.dumps({"step": step, "status": status, "msg": msg, **extra})
        await resp.write(f"data: {payload}\n\n".encode())

    loop = asyncio.get_event_loop()

    # ── Gate: Linux only ──
    if _platform.system() != "Linux":
        await emit("k3s_install", "error", "k3s is only supported on Linux")
        await emit("done", "error")
        return resp

    # ── Step 1: Install k3s (skip if already running) ──
    k3s = await loop.run_in_executor(None, _k3s_status)
    if k3s["running"]:
        await emit("k3s_install", "skip", "k3s is already running")
    elif k3s["installed"]:
        # Installed but not running — try to start it
        await emit("k3s_install", "running", "k3s is installed but not running, starting service...")
        def _start_k3s():
            try:
                r = _subprocess.run(
                    ["sudo", "systemctl", "start", "k3s"],
                    capture_output=True, text=True, timeout=30,
                )
                return r.returncode == 0, r.stderr.strip()
            except Exception as e:
                return False, str(e)
        ok, err = await loop.run_in_executor(None, _start_k3s)
        if ok:
            await emit("k3s_install", "ok", "k3s service started")
        else:
            await emit("k3s_install", "error", f"Failed to start k3s: {err}")
            await emit("done", "error")
            return resp
    else:
        await emit("k3s_install", "running", "Installing k3s (this may take a minute)...")
        def _install_k3s():
            try:
                r = _subprocess.run(
                    ["sh", "-c",
                     "curl -sfL https://get.k3s.io | "
                     "INSTALL_K3S_EXEC='server --disable=traefik --disable=servicelb "
                     "--write-kubeconfig-mode=644' sh -"],
                    capture_output=True, text=True, timeout=300,
                )
                return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
            except Exception as e:
                return False, "", str(e)
        ok, stdout, stderr = await loop.run_in_executor(None, _install_k3s)
        if ok:
            await emit("k3s_install", "ok", "k3s installed successfully")
        else:
            await emit("k3s_install", "error", f"Installation failed: {stderr[:300]}")
            await emit("done", "error")
            return resp

    # ── Step 2: Wait for k3s to become ready ──
    await emit("k3s_wait", "running", "Waiting for k3s to be ready...")
    _kube_path = "/etc/rancher/k3s/k3s.yaml"
    def _wait_k3s_ready():
        for _ in range(60):  # up to ~120s
            if not os.path.exists(_kube_path):
                time.sleep(2)
                continue
            try:
                r = _subprocess.run(
                    ["k3s", "kubectl", "get", "nodes"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and "Ready" in r.stdout:
                    return True, ""
            except Exception:
                pass
            time.sleep(2)
        return False, "Timed out waiting for k3s to become ready"
    ready, err = await loop.run_in_executor(None, _wait_k3s_ready)
    if ready:
        await emit("k3s_wait", "ok", "k3s cluster is ready")
    else:
        await emit("k3s_wait", "error", err)
        await emit("done", "error")
        return resp

    # ── Step 3: Create hermes namespace ──
    await emit("namespace_create", "running", "Creating hermes namespace...")
    def _create_namespace():
        try:
            r = _subprocess.run(
                ["k3s", "kubectl", "create", "namespace", "hermes",
                 "--dry-run=client", "-o", "yaml"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return False, r.stderr.strip()
            r2 = _subprocess.run(
                ["k3s", "kubectl", "apply", "-f", "-"],
                input=r.stdout, capture_output=True, text=True, timeout=10,
            )
            return r2.returncode == 0, r2.stderr.strip()
        except Exception as e:
            return False, str(e)
    ns_ok, ns_err = await loop.run_in_executor(None, _create_namespace)
    if ns_ok:
        await emit("namespace_create", "ok", "hermes namespace ready")
    else:
        await emit("namespace_create", "error", f"Failed to create namespace: {ns_err}")
        await emit("done", "error")
        return resp

    # ── Step 4: Read kubeconfig ──
    try:
        kubeconfig = pathlib.Path(_kube_path).read_text(encoding="utf-8")
    except Exception as e:
        await emit("done", "error", msg=f"Could not read kubeconfig: {e}")
        return resp

    await emit("done", "ok", kubeconfig=kubeconfig)
    return resp


async def handle_setup_sandbox_setup(request: web.Request) -> web.Response:
    """
    POST /api/setup/sandbox-setup   (SSE stream)

    Attempts to automatically:
      1. Install the openshell CLI (tries uv, pip, then binary download)
      2. Build the logos-hermes-sandbox Docker image

    Streams progress as SSE events:
      data: {"step": "openshell_install", "status": "running"|"ok"|"error", "msg": "..."}
      data: {"step": "image_build",       "status": "running"|"ok"|"error", "msg": "..."}
      data: {"step": "done",              "status": "ok"|"error",           "sandbox_ready": bool}
    """
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    async def emit(step: str, status: str, msg: str = "", **extra):
        payload = json.dumps({"step": step, "status": status, "msg": msg, **extra})
        await resp.write(f"data: {payload}\n\n".encode())

    loop = asyncio.get_event_loop()

    # ── Platform gate: OpenShell only supports Linux and macOS ───────────
    # On Windows (or other unsupported platforms), skip OpenShell and use
    # Docker-only sandbox mode instead: just build the image.
    if _platform.system() == "Windows":
        await emit("openshell_install", "skip",
                    "OpenShell is not available on Windows. "
                    "Setting up Docker container sandbox instead (reduced isolation).")

        # Build the Docker-only sandbox image
        image_ok = await loop.run_in_executor(None, _sandbox_image_exists)
        if image_ok:
            await emit("image_build", "ok", f"Image '{_OPENSHELL_IMAGE}' already present")
        else:
            await emit("image_build", "running", f"Building '{_OPENSHELL_IMAGE}' image (this takes ~2 minutes)…")
            build_ok, build_err = await loop.run_in_executor(
                None, lambda: _build_sandbox_image(dockerfile_name="docker/Dockerfile.docker-sandbox"))
            if build_ok:
                await emit("image_build", "ok", "Image built successfully")
            else:
                await emit("image_build", "error", f"Build failed: {build_err}")
                await emit("done", "error", sandbox_ready=False)
                return resp

        await emit("done", "ok", sandbox_ready=False, docker_sandbox_ready=True)
        return resp

    # ── Step 1: install openshell if missing ──────────────────────────────
    openshell_path = await loop.run_in_executor(None, _openshell_exe)
    if openshell_path:
        await emit("openshell_install", "ok", f"openshell found at {openshell_path}")
    else:
        await emit("openshell_install", "running", "Installing openshell CLI…")
        installed_path, err = await loop.run_in_executor(None, _install_openshell)
        if installed_path:
            await emit("openshell_install", "ok", f"Installed to {installed_path}")
        else:
            await emit("openshell_install", "error", f"Could not install openshell: {err}")
            await emit("done", "error", sandbox_ready=False)
            return resp

    # ── Step 2: build sandbox image if missing ────────────────────────────
    image_ok = await loop.run_in_executor(None, _sandbox_image_exists)
    if image_ok:
        await emit("image_build", "ok", f"Image '{_OPENSHELL_IMAGE}' already present")
    else:
        await emit("image_build", "running", f"Building '{_OPENSHELL_IMAGE}' image (this takes ~2 minutes)…")
        build_ok, build_err = await loop.run_in_executor(None, _build_sandbox_image)
        if build_ok:
            await emit("image_build", "ok", "Image built successfully")
        else:
            await emit("image_build", "error", f"Build failed: {build_err}")
            await emit("done", "error", sandbox_ready=False)
            return resp

    await emit("done", "ok", sandbox_ready=True)
    return resp


def _install_openshell() -> tuple[str, str]:
    """
    Try to install the openshell CLI.  Returns (path, "") on success or
    ("", error_message) on failure.

    Tries in order:
      1. uv tool install openshell  (isolated, preferred)
      2. pip install openshell       (system / venv)
      3. Download binary from GitHub releases
    """
    _HERMES_BIN_DIR.mkdir(parents=True, exist_ok=True)

    # 1. uv
    uv_exe = _shutil.which("uv")
    if uv_exe:
        try:
            r = _subprocess.run(
                [uv_exe, "tool", "install", "-U", "openshell"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                found = _shutil.which("openshell")
                if found:
                    return found, ""
        except Exception:
            pass

    # 2. pip (use the Python that's running Logos)
    pip_python = _sys.executable if not getattr(_sys, "frozen", False) else None
    if pip_python:
        try:
            r = _subprocess.run(
                [pip_python, "-m", "pip", "install", "--quiet", "--upgrade", "openshell"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                found = _shutil.which("openshell")
                if found:
                    return found, ""
        except Exception:
            pass

    # 3. Download binary from GitHub releases (tar.gz archive)
    arch_raw = _platform.machine()
    arch     = _ARCH_MAP.get(arch_raw, "x86_64")
    asset_key = (_platform.system(), arch)
    asset_suffix = _OPENSHELL_ASSET_MAP.get(asset_key)
    if not asset_suffix:
        return "", f"No OpenShell binary available for {_platform.system()}/{arch_raw}"

    archive_name = f"openshell-{asset_suffix}.tar.gz"
    url = f"{_OPENSHELL_RELEASES}/{archive_name}"
    dest = _HERMES_BIN_DIR / "openshell"

    try:
        import tarfile as _tarfile
        import tempfile as _tempfile
        logger.info("Downloading openshell from %s", url)
        with _tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            _urllib_request.urlretrieve(url, tmp.name)
            with _tarfile.open(tmp.name, "r:gz") as tf:
                # Find the openshell binary inside the archive
                for member in tf.getmembers():
                    basename = pathlib.Path(member.name).name
                    if basename == "openshell" and member.isfile():
                        member.name = "openshell"  # flatten path
                        tf.extract(member, str(_HERMES_BIN_DIR))
                        break
                else:
                    return "", f"Could not find 'openshell' binary inside {archive_name}"
            os.unlink(tmp.name)
        dest.chmod(0o755)
        # Add ~/.hermes/bin to PATH for this process so _openshell_exe finds it
        os.environ["PATH"] = str(_HERMES_BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
        return str(dest), ""
    except Exception as exc:
        return "", str(exc)


def _build_sandbox_image(dockerfile_name: str = "docker/Dockerfile.openshell-sandbox") -> tuple[bool, str]:
    """
    Build the logos-hermes-sandbox Docker image.

    Args:
        dockerfile_name: Which Dockerfile to use.  "docker/Dockerfile.openshell-sandbox"
            for full OpenShell mode, "docker/Dockerfile.docker-sandbox" for Docker-only.

    Looks for the Dockerfile relative to the package root (works both in
    development and inside a frozen .exe where files are extracted to a
    temp directory).
    """
    docker_exe = _shutil.which("docker")
    if not docker_exe:
        return False, "docker not found on PATH"

    # Find Dockerfile — check package root, then sys._MEIPASS (PyInstaller temp)
    roots = [pathlib.Path(__file__).parent.parent]
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        roots.insert(0, pathlib.Path(meipass))

    dockerfile = None
    for root in roots:
        candidate = root / dockerfile_name
        if candidate.exists():
            dockerfile = candidate
            break

    if dockerfile is None:
        return False, f"{dockerfile_name} not found in package"

    # When running from a frozen .exe, _MEIPASS is a temp directory that persists
    # only while the process is alive — but the Docker daemon is a separate process
    # that runs the build asynchronously.  Copy the Dockerfile to a stable location
    # inside ~/.hermes so Docker can always find it.
    stable_dir = _HERMES_BIN_DIR.parent / "openshell-build"
    stable_dir.mkdir(parents=True, exist_ok=True)
    stable_dockerfile = stable_dir / "Dockerfile"
    try:
        import shutil as _shutil2
        _shutil2.copy2(str(dockerfile), str(stable_dockerfile))
    except Exception as exc:
        return False, f"Could not stage Dockerfile: {exc}"

    try:
        r = _subprocess.run(
            [docker_exe, "build",
             "-f", str(stable_dockerfile),
             "-t", _OPENSHELL_IMAGE,
             str(stable_dir)],
            capture_output=True, text=True, timeout=600,   # 10 min for first build
        )
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "unknown error").strip()[-500:]
    except Exception as exc:
        return False, str(exc)
