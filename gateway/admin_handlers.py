"""Admin handlers: machines, routing policies, user-policy assignment, routing resolver."""

import json
import logging
import os
import time as _time

import aiohttp
from aiohttp import web

import gateway.auth.db as auth_db

logger = logging.getLogger(__name__)

# ── Health cache ───────────────────────────────────────────────────────────────
# Maps endpoint_url → (result_dict, unix_timestamp)
# Used by resolve_route() so repeated routing decisions don't spam /health.
_HEALTH_CACHE_TTL_OK: float = 60.0    # healthy results cached 60 s
_HEALTH_CACHE_TTL_FAIL: float = 5.0   # failed/unreachable results re-checked every 5 s
_health_cache: dict[str, tuple[dict, float]] = {}


class RoutingError(Exception):
    """Raised by resolve_route() when no machine can satisfy the request."""
    def __init__(self, reason: str, profile_name: str | None = None):
        super().__init__(reason)
        self.profile_name = profile_name

KNOWN_MODEL_CLASSES = [
    "lightweight", "coding", "embedding", "vision", "reasoning", "general",
]

# Maps route alias → model class (mirrors providers.yaml route_model_classes)
ALIAS_TO_CLASS: dict[str, str] = {
    "fast":     "lightweight",
    "small":    "lightweight",
    "short":    "lightweight",
    "balanced": "general",
    "medium":   "general",
    "large":    "general",
    "long":     "general",
    "coding":   "coding",
    "coder":    "coding",
    "brain":    "reasoning",
    "gpt20b":   "general",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _caps_as_classes(caps: list) -> list[str]:
    """Normalise machine_capabilities rows to a simple list of class strings."""
    return [
        (c["model_class"] if isinstance(c, dict) else str(c))
        for c in caps
    ]


async def _probe_health(endpoint_url: str) -> dict:
    base = endpoint_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]

    api_key = os.environ.get("OPENAI_API_KEY", "")
    auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "ollama" else {}

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Try /health (works for Ollama, internal Hermes nodes — no auth needed)
            try:
                async with session.get(
                    f"{base}/health", timeout=aiohttp.ClientTimeout(total=3)
                ) as r:
                    if r.ok:
                        return {"status": "ok", "http": r.status}
            except Exception:
                pass

            # 2. Fall back to /v1/models with API key (LM Studio, OpenAI-compatible servers)
            for headers in (auth_headers, {}):
                try:
                    async with session.get(
                        f"{base}/v1/models", headers=headers,
                        timeout=aiohttp.ClientTimeout(total=3)
                    ) as r:
                        if r.ok:
                            return {"status": "ok", "http": r.status}
                        if r.status not in (401, 403):
                            return {"status": "degraded", "http": r.status}
                except Exception:
                    pass
                if not headers:
                    break

            return {"status": "unreachable", "error": "no reachable endpoint"}
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)[:80]}


async def _probe_health_cached(endpoint_url: str) -> dict:
    """Return cached health result; TTL is 60 s for ok, 5 s for failures."""
    now = _time.time()
    cached = _health_cache.get(endpoint_url)
    if cached:
        result, ts = cached
        ttl = _HEALTH_CACHE_TTL_OK if result.get("status") == "ok" else _HEALTH_CACHE_TTL_FAIL
        if (now - ts) < ttl:
            return result
    result = await _probe_health(endpoint_url)
    _health_cache[endpoint_url] = (result, now)
    return result


def _invalidate_health_cache(endpoint_url: str) -> None:
    _health_cache.pop(endpoint_url, None)


# ── Model classes ─────────────────────────────────────────────────────────────

async def handle_model_classes(request: web.Request) -> web.Response:
    return web.json_response({"model_classes": KNOWN_MODEL_CLASSES})


# ── Machines ──────────────────────────────────────────────────────────────────

async def handle_machines_list(request: web.Request) -> web.Response:
    machines = auth_db.list_machines()
    for m in machines:
        m["capabilities"]  = _caps_as_classes(auth_db.get_machine_capabilities(m["id"]))
        m["profile_count"] = auth_db.count_profiles_using_machine(m["id"])
        # Never expose raw api_key — only signal whether one is stored
        m["has_api_key"] = bool(m.pop("api_key", None))
    return web.json_response({"machines": machines})


