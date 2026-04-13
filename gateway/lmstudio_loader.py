"""
LM Studio on-demand model loader.

The Logos gateway has always known how to call LM Studio's
``/api/v1/models/load`` endpoint, but only at startup-time (run.py
preloads the gateway-wide active model when a platform connects).
The chat dispatch path didn't call it, so an agent bound to a model
that LM Studio hadn't loaded yet would hit the OpenAI-compatible
``/v1/chat/completions`` endpoint, get an "Unexpected endpoint or
method" error from LM Studio, and the worker would silently fail.

This module fills that gap with a single async helper called from
``_handle_chat`` before the worker dispatch. It maintains an in-memory
cache of ``(base_url, model_id)`` tuples that have already been
confirmed loaded, so the call is a no-op after the first dispatch
for any given (host, model) pair.

NOTE on path layout: LM Studio exposes TWO endpoint trees from the
same port:

  * ``/v1/...``    — OpenAI-compatible (chat/completions, embeddings, …)
  * ``/api/v1/...`` — LM Studio REST API (models, models/load, …)

The user's ``machines.endpoint_url`` typically ends in ``/v1`` because
that's what they configured for the OpenAI-compatible client. We strip
that suffix here before hitting ``/api/v1/...`` so we land on the right
tree regardless of how the user wrote the URL.
"""

from __future__ import annotations

import logging
from typing import Optional, Set, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# Cache of (base_url, model_id) → last_verified_unix_seconds. Used as a
# short-TTL fast-path so we don't hammer LM Studio's /api/v1/models on
# every chat dispatch. After the TTL expires we re-query LM Studio
# directly because the user might have manually unloaded the model in
# LM Studio's UI in between — earlier code with a permanent cache made
# unloading the model invisible to the gateway, so the next chat would
# silently dispatch into a "no model loaded" LM Studio and the worker
# would hang.
_LOADED: dict = {}
# 30 seconds is short enough that manual unloads are caught quickly
# but long enough that a flurry of chats from the same agent doesn't
# round-trip to LM Studio for every single one.
_CACHE_TTL_SECONDS = 30.0


def _strip_v1_suffix(base_url: str) -> str:
    """Normalise a user-supplied base URL to the LM Studio host root.

    The user's machines.endpoint_url usually ends in ``/v1`` (e.g.
    ``http://192.168.1.117:1234/v1``) because that's the OpenAI-
    compatible API base. The LM Studio REST endpoints we use here
    live at ``/api/v1/...`` from the host root, so we strip the
    trailing ``/v1`` (and any trailing slash) before joining.
    """
    s = base_url.rstrip("/")
    if s.endswith("/v1"):
        s = s[:-3]
    return s


def invalidate_cache(base_url: Optional[str] = None) -> None:
    """Drop cached loaded-state. With ``base_url`` only that host's
    entries are dropped; without it the entire cache is cleared.

    Useful from explicit user actions like /admin/sandboxes/restart
    where the user might be trying to recover from a stale cache."""
    global _LOADED
    if base_url is None:
        _LOADED.clear()
        return
    host = _strip_v1_suffix(base_url)
    _LOADED = {k: v for k, v in _LOADED.items() if k[0] != host}


