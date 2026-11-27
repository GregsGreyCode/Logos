"""The local model server (LM Studio) for the dashboard's "Local model" section.

Every LM Studio server an agent definition points at is shown on its own (a new PC or an
always-on box appears as soon as an agent uses it). Each server's GPU (a name and its memory) is
told to Logos on the page and kept in ``~/.config/logos/model-servers.json``; the checks and the
recommended app settings follow from it, so a new GPU needs no code change.

What is loaded and with which settings, next to what the agents ask for; plain warnings when
they disagree; how the model guard's queue is doing and how fast replies have been; and the two
changes the dashboard may make (reload the agents' model with Logos's window, unload a model
no agent uses). Stdlib only; ``http`` is ``Runner.http`` from :mod:`gateway.agent_runtime`.

LM Studio's API cannot set or show everything: GPU offload, forcing MoE experts onto the CPU and
KV cache quantisation are set in the LM Studio app, and it reports no memory use. The page
says so and names the recommended values instead.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from gateway import local_models

JSON = {"Content-Type": "application/json"}
SETTINGS_FILE = "model-servers.json"    # in ~/.config/logos: {"host:port": {"gpu": "...", "vram_gb": 16}}


def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos"


def server_key(base: str) -> str:
    """'http://192.0.2.17:1234' -> '192.0.2.17:1234', how servers are named on the page."""
    return base.split("//", 1)[-1].rstrip("/")


def hardware(env: Mapping[str, str]) -> Dict[str, dict]:
    """What Logos has been told about each server's GPU: {"host:port": {"gpu", "vram_gb"}}."""
    try:
        data = json.loads((config_dir(env) / SETTINGS_FILE).read_text())
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_hardware(env: Mapping[str, str], server: str, gpu: str, vram_gb: float) -> dict:
    """Remember one server's GPU. ``vram_gb`` 0 forgets it."""
    gpu = " ".join(str(gpu or "").split())[:80]
    vram = float(vram_gb or 0)
    if not 0 <= vram <= 1024:
        raise ValueError("GPU memory is a number of GB, 0 to 1024")
    data = hardware(env)
    if vram:
        data[server] = {"gpu": gpu, "vram_gb": vram}
    else:
        data.pop(server, None)
    path = config_dir(env) / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return data.get(server, {})


def advice(m: Optional[dict], vram: float) -> List[dict]:
    """The settings only the LM Studio app can change, for this model on this GPU."""
    fits = bool(vram and m and m["size_gb"] and m["size_gb"] * 1.15 <= vram)   # room for the window
    unknown = not vram or not m
    moe = bool(m and m["moe"])
    if unknown:
        offload = ("Max", "Every layer the GPU can hold runs there. Tell Logos this PC's GPU memory "
                          "to get advice for this model.")
        experts = ("On for a mixture-of-experts model bigger than the GPU", "Keeps the experts in RAM and "
                   "the always-used layers on the GPU.")
    elif fits:
        offload = ("Max", f"The model ({m['size_gb']} GB) fits in the GPU's {vram:g} GB: all of it on the GPU.")
        experts = ("Off", "It fits on the GPU, so nothing needs to go to RAM.")
    else:
        offload = ("Max", f"The model ({m['size_gb']} GB) is bigger than the GPU's {vram:g} GB: as much as "
                          "fits goes on the GPU, the rest runs from RAM.")
        experts = (("On", "The experts sit in RAM and the always-used layers stay on the GPU: the fast way "
                          "to run a model this size on this GPU.") if moe else
                   ("(not a mixture-of-experts model)", "Part of the model runs from RAM, which is slow. "
                                                         "A smaller quantisation would fit."))
    rows = [{"setting": "GPU offload", "value": offload[0], "why": offload[1]}]
    if moe or unknown:
        rows.append({"setting": "Force model expert weights onto CPU", "value": experts[0], "why": experts[1]})
    rows += [
        {"setting": "K Cache / V Cache quantization", "value": "Q8_0",
         "why": "Halves the memory of the conversation window with no visible loss."},
        {"setting": "Flash attention", "value": "On", "why": "Less memory and faster. Logos turns it on when it loads the model."},
        {"setting": "Context length", "value": "Leave to Logos", "why": "Logos loads the window the agents need."},
        {"setting": "Keep model in memory", "value": "On", "why": "Stops the model being pushed out of memory."},
    ]
    return rows


