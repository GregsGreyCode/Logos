"""Model metadata, context lengths, and token estimation utilities.

Pure utility functions with no AIAgent dependency. Used by ContextCompressor
and run_agent.py for pre-flight context checks.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

from core.constants import OPENROUTER_MODELS_URL

logger = logging.getLogger(__name__)

_model_metadata_cache: Dict[str, Dict[str, Any]] = {}
_model_metadata_cache_time: float = 0
_MODEL_CACHE_TTL = 3600

# Descending tiers for context length probing when the model is unknown.
# We start high and step down on context-length errors until one works.
CONTEXT_PROBE_TIERS = [
    2_000_000,
    1_000_000,
    512_000,
    200_000,
    128_000,
    64_000,
    32_000,
    16_000,
    8_000,
]

# Intentionally no hardcoded context-length table.
#
# Previously a dict of model-id → context-length lived here as a last-
# resort fallback. It was a constant source of silent bugs: every
# vendor release required a manual update; entries for sibling model
# families (Qwen 2.5 vs 3 vs 3.5) got copy-pasted with the wrong
# number and shipped; and the table's mere existence meant callers
# assumed a non-None return and stopped handling the "unknown" case.
#
# The cascade below (probe cache → setup benchmark → live /v1/models
# → OpenRouter) covers every case we can answer from authoritative
# sources. When it doesn't, `get_model_context_length` now returns
# None and logs a warning telling the user how to populate the
# benchmark. Callers that cannot tolerate None must pick their own
# conservative fallback explicitly, at the call site, with a log line
# that says so — not silently via a distant lookup table.


def fetch_model_metadata(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Fetch model metadata from OpenRouter (cached for 1 hour)."""
    global _model_metadata_cache, _model_metadata_cache_time

    if not force_refresh and _model_metadata_cache and (time.time() - _model_metadata_cache_time) < _MODEL_CACHE_TTL:
        return _model_metadata_cache

    try:
        response = requests.get(OPENROUTER_MODELS_URL, timeout=10)
        response.raise_for_status()
        data = response.json()

        cache = {}
        for model in data.get("data", []):
            model_id = model.get("id", "")
            cache[model_id] = {
                "context_length": model.get("context_length", 128000),
                "max_completion_tokens": model.get("top_provider", {}).get("max_completion_tokens", 4096),
                "name": model.get("name", model_id),
                "pricing": model.get("pricing", {}),
            }
            canonical = model.get("canonical_slug", "")
            if canonical and canonical != model_id:
                cache[canonical] = cache[model_id]

        _model_metadata_cache = cache
        _model_metadata_cache_time = time.time()
        logger.debug("Fetched metadata for %s models from OpenRouter", len(cache))
        return cache

    except Exception as e:
        logging.warning(f"Failed to fetch model metadata from OpenRouter: {e}")
        return _model_metadata_cache or {}


def _get_context_cache_path() -> Path:
    """Return path to the persistent context length cache file."""
    hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
    return hermes_home / "context_length_cache.yaml"


def _load_context_cache() -> Dict[str, int]:
    """Load the model+provider → context_length cache from disk."""
    path = _get_context_cache_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("context_lengths", {})
    except Exception as e:
        logger.debug("Failed to load context length cache: %s", e)
        return {}


def save_context_length(model: str, base_url: str, length: int) -> None:
    """Persist a discovered context length for a model+provider combo.

    Cache key is ``model@base_url`` so the same model name served from
    different providers can have different limits.
    """
    key = f"{model}@{base_url}"
    cache = _load_context_cache()
    if cache.get(key) == length:
        return  # already stored
    cache[key] = length
    path = _get_context_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump({"context_lengths": cache}, f, default_flow_style=False)
        logger.info("Cached context length %s → %s tokens", key, f"{length:,}")
    except Exception as e:
        logger.debug("Failed to save context length cache: %s", e)


def get_cached_context_length(model: str, base_url: str) -> Optional[int]:
    """Look up a previously discovered context length for model+provider."""
    key = f"{model}@{base_url}"
    cache = _load_context_cache()
    return cache.get(key)


def get_next_probe_tier(current_length: int) -> Optional[int]:
    """Return the next lower probe tier, or None if already at minimum."""
    for tier in CONTEXT_PROBE_TIERS:
        if tier < current_length:
            return tier
    return None