async def handle_machines_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not body.get("name") or not body.get("endpoint_url"):
        return web.json_response(
            {"error": "missing_fields", "required": ["name", "endpoint_url"]}, status=400
        )

    try:
        machine = auth_db.create_machine(
            name=body["name"],
            endpoint_url=body["endpoint_url"],
            description=body.get("description"),
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            return web.json_response({"error": "name_exists"}, status=409)
        raise

    auth_db.write_audit_log(
        request["current_user"]["sub"], "create_machine",
        target_type="machine", target_id=machine["id"],
        metadata={"name": machine["name"]},
        ip_address=request.remote,
    )
    machine["capabilities"] = []
    return web.json_response({"machine": machine}, status=201)


async def handle_machines_patch(request: web.Request) -> web.Response:
    mid = request.match_info["id"]
    machine = auth_db.get_machine(mid)
    if not machine:
        raise web.HTTPNotFound(reason="machine_not_found")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    updates = {k: body[k] for k in ("name", "endpoint_url", "description", "enabled", "default_model", "api_key") if k in body}
    if updates:
        auth_db.update_machine(mid, **updates)
        auth_db.write_audit_log(
            request["current_user"]["sub"], "update_machine",
            target_type="machine", target_id=mid,
            metadata=updates, ip_address=request.remote,
        )

        # Propagate credential / URL changes to every OpenShell sub-gateway's
        # provider record. Without this hook, rotating the LM Studio API key
        # (or moving the LM Studio host) silently leaves every previously-
        # provisioned sub-gateway holding a stale credential — workers in
        # those gateways still register and ensure_loaded still works
        # (because both read auth.db directly), but the worker's chat
        # completion call goes through OpenShell's privacy router which
        # injects the stored stale credential and gets rejected by LM Studio.
        # Run in a background task so the PATCH response returns immediately;
        # the resync is best-effort and idempotent. See
        # ``ensure_provider_configured`` for the per-gateway sync logic.
        if "api_key" in updates or "endpoint_url" in updates:
            try:
                import asyncio as _asyncio
                from gateway.openshell_routes import ensure_provider_configured
                async def _propagate_bg():
                    try:
                        routes = auth_db.list_model_routes()
                        for r in routes:
                            try:
                                await _asyncio.to_thread(
                                    ensure_provider_configured,
                                    r["openshell_name"], r["provider"],
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Failed to propagate machine update to route %s (%s): %s",
                                    r["id"], r["openshell_name"], exc,
                                )
                    except Exception as exc:
                        logger.warning("Machine-update propagation raised: %s", exc)
                _asyncio.create_task(_propagate_bg())
            except Exception as exc:
                logger.warning("Could not schedule machine-update propagation: %s", exc)

    machine = auth_db.get_machine(mid)
    machine["capabilities"] = auth_db.get_machine_capabilities(mid)
    machine["has_api_key"] = bool(machine.pop("api_key", None))
    return web.json_response({"machine": machine})


async def handle_machines_delete(request: web.Request) -> web.Response:
    mid = request.match_info["id"]
    machine = auth_db.get_machine(mid)
    if not machine:
        raise web.HTTPNotFound(reason="machine_not_found")

    auth_db.delete_machine(mid)
    auth_db.write_audit_log(
        request["current_user"]["sub"], "delete_machine",
        target_type="machine", target_id=mid,
        metadata={"name": machine["name"]},
        ip_address=request.remote,
    )
    return web.Response(status=204)


async def handle_machines_reorder(request: web.Request) -> web.Response:
    """POST /admin/machines/reorder — body: {"ids": ["id1","id2",...]}"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    ids = body.get("ids")
    if not isinstance(ids, list):
        return web.json_response({"error": "ids must be an array"}, status=400)

    auth_db.reorder_machines(ids)
    auth_db.write_audit_log(
        request["current_user"]["sub"], "reorder_machines",
        metadata={"order": ids},
        ip_address=request.remote,
    )
    return web.json_response({"ok": True})


async def handle_machine_claims_get(request: web.Request) -> web.Response:
    """GET /admin/machines/{id}/claims — list all user claims for a machine."""
    mid = request.match_info["id"]
    if not auth_db.get_machine(mid):
        raise web.HTTPNotFound(reason="machine_not_found")
    claims = auth_db.list_machine_claims(mid)
    return web.json_response({"claims": claims})


async def handle_machine_claim_put(request: web.Request) -> web.Response:
    """PUT /machines/{id}/claim — claim or update priority on a machine.
    Requires claim_machine permission. Admins can claim on behalf of any user.
    """
    mid = request.match_info["id"]
    if not auth_db.get_machine(mid):
        raise web.HTTPNotFound(reason="machine_not_found")
    current_user = request["current_user"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Admins can specify a user_id; everyone else claims for themselves
    target_user_id = body.get("user_id", current_user["sub"])
    if target_user_id != current_user["sub"]:
        from gateway.auth.rbac import has_permission
        if not has_permission(current_user.get("role", "viewer"), "manage_machines"):
            raise web.HTTPForbidden(text='{"error":"forbidden"}', content_type="application/json")
    priority = int(body.get("priority", 100))
    claim = auth_db.claim_machine(mid, target_user_id, priority)
    return web.json_response({"claim": claim})


async def handle_machine_claim_delete(request: web.Request) -> web.Response:
    """DELETE /machines/{id}/claim — remove a claim. Admin can remove any; user removes own."""
    mid = request.match_info["id"]
    current_user = request["current_user"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    target_user_id = body.get("user_id", current_user["sub"])
    if target_user_id != current_user["sub"]:
        from gateway.auth.rbac import has_permission
        if not has_permission(current_user.get("role", "viewer"), "manage_machines"):
            raise web.HTTPForbidden(text='{"error":"forbidden"}', content_type="application/json")
    auth_db.unclaim_machine(mid, target_user_id)
    return web.Response(status=204)


async def handle_machine_capabilities_put(request: web.Request) -> web.Response:
    mid = request.match_info["id"]
    if not auth_db.get_machine(mid):
        raise web.HTTPNotFound(reason="machine_not_found")

    try:
        capabilities = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(capabilities, list):
        return web.json_response({"error": "expected_array"}, status=400)

    auth_db.set_machine_capabilities(mid, capabilities)
    auth_db.write_audit_log(
        request["current_user"]["sub"], "update_machine_capabilities",
        target_type="machine", target_id=mid,
        metadata={"count": len(capabilities)},
        ip_address=request.remote,
    )
    return web.json_response({"capabilities": auth_db.get_machine_capabilities(mid)})


async def handle_machine_health(request: web.Request) -> web.Response:
    mid = request.match_info["id"]
    machine = auth_db.get_machine(mid)
    if not machine:
        raise web.HTTPNotFound(reason="machine_not_found")

    # Always do a live probe (explicit button press) and write through the cache
    _invalidate_health_cache(machine["endpoint_url"])
    result = await _probe_health_cached(machine["endpoint_url"])
    checked_at = int(_time.time())
    return web.json_response({
        **result,
        "machine_id": mid,
        "checked_at": checked_at,
    })


# ── Cloud providers ───────────────────────────────────────────────────────────

async def handle_cloud_providers_list(request: web.Request) -> web.Response:
    providers = auth_db.list_cloud_providers()
    for p in providers:
        p["has_api_key"] = bool(p.get("api_key"))
        p.pop("api_key", None)
    return web.json_response({"providers": providers})


async def handle_cloud_providers_post(request: web.Request) -> web.Response:
    body = await request.json()
    provider = (body.get("provider") or "").strip()
    name = (body.get("name") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    active_model = (body.get("model") or "").strip()
    if not provider or not name:
        return web.json_response({"error": "provider and name required"}, status=400)
    p = auth_db.create_cloud_provider(
        provider=provider, name=name, api_key=api_key,
        base_url=base_url, active_model=active_model,
    )
    p["has_api_key"] = bool(p.get("api_key"))
    p.pop("api_key", None)
    return web.json_response(p, status=201)


async def handle_cloud_providers_patch(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    existing = auth_db.get_cloud_provider(pid)
    if not existing:
        return web.json_response({"error": "not_found"}, status=404)
    body = await request.json()
    updates = {}
    for k in ("name", "base_url", "active_model", "enabled"):
        if k in body:
            updates[k] = body[k]
    if "api_key" in body:
        updates["api_key"] = body["api_key"]  # empty string clears
    p = auth_db.update_cloud_provider(pid, **updates)
    p["has_api_key"] = bool(p.get("api_key"))
    p.pop("api_key", None)
    return web.json_response(p)


async def handle_cloud_providers_delete(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    existing = auth_db.get_cloud_provider(pid)
    if not existing:
        return web.json_response({"error": "not_found"}, status=404)
    auth_db.delete_cloud_provider(pid)
    return web.Response(status=204)


async def handle_cloud_providers_activate(request: web.Request) -> web.Response:
    """Set a cloud provider as active and sync env vars + config.yaml."""
    pid = request.match_info["id"]
    prov = auth_db.get_cloud_provider(pid)
    if not prov:
        return web.json_response({"error": "not_found"}, status=404)

    auth_db.set_active_cloud_provider(pid)

    # Sync to .env and os.environ so agent picks up immediately
    from gateway.setup_handlers import _FRONTIER_PROVIDERS
    provider_type = prov["provider"]
    api_key = prov.get("api_key") or ""
    frov = _FRONTIER_PROVIDERS.get(provider_type, {})
    effective_url = prov.get("base_url") or frov.get("base_url", "")
    active_model = prov.get("active_model") or ""

    try:
        from logos_cli.config import save_env_value
        if frov.get("env_key"):
            save_env_value(frov["env_key"], api_key)
        save_env_value("OPENAI_API_KEY", api_key)
        save_env_value("OPENAI_BASE_URL", effective_url)
    except Exception as e:
        logger.warning("cloud provider activate: env write failed: %s", e)

    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["OPENAI_BASE_URL"] = effective_url
    if active_model:
        os.environ["HERMES_MODEL"] = active_model
    if frov.get("server_type"):
        os.environ["HERMES_SERVER_TYPE"] = frov["server_type"]

    # Update config.yaml
    try:
        import pathlib
        import yaml
        _hermes_home = pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".logos"))
        _config_path = _hermes_home / "config.yaml"
        cfg: dict = yaml.safe_load(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
        cfg["OPENAI_BASE_URL"] = effective_url
        cfg["OPENAI_API_KEY"] = api_key
        if active_model:
            cfg["HERMES_MODEL"] = active_model
        if frov.get("server_type"):
            cfg["HERMES_SERVER_TYPE"] = frov["server_type"]
        cfg.setdefault("model", {})
        if isinstance(cfg["model"], str):
            cfg["model"] = {"default": cfg["model"]}
        cfg["model"]["provider"] = provider_type
        _config_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
    except Exception as e:
        logger.warning("cloud provider activate: config.yaml write failed: %s", e)

    result = auth_db.get_cloud_provider(pid)
    result["has_api_key"] = bool(result.get("api_key"))
    result.pop("api_key", None)
    return web.json_response({"ok": True, "provider": result})


async def handle_cloud_providers_test(request: web.Request) -> web.Response:
    """Validate a cloud provider's API key."""
    pid = request.match_info["id"]
    prov = auth_db.get_cloud_provider(pid)
    if not prov:
        return web.json_response({"error": "not_found"}, status=404)

    from gateway.setup_handlers import validate_provider_key
    result = await validate_provider_key(
        prov["provider"],
        prov.get("api_key") or "",
        prov.get("base_url") or "",
    )
    return web.json_response(result)


async def handle_cloud_provider_models(request: web.Request) -> web.Response:
    """Fetch the live model catalog from a configured cloud provider.

    Used by the model-route provisioning UI so the dropdown shows
    whatever models the provider currently exposes (instead of a
    hardcoded list that rots as new models ship). On failure the UI
    falls back to a small hardcoded list of known-good IDs so the
    user can still type-pick if the provider's /v1/models is unreachable.
    """
    provider = request.rel_url.query.get("provider", "").strip().lower()
    if provider not in ("anthropic", "openai", "openrouter"):
        return web.json_response(
            {"models": [], "error": f"dynamic listing unsupported for provider {provider!r}"},
            status=200,
        )

    # Find the active cloud provider record for this provider name. We
    # look up by provider type so the UI doesn't have to know the row id.
    rows = auth_db.list_cloud_providers() or []
    prov = next(
        (p for p in rows if p.get("provider") == provider and p.get("enabled")),
        None,
    )
    if not prov or not prov.get("api_key"):
        return web.json_response(
            {"models": [], "error": f"no enabled {provider} provider with an API key"},
            status=200,
        )

    api_key = prov["api_key"]
    base_url = (prov.get("base_url") or "").rstrip("/") or {
        "anthropic":  "https://api.anthropic.com",
        "openai":     "https://api.openai.com",
        "openrouter": "https://openrouter.ai/api",
    }[provider]

    headers = {"Accept": "application/json"}
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        url = f"{base_url}/v1/models"
    elif provider == "openai":
        headers["Authorization"] = f"Bearer {api_key}"
        url = f"{base_url}/v1/models"
    else:  # openrouter
        headers["Authorization"] = f"Bearer {api_key}"
        url = f"{base_url}/v1/models"

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    return web.json_response(
                        {"models": [], "error": f"HTTP {resp.status}: {body}"},
                        status=200,
                    )
                data = await resp.json()
    except Exception as exc:
        return web.json_response(
            {"models": [], "error": f"fetch failed: {exc}"},
            status=200,
        )

    # Normalize: each provider returns the catalog in a different shape.
    if provider == "anthropic":
        # {"data": [{"id": "claude-opus-4-6", "type": "model", ...}], ...}
        models = [m["id"] for m in (data.get("data") or []) if m.get("id")]
    else:
        # OpenAI-compatible: {"data": [{"id": "gpt-5", ...}], ...}
        models = [m["id"] for m in (data.get("data") or []) if m.get("id")]
    return web.json_response({"models": sorted(models), "provider": provider})


# ── Dispatch ledger (M8 Phase B) ─────────────────────────────────────────────

async def handle_dispatches_list(request: web.Request) -> web.Response:
    """GET /admin/dispatches — query the dispatch activity ledger.

    Query params: agent_id, origin, status, limit (max 200), offset.
    """
    agent_id = request.rel_url.query.get("agent_id")
    origin = request.rel_url.query.get("origin")
    status = request.rel_url.query.get("status")
    limit = min(int(request.rel_url.query.get("limit", 50)), 200)
    offset = int(request.rel_url.query.get("offset", 0))
    rows, total = auth_db.list_dispatches(
        agent_id=agent_id, origin=origin, status=status,
        limit=limit, offset=offset,
    )
    return web.json_response({"dispatches": rows, "total": total})


# ── Named agents ─────────────────────────────────────────────────────────────

async def handle_agents_list(request: web.Request) -> web.Response:
    user = request.get("current_user") or {}
    agents = auth_db.list_agents(user.get("id", ""))

    # Augment each agent with the deterministic worker_id / sandbox_name that
    # its OpenShell sandbox will register under, plus live worker state (if
    # the sandbox is currently connected). The frontend uses this to grey
    # out the chat UI until the sandbox is ready, and to pass worker_id on
    # every /chat request.
    try:
        from gateway.executors.openshell import _sanitize_sandbox_name
    except Exception:
        _sanitize_sandbox_name = lambda s: s  # noqa: E731

    # Aggregate dispatch counts per agent in a single query so the world
    # view's per-agent maturity tier ("Sprout/Sapling/Branch/Tree/Old
    # Growth") doesn't need N round-trips. Sessions are filesystem-backed
    # at ~/.logos/agents/<name>/sessions/*.json — count via a directory
    # listing per agent (cheap, O(agents) stat calls). Memory count is
    # currently always 0 in the OpenShell runtime (M10) so we stamp the
    # field but the maturity formula's memory term is inert until
    # sandbox_worker.py starts writing to ~/.logos/agents/<name>/memories/.
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path
    dispatch_counts: dict[str, int] = {}
    try:
        with auth_db._conn() as _conn:
            for row in _conn.execute(
                "SELECT agent_id, COUNT(*) AS n FROM dispatches WHERE agent_id IS NOT NULL GROUP BY agent_id"
            ).fetchall():
                dispatch_counts[row["agent_id"]] = int(row["n"])
    except _sqlite3.Error:
        pass  # leave map empty; UI handles missing counts as 0

    worker_registry = request.app.get("worker_registry")
    for a in agents:
        name = a.get("name", "")
        sandbox_name = _sanitize_sandbox_name(f"hermes-{name}") if name else ""
        a["sandbox_name"] = sandbox_name
        a["worker_id"] = sandbox_name
        # Live worker status, if the sandbox has a registered state-file
        # entry (Plan A-prime / TASKS.md #24: per-task ``openshell
        # sandbox exec`` dispatch — ``worker_connected`` now means "the
        # gateway has a state-file entry for this sandbox", and
        # ``worker_healthy`` means "the sandbox CR is in phase=ready").
        # M7 in docs/MISSING.md is where the richer sandbox_phase /
        # api_latency_ms / last_probe_ts fields will land.
        #
        # ``active_tasks`` is the in-flight dispatch counter from
        # ``WorkerRegistry.active_task_count`` (MISSING.md — dispatch
        # activity tracking, Phase A). Goes > 0 while a chat dispatch
        # is running a task in this sandbox; the world view renders a
        # thought-bubble indicator while > 0 so the user can see the
        # agent "thinking" live.
        worker = worker_registry.get(sandbox_name) if worker_registry and sandbox_name else None
        if worker:
            a["worker_healthy"] = worker.healthy
            a["worker_status"] = worker.status
            a["worker_connected"] = True
        else:
            a["worker_healthy"] = False
            a["worker_status"] = "disconnected"
            a["worker_connected"] = False
        a["active_tasks"] = (
            worker_registry.active_task_count(sandbox_name)
            if worker_registry and sandbox_name else 0
        )

        # Maturity inputs (tier computation lives in the frontend so the
        # tier names + glyphs can iterate without a server roundtrip).
        a["dispatch_count"] = dispatch_counts.get(a["id"], 0)
        sess_count = 0
        mem_count = 0
        if name:
            try:
                _sess_dir = _Path.home() / ".logos" / "agents" / name / "sessions"
                if _sess_dir.is_dir():
                    sess_count = sum(1 for _ in _sess_dir.glob("session_*.json"))
            except OSError:
                pass
            try:
                _mem_dir = _Path.home() / ".logos" / "agents" / name / "memories"
                if _mem_dir.is_dir():
                    # Count any file — memory format may evolve; total
                    # files is a reasonable proxy for "stuff remembered".
                    mem_count = sum(1 for _ in _mem_dir.iterdir() if _.is_file())
            except OSError:
                pass
        a["session_count"] = sess_count
        a["memory_count"] = mem_count

    return web.json_response({"agents": agents})


async def handle_agents_post(request: web.Request) -> web.Response:
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if auth_db.get_agent_by_name(name):
        return web.json_response({"error": "An agent with that name already exists"}, status=409)
    user = request.get("current_user") or {}
    import json as _json
    toolsets_raw = body.get("toolsets")
    toolsets_str = _json.dumps(toolsets_raw) if isinstance(toolsets_raw, list) else ""
    soul_slug = (body.get("soul_slug") or "general").strip()
    model = (body.get("model") or "").strip()
    # "Auto" / empty model: pin to the gateway's currently active model so
    # the agent card displays a concrete value and the executor + sandbox
    # see the same model the rest of the gateway is using. Without this,
    # the DB record stays empty and the world-view card shows no model
    # badge even though the executor's spawn-time fallback gives the
    # sandbox a working model. Resolution mirrors openshell.spawn().
    if not model:
        model = (
            os.environ.get("HERMES_MODEL")
            or os.environ.get("LLM_MODEL")
            or ""
        ).strip()
    # Optional sprite selection (0..7); None falls back to name-hash render.
    try:
        char_index = body.get("char_index")
        char_index = int(char_index) if char_index is not None else None
    except (TypeError, ValueError):
        char_index = None
    # Bind to a model_routes row. UI route picker provides one; "Auto
    # (use default)" sends NULL and we resolve the default here so the
    # DB record is never NULL — keeps the Dashboards "bound agents"
    # count accurate and lets the executor + admin queries treat the
    # binding as the single source of truth instead of relying on a
    # spawn-time fallback that leaves the DB looking unattached.
    model_route_id = body.get("model_route_id") or None
    if model_route_id:
        _route = auth_db.get_model_route(model_route_id)
        if _route:
            # Sync model to the route's model so the agent card and
            # the executor see the same value.
            model = _route.get("model") or model
        else:
            # UI sent a stale id (route deleted between fetch and POST)
            # — drop the binding so the default-route fallback below kicks in.
            logger.warning(
                "create_agent: model_route_id=%r not found, dropping binding",
                model_route_id,
            )
            model_route_id = None
    if not model_route_id:
        _default = auth_db.get_default_model_route()
        if _default:
            model_route_id = _default["id"]
            model = _default.get("model") or model
            logger.info(
                "create_agent(%s): no route specified, auto-binding to default route %s (%s)",
                name, model_route_id, model,
            )
    agent = auth_db.create_agent(
        name=name,
        soul_slug=soul_slug,
        model=model,
        description=(body.get("description") or "").strip(),
        creator_id=user.get("id", ""),
        shared=body.get("shared", True),
        toolsets=toolsets_str,
        char_index=char_index,
        model_route_id=model_route_id,
    )

    # B-tier defaults: apply always_on capabilities + any flagged
    # default_on_create in capabilities.yaml (today: web + code_execution).
    # Replaces the old "auto-apply browserless preset" path — that was
    # the host-bridge architecture; m12's sandbox image bundles local
    # Chromium so no network preset is needed for browser_navigate.
    try:
        from gateway import capabilities as _caps
        _applied = _caps.apply_initial_defaults(agent["id"])
        logger.info(
            "create_agent(%s): applied B-tier defaults: %s",
            agent["id"], _applied,
        )
    except Exception as _exc:
        logger.warning(
            "create_agent(%s): apply_initial_defaults failed: %s",
            agent["id"], _exc,
        )

    # If OpenShell runtime is active, spawn a sandbox for this agent.
    # `executor.spawn()` runs `openshell sandbox create` synchronously,
    # which can take >60s while the underlying k8s Sandbox CR provisions.
    # We must NOT block the asyncio event loop on it — instead we kick
    # the spawn into a background thread and return immediately. The
    # executor writes its state record with phase="provisioning" up
    # front, so the Sandboxes dashboard sees the new entry within a
    # second of this handler returning.
    spawn_result = None
    executor = request.app.get("executor")
    if executor and type(executor).__name__ == "OpenShellExecutor":
        try:
            import asyncio as _asyncio
            from gateway.executors.base import InstanceConfig
            config = InstanceConfig(
                name=name,
                soul_name=soul_slug,
                model=model,
                requester=user.get("display_name") or user.get("username") or "",
                instance_label=name,
                toolsets=_json.loads(toolsets_str) if toolsets_str else [],
                model_route_id=model_route_id,
            )

            async def _spawn_bg():
                try:
                    await _asyncio.to_thread(executor.spawn, config)
                    logger.info("Spawned OpenShell sandbox for agent '%s'", name)
                except Exception as exc:
                    logger.warning("Failed to spawn OpenShell sandbox for agent '%s': %s", name, exc)

            _asyncio.create_task(_spawn_bg())
            spawn_result = {"status": "provisioning", "agent": name}
        except Exception as exc:
            logger.warning("Failed to schedule sandbox spawn for agent '%s': %s", name, exc)
            spawn_result = {"error": str(exc)}

    resp = dict(agent)
    if spawn_result:
        resp["spawn"] = spawn_result
    return web.json_response(resp, status=201)


async def handle_agents_patch(request: web.Request) -> web.Response:
    aid = request.match_info["id"]
    existing = auth_db.get_agent(aid)
    if not existing:
        return web.json_response({"error": "not_found"}, status=404)
    body = await request.json()
    import json as _json
    updates = {}
    for k in ("name", "soul_slug", "model", "description", "shared", "char_index",
              "daily_budget_usd", "fallback_route_id"):
        if k in body:
            updates[k] = body[k]
    if "toolsets" in body:
        updates["toolsets"] = _json.dumps(body["toolsets"]) if isinstance(body["toolsets"], list) else ""
    # model_route_id rebind: validate the row exists and sync the
    # agent's `model` field to the route's model so the card and
    # executor stay consistent. NULL is allowed — clears the binding
    # and falls back to the default route at spawn time. The chat M
    # dropdown uses this path to switch an agent between routes.
    if "model_route_id" in body:
        _new_route_id = body.get("model_route_id") or None
        if _new_route_id:
            _route = auth_db.get_model_route(_new_route_id)
            if not _route:
                return web.json_response({"error": "model_route_id not found"}, status=404)
            updates["model_route_id"] = _new_route_id
            # Pull the model field forward from the route — keeps
            # agent.model in sync with the bound route's model so
            # display + executor see the same value.
            updates["model"] = _route.get("model") or updates.get("model", existing.get("model", ""))
        else:
            updates["model_route_id"] = None
    if "name" in updates and updates["name"] != existing["name"]:
        dup = auth_db.get_agent_by_name(updates["name"])
        if dup and dup["id"] != aid:
            return web.json_response({"error": "An agent with that name already exists"}, status=409)
    agent = auth_db.update_agent(aid, **updates)
    return web.json_response(agent)


async def handle_agents_delete(request: web.Request) -> web.Response:
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)

    # If OpenShell runtime is active, destroy the agent's sandbox AND
    # purge per-agent host data (memories, logs, sessions synced from
    # sandbox by worker_registry). Run in a background thread so we
    # don't block the asyncio event loop. Sandbox teardown must happen
    # before rmtree — worker_registry may still be writing syncs until
    # the sandbox is gone.
    executor = request.app.get("executor")
    agent_name = agent["name"]
    if executor and type(executor).__name__ == "OpenShellExecutor":
        try:
            import asyncio as _asyncio
            import shutil as _shutil
            from gateway.executors.openshell import _HERMES_HOME as _HH
            async def _delete_bg():
                try:
                    await _asyncio.to_thread(executor.delete_instance, agent_name)
                    logger.info("Destroyed OpenShell sandbox for agent '%s'", agent_name)
                except Exception as exc:
                    logger.warning("Failed to destroy sandbox for agent '%s': %s", agent_name, exc)
                try:
                    agent_dir = _HH / "agents" / agent_name
                    if agent_dir.exists():
                        await _asyncio.to_thread(_shutil.rmtree, str(agent_dir), True)
                        logger.info("Purged host data dir %s", agent_dir)
                except Exception as exc:
                    logger.warning("Failed to purge host data for agent '%s': %s", agent_name, exc)
            _asyncio.create_task(_delete_bg())
        except Exception as exc:
            logger.warning("Failed to schedule sandbox delete for agent '%s': %s", agent_name, exc)

    auth_db.delete_agent(aid)
    return web.Response(status=204)


