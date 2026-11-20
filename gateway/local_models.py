"""See, load and unload models on the local model server (LM Studio).

Agents name their model in ``agents/<name>.yaml`` (``model.default`` and
``model.context_length``). LM Studio loads a model by itself when a request names
one, but with its own default window, and it treats a second download of the same
model as a different model. This module is the one place that decides what should
be loaded, so nothing loads a duplicate by accident:

- :func:`wanted` — the models the agent definitions ask for.
- :func:`loaded` — what the server holds right now.
- :func:`ensure` — load a model with the configured window unless it is already there.
- :func:`unload` — remove one loaded model.
- :func:`strays` — loaded models no agent asks for.

Stdlib only. ``http`` is ``Runner.http`` from :mod:`gateway.agent_runtime`.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Mapping

JSON = {"Content-Type": "application/json"}


def server(model: Mapping[str, Any]) -> str:
    """The server root for an agent's ``model`` block, or "" when it is not a local LM Studio model."""
    if model.get("provider") != "lmstudio" or not model.get("base_url"):
        return ""
    return str(model["base_url"]).rsplit("/v1", 1)[0].rstrip("/")


def load_window(model: Mapping[str, Any]) -> int:
    """The window to load the model with: ``load_context_length`` when set (a bigger, shared window,
    so parallel requests have room beside each other), never less than what the agent uses."""
    return max(int(model.get("context_length") or 0), int(model.get("load_context_length") or 0))


def wanted(agents: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    """``{server: {model id: largest context any agent asks for}}`` over the agent definitions."""
    out: Dict[str, Dict[str, int]] = {}
    for agent in agents.values():
        model = agent.get("model") or {}
        base = server(model)
        if not base or not model.get("default"):
            continue
        ctx = load_window(model)
        models = out.setdefault(base, {})
        models[model["default"]] = max(models.get(model["default"], 0), ctx)
    return out


def loaded(http: Callable[..., bytes], base: str) -> Dict[str, int]:
    """``{model id: loaded context length}`` for every model the server has in memory."""
    data = json.loads(http(base + "/api/v0/models", timeout=10))["data"]
    return {m["id"]: int(m.get("loaded_context_length") or 0) for m in data if m.get("state") == "loaded"}


def reachable(http: Callable[..., bytes], base: str, timeout: float = 3) -> bool:
    """Is the model server answering at all? A machine that is on with LM Studio closed
    usually drops the connection without a word, so the wait is kept short."""
    try:
        http(base + "/api/v0/models", timeout=timeout)
        return True
    except Exception:
        return False


def is_ready(have: Mapping[str, int], model_id: str, context: int) -> bool:
    """A loaded window at least as large is fine: the agent limits itself to its own size.
    Loading again would start a second instance and can exhaust memory."""
    return model_id in have and have[model_id] >= int(context)


def ensure(http: Callable[..., bytes], base: str, model_id: str, context: int) -> bool:
    """Load ``model_id`` with ``context`` unless it is already loaded. True when it had to load."""
    have = loaded(http, base)
    if is_ready(have, model_id, context):
        return False
    if model_id in have:                     # loaded too small: replace it, never stack a second copy
        unload(http, base, model_id)
    body = json.dumps({"model": model_id, "context_length": int(context)}).encode()
    http(base + "/api/v1/models/load", data=body, headers=JSON, timeout=600)
    return True


def unload(http: Callable[..., bytes], base: str, model_id: str) -> None:
    http(base + "/api/v1/models/unload", data=json.dumps({"instance_id": model_id}).encode(),
         headers=JSON, timeout=120)


def strays(have: Mapping[str, int], want: Mapping[str, int]) -> List[str]:
    """Loaded models that no agent asks for (for example a second download of the same model)."""
    return sorted(m for m in have if m not in want)


def report(http: Callable[..., bytes], agents: Mapping[str, Mapping[str, Any]]) -> str:
    lines = []
    for base, want in wanted(agents).items():
        have = loaded(http, base)
        lines.append(f"model server {base}")
        for model_id, ctx in sorted(want.items()):
            if is_ready(have, model_id, ctx):
                state = f"loaded (context {have[model_id]})"
            elif model_id in have:
                state = f"loaded too small (context {have[model_id]}, agents need {ctx})"
            else:
                state = "not loaded (the first message will wait for it to load)"
            lines.append(f"  {model_id}: {state}")
        for model_id in strays(have, want):
            lines.append(f"  {model_id}: loaded, but no agent uses it (context {have[model_id]})")
    return "\n".join(lines) or "no agent uses a local model server"


def available(http: Callable[..., bytes], base: str) -> List[str]:
    """Every model the server has downloaded, loaded or not."""
    return sorted(m["id"] for m in json.loads(http(base + "/api/v0/models", timeout=10))["data"]
                  if m.get("type") != "embeddings")
