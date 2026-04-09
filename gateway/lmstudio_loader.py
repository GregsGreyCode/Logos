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

# Cache of (base_url, model_id) tuples that we've confirmed are loaded
# in LM Studio. Cleared when the gateway restarts (intentional — we
# re-verify on first dispatch after a restart in case LM Studio also
# restarted in between).
_LOADED: Set[Tuple[str, str]] = set()


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
    _LOADED = {(b, m) for (b, m) in _LOADED if b != host}


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
    if cache_key in _LOADED:
        return True

    if not api_key:
        import os as _os
        api_key = _os.environ.get("OPENAI_API_KEY") or "lm-studio"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Query loaded models. If the model is already there,
            # add to cache and return without touching /load.
            try:
                async with session.get(
                    f"{host}/api/v1/models",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        for m in (data.get("data") or []):
                            mid = m.get("id") or m.get("model")
                            state = (m.get("state") or m.get("status") or "").lower()
                            # LM Studio reports state="loaded" for active
                            # models. Empty state from older LM Studio
                            # builds is also treated as "loaded" (it
                            # only lists loaded models on those builds).
                            if mid == model_id and state in ("loaded", "ready", ""):
                                _LOADED.add(cache_key)
                                return True
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
                    _LOADED.add(cache_key)
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