# ── Agent tools (per-agent toolset + policy preset editor) ───────────────────
#
# These endpoints back the Tools (T) pill dropdown in the Chats tab
# (MISSING.md M1 / M10 scope item 5). Each agent has two independent
# dimensions of "tools":
#
#   1. Application-layer toolsets (agents.toolsets column, JSON array)
#      — which tool NAMES are loaded into AIAgent at dispatch time.
#      Toggled via ``update_agent(toolsets=[...])``, enforced at
#      run_conversation time by the agent's own tool registry.
#
#   2. Infrastructure-layer network policy presets (agents.applied_presets
#      column, JSON array) — which endpoint groups the sandbox can
#      reach on the network. Managed by ``gateway.policies.apply_preset``
#      / ``remove_preset`` which also push the merged effective policy
#      to the running sandbox via ``openshell policy set``.
#
# Both dimensions surface in the same T pill UI because they're
# conceptually "the tools this agent can use", but the underlying
# enforcement layers are very different — see MISSING.md M10's
# two-layer STAMP model for the division.


async def handle_agent_tools_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/tools — bundle the state the T pill needs.

    Returns both dimensions in one round trip so the UI can open the
    dropdown with a single fetch::

        {
            "toolsets": {
                "enabled":   ["hermes-cli"],         // agents.toolsets
                "available": [
                    {"name": "web", "description": "Web research..."},
                    ...
                ]
            },
            "presets": {
                "applied":   ["github"],              // agents.applied_presets
                "available": [
                    {"name": "github", "description": "GitHub.com ..."},
                    ...
                ]
            }
        }

    Unknown or malformed state is degraded gracefully (empty lists +
    a warning log) so a broken preset directory or a corrupt JSON
    column doesn't block the editor UI from rendering.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)

    # ── Application layer: available + enabled toolsets ──
    available_toolsets: list[dict] = []
    try:
        from core.toolsets import TOOLSETS
        for name, meta in sorted(TOOLSETS.items()):
            description = ""
            if isinstance(meta, dict):
                description = str(meta.get("description", ""))
            available_toolsets.append({"name": name, "description": description})
    except Exception as exc:
        logger.warning(
            "handle_agent_tools_get(%s): failed to load core.toolsets.TOOLSETS: %s",
            aid, exc,
        )

    import json as _json
    enabled_toolsets: list[str] = []
    raw_ts = agent.get("toolsets")
    if raw_ts:
        try:
            loaded = _json.loads(raw_ts)
            if isinstance(loaded, list):
                enabled_toolsets = [str(t) for t in loaded if isinstance(t, str)]
        except _json.JSONDecodeError:
            logger.warning(
                "handle_agent_tools_get(%s): agents.toolsets is not valid JSON: %r",
                aid, raw_ts,
            )

    # ── Infrastructure layer: available + applied presets ──
    available_presets: list[dict] = []
    applied_presets: list[str] = []
    try:
        from gateway import policies as gp
        for p in gp.list_presets():
            available_presets.append({
                "name": p.name,
                "description": p.description,
            })
        applied_presets = gp.get_applied_presets(aid)
    except Exception as exc:
        logger.warning(
            "handle_agent_tools_get(%s): failed to load policy presets: %s",
            aid, exc,
        )

    # ── Tool readiness — per-tool status check ──
    tool_readiness: list[dict] = []
    try:
        from gateway import policies as gp
        tool_readiness = gp.get_tool_readiness(aid)
    except Exception as exc:
        logger.warning(
            "handle_agent_tools_get(%s): failed to compute tool readiness: %s",
            aid, exc,
        )

    return web.json_response({
        "toolsets": {
            "enabled": enabled_toolsets,
            "available": available_toolsets,
        },
        "presets": {
            "applied": applied_presets,
            "available": available_presets,
        },
        "readiness": tool_readiness,
    })