def parse_context_limit_from_error(error_msg: str) -> Optional[int]:
    """Try to extract the actual context limit from an API error message.

    Many providers include the limit in their error text, e.g.:
      - "maximum context length is 32768 tokens"
      - "context_length_exceeded: 131072"
      - "Maximum context size 32768 exceeded"
      - "model's max context length is 65536"
    """
    error_lower = error_msg.lower()
    # Pattern: look for numbers near context-related keywords
    patterns = [
        r'n_ctx[:\s]+(\d{4,})',                 # llama.cpp/LM Studio: "n_ctx: 16384"
        r'context_length_exceeded[:\s]+(\d{4,})', # Anthropic: "context_length_exceeded: 131072"
        r'context\s*size\s*\(?(\d{4,})',        # LM Studio: "context size (16384 tokens)"
        r'(?:max(?:imum)?|limit)\s*(?:context\s*)?(?:length|size|window)?\s*(?:is|of|:)?\s*\(?(\d{4,})',
        r'context\s*(?:length|size|window)\s*(?:is|of|:)?\s*\(?(\d{4,})',
        r'(\d{4,})\s*(?:token)?\s*(?:context|limit)',
        r'>\s*(\d{4,})\s*(?:max|limit|token)',  # "250000 tokens > 200000 maximum"
        r'(\d{4,})\s*(?:max(?:imum)?)\b',  # "200000 maximum"
    ]
    for pattern in patterns:
        match = re.search(pattern, error_lower)
        if match:
            limit = int(match.group(1))
            # Sanity check: must be a reasonable context length
            if 1024 <= limit <= 10_000_000:
                return limit
    return None


def _get_config_context_length(model: str) -> Optional[int]:
    """Check config.yaml for VRAM-validated context lengths from the setup benchmark.

    The setup wizard probes each model by actually loading it at decreasing
    context sizes and verifying with a full-payload request.  The result is
    the largest context the user's hardware can actually serve — not the
    model's theoretical max.

    Storage shape (see gateway/setup_handlers.py:get_cached_machine_context):
      lmstudio_context_lengths:
        <base_url>:           # nested form — current writer
          <model_id>: <int>
        <model_id>: <int>     # legacy flat form — old writer

    Both are read here. We don't know the caller's inference base_url at
    this layer (the Logos agent talks to inference.local while the
    benchmark keyed the physical LM Studio URL like
    http://host.docker.internal:1234/v1), so we scan all nested entries
    for a matching model id and return the largest value seen.
    """
    try:
        hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
        config_path = hermes_home / "config.yaml"
        if not config_path.exists():
            return None
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        ctx_map = cfg.get("lmstudio_context_lengths") or {}
        if not isinstance(ctx_map, dict):
            return None

        model_lower = model.lower()
        best: Optional[int] = None

        def _consider(val: Any) -> None:
            nonlocal best
            if isinstance(val, int) and val > 0:
                if best is None or val > best:
                    best = val

        # Nested form: {base_url: {model_id: int}}. Keys look like URLs.
        # Legacy flat form: {model_id: int}. We tell them apart by the
        # leaf type.
        for k, v in ctx_map.items():
            if isinstance(v, dict):
                # Nested — scan this base_url's models.
                if model in v:
                    _consider(v[model])
                    continue
                for inner_k, inner_v in v.items():
                    inner_lower = (inner_k or "").lower()
                    if inner_lower == model_lower or inner_lower in model_lower or model_lower in inner_lower:
                        _consider(inner_v)
            elif isinstance(v, int):
                # Flat — k is a model id.
                k_lower = (k or "").lower()
                if k == model or k_lower == model_lower or k_lower in model_lower or model_lower in k_lower:
                    _consider(v)

        return best
    except Exception:
        pass
    return None


