"""Cloud-model pricing catalogue sourced from OpenRouter.

Why OpenRouter: Anthropic and OpenAI don't publish pricing via their APIs
(only via their public HTML pages). OpenRouter's free /v1/models endpoint
returns structured pricing for 350+ models including every major cloud
provider's offerings — per-token strings in the `pricing` object. Their
numbers mirror upstream provider pricing (they take their cut as a flat
service fee, not per-token markup), so the amounts are accurate for
budgeting direct API calls.

Cache strategy: fetch once at gateway startup, cache to
``~/.logos/openrouter_pricing.json``, refresh every 24h or on demand via
``/admin/pricing/refresh``. The cached file keeps us working if
openrouter.ai is briefly unreachable.

Lookup strategy: OpenRouter model ids are like ``anthropic/claude-sonnet-4.6``
while Logos/Hermes model fields use ``claude-sonnet-4-6`` (Anthropic's
own API shape) or ``openai/gpt-oss-20b`` (LM Studio convention). The
``lookup`` function normalises both sides so common aliases resolve.
"""
from __future__ import annotations

import json
import logging
import os
import time
import threading
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CACHE_FILENAME = "openrouter_pricing.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h — model pricing rarely changes more often

# Anthropic's prompt-caching token classes aren't in OpenRouter's top-level
# pricing (they only expose prompt/completion/image/request). Apply the
# published Anthropic ratios against the `prompt` price when we see usage
# stats for cache_read / cache_write tokens. These ratios are documented at
# https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching.
_ANTHROPIC_CACHE_READ_RATIO = 0.10      # 10% of input price
_ANTHROPIC_CACHE_WRITE_RATIO = 1.25     # 125% of input price

_state_lock = threading.Lock()
_pricing_by_id: Dict[str, dict] = {}      # OpenRouter id → full pricing dict
_loaded_at: float = 0.0


def _cache_path() -> Path:
    base = Path(os.environ.get("LOGOS_DATA_DIR", str(Path.home() / ".logos")))
    return base / _CACHE_FILENAME


def _load_cache_if_fresh() -> bool:
    """Return True if we populated _pricing_by_id from a fresh cache file."""
    global _loaded_at
    p = _cache_path()
    if not p.exists():
        return False
    try:
        age = time.time() - p.stat().st_mtime
        if age > _CACHE_TTL_SECONDS:
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        models = data.get("data", [])
        with _state_lock:
            _pricing_by_id.clear()
            for m in models:
                mid = m.get("id")
                if mid:
                    _pricing_by_id[mid] = m
            _loaded_at = p.stat().st_mtime
        logger.info("pricing: loaded %d models from cache (%.0fh old)",
                    len(_pricing_by_id), age / 3600)
        return True
    except Exception as exc:
        logger.warning("pricing: cache read failed: %s", exc)
        return False