async def handle_agent_toolsets_toggle(request: web.Request) -> web.Response:
    """POST /admin/agents/{id}/tools/toolsets/toggle

    Body: ``{"toolset": "<name>", "enabled": <bool>}``

    Adds or removes the named toolset from ``agents.toolsets``.
    Unknown toolset names are rejected with 400 so typos don't
    silently persist as dead entries. Returns the updated enabled
    list as the canonical "after" state the UI can render directly::

        {"enabled": ["hermes-cli", "web"]}

    Application-layer only — does not touch the network policy.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    toolset = (body.get("toolset") or "").strip()
    enabled = bool(body.get("enabled"))
    if not toolset:
        return web.json_response({"error": "toolset is required"}, status=400)

    # Validate against the canonical toolset registry before writing.
    try:
        from core.toolsets import TOOLSETS
        if toolset not in TOOLSETS:
            return web.json_response(
                {"error": f"unknown toolset: {toolset}"},
                status=400,
            )
    except Exception as exc:
        return web.json_response(
            {"error": f"toolsets module unavailable: {exc}"},
            status=500,
        )

    # Read, mutate, write-back the current JSON list.
    import json as _json
    current: list[str] = []
    raw_ts = agent.get("toolsets")
    if raw_ts:
        try:
            loaded = _json.loads(raw_ts)
            if isinstance(loaded, list):
                current = [str(t) for t in loaded if isinstance(t, str)]
        except _json.JSONDecodeError:
            pass  # treat corrupt state as empty — the toggle will fix it

    if enabled and toolset not in current:
        current.append(toolset)
    elif not enabled and toolset in current:
        current.remove(toolset)

    auth_db.update_agent(aid, toolsets=_json.dumps(current))
    logger.info(
        "agent_toolsets_toggle(%s, %s=%s): enabled=%s",
        aid, toolset, enabled, current,
    )
    # Push the new config to the running sandbox so the change takes
    # effect on the NEXT dispatch (no sandbox restart needed). Plan
    # A-prime spawns sandbox_worker.py per dispatch; each subprocess
    # re-reads /tmp/hermes/instance-config.json at startup, so a fresh
    # upload here is enough. Best-effort — if the sandbox isn't running
    # yet (provisioning) or the gateway isn't reachable, the next
    # spawn will pick up the new toolsets from the DB anyway.
    executor = request.app.get("executor")
    if executor and hasattr(executor, "refresh_instance_config"):
        try:
            executor.refresh_instance_config(agent["name"])
        except Exception as exc:
            logger.warning(
                "agent_toolsets_toggle: refresh_instance_config failed for %s: %s",
                agent["name"], exc,
            )
    return web.json_response({"enabled": current})


async def handle_agent_presets_toggle(request: web.Request) -> web.Response:
    """POST /admin/agents/{id}/tools/presets/toggle

    Body: ``{"preset": "<name>", "enabled": <bool>}``

    When ``enabled=true`` calls ``gateway.policies.apply_preset``,
    otherwise ``remove_preset``. Both update ``agents.applied_presets``
    in the DB AND push the merged effective policy to the running
    sandbox via ``openshell policy set --wait``.

    The push is best-effort — if the sandbox isn't spawned yet, or
    openshell is unreachable, the DB update still happens and the
    next spawn picks up the new effective policy. Returns the full
    applied list so the UI can re-render without an extra GET::

        {"applied": ["github", "slack"]}

    Unknown preset names return 400 (validated up front in
    ``apply_preset`` via ``load_preset``). Any other exception from
    the policies module surfaces as a 500 with the error text.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    preset = (body.get("preset") or "").strip()
    enabled = bool(body.get("enabled"))
    if not preset:
        return web.json_response({"error": "preset is required"}, status=400)

    try:
        from gateway import policies as gp
    except Exception as exc:
        return web.json_response(
            {"error": f"policies module unavailable: {exc}"},
            status=500,
        )

    try:
        if enabled:
            gp.apply_preset(aid, preset)
        else:
            gp.remove_preset(aid, preset)
        applied = gp.get_applied_presets(aid)
    except gp.PresetNotFound as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception(
            "agent_presets_toggle(%s, %s=%s): failed",
            aid, preset, enabled,
        )
        return web.json_response({"error": str(exc)}, status=500)

    logger.info(
        "agent_presets_toggle(%s, %s=%s): applied=%s",
        aid, preset, enabled, applied,
    )
    return web.json_response({"applied": applied})