async def ensure_loaded(
    base_url: str,
    model_id: str,
    api_key: Optional[str] = None,
    *,
    timeout: float = 120.0,
) -> bool:
    """Ensure ``model_id`` is loaded in the LM Studio at ``base_url``.

    Returns True if the model is loaded (or already was), False on any
    failure. Failures are logged but never raised — the caller is
    expected to proceed with the dispatch and let the actual chat call
    surface the underlying error if the load attempt didn't help.

    Cached: subsequent calls for the same ``(base_url, model_id)`` pair
    are a no-op until ``invalidate_cache`` is called or the gateway
    process restarts.

    ``api_key`` resolution: must work for both LM Studio in no-auth mode
    AND token-auth mode. If the caller passes None, we fall back to the
    OPENAI_API_KEY env var, then to the literal "lm-studio" placeholder
    (which LM Studio accepts as a well-formed token in both modes —
    same fallback we use for the openshell provider credential in
    openshell_routes.finish_provisioning).
    """
    if not base_url or not model_id:
        return False

    host = _strip_v1_suffix(base_url)
    cache_key = (host, model_id)
    # Short-TTL fast path. If we verified this (host, model) within the
    # last _CACHE_TTL_SECONDS, skip the network round-trip. Beyond the
    # TTL we MUST re-verify because the user might have manually
    # unloaded the model in LM Studio's UI — a permanent cache would
    # silently miss that and dispatch into a no-model-loaded server.
    import time as _time
    now = _time.time()
    last_verified = _LOADED.get(cache_key, 0.0)
    if (now - last_verified) < _CACHE_TTL_SECONDS:
        return True

    if not api_key:
        import os as _os
        api_key = _os.environ.get("OPENAI_API_KEY") or "lm-studio"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Query loaded models. If the model is already there,
            # add to cache and return without touching /load.
            #
            # LM Studio's /api/v1/models response shape (verified by
            # actual probe, not assumed):
            #
            #   {
            #     "models": [
            #       {
            #         "key": "openai/gpt-oss-20b",
            #         "loaded_instances": [...],  // non-empty = loaded
            #         "type": "llm",
            #         ...
            #       }
            #     ]
            #   }
            #
            # Earlier code looked for `data["data"]`, `m["id"]`,
            # `m["state"]` — none of which exist in LM Studio's
            # response — so the query NEVER matched anything and we
            # called /api/v1/models/load on every single chat. The
            # user's "every new request is loading a new model"
            # symptom was caused by these wrong field names.
            try:
                async with session.get(
                    f"{host}/api/v1/models",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        # LM Studio's REST API uses "models"; the
                        # OpenAI-compat endpoint at /v1/models uses
                        # "data". Tolerate both so we work against
                        # either tree.
                        items = data.get("models") or data.get("data") or []
                        for m in items:
                            mid = m.get("key") or m.get("id") or m.get("model")
                            if mid != model_id:
                                continue
                            # Loaded state: LM Studio reports an array
                            # of `loaded_instances` (non-empty = at
                            # least one running instance of this model).
                            # Older builds may use a flat "state" field
                            # instead — accept both.
                            instances = m.get("loaded_instances")
                            if isinstance(instances, list) and len(instances) > 0:
                                _LOADED[cache_key] = now
                                return True
                            state = (m.get("state") or m.get("status") or "").lower()
                            if state in ("loaded", "ready"):
                                _LOADED[cache_key] = now
                                return True
                            # Found the model in the catalog but not
                            # loaded — fall through to /load below.
                            break
                    else:
                        logger.warning(
                            "ensure_loaded: GET %s/api/v1/models returned %d",
                            host, resp.status,
                        )
            except Exception as q_exc:
                logger.warning("ensure_loaded: query failed for %s: %s", host, q_exc)
                # Fall through to load — the load call will surface
                # any deeper connectivity error too.

            # 2. Not in the loaded set (or query failed) — POST load.
            logger.info(
                "ensure_loaded: requesting LM Studio at %s to load %r",
                host, model_id,
            )
            async with session.post(
                f"{host}/api/v1/models/load",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "flash_attention": True,
                    "echo_load_config": True,
                },
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    _LOADED[cache_key] = now
                    logger.info(
                        "ensure_loaded: %r loaded successfully on %s",
                        model_id, host,
                    )
                    return True
                body = ""
                try:
                    body = await resp.text()
                except Exception:
                    pass
                logger.warning(
                    "ensure_loaded: POST %s/api/v1/models/load returned %d: %s",
                    host, resp.status, body[:200],
                )
                return False
    except Exception as exc:
        logger.warning("ensure_loaded: exception loading %r on %s: %s", model_id, host, exc)
        return False


# ── Model download (LM Studio 0.4.x+ REST API) ─────────────────────────
#
# LM Studio exposes three REST endpoints for programmatic model management:
#   POST /api/v1/models/download             — start a download, returns job_id
#   GET  /api/v1/models/download/status/:id  — poll progress / completion
#   POST /api/v1/models/load                 — load a downloaded model into a slot
#
# We use (1)+(2) here to give Logos the ability to install a known-good
# agent-capable model without making the user hunt on Hugging Face. The
# load path already lives above in ensure_loaded. Both the download and
# status endpoints accept the same Bearer token as the rest of the LM
# Studio API, or no auth if the user hasn't configured a token.

async def download_model(
    base_url: str,
    model: str,
    quantization: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    timeout: float = 30.0,
) -> dict:
    """Kick off a download via POST /api/v1/models/download.

    Returns a dict with at least ``status`` (one of downloading / paused /
    completed / failed / already_downloaded) and ``job_id`` when the
    download actually started (already_downloaded skips the job_id).
    Body parity with the LM Studio spec — we don't strip or rename fields
    so ``total_size_bytes``, ``started_at``, etc. pass through unchanged.

    Raises aiohttp.ClientError / asyncio.TimeoutError on transport failure;
    callers should guard with try/except and surface the error to the UI.
    """
    import aiohttp
    host = _strip_v1_suffix(base_url)
    url = f"{host}/api/v1/models/download"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict = {"model": model}
    if quantization:
        payload["quantization"] = quantization
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.text()
            try:
                data = json.loads(body)
            except Exception:
                data = {"status": "failed", "error": f"non-JSON response: {body[:200]}"}
            if resp.status >= 400:
                # LM Studio's error bodies vary; wrap whatever we got
                # with the status code so the UI can distinguish
                # auth failures from model-not-found.
                data.setdefault("status", "failed")
                data.setdefault("error", f"HTTP {resp.status}")
                data["http_status"] = resp.status
            return data


async def download_status(
    base_url: str,
    job_id: str,
    api_key: Optional[str] = None,
    *,
    timeout: float = 10.0,
) -> dict:
    """Poll GET /api/v1/models/download/status/:job_id.

    Returns the raw LM Studio response dict — ``status``, ``downloaded_bytes``,
    ``total_size_bytes``, ``bytes_per_second``, ``estimated_completion``,
    ``completed_at`` — letting the UI render progress directly without
    a translation layer. Raises on transport failure.
    """
    import aiohttp
    host = _strip_v1_suffix(base_url)
    url = f"{host}/api/v1/models/download/status/{job_id}"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.text()
            try:
                data = json.loads(body)
            except Exception:
                data = {"status": "failed", "error": f"non-JSON response: {body[:200]}"}
            if resp.status >= 400:
                data.setdefault("status", "failed")
                data.setdefault("error", f"HTTP {resp.status}")
                data["http_status"] = resp.status
            return data
