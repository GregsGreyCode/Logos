#!/usr/bin/env python3
"""Standalone diagnostic script for setup wizard model server functions.

Tests _probe_server and _stream_chat independently of the HTTP layer so you
can confirm the backend logic works before touching the UI.

Usage:
    python scripts/test_model_server.py
    python scripts/test_model_server.py --url http://192.168.1.x:1234/v1 --model qwen3-8b
    python scripts/test_model_server.py --url http://localhost:11434/v1 --ollama

Options:
    --url URL       Full /v1 endpoint to test (default: http://localhost:1234/v1)
    --model MODEL   Model ID to use (default: auto-detect from server)
    --api-key KEY   Bearer token if server requires auth (default: "ollama")
    --ollama        Probe Ollama instead of LM Studio (uses port 11434)
    --timeout N     Stream timeout in seconds (default: 90)
    --verbose       Print raw SSE lines
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp


# ── Colours ────────────────────────────────────────────────────────────────────

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1",  t)
DIM    = lambda t: _c("2",  t)


# ── Probe (mirrors setup_handlers._probe_server) ───────────────────────────────

async def probe(session: aiohttp.ClientSession, base_url: str, api_key: str | None = None) -> dict:
    base = base_url.rstrip("/").removesuffix("/v1")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = aiohttp.ClientTimeout(total=5)

    # Step 1: Ollama fingerprint
    try:
        async with session.get(f"{base}/api/version", timeout=timeout) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                if "version" in data:
                    models = []
                    try:
                        async with session.get(f"{base}/api/tags", timeout=timeout) as tr:
                            td = await tr.json(content_type=None)
                            models = [m["name"] for m in td.get("models", [])]
                    except Exception:
                        pass
                    return {"type": "ollama", "base": base, "status": "up", "models": models}
    except Exception:
        pass

    # Step 2: OpenAI-compat (LM Studio)
    try:
        async with session.get(f"{base}/v1/models", headers=headers, timeout=timeout) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                models = [m["id"] for m in data.get("data", [])]
                return {"type": "lmstudio", "base": base, "status": "up", "models": models}
            if r.status == 401:
                return {"type": "lmstudio", "base": base, "status": "auth_required", "models": []}
    except Exception:
        pass

    return {"type": "unknown", "base": base, "status": "down", "models": []}


# ── Stream chat (mirrors setup_handlers._stream_chat) ─────────────────────────

async def stream_chat(
    session: aiohttp.ClientSession,
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int = 80,
    timeout_s: int = 90,
    verbose: bool = False,
) -> tuple[str, float, float, int]:
    """Return (text, ttft_s, total_s, token_count)."""
    t0 = time.time()
    ttft: float | None = None
    text = ""
    tok = 0

    async with session.post(
        f"{endpoint}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "max_tokens": max_tokens,
        },
        timeout=aiohttp.ClientTimeout(total=timeout_s),
    ) as r:
        if r.status != 200:
            body = await r.text()
            raise RuntimeError(f"HTTP {r.status}: {body[:300]}")

        async for raw in r.content:
            line = raw.decode().strip()
            if verbose and line:
                print(DIM(f"  raw: {line}"))
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
                    tok += 1
            except Exception:
                pass

    return text, ttft or 0.0, time.time() - t0, tok


# ── Main ───────────────────────────────────────────────────────────────────────

BENCH_PROMPTS = [
    "Say hello in one sentence.",
    "What is 17 multiplied by 6? Answer with just the number.",
    "Name the capital of France in one word.",
]
QUALITY_PROMPT   = "What is 2+2? Reply with only the number."
QUALITY_EXPECTED = "4"


async def main(args: argparse.Namespace) -> int:
    url     = args.url.rstrip("/")
    api_key = args.api_key or "ollama"
    timeout = args.timeout
    verbose = args.verbose

    # Normalise: strip /v1 for probe, keep for stream
    base    = url.removesuffix("/v1")
    endpoint = base + "/v1"

    print(BOLD(f"\n=== Logos model server diagnostic ==="))
    print(f"Endpoint : {CYAN(endpoint)}")
    print(f"API key  : {DIM(api_key[:8] + '…' if len(api_key) > 8 else api_key)}")
    print(f"Timeout  : {timeout}s\n")

    connector = aiohttp.TCPConnector(limit=4)
    async with aiohttp.ClientSession(connector=connector) as session:

        # ── Step 1: Probe ─────────────────────────────────────────────────────
        print(BOLD("1/4  Probing server…"))
        t0 = time.time()
        info = await probe(session, base, api_key if args.api_key else None)
        elapsed = time.time() - t0

        status_colour = GREEN if info["status"] == "up" else (YELLOW if info["status"] == "auth_required" else RED)
        print(f"     type   : {info['type']}")
        print(f"     status : {status_colour(info['status'])}  ({elapsed*1000:.0f}ms)")
        print(f"     models : {', '.join(info['models']) or '(none)'}")

        if info["status"] == "down":
            print(RED("\nServer is not reachable. Check the URL and that the server is running."))
            return 1

        if info["status"] == "auth_required":
            print(YELLOW("\nServer requires an API key. Pass --api-key <key> and retry."))
            return 1

        # ── Step 2: Pick model ────────────────────────────────────────────────
        model = args.model
        if not model:
            if not info["models"]:
                print(RED("\nNo models available on the server. Load one first."))
                return 1
            model = info["models"][0]
            print(f"\n     Auto-selected model: {CYAN(model)}")
        else:
            print(f"\n     Using model: {CYAN(model)}")

        # LM Studio native load attempt
        if info["type"] == "lmstudio":
            print(BOLD("\n2/4  Requesting LM Studio native model load…"))
            try:
                async with session.post(
                    f"{base}/api/v1/models/load",
                    json={"identifier": model, "config": {"context_length": 4096}},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as lr:
                    print(f"     load API status: {lr.status}")
            except Exception as e:
                print(f"     load API: {DIM(str(e))} (non-fatal — will auto-load on first call)")
        else:
            print(BOLD("\n2/4  Skipping native load (Ollama auto-loads models)"))

        # ── Step 3: Phase 1 — live streaming ─────────────────────────────────
        print(BOLD(f"\n3/4  Phase 1/3 — live streaming response…"))
        print(f"     prompt: {DIM(repr(BENCH_PROMPTS[0]))}")
        t0 = time.time()
        hint_printed = False
        ttft_ms = None
        tok_count = 0
        text1 = ""

        # Show loading hint after 8s
        async def _hint():
            await asyncio.sleep(8)
            nonlocal hint_printed
            print(YELLOW("     (no tokens yet — model may be loading… please wait)"))
            hint_printed = True

        hint_task = asyncio.create_task(_hint())
        try:
            text1, ttft_s, total_s, tok_count = await stream_chat(
                session, endpoint, model, api_key, BENCH_PROMPTS[0],
                max_tokens=80, timeout_s=timeout, verbose=verbose,
            )
            hint_task.cancel()
            ttft_ms = round(ttft_s * 1000)
            tok_s_1 = round(tok_count / total_s) if total_s > 0 else 0
            print(f"     TTFT     : {GREEN(str(ttft_ms) + 'ms')}")
            print(f"     tokens   : {tok_count} in {total_s:.1f}s  ({GREEN(str(tok_s_1) + ' tok/s')})")
            print(f"     response : {DIM(repr(text1[:120]))}")
        except asyncio.TimeoutError:
            hint_task.cancel()
            print(RED(f"     TIMEOUT after {timeout}s — model may still be loading"))
            print(YELLOW("     Try increasing --timeout or loading the model manually first"))
            return 1
        except Exception as e:
            hint_task.cancel()
            print(RED(f"     ERROR: {e}"))
            return 1

        # ── Phase 2: throughput runs ──────────────────────────────────────────
        print(BOLD(f"\n     Phase 2/3 — throughput benchmark (2 more runs)…"))
        run_times = [total_s]
        run_toks  = [tok_count]
        for i, prompt in enumerate(BENCH_PROMPTS[1:], start=2):
            print(f"     Run {i}/{len(BENCH_PROMPTS)}: {DIM(repr(prompt))}")
            try:
                _, _, t, n = await stream_chat(
                    session, endpoint, model, api_key, prompt,
                    max_tokens=40, timeout_s=45, verbose=verbose,
                )
                run_times.append(t)
                run_toks.append(n)
                print(f"     → {n} tokens · {t:.1f}s · {round(n/t) if t > 0 else 0} tok/s")
            except Exception as e:
                print(YELLOW(f"     → failed: {e}"))

        avg_tok_s = round(sum(run_toks) / sum(run_times)) if sum(run_times) > 0 else 0
        avg_ms    = round(sum(run_times) / len(run_times) * 1000)
        if   avg_tok_s >= 30: speed_label, speed_c = "Fast",   GREEN
        elif avg_tok_s >= 15: speed_label, speed_c = "Good",   CYAN
        elif avg_tok_s >= 6:  speed_label, speed_c = "Usable", YELLOW
        else:                 speed_label, speed_c = "Slow",   RED
        print(f"\n     avg tok/s : {speed_c(str(avg_tok_s) + ' — ' + speed_label)}")
        print(f"     avg ms    : {avg_ms}ms per run")

        # ── Phase 3: quality check ────────────────────────────────────────────
        print(BOLD(f"\n     Phase 3/3 — quality check…"))
        print(f"     prompt: {DIM(repr(QUALITY_PROMPT))}")
        try:
            qtext, _, _, _ = await stream_chat(
                session, endpoint, model, api_key, QUALITY_PROMPT,
                max_tokens=10, timeout_s=30, verbose=verbose,
            )
            quality_pass = QUALITY_EXPECTED in qtext.strip()
            if quality_pass:
                print(f"     {GREEN('✓ PASS')} — model answered {DIM(repr(qtext.strip()))}")
            else:
                print(f"     {YELLOW('⚠ FAIL')} — expected '4', got {DIM(repr(qtext.strip()))}")
        except Exception as e:
            quality_pass = False
            print(YELLOW(f"     quality check error: {e}"))

        # ── Summary ───────────────────────────────────────────────────────────
        print(BOLD(f"\n4/4  Summary"))
        print(f"     model     : {CYAN(model)}")
        print(f"     server    : {info['type']} @ {endpoint}")
        print(f"     speed     : {speed_c(str(avg_tok_s) + ' tok/s — ' + speed_label)}")
        print(f"     TTFT      : {ttft_ms}ms")
        print(f"     reasoning : {GREEN('ok') if quality_pass else YELLOW('check failed')}")

        if not quality_pass:
            print(YELLOW("\n  ⚠ Reasoning check failed — this model may not follow instructions well."))
            print(YELLOW("    Consider a general-purpose model like Qwen3-8B or Llama-3.1-8B."))

        if avg_tok_s < 6:
            print(YELLOW("\n  ⚠ Speed is very low. Try a smaller or more quantised model (Q4_K_M)."))
        elif avg_tok_s >= 30 and quality_pass:
            print(GREEN("\n  ✓ You have headroom — could try a larger model for stronger reasoning."))

        print()
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Test a model server the same way the setup wizard does",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--url",     default="http://localhost:1234/v1",
                   help="Full /v1 endpoint (default: http://localhost:1234/v1)")
    p.add_argument("--model",   default=None,
                   help="Model ID to benchmark (default: auto-detect first available)")
    p.add_argument("--api-key", default=None, dest="api_key",
                   help="Bearer token for auth-protected servers")
    p.add_argument("--ollama",  action="store_true",
                   help="Shortcut: use http://localhost:11434/v1")
    p.add_argument("--timeout", type=int, default=90,
                   help="Streaming timeout per call in seconds (default: 90)")
    p.add_argument("--verbose", action="store_true",
                   help="Print raw SSE lines")
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.ollama:
        args.url = "http://localhost:11434/v1"
    sys.exit(asyncio.run(main(args)))