# ── Capabilities (user-facing collapse of toolsets+presets) ──────────────────

async def handle_agent_capabilities_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/capabilities — full capability state for the UI.

    Returns the catalogue (always_on / capabilities / power_tools) annotated
    with per-agent enabled/ready/missing_creds. UI consumes this to render
    the P dropdown without needing any further round-trips.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        from gateway import capabilities as _caps
        return web.json_response(_caps.compute_state(aid))
    except Exception as exc:
        logger.exception("capabilities GET failed for %s", aid)
        return web.json_response({"error": str(exc)}, status=500)


async def handle_agent_capabilities_toggle(request: web.Request) -> web.Response:
    """POST /admin/agents/{id}/capabilities/toggle

    Body: ``{"capability": "<id>", "enabled": <bool>}``

    Atomically applies or removes ALL toolsets + presets the capability
    bundles. Returns the recomputed full state so the UI re-renders in
    one call. Also pushes the resulting config to the running sandbox
    via ``executor.refresh_instance_config`` so the next dispatch
    already has the new tools available.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    cap_id = (body.get("capability") or "").strip()
    enabled = bool(body.get("enabled"))
    if not cap_id:
        return web.json_response({"error": "capability is required"}, status=400)
    try:
        from gateway import capabilities as _caps
        new_state = _caps.apply(aid, cap_id, enabled)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("capability toggle failed for %s/%s", aid, cap_id)
        return web.json_response({"error": str(exc)}, status=500)

    # Push the new config to the running sandbox so the next dispatch has it
    # — same hook the toolset toggle handler uses.
    executor = request.app.get("executor")
    if executor and hasattr(executor, "refresh_instance_config"):
        try:
            executor.refresh_instance_config(agent["name"])
        except Exception as exc:
            logger.warning(
                "capability toggle: refresh_instance_config(%s) failed: %s",
                agent["name"], exc,
            )
    return web.json_response(new_state)


async def handle_agent_website_blocklist_put(request: web.Request) -> web.Response:
    """PUT /admin/agents/{id}/website-blocklist

    Body: ``{"patterns": ["*.example.com", "!facebook.com"], "enabled": <bool>}``

    Layer 1 of URL control — patterns the local browser tool checks before
    every navigation (hermes's tools/website_policy.py). Saved per-agent
    in the DB and pushed to the sandbox via instance-config. Glob syntax;
    leading ``!`` flips the entry from allow → block.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    raw = body.get("patterns") or []
    if isinstance(raw, str):
        # accept newline-separated text from a textarea
        patterns = [p.strip() for p in raw.splitlines() if p.strip() and not p.strip().startswith("#")]
    elif isinstance(raw, list):
        patterns = [str(p).strip() for p in raw if str(p).strip()]
    else:
        return web.json_response({"error": "patterns must be list or string"}, status=400)
    enabled = bool(body.get("enabled", True))
    config = {"enabled": enabled, "patterns": patterns}
    auth_db.update_agent(aid, website_blocklist=json.dumps(config))
    # Push to the sandbox immediately
    executor = request.app.get("executor")
    if executor and hasattr(executor, "refresh_instance_config"):
        try:
            executor.refresh_instance_config(agent["name"])
        except Exception as exc:
            logger.warning(
                "website_blocklist PUT: refresh_instance_config(%s) failed: %s",
                agent["name"], exc,
            )
    return web.json_response({"ok": True, **config})


# ── Routing policies ──────────────────────────────────────────────────────────

async def handle_policies_list(request: web.Request) -> web.Response:
    policies = auth_db.list_policies()
    for p in policies:
        p["rules"] = auth_db.get_policy_rules(p["id"])
        p["user_count"] = auth_db.count_users_with_policy(p["id"])
    return web.json_response({"policies": policies})


async def handle_policies_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not body.get("name"):
        return web.json_response({"error": "name_required"}, status=400)

    valid_fallbacks = ("any_available", "fail")
    fallback = body.get("fallback", "any_available")
    if fallback not in valid_fallbacks:
        return web.json_response(
            {"error": "invalid_fallback", "valid": list(valid_fallbacks)}, status=400
        )

    try:
        policy = auth_db.create_policy(
            name=body["name"],
            description=body.get("description"),
            fallback=fallback,
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            return web.json_response({"error": "name_exists"}, status=409)
        raise

    auth_db.write_audit_log(
        request["current_user"]["sub"], "create_policy",
        target_type="policy", target_id=policy["id"],
        metadata={"name": policy["name"]},
        ip_address=request.remote,
    )
    policy["rules"] = []
    policy["user_count"] = 0
    return web.json_response({"policy": policy}, status=201)


async def handle_policies_patch(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    if not auth_db.get_policy(pid):
        raise web.HTTPNotFound(reason="policy_not_found")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    updates = {k: body[k] for k in ("name", "description", "fallback") if k in body}
    if updates:
        auth_db.update_policy(pid, **updates)
        auth_db.write_audit_log(
            request["current_user"]["sub"], "update_policy",
            target_type="policy", target_id=pid,
            metadata=updates, ip_address=request.remote,
        )

    policy = auth_db.get_policy(pid)
    policy["rules"] = auth_db.get_policy_rules(pid)
    policy["user_count"] = auth_db.count_users_with_policy(pid)
    return web.json_response({"policy": policy})


async def handle_policies_delete(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    policy = auth_db.get_policy(pid)
    if not policy:
        raise web.HTTPNotFound(reason="policy_not_found")

    auth_db.delete_policy(pid)
    auth_db.write_audit_log(
        request["current_user"]["sub"], "delete_policy",
        target_type="policy", target_id=pid,
        metadata={"name": policy["name"]},
        ip_address=request.remote,
    )
    return web.Response(status=204)


async def handle_policy_rules_put(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    if not auth_db.get_policy(pid):
        raise web.HTTPNotFound(reason="policy_not_found")

    try:
        rules = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(rules, list):
        return web.json_response({"error": "expected_array"}, status=400)

    # Validate each rule has a machine_id; assign sequential ranks from array order
    validated = []
    for i, rule in enumerate(rules):
        if not rule.get("machine_id"):
            continue
        validated.append({
            "model_class": rule.get("model_class", "*"),
            "machine_id": rule["machine_id"],
            "rank": i + 1,
        })

    auth_db.set_policy_rules(pid, validated)
    auth_db.write_audit_log(
        request["current_user"]["sub"], "update_policy_rules",
        target_type="policy", target_id=pid,
        metadata={"rule_count": len(validated)},
        ip_address=request.remote,
    )
    return web.json_response({"rules": auth_db.get_policy_rules(pid)})


# ── User delete / reset ──────────────────────────────────────────────────────

async def handle_users_delete(request: web.Request) -> web.Response:
    """DELETE /users/{id} — remove a user account (admin only, cannot self-delete)."""
    uid = request.match_info["id"]
    caller = request["current_user"]

    if uid == caller["sub"]:
        return web.json_response({"error": "cannot_delete_self"}, status=400)

    user = auth_db.get_user_by_id(uid)
    if not user:
        raise web.HTTPNotFound(reason="user_not_found")

    auth_db.delete_user(uid)
    auth_db.write_audit_log(
        caller["sub"], "delete_user",
        target_type="user", target_id=uid,
        metadata={"username": user.get("username", "")},
        ip_address=request.remote,
    )
    return web.Response(status=204)


async def handle_users_reset(request: web.Request) -> web.Response:
    """POST /users/{id}/reset — wipe run history and invalidate sessions for a user."""
    uid = request.match_info["id"]
    caller = request["current_user"]

    if not auth_db.get_user_by_id(uid):
        raise web.HTTPNotFound(reason="user_not_found")

    auth_db.reset_user_data(uid)
    auth_db.write_audit_log(
        caller["sub"], "reset_user_data",
        target_type="user", target_id=uid,
        ip_address=request.remote,
    )
    return web.json_response({"ok": True})


# ── User → Policy assignment ──────────────────────────────────────────────────

async def handle_user_policy_patch(request: web.Request) -> web.Response:
    uid = request.match_info["id"]
    caller = request["current_user"]

    if not auth_db.get_user_by_id(uid):
        raise web.HTTPNotFound(reason="user_not_found")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    policy_id = body.get("policy_id") or None
    if policy_id and not auth_db.get_policy(policy_id):
        return web.json_response({"error": "policy_not_found"}, status=404)

    auth_db.assign_user_policy(uid, policy_id)
    auth_db.write_audit_log(
        caller["sub"], "assign_policy",
        target_type="user", target_id=uid,
        metadata={"policy_id": policy_id},
        ip_address=request.remote,
    )
    return web.json_response({"user_id": uid, "policy_id": policy_id})


# ── Routing resolver (debug tool) ─────────────────────────────────────────────

async def handle_routing_resolve(request: web.Request) -> web.Response:
    """Trace the routing decision for a given user + model alias.

    Query params:
      model     — model alias (default: balanced)
      user_id   — user to resolve for (default: caller)
    """
    caller = request["current_user"]
    uid = request.rel_url.query.get("user_id") or caller["sub"]
    model_alias = request.rel_url.query.get("model", "balanced")
    model_class = ALIAS_TO_CLASS.get(model_alias, "general")

    user = auth_db.get_user_by_id(uid)
    if not user:
        return web.json_response({"error": "user_not_found"}, status=404)

    trace: list[dict] = []
    fallback_chain: list[dict] = []
    result_machine: dict | None = None

    profile_id = user.get("policy_id")

    if profile_id:
        profile = auth_db.get_policy(profile_id)
        rules = auth_db.get_policy_rules(profile_id)

        # Exact class rules first, then wildcard
        exact    = sorted([r for r in rules if r["model_class"] == model_class], key=lambda r: r["rank"])
        wildcard = sorted([r for r in rules if r["model_class"] == "*"],          key=lambda r: r["rank"])
        ordered  = exact + wildcard

        for rule in ordered:
            machine = auth_db.get_machine(rule["machine_id"])
            if not machine:
                continue
            caps = _caps_as_classes(auth_db.get_machine_capabilities(machine["id"]))
            health = await _probe_health(machine["endpoint_url"])
            checks = {
                "enabled":   bool(machine["enabled"]),
                "reachable": health["status"] == "ok",
                "capable":   (model_class in caps) or rule["model_class"] == "*",
            }
            entry = {
                "rank":         rule["rank"],
                "rule_class":   rule["model_class"],
                "machine_id":   machine["id"],
                "machine_name": machine["name"],
                "endpoint_url": machine["endpoint_url"],
                "health":       health,
                "checks":       checks,
                "selected":     False,
            }
            fallback_chain.append(entry)

            if all(checks.values()) and result_machine is None:
                result_machine = machine
                entry["selected"] = True
                trace.append({
                    "layer":        "user_profile",
                    "result":       "match",
                    "profile_name": profile["name"] if profile else profile_id,
                    "rule_class":   rule["model_class"],
                    "machine":      machine["name"],
                })

        if result_machine is None:
            fallback_mode = profile.get("fallback", "any_available") if profile else "any_available"
            if fallback_mode == "fail":
                return web.json_response({
                    "error": "no_available_machine",
                    "profile": profile["name"] if profile else profile_id,
                    "trace": trace,
                    "fallback_chain": fallback_chain,
                }, status=503)
            trace.append({
                "layer":        "user_profile",
                "result":       "exhausted",
                "profile_name": profile["name"] if profile else profile_id,
            })
    else:
        trace.append({"layer": "user_profile", "result": "skip", "reason": "no profile assigned"})

    # Best-effort fallback: first enabled machine
    if result_machine is None:
        for m in auth_db.list_machines():
            if m["enabled"]:
                result_machine = m
                trace.append({"layer": "best_effort", "result": "match", "machine": m["name"]})
                break

    if result_machine is None:
        return web.json_response({
            "error": "no_machines_registered",
            "trace": trace,
        }, status=503)

    return web.json_response({
        "input": {
            "user_id":     uid,
            "user_name":   user.get("display_name") or user.get("username"),
            "model_alias": model_alias,
            "model_class": model_class,
        },
        "result": {
            "machine_id":   result_machine["id"],
            "machine_name": result_machine["name"],
            "endpoint_url": result_machine["endpoint_url"],
        },
        "trace":          trace,
        "fallback_chain": fallback_chain,
    })


# ── Standalone resolver (used by spawn logic) ─────────────────────────────────

async def resolve_route(
    user_id: str | None,
    model_alias: str = "balanced",
    machine_id_override: str | None = None,
) -> dict:
    """Resolve routing for a user + model alias through the full hierarchy.

    Layers (in priority order):
      1. instance_override  — explicit machine_id supplied in the spawn request
      2. user_profile       — rules from the user's assigned routing profile
      3. best_effort        — first enabled machine (no health check required)

    Returns:
        {
          "machine":     <machines row dict> | None,
          "model_class": str,
          "layer":       str,   # which layer produced the result
          "trace":       list[dict],
        }

    Raises RoutingError if the user's profile has fallback='fail' and all
    profile machines are unavailable.  Never raises for best_effort.
    """
    model_class = ALIAS_TO_CLASS.get(model_alias, "general")
    trace: list[dict] = []
    result_machine: dict | None = None

    # ── Layer 1: instance override ────────────────────────────────────────────
    if machine_id_override:
        machine = auth_db.get_machine(machine_id_override)
        if machine and machine["enabled"]:
            health = await _probe_health_cached(machine["endpoint_url"])
            if health["status"] == "ok":
                result_machine = machine
                trace.append({"layer": "instance_override", "result": "match",
                               "machine": machine["name"]})
            else:
                trace.append({"layer": "instance_override", "result": "unreachable",
                               "machine": machine["name"], "health": health})
        else:
            trace.append({"layer": "instance_override", "result": "skip",
                          "reason": "machine not found or disabled"})

    # ── Layer 2: user profile ─────────────────────────────────────────────────
    if result_machine is None:
        profile_id = None
        if user_id:
            user = auth_db.get_user_by_id(user_id)
            profile_id = user.get("policy_id") if user else None

        if profile_id:
            profile = auth_db.get_policy(profile_id)
            rules   = auth_db.get_policy_rules(profile_id)

            exact    = sorted([r for r in rules if r["model_class"] == model_class], key=lambda r: r["rank"])
            wildcard = sorted([r for r in rules if r["model_class"] == "*"],          key=lambda r: r["rank"])

            for rule in exact + wildcard:
                machine = auth_db.get_machine(rule["machine_id"])
                if not machine or not machine["enabled"]:
                    continue
                caps      = _caps_as_classes(auth_db.get_machine_capabilities(machine["id"]))
                health    = await _probe_health_cached(machine["endpoint_url"])
                capable   = (model_class in caps) or (rule["model_class"] == "*")
                reachable = health["status"] == "ok"

                if reachable and capable:
                    result_machine = machine
                    trace.append({
                        "layer":        "user_profile",
                        "result":       "match",
                        "profile_name": profile["name"] if profile else profile_id,
                        "rule_class":   rule["model_class"],
                        "machine":      machine["name"],
                    })
                    break

            if result_machine is None:
                fallback_mode = (profile.get("fallback", "any_available") if profile else "any_available")
                trace.append({"layer": "user_profile", "result": "exhausted",
                               "profile_name": profile["name"] if profile else profile_id})
                if fallback_mode == "fail":
                    raise RoutingError(
                        f"No available machine for profile '{profile['name'] if profile else profile_id}'",
                        profile_name=profile["name"] if profile else profile_id,
                    )
        else:
            trace.append({"layer": "user_profile", "result": "skip",
                          "reason": "no profile assigned"})

    # ── Layer 3: best-effort fallback ─────────────────────────────────────────
    if result_machine is None:
        for m in auth_db.list_machines():
            if m["enabled"]:
                result_machine = m
                trace.append({"layer": "best_effort", "result": "match", "machine": m["name"]})
                break
        if result_machine is None:
            trace.append({"layer": "best_effort", "result": "no_machines"})

    layer = trace[-1]["layer"] if trace else "none"
    return {"machine": result_machine, "model_class": model_class, "layer": layer, "trace": trace}


# ── Self-service routing preview (no admin permission required) ────────────────

_LAYER_LABELS: dict[str, str] = {
    "instance_override": "manual override",
    "user_profile":      "your routing profile",
    "best_effort":       "default (no profile)",
}


async def handle_routing_preview(request: web.Request) -> web.Response:
    """Resolve routing for the calling user + model alias.

    Used by the spawn form to show the routing preview before spawning.
    Any authenticated user can call this for themselves.

    Query params:
      model       — model alias (default: balanced)
      machine_id  — optional override (admin/operator only; ignored for others)
    """
    caller      = request["current_user"]
    uid         = caller["sub"]
    model_alias = request.rel_url.query.get("model", "balanced")
    model_class = ALIAS_TO_CLASS.get(model_alias, "general")

    # Only admin/operator may request a machine override via query param
    from gateway.auth.rbac import has_permission as _hp
    role     = caller.get("role", "viewer")
    can_override = _hp(role, "manage_machines") or _hp(role, "override_toolsets")
    machine_id_override = (
        request.rel_url.query.get("machine_id") or None
        if can_override else None
    )

    try:
        route = await resolve_route(
            user_id=uid,
            model_alias=model_alias,
            machine_id_override=machine_id_override,
        )
    except RoutingError as exc:
        return web.json_response({
            "model_alias": model_alias,
            "model_class": model_class,
            "machine":     None,
            "layer":       "none",
            "layer_label": "profile set to fail",
            "error":       str(exc),
        }, status=200)   # 200 so the UI can display the reason without treating as fetch error

    machine = route["machine"]
    layer   = route["layer"]
    profile_name = next(
        (t.get("profile_name") for t in route["trace"] if t.get("profile_name")),
        None,
    )

    return web.json_response({
        "model_alias":   model_alias,
        "model_class":   model_class,
        "machine": {
            "id":           machine["id"],
            "name":         machine["name"],
            "endpoint_url": machine["endpoint_url"],
        } if machine else None,
        "layer":       layer,
        "layer_label": _LAYER_LABELS.get(layer, layer),
        "profile_name": profile_name,
    })


# ── Setup wizard ──────────────────────────────────────────────────────────────

async def handle_setup_wizard(request: web.Request) -> web.Response:
    """Apply a quick-setup preset.  Requires manage_machines permission.

    Only allowed when all existing machines are seeded examples
    (description starts with "Example").  The existing example machines and
    their associated profiles are cleared first so the new configuration
    starts clean.

    Body JSON:
      mode         — "single" or "multi"
      endpoint_url — (single only, optional) override the default localhost URL
    """
    from gateway import seed as _seed

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    mode = body.get("mode")
    if mode not in ("single", "multi"):
        return web.json_response({"error": "invalid_mode", "valid": ["single", "multi"]}, status=400)

    # Safety check: only run when all machines are example placeholders
    machines = auth_db.list_machines()
    if machines and not all(
        (m.get("description") or "").startswith("Example") for m in machines
    ):
        return web.json_response(
            {"error": "system_not_in_example_state",
             "detail": "Setup wizard only runs when all machines are example placeholders."},
            status=409,
        )

    # Remove example machines (cascade deletes capabilities via FK) and example profiles
    for m in machines:
        auth_db.delete_machine(m["id"])
    for p in auth_db.list_policies():
        if (p.get("description") or "").startswith(("Auto-generated", "Auto-created")):
            auth_db.delete_policy(p["id"])  # also NULLs user policy_id assignments

    # Apply the chosen preset
    if mode == "single":
        endpoint_url = (body.get("endpoint_url") or "http://localhost:1234/v1").strip()
        result = _seed.apply_single_machine_setup(endpoint_url)
    else:
        result = _seed.apply_multi_machine_setup()

    if "error" in result:
        return web.json_response(result, status=409)

    auth_db.write_audit_log(
        request["current_user"]["sub"], "setup_wizard",
        metadata={"mode": mode},
        ip_address=request.remote,
    )
    logger.info("setup wizard applied: mode=%s by %s", mode, request["current_user"].get("sub"))
    return web.json_response({"ok": True, "mode": mode, **result})


# ── Routing log ────────────────────────────────────────────────────────────────

async def handle_routing_log(request: web.Request) -> web.Response:
    """List routing decisions.  Requires view_audit_logs permission.

    Query params:
      user_id — filter to a specific user
      since   — unix timestamp lower bound
      until   — unix timestamp upper bound
      page    — page number (default 1)
      limit   — max rows per page (default 50, max 200)
    """
    page     = max(1, int(request.rel_url.query.get("page",  1)))
    limit    = min(200, int(request.rel_url.query.get("limit", 50)))
    user_id  = request.rel_url.query.get("user_id") or None
    since    = request.rel_url.query.get("since")
    until    = request.rel_url.query.get("until")

    rows, total = auth_db.list_routing_log(
        user_id=user_id,
        since=int(since)  if since else None,
        until=int(until)  if until else None,
        page=page,
        limit=limit,
    )
    return web.json_response({"entries": rows, "total": total, "page": page, "limit": limit})


async def handle_costs(request: web.Request) -> web.Response:
    """GET /admin/costs — rollup stats for the Costs dashboard.

    Query params:
      agent_id — filter to a specific agent (optional)
      window   — "1h" | "24h" | "7d" | "30d" | "all" (default 24h)

    Returns aggregated counts, total/average USD, last/largest request,
    and a by-model breakdown. Designed for a single dashboard render —
    the UI polls this every few seconds while the Activity tab is open.
    """
    import time as _time
    agent_id = request.rel_url.query.get("agent_id") or None
    window = (request.rel_url.query.get("window") or "24h").lower()
    now_ms = int(_time.time() * 1000)
    deltas = {"1h": 3600, "24h": 86400, "7d": 86400 * 7, "30d": 86400 * 30}
    since = None
    if window != "all":
        since = now_ms - (deltas.get(window, 86400) * 1000)
    return web.json_response(auth_db.cost_rollup(
        agent_id=agent_id, since_ts=since, until_ts=now_ms,
    ))


async def handle_pricing_status(request: web.Request) -> web.Response:
    """GET /admin/pricing/status — pricing catalogue health."""
    from gateway import pricing
    return web.json_response(pricing.catalogue_summary())


async def handle_pricing_refresh(request: web.Request) -> web.Response:
    """POST /admin/pricing/refresh — force re-fetch from OpenRouter."""
    from gateway import pricing
    count = pricing.ensure_loaded(force_refresh=True)
    return web.json_response({"models_loaded": count, **pricing.catalogue_summary()})


async def handle_spawn_stats(request: web.Request) -> web.Response:
    """Return aggregated sandbox spawn-duration stats.

    Read by the chat UI to render an honest "this usually takes ~Ns" hint
    while a sandbox is provisioning, instead of a hardcoded guess. Two
    buckets: warm (image already in cluster) vs cold (image had to be
    imported — typical when switching models into a fresh cluster).

    Returns null medians when fewer than `MIN_FOR_LEARNED` samples exist
    in a bucket; the UI then falls back to its hardcoded copy.
    """
    from gateway import spawn_metrics
    return web.json_response(spawn_metrics.stats())
