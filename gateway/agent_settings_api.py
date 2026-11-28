"""Agent settings in the dashboard (story E6): an agent's personality, model and chat settings,
changed by the people it belongs to (and by whoever manages Logos), then "Save and restart".

    GET  /api/v2/agents/{name}/settings                    everything the page shows
    PUT  /api/v2/agents/{name}/settings                    {soul | soul_reset | soul_restore, model,
                                                            display_name, voice_echo, speaker_name, confirm}
    GET  /api/v2/agents/{name}/settings/soul/{version}     one earlier version of the personality
    POST /api/v2/agents/{name}/settings/apply              recreate the sandbox (scripts/logos-agent up)
    GET  /api/v2/agents/{name}/settings/apply              how that is going

Everything is checked here, whatever the page allowed; the choices are kept outside the repo by
:mod:`gateway.agent_settings`. "Apply" runs ``scripts/logos-agent up <name> --no-model-load``, the
same code path as by hand, in the background: never while the agent is writing a reply (it waits
up to WAIT_IDLE_S for it to finish), one agent at a time, and LM Studio is left alone (the model
guard loads the model at its window when the agent next asks). See docs/agent-settings.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml
from aiohttp import web

import gateway.auth.db as auth_db
from gateway import agent_chat
from gateway import agent_settings as S
from gateway.lib.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

WAIT_IDLE_S = 180.0          # how long "apply" waits for a reply in progress to finish
POLL_S = 5.0
UP_TIMEOUT_S = 600.0
MAX_LINES = 40
RUNNING = ("waiting", "restarting")
CHAT_KEYS = ("display_name", "voice_echo", "speaker_name")


def _err(status: int, error: str, message: str = "", **extra) -> web.Response:
    return web.json_response({"error": error, "message": message, **extra}, status=status)


def _runtime(request: web.Request):
    return agent_chat.runtime_of(request.app.get("sandbox_backend"))


async def _agent(request: web.Request):
    """(name, owners, user, runtime) for an agent this person may change: one they may use, or any
    shown agent for an admin. Anything else is a 404, as for the agent's other pages."""
    from gateway import dashboard_api as d
    user = d._user(request)
    name = request.match_info["name"]
    runtime = _runtime(request)
    if runtime is None:
        raise web.HTTPServiceUnavailable(text='{"error":"no_backend"}', content_type="application/json")
    shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, auth_db)
    if name not in shown or not (agent_chat.may_use(shown[name], user["username"]) or user["role"] == "admin"):
        raise web.HTTPNotFound(text='{"error":"not_found"}', content_type="application/json")
    return name, shown[name], user, runtime


# ── what is set where ────────────────────────────────────────────────────────────────────────

def locked(runtime: Any, name: str) -> Dict[str, str]:
    """Settings this machine's own agents/*.local.yaml files fix (they win over the dashboard)."""
    from gateway import agent_runtime as ar
    path = runtime.definition_path(name)
    out: Dict[str, str] = {}
    for local in (path.with_name(ar.ALL_LOCAL), path.with_suffix(".local.yaml")):
        try:
            data = (yaml.safe_load(local.read_text()) or {}) if local.exists() else {}
        except (OSError, yaml.YAMLError):
            data = {}
        if not isinstance(data, dict):
            continue
        why = f"Set on this machine in agents/{local.name}, which comes first."
        for key in CHAT_KEYS:
            if key in data:
                out[key] = why
        if "speaker_wake" in data:
            out["speaker_name"] = why
        model = data.get("model") or {}
        if isinstance(model, dict) and ("default" in model or "context_length" in model):
            out["model"] = why
    return out


def listing(runtime: Any, definition: Mapping[str, Any]) -> Optional[dict]:
    """LM Studio's list of models (a GET; nothing is loaded), or None when it does not answer."""
    from gateway import local_models
    base = local_models.server(definition.get("model") or {})
    if not base:
        return None
    try:
        return json.loads(runtime.runner.http(base + "/api/v0/models", timeout=4))
    except Exception:
        return None


def _others(runtime: Any, name: str) -> Dict[str, Mapping[str, Any]]:
    """The other agents' model blocks (they share the model server)."""
    out = {}
    for other in runtime.defined():
        if other == name:
            continue
        try:
            d = runtime.load(other)
        except Exception:
            continue
        if d.get("sandbox", True) is not False and d.get("dashboard") is not False:
            out[other] = d.get("model") or {}
    return out


def _speaks(definition: Mapping[str, Any]) -> bool:
    from gateway import tts_host, wake_host
    return wake_host.uses_wake(definition) or tts_host.uses_speaker(definition)