def _fetch_and_cache() -> int:
    """Hit OpenRouter, populate _pricing_by_id, and write the cache file.
    Returns the number of models loaded, or 0 on failure."""
    global _loaded_at
    try:
        req = urllib.request.Request(
            _OPENROUTER_MODELS_URL,
            headers={"User-Agent": "logos-pricing/1"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("pricing: fetch failed: %s", exc)
        return 0
    models = data.get("data", [])
    with _state_lock:
        _pricing_by_id.clear()
        for m in models:
            mid = m.get("id")
            if mid:
                _pricing_by_id[mid] = m
        _loaded_at = time.time()
    # Write-through to disk — best-effort, never raises.
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.debug("pricing: cache write skipped: %s", exc)
    logger.info("pricing: fetched %d models from OpenRouter", len(_pricing_by_id))
    return len(_pricing_by_id)


def ensure_loaded(force_refresh: bool = False) -> int:
    """Load the pricing catalogue if we haven't yet (or if it's stale).

    Returns the count of models available. Callers can ignore the return —
    an empty catalogue just means cost lookups return None and the cost
    logger records 0 / unknown-price for those requests, which shows up
    in the dashboard as "pricing unavailable" rather than silently 0'ing.
    """
    if not force_refresh:
        with _state_lock:
            if _pricing_by_id and (time.time() - _loaded_at) < _CACHE_TTL_SECONDS:
                return len(_pricing_by_id)
        if _load_cache_if_fresh():
            return len(_pricing_by_id)
    return _fetch_and_cache()


def _normalize_model_id(model: str) -> Tuple[str, ...]:
    """Return a tuple of candidate OpenRouter ids to probe for a Logos model.

    Logos stores models in multiple shapes depending on provenance:
      - "claude-sonnet-4-6"           (direct Anthropic API convention)
      - "anthropic/claude-sonnet-4.6" (OpenRouter-style, if provisioned there)
      - "openai/gpt-oss-20b"          (LM Studio convention)
      - "qwen/qwen3.5-9b"             (LM Studio convention for local)

    This generates a small set of alternates we try in order against the
    OpenRouter catalogue. We DON'T try to cover every edge case — if the
    direct translations miss, the lookup returns None and the UI shows
    "unknown price" rather than guessing.
    """
    if not model:
        return ()
    m = model.strip()
    candidates: list[str] = [m]
    # Anthropic direct → OpenRouter naming: claude-sonnet-4-6 → anthropic/claude-sonnet-4.6
    low = m.lower()
    if low.startswith("claude-") and "/" not in m:
        # Convert "-4-6" → "-4.6" (numeric dot); keep rest
        parts = m.split("-")
        # Rebuild trailing version tokens if they look numeric
        fixed_parts: list[str] = []
        i = 0
        while i < len(parts):
            if (i + 1 < len(parts)
                    and parts[i].isdigit() and parts[i + 1].isdigit()):
                fixed_parts.append(parts[i] + "." + parts[i + 1])
                i += 2
            else:
                fixed_parts.append(parts[i])
                i += 1
        candidates.append("anthropic/" + "-".join(fixed_parts))
    # OpenAI direct → OpenRouter naming
    elif (low.startswith(("gpt-", "o1", "o3", "o4"))
          and "/" not in m):
        candidates.append("openai/" + m)
    return tuple(dict.fromkeys(candidates))  # dedupe, preserve order


def lookup(model: str) -> Optional[dict]:
    """Return the OpenRouter pricing dict for a Logos model, or None."""
    if not model:
        return None
    ensure_loaded()
    for mid in _normalize_model_id(model):
        with _state_lock:
            m = _pricing_by_id.get(mid)
        if m:
            return m.get("pricing") or {}
    return None


def cost_for_usage(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Optional[float]:
    """Return USD cost for a single request, or None if pricing unknown.

    Cache-read/write use Anthropic's published ratios against the prompt
    price since OpenRouter's `/v1/models` doesn't break them out. For
    non-Anthropic models with zero cache token counts this reduces to the
    simple prompt + completion math.
    """
    pricing = lookup(model)
    if not pricing:
        return None
    try:
        p_prompt = float(pricing.get("prompt") or 0)
        p_compl = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return None
    # Direct tokens
    cost = p_prompt * input_tokens + p_compl * output_tokens
    # Cache accounting (Anthropic)
    if cache_read_tokens:
        cost += p_prompt * _ANTHROPIC_CACHE_READ_RATIO * cache_read_tokens
    if cache_write_tokens:
        cost += p_prompt * _ANTHROPIC_CACHE_WRITE_RATIO * cache_write_tokens
    return cost


def catalogue_summary() -> dict:
    """Small dict for /admin/pricing/status — useful for debugging."""
    with _state_lock:
        return {
            "count": len(_pricing_by_id),
            "loaded_at": _loaded_at,
            "age_seconds": (time.time() - _loaded_at) if _loaded_at else None,
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
            "cache_path": str(_cache_path()),
        }
