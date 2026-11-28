"""See, load and unload models on the local model server (LM Studio or llama-swap).

Two kinds of server are understood (:func:`kind` tells them apart by asking):

- **LM Studio**: Logos loads the model with its window (``/api/v1/models/load``).
- **llama-swap** in front of llama.cpp's ``llama-server`` (docs/llama-swap.md): every setting is
  in its ``config.yaml`` on the model PC, and it loads a model by itself when a request names it.
  Logos only lists, wakes (:func:`ensure`) and unloads; :func:`listing_v0` and
  :func:`listing_v1` give its answers in LM Studio's shapes, so the pages need no second path.

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
import re
import urllib.error
from typing import Any, Callable, Dict, List, Mapping

LLAMA_SWAP, LM_STUDIO = "llama-swap", "lmstudio"
_kinds: Dict[str, str] = {}


def kind(http: Callable[..., bytes], base: str) -> str:
    """Which server answers at ``base``: llama-swap (it has ``/running``) or LM Studio.
    Remembered once the server has answered; a server that is off is asked again next time."""
    if base in _kinds:
        return _kinds[base]
    try:
        found = LLAMA_SWAP if "running" in (json.loads(http(base + "/running", timeout=4)) or {}) else LM_STUDIO
    except urllib.error.HTTPError:
        found = LM_STUDIO                    # it answered, without /running
    except (ValueError, AttributeError, TypeError):
        found = LM_STUDIO
    except Exception:                        # not answering: unknown, guess the old default
        return LM_STUDIO
    _kinds[base] = found
    return found


def _flag(cmd: str, *names: str) -> str:
    """The value after a command-line flag in llama-swap's ``cmd`` ("" when absent)."""
    for n in names:
        m = re.search(r"(?:^|\s)" + re.escape(n) + r"[\s=]+(\S+)", cmd)
        if m:
            return m.group(1).strip('"')
    return ""


def _swap(http: Callable[..., bytes], base: str):
    """llama-swap's model list and what is running: ({id: model row}, {id: running row})."""
    listed = {str(m.get("id")): m for m in json.loads(http(base + "/v1/models", timeout=10)).get("data", [])
              if isinstance(m, dict) and m.get("id")}
    running = {str(r.get("model")): r for r in (json.loads(http(base + "/running", timeout=10)) or {}).get("running", [])
               if isinstance(r, dict) and r.get("state") == "ready"}
    return listed, running


def _swap_context(row: Mapping[str, Any], run: Mapping[str, Any]) -> int:
    return int(row.get("context_length") or _flag(str(run.get("cmd") or ""), "-c", "--ctx-size") or 0)


def listing_v0(http: Callable[..., bytes], base: str) -> dict:
    """``GET /api/v0/models`` (LM Studio's simple list), from either kind of server."""
    if kind(http, base) != LLAMA_SWAP:
        return json.loads(http(base + "/api/v0/models", timeout=10))
    listed, running = _swap(http, base)
    data = []
    for mid, row in sorted(listed.items()):
        run = running.get(mid)
        ctx = _swap_context(row, run or {})
        data.append({"id": mid, "type": "vlm" if (row.get("capabilities") or {}).get("vision") else "llm",
                     "state": "loaded" if run else "not-loaded", "max_context_length": ctx,
                     "loaded_context_length": ctx if run else 0})
    return {"data": data}


def listing_v1(http: Callable[..., bytes], base: str) -> List[dict]:
    """``GET /api/v1/models`` (LM Studio's full list, with loaded instances), from either kind."""
    if kind(http, base) != LLAMA_SWAP:
        data = json.loads(http(base + "/api/v1/models", timeout=10))
        return [m for m in data.get("models", []) if isinstance(m, dict)]
    listed, running = _swap(http, base)
    out = []
    for mid, row in sorted(listed.items()):
        run = running.get(mid)
        cmd = str((run or {}).get("cmd") or "")
        inst = []
        if run:
            fa = _flag(cmd, "-fa", "--flash-attn")
            inst = [{"id": mid, "config": {
                "context_length": _swap_context(row, run),
                "parallel": int(_flag(cmd, "-np", "--parallel") or 1),
                "flash_attention": None if not fa else fa.lower() not in ("off", "false", "0"),
                "offload_kv_cache_to_gpu": False if "--no-kv-offload" in cmd or " -nkvo" in cmd else True,
                "kv_cache_type": _flag(cmd, "-ctk", "--cache-type-k") or "f16",
                "n_cpu_moe": int(_flag(cmd, "--n-cpu-moe", "-ncmoe") or 0) or ("all" if "--cpu-moe" in cmd else 0),
                "eval_batch_size": int(_flag(cmd, "-b", "--batch-size") or 0) or None}}]
        size = 0
        if run:                               # the loaded server's own numbers (file size)
            try:
                meta = (json.loads(http(f"{base}/upstream/{mid}/v1/models", timeout=5)).get("data") or [{}])[0].get("meta") or {}
                size = int(meta.get("size") or 0)
            except Exception:
                pass
        model_file = _flag(cmd, "-m", "--model")
        quant = re.search(r"(Q\d[_A-Z0-9]*|IQ\d[_A-Z0-9]*|BF16|F16)", model_file.rsplit("\\", 1)[-1].rsplit("/", 1)[-1], re.I)
        out.append({"key": mid, "display_name": mid, "type": "llm", "max_context_length": _swap_context(row, run or {}),
                    "quantization": {"name": quant.group(1).upper() if quant else ""},
                    "size_bytes": size, "loaded_instances": inst, "server": LLAMA_SWAP})
    return out

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
    data = listing_v0(http, base)["data"]
    return {m["id"]: int(m.get("loaded_context_length") or 0) for m in data if m.get("state") == "loaded"}


def reachable(http: Callable[..., bytes], base: str, timeout: float = 3) -> bool:
    """Is the model server answering at all? A machine that is on with LM Studio closed
    usually drops the connection without a word, so the wait is kept short."""
    try:
        http(base + "/api/v0/models", timeout=timeout)
        return True
    except urllib.error.HTTPError:           # it answered (llama-swap has no /api/v0): it is on
        return True
    except Exception:
        return False


def is_ready(have: Mapping[str, int], model_id: str, context: int) -> bool:
    """A loaded window at least as large is fine: the agent limits itself to its own size.
    Loading again would start a second instance and can exhaust memory."""
    return model_id in have and have[model_id] >= int(context)


def ensure(http: Callable[..., bytes], base: str, model_id: str, context: int) -> bool:
    """Load ``model_id`` with ``context`` unless it is already loaded. True when it had to load.

    llama-swap loads with the settings in its own config (``context`` is not sent): asking its
    ``/upstream/<model>/health`` starts the model and answers once it is ready."""
    have = loaded(http, base)
    if is_ready(have, model_id, context):
        return False
    if kind(http, base) == LLAMA_SWAP:
        http(f"{base}/upstream/{model_id}/health", timeout=600)
        return True
    if model_id in have:                     # loaded too small: replace it, never stack a second copy
        unload(http, base, model_id)
    body = json.dumps({"model": model_id, "context_length": int(context)}).encode()
    http(base + "/api/v1/models/load", data=body, headers=JSON, timeout=600)
    return True


def unload(http: Callable[..., bytes], base: str, model_id: str) -> None:
    if kind(http, base) == LLAMA_SWAP:
        http(f"{base}/api/models/unload/{model_id}", data=b"", timeout=120)
        return
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
    return sorted(m["id"] for m in listing_v0(http, base)["data"]
                  if m.get("type") != "embeddings")