def wanted(definitions: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, dict]]:
    """``{server: {model: {"agents": [...], "uses": largest context_length, "load": load window}}}``."""
    out: Dict[str, Dict[str, dict]] = {}
    for name, d in sorted(definitions.items()):
        model = d.get("model") or {}
        base = local_models.server(model)
        if not base or not model.get("default"):
            continue
        w = out.setdefault(base, {}).setdefault(str(model["default"]), {"agents": [], "uses": 0, "load": 0})
        w["agents"].append(name)
        w["uses"] = max(w["uses"], int(model.get("context_length") or 0))
        w["load"] = max(w["load"], local_models.load_window(model))
    return out


def models(http: Callable[..., bytes], base: str) -> List[dict]:
    """LM Studio's own list (GET /api/v1/models): every downloaded model and its loaded instances."""
    data = json.loads(http(base + "/api/v1/models", timeout=10))
    return [m for m in data.get("models", []) if isinstance(m, dict)]


def describe(m: dict) -> dict:
    q = m.get("quantization") or {}
    return {"key": str(m.get("key") or ""), "name": str(m.get("display_name") or m.get("key") or ""),
            "quantization": str(q.get("name") or ""), "size_gb": round((m.get("size_bytes") or 0) / 1e9, 1),
            "params": str(m.get("params_string") or ""), "max_context": int(m.get("max_context_length") or 0),
            # Mixture of experts: LM Studio reports experts for a loaded one; names say "35B-A3B".
            "moe": bool(re.search(r"\ba\d+(\.\d+)?b\b", str(m.get("key") or "").lower()))
                   or any((i.get("config") or {}).get("num_experts") for i in (m.get("loaded_instances") or [])),
            "instances": [{"id": str(i.get("id") or ""), **{k: (i.get("config") or {}).get(k) for k in (
                "context_length", "parallel", "flash_attention", "offload_kv_cache_to_gpu", "num_experts",
                "eval_batch_size")}} for i in (m.get("loaded_instances") or [])]}


def checks(want: Mapping[str, dict], listed: List[dict], vram: float = 0.0) -> List[dict]:
    """Plain warnings ("warn") and notes ("ok", "info") for what is loaded against what is wanted."""
    out: List[dict] = []
    by_key = {m["key"]: m for m in listed}
    loaded = [m for m in listed if m["instances"]]
    for model, w in sorted(want.items()):
        who = ", ".join(w["agents"])
        m = by_key.get(model)
        if m is None:
            out.append({"level": "warn", "text": f"{model} (used by {who}) is not downloaded in LM Studio."})
            continue
        if not m["instances"]:
            out.append({"level": "info", "text": f"{model} is not loaded. The first message loads it with a "
                                                 f"{w['load']:,}-token window, which takes a minute."})
            continue
        if len(m["instances"]) > 1:
            out.append({"level": "warn", "text": f"{model} is loaded {len(m['instances'])} times: each copy uses "
                                                 "its own memory. Unload the extra ones."})
        inst = m["instances"][0]
        ctx, slots = int(inst.get("context_length") or 0), int(inst.get("parallel") or 1)
        if ctx < w["uses"]:
            out.append({"level": "warn", "text": f"{model} is loaded with a {ctx:,}-token window, smaller than the "
                                                 f"{w['uses']:,} the agents use. Long conversations will fail: "
                                                 "reload it with Logos's settings."})
        elif ctx < w["load"]:
            out.append({"level": "info", "text": f"Loaded with {ctx:,} tokens; Logos would load {w['load']:,}, "
                                                 "which leaves room for a second request beside a long one."})
        else:
            out.append({"level": "ok", "text": f"{model}: {ctx:,}-token window, enough for the agents' "
                                               f"{w['uses']:,} plus room beside it."})
        if slots > 1 and ctx:
            out.append({"level": "info", "text": f"LM Studio's {slots} parallel slots share that one window; "
                                                 "the model guard lets at most 2 requests run at once."})
        if inst.get("flash_attention") is False:
            out.append({"level": "warn", "text": "Flash attention is off. Turn it on: it saves memory and is faster."})
        if inst.get("offload_kv_cache_to_gpu") is False:
            out.append({"level": "warn", "text": "The KV cache is kept in RAM, which slows every reply. "
                                                 "Turn on \"Offload KV cache to GPU memory\"."})
        if vram and m["size_gb"] and m["size_gb"] * 1.15 <= vram:
            out.append({"level": "ok", "text": f"The model ({m['size_gb']} GB) fits in the GPU's {vram:g} GB with "
                                               "room for the window: all of it can run on the GPU."})
        elif vram and m["size_gb"] > vram:
            out.append({"level": "info", "text": (
                f"The model file ({m['size_gb']} GB) is larger than the GPU's {vram:g} GB, so part of it runs "
                "from RAM. " + ("Expected for this mixture-of-experts model: keep the experts on the CPU "
                                "and everything else on the GPU (LM Studio app settings below)." if m["moe"]
                                else "Replies will be slow; a smaller quantisation would fit."))})
    for m in loaded:
        if m["key"] not in want:
            out.append({"level": "warn", "text": f"{m['key']} is loaded but no agent uses it: it takes GPU "
                                                 "memory from the agents' model. Unload it."})
    return out