def _taken_wake_words(runtime: Any, name: str) -> Dict[str, str]:
    from gateway import wake_host
    taken = {}
    for other in runtime.defined():
        if other == name:
            continue
        try:
            d = runtime.load(other)
        except Exception:
            continue
        for p in wake_host.phrases_of(other, d):
            taken[S.wake_key(p["phrase"])] = str(d.get("display_name") or other)
    return taken


def settings_view(runtime: Any, name: str, owners: Optional[List[str]], models: Optional[dict]) -> dict:
    """Everything the Settings page shows for one agent. Blocking."""
    from gateway import agent_runtime as ar, wake_host
    env = runtime.env
    agent, plain = runtime.load(name), runtime.load(name, settings=False)
    lock = locked(runtime, name)
    own = S.own_soul(runtime.repo, env, agent)
    total = len(runtime.soul_text(agent))
    m, pm = agent.get("model") or {}, plain.get("model") or {}
    over = S.overlay(env, name)
    chat: Dict[str, Any] = {"display_name": {"value": str(agent.get("display_name") or name),
                                             "default": str(plain.get("display_name") or name),
                                             "locked": lock.get("display_name")}}
    if agent.get("voice"):
        chat["voice_echo"] = {"value": bool(agent.get("voice_echo")), "default": bool(plain.get("voice_echo")),
                              "locked": lock.get("voice_echo")}
    if _speaks(plain):
        chat["speaker_name"] = {"value": str(agent.get("speaker_name") or ""), "default": str(plain.get("speaker_name") or ""),
                                "locked": lock.get("speaker_name"),
                                "wake": [p["phrase"] for p in wake_host.phrases_of(name, agent)]}
    return {
        "name": name, "display_name": str(agent.get("display_name") or name),
        "shared": owners is None or len(owners) > 1,
        "soul": {"text": own, "default": S.default_soul(runtime.repo, agent), "custom": S.soul_file(env, name) is not None,
                 "limit": S.SOUL_LIMIT, "total": total, "total_limit": ar.SOUL_MAX_CHARS,
                 "history": S.history(env, name)},
        "model": {"default": str(m.get("default") or ""), "context_length": int(m.get("context_length") or 0),
                  "vision": bool(m.get("vision")), "local": m.get("provider") == "lmstudio",
                  "definition": {"default": str(pm.get("default") or ""), "context_length": int(pm.get("context_length") or 0)},
                  "custom": "model" in over, "locked": lock.get("model"),
                  "server_up": models is not None, "choices": S.model_choices(models) if models is not None else [],
                  "context_choices": list(S.CONTEXT_CHOICES)},
        "chat": chat,
        "pending": S.pending(env, name),
    }


