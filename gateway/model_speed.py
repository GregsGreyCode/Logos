"""Measure generation speed (tokens/second) of an OpenAI-compatible server.

The one piece of the v1 setup-wizard benchmark worth keeping: stream a short
chat completion from ``<base_url>/chat/completions`` and time it. No scoring,
no eval prompts, no model loading or unloading.

    python -m gateway.model_speed http://localhost:1234/v1 qwen3-8b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Optional

import aiohttp

DEFAULT_PROMPT = "Write a short paragraph about the sea."


@dataclass
class SpeedResult:
    tokens: int               # completion tokens (server-reported, else chunk count)
    ttft_s: float             # time to first token
    total_s: float            # wall time for the whole request
    tokens_per_second: float  # tokens / time spent generating (after first token)
    exact: bool               # True when `tokens` came from the server's usage block


def tokens_per_second(tokens: int, ttft_s: float, total_s: float) -> float:
    """Tokens divided by generation time; 0.0 when nothing was generated.

    The first token arrives at ``ttft_s``, so the remaining ``tokens - 1``
    were produced in ``total_s - ttft_s``. With a single token (or a server
    that sent everything in one burst) fall back to tokens / total.
    """
    if tokens <= 0 or total_s <= 0:
        return 0.0
    gen_s = total_s - ttft_s
    if tokens > 1 and gen_s > 0:
        return (tokens - 1) / gen_s
    return tokens / total_s


async def measure_speed(
    base_url: str,
    model: str,
    api_key: str = "",
    *,
    prompt: str = DEFAULT_PROMPT,
    max_tokens: int = 128,
    timeout_s: float = 60.0,
    session: Optional[aiohttp.ClientSession] = None,
) -> SpeedResult:
    """Stream one completion and return its timing.

    ``base_url`` is the OpenAI-compatible root including ``/v1``. Raises
    ``RuntimeError`` on a non-200 answer or when no tokens come back.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    own_session = session is None
    session = session or aiohttp.ClientSession()
    chunks = 0
    reported: Optional[int] = None
    ttft: Optional[float] = None
    last_token_at = 0.0
    t0 = time.perf_counter()
    try:
        async with session.post(
            url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            async for raw in resp.content:  # StreamReader iterates line by line
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                usage = event.get("usage") or {}
                if isinstance(usage.get("completion_tokens"), int):
                    reported = usage["completion_tokens"]
                choices = event.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                # Thinking models stream reasoning first; it is generation too.
                if delta.get("content") or delta.get("reasoning_content"):
                    last_token_at = time.perf_counter() - t0
                    if ttft is None:
                        ttft = last_token_at
                    chunks += 1
    finally:
        if own_session:
            await session.close()

    if chunks == 0:
        raise RuntimeError("the server returned no tokens")
    tokens = reported if reported else chunks
    return SpeedResult(
        tokens=tokens,
        ttft_s=round(ttft or 0.0, 3),
        total_s=round(last_token_at, 3),
        tokens_per_second=round(tokens_per_second(tokens, ttft or 0.0, last_token_at), 2),
        exact=bool(reported),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", help="OpenAI-compatible root, e.g. http://localhost:1234/v1")
    parser.add_argument("model")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--max-tokens", type=int, default=128)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(measure_speed(
            args.base_url, args.model, args.api_key, max_tokens=args.max_tokens,
        ))
    except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