def _query_server_context_length(model: str, base_url: str) -> Optional[int]:
    """Query the inference server's /v1/models endpoint for context length.

    OpenAI-compatible servers (LM Studio, llama.cpp, vLLM, etc.) expose
    model metadata including context window size.  This is a runtime probe
    — slower than cache but always accurate for the currently loaded model.
    """
    if not base_url:
        return None
    try:
        import httpx
        # Try with common auth patterns
        headers = {}
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LM_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # Strip trailing /v1 to avoid double path (/v1/v1/models)
        _url = base_url.rstrip("/")
        if _url.endswith("/v1"):
            _url = _url[:-3]
        resp = httpx.get(f"{_url}/v1/models", headers=headers, timeout=5)
        if resp.status_code != 200:
            return None
        data = resp.json()
        models = data if isinstance(data, list) else data.get("data", [])
        model_lower = model.lower()
        for m in models:
            mid = (m.get("id") or "").lower()
            if mid == model_lower or model_lower in mid or mid in model_lower:
                # Different servers use different field names
                ctx = (m.get("max_context_length")       # LM Studio native
                       or m.get("context_length")         # some servers
                       or m.get("max_model_len")          # vLLM
                       or m.get("context_window"))        # others
                if isinstance(ctx, int) and ctx > 0:
                    logger.info("Server reports context length for %s: %s tokens", model, f"{ctx:,}")
                    return ctx
    except Exception as exc:
        logger.debug("Could not query server context length: %s", exc)
    return None


def get_model_context_length(model: str, base_url: str = "") -> Optional[int]:
    """Resolve the usable context length for a model, or None if unknown.

    Resolution order — each step consults an authoritative source, and
    we return as soon as one answers:

    1. Persistent probe cache — a prior runtime probe wrote the real
       limit to ``~/.logos/context_length_cache.yaml``.
    2. Setup-wizard benchmark — the VRAM-validated value the benchmark
       stored in ``config.yaml`` under ``lmstudio_context_lengths``.
    3. Live ``/v1/models`` query — the inference server itself reports
       ``max_context_length`` / ``max_model_len`` / etc.
    4. OpenRouter metadata — for cloud models reachable by canonical
       slug (skipped when ``base_url`` points at a private IP, since
       OpenRouter reports the theoretical cloud context which may
       exceed a local VRAM-limited deploy).

    If every step fails we return ``None`` and log a WARNING telling
    the user how to populate the benchmark. Callers that cannot carry
    an Optional (e.g. ``ContextCompressor``) must pick their own
    fallback at the call site, explicitly, with their own log line —
    this function refuses to fabricate a number.
    """
    # 1. Check persistent cache (model+provider)
    if base_url:
        cached = get_cached_context_length(model, base_url)
        if cached is not None:
            logger.debug("Context length for %s: %d (from probe cache)", model, cached)
            return cached

    # 2. Config.yaml benchmark results (VRAM-validated)
    config_ctx = _get_config_context_length(model)
    if config_ctx is not None:
        logger.info("Context length for %s: %s tokens (from setup benchmark)", model, f"{config_ctx:,}")
        # Also cache it for faster lookups
        if base_url:
            save_context_length(model, base_url, config_ctx)
        return config_ctx

    # 3. Live query to the inference server
    if base_url:
        server_ctx = _query_server_context_length(model, base_url)
        if server_ctx is not None:
            save_context_length(model, base_url, server_ctx)
            return server_ctx

    # 4. OpenRouter API metadata (cloud models only)
    # Skip for local/private servers — OpenRouter reports cloud context sizes
    # (e.g. 256K) that don't match the local model's actual VRAM-limited context.
    _is_local = base_url and any(
        h in base_url for h in ("localhost", "127.0.0.1", "192.168.", "10.", "172.16.")
    )
    if not _is_local:
        metadata = fetch_model_metadata()
        if model in metadata:
            ctx = metadata[model].get("context_length")
            if isinstance(ctx, int) and ctx > 0:
                return ctx

    logger.warning(
        "Context length unknown for model %r at base_url=%r. "
        "Run the Logos setup benchmark to probe it, or set "
        "lmstudio_context_lengths[%r][%r] in ~/.logos/config.yaml.",
        model, base_url or "<unset>", base_url or "<base_url>", model,
    )
    return None


def estimate_tokens_rough(text: str) -> int:
    """Rough token estimate (~4 chars/token) for pre-flight checks."""
    if not text:
        return 0
    return len(text) // 4


def estimate_messages_tokens_rough(messages: List[Dict[str, Any]]) -> int:
    """Rough token estimate for a message list (pre-flight only)."""
    total_chars = sum(len(str(msg)) for msg in messages)
    return total_chars // 4