def change(runtime: Any, name: str, body: Mapping[str, Any], by: str, models: Optional[dict]) -> dict:
    """Check every change in ``body`` first, then keep them all. Blocking.

    Returns {"changed": [...], "warnings": [...]} or, when a warning must be agreed to and
    ``body.confirm`` is not set, {"confirm": [...warnings]} with nothing kept."""
    from gateway import agent_runtime as ar, model_gateway
    env = runtime.env
    agent, plain = runtime.load(name), runtime.load(name, settings=False)
    lock = locked(runtime, name)
    over = S.overlay(env, name)
    new = json.loads(json.dumps(over))
    changed: List[str] = []
    warnings: List[dict] = []

    def refuse_locked(key: str) -> None:
        if lock.get(key):
            raise S.Invalid(lock[key])

    soul_op = None
    if "soul" in body:
        text = S.check_soul(body["soul"], redact_sensitive_text)
        soul_op = ("save", text, "edited")
    elif body.get("soul_reset"):
        soul_op = ("reset",)
    elif body.get("soul_restore"):
        text = S.check_soul(S.history_text(env, name, str(body["soul_restore"])), redact_sensitive_text)
        soul_op = ("save", text, "restored")
    if soul_op and soul_op[0] == "save":
        own = S.own_soul(runtime.repo, env, agent)
        total = len(runtime.soul_text(agent)) - len(own.rstrip()) + len(soul_op[1].rstrip())
        if total > ar.SOUL_MAX_CHARS:
            raise S.Invalid(f"With the shared rules that comes to {total:,} characters; the most is {ar.SOUL_MAX_CHARS:,}. Shorten it.")

    if "display_name" in body:
        refuse_locked("display_name")
        value = S.check_display_name(body["display_name"])
        if value == str(plain.get("display_name") or name):
            new.pop("display_name", None)
        else:
            new["display_name"] = value
    if "voice_echo" in body:
        refuse_locked("voice_echo")
        if not agent.get("voice"):
            raise S.Invalid("This agent does not take voice notes.")
        if not isinstance(body["voice_echo"], bool):
            raise S.Invalid("Voice note text is on or off.")
        if body["voice_echo"] == bool(plain.get("voice_echo")):
            new.pop("voice_echo", None)
        else:
            new["voice_echo"] = body["voice_echo"]
    if "speaker_name" in body:
        refuse_locked("speaker_name")
        if not _speaks(plain):
            raise S.Invalid("This agent does not speak on the house speaker.")
        value = S.check_speaker_name(body["speaker_name"], _taken_wake_words(runtime, name))
        if value == str(plain.get("speaker_name") or ""):
            new.pop("speaker_name", None)
            new.pop("speaker_wake", None)
        else:
            # "Hey <name>" only: a bare name wakes the speaker by accident too easily.
            new["speaker_name"], new["speaker_wake"] = value, [f"Hey {value}"]
    if "model" in body:
        refuse_locked("model")
        if (plain.get("model") or {}).get("provider") != "lmstudio":
            raise S.Invalid("This agent's model is not on the local model server, so it is not changed here.")
        if models is None:
            raise S.Invalid("The model server is not answering, so the models cannot be checked. Try again when it is on.")
        block, warns = S.check_model(body["model"], S.model_choices(models), agent.get("model") or {},
                                     _others(runtime, name))
        warnings += warns
        pm = plain.get("model") or {}
        if block["default"] == str(pm.get("default") or ""):
            block.pop("vision")                  # the definition's own model: it says whether it sees
            if block["context_length"] == int(pm.get("context_length") or 0):
                block = {}
        if block:
            new["model"] = block
        else:
            new.pop("model", None)
        merged = json.loads(json.dumps(plain))
        ar.merge(merged, {"model": new.get("model") or {}})
        try:
            model_gateway.require_local_only(name, merged)
        except ValueError as e:
            raise S.Invalid(str(e))

    needs = [w for w in warnings if w.get("confirm")]
    if needs and not body.get("confirm"):
        return {"confirm": warnings}

    if new != over:
        changed += sorted(k for k in set(new) | set(over) if new.get(k) != over.get(k))
        S.save_overlay(env, name, new)
    if soul_op:
        row = (S.save_soul(env, runtime.repo, agent, soul_op[1], by, soul_op[2]) if soul_op[0] == "save"
               else S.reset_soul(env, runtime.repo, agent, by))
        if row is not None:
            changed.append("soul")
    return {"changed": changed, "warnings": warnings}


# ── handlers ─────────────────────────────────────────────────────────────────────────────────

async def handle_settings(request: web.Request) -> web.Response:
    name, owners, user, runtime = await _agent(request)
    jobs = request.app.setdefault("agent_settings_jobs", {})
    definition = await asyncio.to_thread(runtime.load, name)
    # LM Studio's list, kept for 20 s: an AI computer that is off can take seconds to not answer.
    cache = request.app.setdefault("agent_settings_models", {})
    base = str((definition.get("model") or {}).get("base_url") or "")
    hit = cache.get(base)
    if hit and time.monotonic() - hit[0] < 20 and not (request.method == "PUT" and hit[1] is None):
        models = hit[1]
    else:
        models = await asyncio.to_thread(listing, runtime, definition)
        cache[base] = (time.monotonic(), models)
    if request.method == "GET":
        view = await asyncio.to_thread(settings_view, runtime, name, owners, models)
        return web.json_response({**view, "apply": job_view(jobs.get(name))})
    if (jobs.get(name) or {}).get("state") in RUNNING:
        return _err(409, "restarting", "The agent is restarting. Change it again once it is back.")
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not body:
        return _err(400, "bad_request", "Nothing to change.")
    try:
        done = await asyncio.to_thread(change, runtime, name, body, user["username"], models)
    except S.Invalid as e:
        return _err(400, "invalid", str(e))
    if "confirm" in done:
        return _err(409, "confirm", "Check these first.", warnings=done["confirm"])
    if done["changed"]:
        auth_db.write_audit_log(user["id"], "agent_settings", target_type="agent", target_id=name,
                                metadata={"changed": done["changed"]}, ip_address=request.remote)
        logger.info("agent settings: %s changed %s for %s", user["username"], ", ".join(done["changed"]), name)
    view = await asyncio.to_thread(settings_view, runtime, name, owners, models)
    return web.json_response({**view, "apply": job_view(jobs.get(name)), "changed": done["changed"], "warnings": done["warnings"]})


async def handle_soul_version(request: web.Request) -> web.Response:
    name, _owners, _user, runtime = await _agent(request)
    try:
        text = await asyncio.to_thread(S.history_text, runtime.env, name, request.match_info["version"])
    except S.Invalid as e:
        return _err(404, "not_found", str(e))
    return web.json_response({"text": text})


