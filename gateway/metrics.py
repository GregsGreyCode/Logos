"""Prometheus metrics (F42's last piece, Greg 23 Nov), and the numbers the Agents-today widget shows.

Both come from one place: :func:`agent_rows` (which agents are running, how many messages each sent today
and when the last went: gateway/activity.py) and whether the AI computer answers. The endpoint is for
people who run Prometheus or Grafana at home; the widget is for everyone else.

- **Off by default.** Whoever manages Logos turns it on in Settings › Metrics, which shows a token once.
  Logos keeps only the token's hash (``~/.config/logos/metrics.json``, owner-only). Prometheus sends it as
  ``Authorization: Bearer …`` (its ``authorization`` setting). Off, or a wrong token: the page does not exist (404).
- **Numbers only.** Agent names, counts, times and 0/1 states: never a message, a person, a room or a key.
  The widgets' own failures (``logos_widget_needs``) come from their last copy: this never calls a service.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from aiohttp import web

CONFIG_FILE = "metrics.json"
PATH = "/metrics"


def config_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos" / CONFIG_FILE


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        data = json.loads((path or config_path()).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: Mapping[str, Any], path: Optional[Path] = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_ok(conf: Mapping[str, Any], header: str) -> bool:
    """The Authorization header carries the token whose hash is kept (and the endpoint is on)."""
    if not conf.get("enabled") or not conf.get("hash"):
        return False
    scheme, _, token = (header or "").partition(" ")
    return scheme.lower() == "bearer" and bool(token) and hmac.compare_digest(_hash(token.strip()), str(conf["hash"]))


# ── the numbers ──────────────────────────────────────────────────────────────────────────────

async def agent_rows(app: web.Application, names: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[dict]:
    """Per agent: display name, whether its sandbox runs, messages sent today, the last one's time, the total.
    ``names`` is {name: {"definition": …}} (the agents one person may see); None means every agent."""
    from gateway import agent_chat
    activity = app.get("activity")
    backend = app.get("sandbox_backend")
    runtime = agent_chat.runtime_of(backend)
    if names is None:
        from gateway import dashboard_api
        names = {}
        if runtime is not None:
            shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, dashboard_api.auth_db)
            for n in shown:
                names[n] = {"definition": await asyncio.to_thread(runtime.load, n)}
    try:
        states = {st.name: st for st in await asyncio.to_thread(backend.list)} if backend else {}
    except Exception:
        states = {}
    rows = []
    for name, info in sorted(names.items()):
        d = info.get("definition") or {}
        today = activity.today(name) if activity else {"messages": 0, "last": 0}
        rows.append({"agent": name, "name": str(d.get("display_name") or name), "ready": bool(states.get(name) and states[name].running),
                     "messages": today["messages"], "last": today["last"] or (activity.last_ever(name) if activity else 0),
                     "total": activity.total(name) if activity else 0, "today": today["last"] != 0})
    return rows


async def model_up(app: web.Application, names: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Optional[bool]:
    """Whether the AI computer answers (any local model server of those agents); None when there is none."""
    from gateway import dashboard_api
    if names is None:
        from gateway import agent_chat
        runtime = agent_chat.runtime_of(app.get("sandbox_backend"))
        if runtime is None:
            return None
        shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, dashboard_api.auth_db)
        names = {n: {"definition": await asyncio.to_thread(runtime.load, n)} for n in shown}
    ups = [await dashboard_api.model_server_up(app, i["definition"]) for i in names.values()]
    return True if any(u is True for u in ups) else (False if any(u is False for u in ups) else None)


# ── the exposition format ────────────────────────────────────────────────────────────────────

def esc(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(rows: List[dict], model: Optional[bool], needs: Mapping[str, bool], version: Mapping[str, Any],
           vaults: Optional[Mapping[str, Optional[Mapping[str, int]]]] = None, guard: Optional[Mapping[str, Any]] = None,
           extra: Optional[Mapping[str, Any]] = None) -> str:
    out: List[str] = []

    def metric(name: str, kind: str, help_: str, samples: List[tuple]) -> None:
        if not samples:
            return
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
            out.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    metric("logos_info", "gauge", "Which Logos this is.", [({"version": version.get("commit") or "unknown"}, 1)])
    metric("logos_agent_up", "gauge", "1 when the agent's sandbox is running.", [({"agent": r["agent"]}, int(r["ready"])) for r in rows])
    metric("logos_agent_messages_today", "gauge", "Messages the agent sent today (counts only).", [({"agent": r["agent"]}, r["messages"]) for r in rows])
    metric("logos_agent_messages_total", "counter", "Messages the agent has sent since Logos began counting.", [({"agent": r["agent"]}, r["total"]) for r in rows])
    metric("logos_agent_last_message_timestamp_seconds", "gauge", "When the agent last sent a message (0: never).", [({"agent": r["agent"]}, r["last"] or 0) for r in rows])
    if model is not None:
        metric("logos_model_up", "gauge", "1 when the AI computer (the local model server) answers.", [({}, int(model))])
    g = guard or {}
    q = g.get("queue") or {}
    if g:
        metric("logos_model_server_off", "gauge", "1 when the model guard found the model server off (the GPU computer is not answering).", [({}, int(bool(g.get("off"))))])
        metric("logos_model_server_off_since_timestamp_seconds", "gauge", "When the model server was first seen off (0: it is on).", [({}, int(g.get("off_since") or 0))])
    if q:
        metric("logos_model_requests_running", "gauge", "Model requests being worked on now.", [({}, q.get("running", 0))])
        metric("logos_model_requests_waiting", "gauge", "Model requests waiting their turn in the guard's queue.", [({}, q.get("waiting", 0))])
        metric("logos_model_slots", "gauge", "How many model requests the guard lets run at once.", [({}, q.get("slots", 0))])
        metric("logos_model_context_tokens_running", "gauge", "Context tokens of the requests running now.", [({}, q.get("tokens_running", 0))])
        metric("logos_model_context_window_tokens", "gauge", "The loaded model's context window (0: unknown).", [({}, q.get("window", 0))])
        metric("logos_model_requests_served_total", "counter", "Model requests served since the guard started.", [({}, q.get("served", 0))])
        metric("logos_model_requests_queued_total", "counter", "Of those, requests that had to wait.", [({}, q.get("queued", 0))])
        metric("logos_model_wait_seconds_avg", "gauge", "Average time a request waited since the guard started.", [({}, q.get("wait_avg_s", 0))])
        metric("logos_model_wait_seconds_max", "gauge", "Longest a request waited since the guard started.", [({}, q.get("wait_max_s", 0))])
    v = {a: st for a, st in (vaults or {}).items() if st}
    metric("logos_agent_home_bytes", "gauge", "Disk the agent's home volume uses (its settings, sessions, vault and cache).", [({"agent": a}, st["home_kb"] * 1024) for a, st in v.items() if "home_kb" in st])
    metric("logos_vault_files", "gauge", "Files kept in the agent's vault (photos and documents it was sent).", [({"agent": a}, st["files"]) for a, st in v.items()])
    metric("logos_vault_bytes", "gauge", "Disk the agent's vault uses.", [({"agent": a}, st.get("bytes", 0)) for a, st in v.items()])
    metric("logos_vault_files_by_state", "gauge", "Vault files by what has been done: new (waiting for a note), retry, done, failed.",
           [({"agent": a, "state": k}, st.get("state_" + k, 0)) for a, st in v.items() for k in ("new", "retry", "done", "failed")])
    metric("logos_vault_oldest_file_timestamp_seconds", "gauge", "When the oldest vault file was kept (0: none).", [({"agent": a}, st.get("oldest", 0)) for a, st in v.items()])
    metric("logos_vault_newest_file_timestamp_seconds", "gauge", "When the newest vault file was kept (0: none).", [({"agent": a}, st.get("newest", 0)) for a, st in v.items()])
    x = extra or {}
    jobs = [(a, name, j) for a, js in (x.get("jobs") or {}).items() for name, j in sorted((js or {}).items())]
    metric("logos_job_last_run_timestamp_seconds", "gauge", "When the agent's scheduled job last ran.", [({"agent": a, "job": n}, j["last"]) for a, n, j in jobs])
    metric("logos_job_last_success_timestamp_seconds", "gauge", "When the job last finished without error (0: not in the history kept).", [({"agent": a, "job": n}, j["ok"]) for a, n, j in jobs])
    metric("logos_job_last_run_failed", "gauge", "1 when the job's latest run failed or was missed.", [({"agent": a, "job": n}, j["failed"]) for a, n, j in jobs])
    metric("logos_job_runs_24h", "gauge", "Runs of the job in the last 24 hours.", [({"agent": a, "job": n}, j["runs24"]) for a, n, j in jobs])
    metric("logos_job_failures_24h", "gauge", "Of those, runs that failed or were missed.", [({"agent": a, "job": n}, j["fail24"]) for a, n, j in jobs])
    br = {a: b for a, b in (x.get("brain") or {}).items() if b}
    metric("logos_brain_pages", "gauge", "Pages in the agent's brain.", [({"agent": a}, b["pages"]) for a, b in br.items()])
    metric("logos_brain_bytes", "gauge", "Disk the agent's brain uses (with its history).", [({"agent": a}, b.get("kb", 0) * 1024) for a, b in br.items()])
    metric("logos_brain_last_commit_timestamp_seconds", "gauge", "When the brain was last saved to its history (0: never).", [({"agent": a}, b.get("last_commit", 0)) for a, b in br.items()])
    metric("logos_brain_sync_conflicts", "gauge", "Conflict copies Syncthing made in the brain (the same page changed in two places).", [({"agent": a}, b.get("conflicts", 0)) for a, b in br.items()])
    if x.get("sync_up") is not None:
        metric("logos_sync_up", "gauge", "1 when Syncthing (the brains' sync to phones) answers.", [({}, int(bool(x["sync_up"])))])
    if x.get("sync_behind") is not None:
        metric("logos_sync_devices_behind", "gauge", "Phones that lack some of the brain's changes and have been quiet for hours.", [({"agent": a}, n) for a, n in sorted(x["sync_behind"].items())])
    metric("logos_noticeboard_open_requests", "gauge", "Requests waiting on the agent's person (questions, shared changes, reminders).", [({"agent": a}, n) for a, n in sorted((x.get("requests") or {}).items())])
    metric("logos_widget_needs", "gauge", "1 when the Home widget's last check needs someone.", [({"widget": k}, int(v)) for k, v in sorted(needs.items())])
    return "\n".join(out) + "\n"


REQUEST_KINDS = ("question", "shared_change", "reminder")


async def gather_extra(app: web.Application, runtime: Any, rows: List[dict]) -> Dict[str, Any]:
    """Jobs, brains, Syncthing and the noticeboard; whatever cannot be read is left out, never invented."""
    from gateway import agent_stats, sync_status
    out: Dict[str, Any] = {"jobs": {}, "brain": {}, "sync_up": None, "sync_behind": None, "requests": {}}
    if runtime is None:
        return out
    for r in rows:
        out["jobs"][r["agent"]] = await asyncio.to_thread(agent_stats.jobs, runtime, r["agent"])
        out["brain"][r["agent"]] = await asyncio.to_thread(agent_stats.brain, runtime, r["agent"])
    try:
        up = bool(await asyncio.to_thread(sync_status.api_key, runtime.runner))
        out["sync_up"] = up
        if up:
            behind = await asyncio.to_thread(sync_status.behind, runtime.runner, time.time())
            counts = {r["agent"]: 0 for r in rows}
            for b in behind:
                if b.get("agent") in counts:
                    counts[b["agent"]] += 1
            out["sync_behind"] = counts
    except Exception:
        pass
    client = app.get("noticeboard_client")
    if client is None:
        try:
            from gateway.noticeboard_client import NoticeboardClient
            from gateway import dashboard_api
            client = dashboard_api._board(type("R", (), {"app": app})())
        except Exception:
            client = None
    if client is not None:
        for r in rows:
            try:
                out["requests"][r["agent"]] = sum(1 for m in await client.history(r["agent"])
                                                  if m.get("kind") in REQUEST_KINDS and m.get("status") == "open" and m.get("to") == r["agent"])
            except Exception:
                pass
    return out


async def handle_metrics(request: web.Request) -> web.Response:
    """GET /metrics — Prometheus text; 404 unless switched on and the right token is sent."""
    if not token_ok(load(request.app["metrics_path"]), request.headers.get("Authorization", "")):
        raise web.HTTPNotFound()
    from gateway import dashboard_api
    rows = await agent_rows(request.app)
    model = await model_up(request.app)
    w = request.app.get("widgets")
    needs = {k: ((c.get("data") or {}).get("state") == "needs") for k, c in (w.cache.items() if w else [])}
    from gateway import agent_chat, vault_stats
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    vaults = {r["agent"]: await asyncio.to_thread(vault_stats.read, runtime, r["agent"]) for r in rows} if runtime is not None else {}
    guard = None
    if runtime is not None:
        try:
            from gateway import model_gateway
            url = f"http://{model_gateway.ModelGateway(runtime).bind_address()}:{model_gateway.GUARD_PORT}/guard/health"
            h = json.loads(await asyncio.to_thread(runtime.runner.http, url, timeout=3))
            guard = {"off": bool(h.get("model_server_off")), "off_since": h.get("off_since"), "queue": h.get("queue")}
        except Exception:
            guard = None                                      # no guard (or it is down): no model queue numbers
    extra = await gather_extra(request.app, runtime, rows)
    body = render(rows, model, needs, dashboard_api.version(), vaults, guard, extra)
    voice = request.app.get("voice_stats")                    # the house speaker's answer times (voice_converse)
    if voice is not None:
        body += voice.render()
    servers = request.app.get("model_server_metrics")          # the model PC's own numbers (model_server_metrics)
    if servers is not None:
        body += await servers.render(runtime)
    ledger = request.app.get("local_cost")                    # local vs cloud: energy, cost, tokens (local_cost)
    if ledger is not None:
        body += await asyncio.to_thread(ledger.render)
    return web.Response(text=body, content_type="text/plain", charset="utf-8", headers={"Cache-Control": "no-store"})


# ── switching it on, in Settings (whoever manages Logos) ─────────────────────────────────────

def _admin(request: web.Request) -> bool:
    from gateway.dashboard_api import _user
    return _user(request)["role"] == "admin"


async def handle_status(request: web.Request) -> web.Response:
    """GET /api/v2/metrics — {enabled, since}; never the token."""
    if not _admin(request):
        raise web.HTTPForbidden()
    c = load(request.app["metrics_path"])
    return web.json_response({"enabled": bool(c.get("enabled") and c.get("hash")), "since": c.get("since")})


async def handle_set(request: web.Request) -> web.Response:
    """PUT /api/v2/metrics {enabled, rotate?} — on (a token, shown once), off, or a new token."""
    if not _admin(request):
        raise web.HTTPForbidden()
    try:
        body = await request.json()
        enabled = bool(body["enabled"])
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest()
    path = request.app["metrics_path"]
    conf = load(path)
    token = None
    if enabled and (not conf.get("hash") or body.get("rotate") or not conf.get("enabled")):
        token = secrets.token_urlsafe(32)                       # shown now, never again: only its hash is kept
        conf.update(hash=_hash(token), since=int(time.time()))
    conf["enabled"] = enabled
    if not enabled:
        conf.pop("hash", None)                                  # off forgets the token: on again is a new one
        conf.pop("since", None)
    save(conf, path)
    return web.json_response({"enabled": enabled, "token": token, "since": conf.get("since")})


def register(app: web.Application, path: Optional[Path] = None) -> None:
    from gateway.auth.middleware import require_csrf
    from gateway.model_server_metrics import Collector
    app["metrics_path"] = path or config_path()
    app["model_server_metrics"] = Collector()
    app.router.add_get(PATH, handle_metrics)
    app.router.add_get("/api/v2/metrics", handle_status)
    app.router.add_put("/api/v2/metrics", require_csrf(handle_set))