def queue(http: Callable[..., bytes], guard_url: str) -> Optional[dict]:
    """The model guard's queue (``/guard/health``), or None when it cannot be read."""
    try:
        return (json.loads(http(guard_url, timeout=3)) or {}).get("queue")
    except Exception:
        return None


def speed(log_lines: List[str]) -> dict:
    """Replies over the guard's log: counts, median seconds and tokens per second, alone and shared."""
    rows = []
    for line in log_lines:
        line = line.strip()
        if not line.startswith("{") or '"event": "chat"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("status") == 200 and r.get("seconds"):
            rows.append(r)

    def summary(sel: List[dict]) -> dict:
        tps = [r["completion_tokens"] / r["seconds"] for r in sel if r.get("completion_tokens")]
        return {"replies": len(sel),
                "median_seconds": round(statistics.median([r["seconds"] for r in sel]), 1) if sel else None,
                "median_tokens_per_s": round(statistics.median(tps), 1) if tps else None}

    waited = [r.get("waited_s") or 0 for r in rows]
    return {"alone": summary([r for r in rows if not r.get("alongside")]),
            "shared": summary([r for r in rows if r.get("alongside")]),
            "queued": sum(1 for w in waited if w >= 1), "longest_wait_s": max(waited) if waited else 0,
            "since": rows[0]["at"] if rows else None}


def reload(http: Callable[..., bytes], base: str, model: str, window: int) -> dict:
    """Unload every copy of ``model`` and load it once with ``window`` tokens. Returns LM Studio's config."""
    for m in models(http, base):
        if m.get("key") == model:
            for inst in m.get("loaded_instances") or []:
                http(base + "/api/v1/models/unload", data=json.dumps({"instance_id": inst["id"]}).encode(),
                     headers=JSON, timeout=120)
    body = json.dumps({"model": model, "context_length": int(window), "flash_attention": True,
                       "offload_kv_cache_to_gpu": True, "echo_load_config": True}).encode()
    return json.loads(http(base + "/api/v1/models/load", data=body, headers=JSON, timeout=600) or b"{}")


def unload(http: Callable[..., bytes], base: str, instance_id: str, keep: List[str]) -> None:
    """Unload one instance, never one of the agents' models (``keep``)."""
    if instance_id in keep or instance_id.split(":", 1)[0] in keep:
        raise ValueError("that model is in use by the agents")
    http(base + "/api/v1/models/unload", data=json.dumps({"instance_id": instance_id}).encode(),
         headers=JSON, timeout=120)