def job_view(job: Optional[Mapping[str, Any]]) -> dict:
    if not job:
        return {"state": "idle"}
    end = job.get("finished") or time.time()
    return {k: job.get(k) for k in ("state", "message", "started", "finished", "by")} | {
        "seconds": int(end - job["started"]), "lines": list(job.get("lines") or [])[-12:]}


async def is_busy(app: web.Application, runtime: Any, name: str) -> bool:
    """Is the agent in the middle of a reply? Its own conversations say (as for the logo's
    "working"); when it cannot be asked, any reply the model guard is writing counts."""
    from gateway import dashboard_api as d
    links = app.get("agent_links")
    if links is not None:
        try:
            return await d.agent_is_working(await links.client(name), time.time())
        except Exception as e:
            logger.info("agent settings: could not ask %s whether it is busy: %s", name, type(e).__name__)
    progress = await asyncio.to_thread(d.model_progress, runtime)
    return bool(progress.get("live"))


async def run_up(runtime: Any, name: str, job: dict) -> int:
    """``scripts/logos-agent up <name> --no-model-load``, its output into ``job["lines"]``."""
    script = Path(runtime.repo) / "scripts" / "logos-agent"
    env = None if runtime.env is os.environ else {**os.environ, **dict(runtime.env)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), "up", name, "--no-model-load", cwd=str(runtime.repo), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    async def read() -> None:
        async for raw in proc.stdout:
            line = redact_sensitive_text(raw.decode(errors="replace").rstrip())[:300]
            if line:
                job["lines"] = (job["lines"] + [line])[-MAX_LINES:]
    try:
        await asyncio.wait_for(read(), UP_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        job["lines"].append("Stopped waiting after ten minutes.")
    return await proc.wait()


async def _apply(app: web.Application, runtime: Any, name: str, job: dict, user: Mapping[str, str], ip: Any) -> None:
    shown = str((await asyncio.to_thread(runtime.load, name)).get("display_name") or name)
    try:
        deadline = time.monotonic() + WAIT_IDLE_S
        while await is_busy(app, runtime, name):
            if time.monotonic() > deadline:
                job.update(state="failed", message=f"{shown} was still answering after {int(WAIT_IDLE_S // 60)} minutes, "
                                                   "so it was not restarted. Your settings are saved: try again in a while.")
                return
            job.update(state="waiting", message=f"Waiting for {shown} to finish its reply…")
            await asyncio.sleep(POLL_S)
        job.update(state="restarting", message=f"Recreating {shown}: about a minute without replies.")
        rc = await run_up(runtime, name, job)
        if rc == 0:
            job.update(state="done", message=f"{shown} is back, with the new settings.")
        else:
            job.update(state="failed", message=f"{shown} did not come back cleanly. The details are below; "
                                               "your settings are saved.")
        for key in ("models_overview", "activity_cache"):
            app.pop(key, None)
    except Exception as e:
        logger.exception("agent settings: applying for %s failed", name)
        job.update(state="failed", message=f"It did not work ({type(e).__name__}). Your settings are saved.")
    finally:
        job["finished"] = time.time()
        auth_db.write_audit_log(user["id"], "agent_restart", target_type="agent", target_id=name,
                                metadata={"result": job["state"], "seconds": int(job["finished"] - job["started"])},
                                ip_address=ip)


async def handle_apply(request: web.Request) -> web.Response:
    name, _owners, user, runtime = await _agent(request)
    jobs = request.app.setdefault("agent_settings_jobs", {})
    if request.method == "GET":
        return web.json_response(job_view(jobs.get(name)))
    busy = next((n for n, j in jobs.items() if j.get("state") in RUNNING), None)
    if busy:
        # `up` also refreshes the shared services (gateway, guard, wake words): one at a time.
        return _err(409, "busy", "An agent is restarting already. Try again when it is back.", agent=busy)
    job = {"state": "waiting", "message": "Starting…", "started": time.time(), "finished": None,
           "lines": [], "by": user["username"]}
    jobs[name] = job
    tasks = request.app.setdefault("agent_settings_tasks", set())
    task = asyncio.create_task(_apply(request.app, runtime, name, job, user, request.remote))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return web.json_response(job_view(job), status=202)


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/agents/{name}/settings", handle_settings)
    app.router.add_put("/api/v2/agents/{name}/settings", require_csrf(handle_settings))
    app.router.add_get("/api/v2/agents/{name}/settings/soul/{version}", handle_soul_version)
    app.router.add_get("/api/v2/agents/{name}/settings/apply", handle_apply)
    app.router.add_post("/api/v2/agents/{name}/settings/apply", require_csrf(handle_apply))
