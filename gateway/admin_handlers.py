"""Admin handlers: agents, dispatches, credentials, users, costs."""

import asyncio
import json
import logging
import os
import time as _time
from typing import Optional

from aiohttp import web

import gateway.auth.db as auth_db

logger = logging.getLogger(__name__)

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


# ── Dispatch ledger (M8 Phase B) ─────────────────────────────────────────────

async def handle_dispatches_list(request: web.Request) -> web.Response:
    """GET /admin/dispatches — query the dispatch activity ledger.

    Query params: agent_id, origin, status, q, limit (max 200), offset.

    LOG-25.1: non-admin/operator callers are force-scoped to their own
    dispatches via ``user_id``. Users have ``view_runs`` in RBAC so the
    Runs tab shows up for them, but they should only see their own
    runs — not every other user's history.
    """
    agent_id = request.rel_url.query.get("agent_id")
    origin = request.rel_url.query.get("origin")
    status = request.rel_url.query.get("status")
    q = (request.rel_url.query.get("q") or "").strip() or None
    try:
        limit = min(int(request.rel_url.query.get("limit", 20)), 200)
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(request.rel_url.query.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    # Per-user isolation: admins/operators see everyone; user/viewer
    # only their own. Key off role rather than permissions so a future
    # role with view_runs still inherits this guard unless explicitly
    # granted operator-tier visibility.
    current = request.get("current_user") or {}
    user_id_filter: Optional[str] = None
    if current.get("role") not in ("admin", "operator"):
        user_id_filter = current.get("id") or current.get("sub") or ""
        if not user_id_filter:
            # No identified caller — be restrictive, return empty.
            return web.json_response({
                "dispatches": [], "total": 0,
                "limit": limit, "offset": offset,
            })
    rows, total = auth_db.list_dispatches(
        agent_id=agent_id, origin=origin, status=status, q=q,
        user_id=user_id_filter,
        limit=limit, offset=offset,
    )
    # Expand STAMP JSON columns so the UI doesn't have to parse twice.
    # toolsets_snapshot is stored as a JSON list string; tool_sequence
    # ditto. We hand back arrays (empty when missing or malformed).
    import json as _json
    for r in rows:
        for field in ("toolsets_snapshot", "tool_sequence", "policy_snapshot"):
            raw = r.get(field)
            if raw:
                try:
                    r[field] = _json.loads(raw)
                except Exception:
                    r[field] = []
            else:
                r[field] = []
    # Resolve user_id → username so the Runs table can show who initiated
    # each dispatch. Platform/cron/workflow-origin dispatches have no
    # user_id and render as '—' client-side.
    user_ids = {r["user_id"] for r in rows if r.get("user_id")}
    user_map: dict[str, str] = {}
    for uid in user_ids:
        u = auth_db.get_user_by_id(uid)
        if u:
            user_map[uid] = u.get("username") or u.get("email") or uid
    for r in rows:
        if r.get("user_id"):
            r["username"] = user_map.get(r["user_id"], r["user_id"])
    return web.json_response({
        "dispatches": rows, "total": total,
        "limit": limit, "offset": offset,
    })


# ── Named agents ─────────────────────────────────────────────────────────────

async def handle_agents_list(request: web.Request) -> web.Response:
    user = request.get("current_user") or {}
    agents = auth_db.list_agents(user.get("id", ""))

    # Augment each agent with the deterministic worker_id / sandbox_name its
    # sandbox runs under, plus live state from the sandbox backend. The
    # frontend uses this to grey out the chat UI until the sandbox is
    # ready. NullBackend (until E2) reports every agent as disconnected.
    from gateway.sandbox_backend import get_backend, sanitize_sandbox_name
    backend = request.app.get("sandbox_backend") or get_backend()

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

    for a in agents:
        name = a.get("name", "")
        sandbox_name = sanitize_sandbox_name(f"hermes-{name}") if name else ""
        a["sandbox_name"] = sandbox_name
        a["worker_id"] = sandbox_name
        try:
            st = backend.status(sandbox_name) if sandbox_name else None
        except Exception:
            st = None
        running = bool(st and st.running)
        a["worker_healthy"] = running
        a["worker_status"] = st.state if (st and running) else "disconnected"
        a["worker_connected"] = running

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

    # LOG-25.5: per-user agent limit. Admins bypass; everyone else is
    # capped at their users.max_agents row value (NULL = unlimited, the
    # default). Deny before we create the DB row so the UI gets a clean
    # 429 + reason to surface to the user.
    if user.get("role") != "admin":
        uid = user.get("id") or ""
        user_row = auth_db.get_user_by_id(uid) if uid else None
        cap = (user_row or {}).get("max_agents")
        if cap is not None:
            try: cap_int = int(cap)
            except (TypeError, ValueError): cap_int = 0
            if cap_int >= 0:
                existing = auth_db.count_agents_by_creator(uid)
                if existing >= cap_int:
                    return web.json_response(
                        {
                            "error": "agent_limit_reached",
                            "detail": (
                                f"You've reached your agent limit "
                                f"({existing}/{cap_int}). Delete one of your "
                                f"existing agents or ask an admin to raise "
                                f"your cap."
                            ),
                            "existing": existing,
                            "limit": cap_int,
                        },
                        status=429,
                    )
    import json as _json
    # The per-agent toolsets selector was removed from the UI (2026-04-21)
    # because the v1 sandbox config builder hardcoded
    # `hermes-cli` for every platform regardless of what this column says.
    # We still write the column so readers always find a list. Legacy
    # callers that still POST `toolsets` are honoured — the selector is
    # gone from the UI but the API contract isn't breaking.
    toolsets_raw = body.get("toolsets")
    if not isinstance(toolsets_raw, list) or not toolsets_raw:
        toolsets_raw = ["hermes-cli"]
    toolsets_str = _json.dumps(toolsets_raw)
    soul_slug = (body.get("soul_slug") or "general").strip()
    model = (body.get("model") or "").strip()
    # "Auto" / empty model: pin to the gateway's currently active model so
    # the agent card displays a concrete value and the executor + sandbox
    # see the same model the rest of the gateway is using. Without this,
    # the DB record stays empty and the world-view card shows no model
    # badge even though the executor's spawn-time fallback gives the
    # sandbox a working model.
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
    agent = auth_db.create_agent(
        name=name,
        soul_slug=soul_slug,
        model=model,
        description=(body.get("description") or "").strip(),
        creator_id=user.get("id", ""),
        shared=body.get("shared", True),
        toolsets=toolsets_str,
        char_index=char_index,
    )

    # Duplicate-from: when the caller came through the "Duplicate" action
    # on an existing agent, copy the source agent's persisted state
    # (memories + sessions) into the new agent's dir. Logs are
    # intentionally NOT copied — they're execution history of the source
    # and would mislead future observability on the clone. The sandbox
    # itself is always spawned fresh on first dispatch; copying the
    # memories dir is what makes the duplicate start with the source's
    # accumulated knowledge rather than a blank slate. Best-effort:
    # failures here don't undo the agent creation (user can always
    # delete + retry), but we surface them in the response.
    duplicate_from_id = body.get("duplicate_from")
    duplicate_result = None
    if duplicate_from_id:
        try:
            import pathlib as _pathlib
            import shutil as _shutil
            src_agent = auth_db.get_agent(str(duplicate_from_id))
            if not src_agent:
                duplicate_result = {"ok": False, "error": "source_not_found"}
            else:
                src_name = src_agent.get("name") or ""
                logos_home = _pathlib.Path(
                    os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME")
                    or str(_pathlib.Path.home() / ".logos")
                )
                src_dir = logos_home / "agents" / src_name
                dst_dir = logos_home / "agents" / name
                copied = []
                for sub in ("memories", "sessions"):
                    src_sub = src_dir / sub
                    if src_sub.is_dir():
                        dst_sub = dst_dir / sub
                        dst_sub.mkdir(parents=True, exist_ok=True)
                        for p in src_sub.iterdir():
                            if p.is_file():
                                _shutil.copy2(p, dst_sub / p.name)
                        copied.append(sub)
                duplicate_result = {"ok": True, "from": src_name, "copied": copied}
                logger.info(
                    "create_agent(%s): duplicated %s from %s",
                    name, copied, src_name,
                )
        except Exception as _dexc:
            logger.exception("create_agent(%s): duplicate copy failed", name)
            duplicate_result = {"ok": False, "error": str(_dexc)[:200]}

    # The v1 runtime spawned an OpenShell sandbox here. No sandbox
    # backend until E2: the agent row exists, chat answers 503
    # no_agents_running until a backend can run it.
    spawn_result = None

    resp = dict(agent)
    if spawn_result:
        resp["spawn"] = spawn_result
    if duplicate_result is not None:
        resp["duplicate"] = duplicate_result
    return web.json_response(resp, status=201)


async def handle_agents_patch(request: web.Request) -> web.Response:
    aid = request.match_info["id"]
    existing = auth_db.get_agent(aid)
    if not existing:
        return web.json_response({"error": "not_found"}, status=404)
    # Ownership check: only the creator or an admin can modify an agent.
    # Other users can chat with shared agents but shouldn't be able to
    # rename, retool, or repurpose them. Auth middleware stores the
    # user on ``request["current_user"]`` (dict) — NOT ``request._user``
    # (attribute; that was a stale pattern that silently evaluated to
    # ``{}`` and made the check fail for EVERYONE including the real
    # creator and admins). Also note the user-id field is ``sub``,
    # not ``id`` (matching JWT claim name used across the rest of
    # the codebase).
    req_user = request.get("current_user") or {}
    req_user_id = req_user.get("sub") or ""
    req_role = req_user.get("role") or ""
    if req_role != "admin" and existing.get("creator_id") and existing["creator_id"] != req_user_id:
        return web.json_response(
            {"error": "Only the agent's creator or an admin can modify this agent"},
            status=403,
        )
    body = await request.json()
    import json as _json
    updates = {}
    for k in ("name", "soul_slug", "model", "description", "shared", "char_index",
              "daily_budget_usd"):
        if k in body:
            updates[k] = body[k]
    if "toolsets" in body:
        updates["toolsets"] = _json.dumps(body["toolsets"]) if isinstance(body["toolsets"], list) else ""
    renamed_from: Optional[str] = None
    if "name" in updates and updates["name"] != existing["name"]:
        dup = auth_db.get_agent_by_name(updates["name"])
        if dup and dup["id"] != aid:
            return web.json_response({"error": "An agent with that name already exists"}, status=409)
        renamed_from = existing["name"]
    agent = auth_db.update_agent(aid, **updates)

    # On rename: move the per-agent host data dir to the new name so
    # memories / logs follow the agent. Runs in the background so the
    # PATCH response returns immediately. (The v1 runtime also reaped the
    # old `hermes-<old_name>` sandbox here; no sandbox backend until E2.)
    if renamed_from:
        import asyncio as _asyncio
        import pathlib as _pathlib
        import shutil as _shutil
        _HH = _pathlib.Path(
            os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME")
            or str(_pathlib.Path.home() / ".logos")
        )
        new_name = agent["name"]
        async def _rename_bg():
            try:
                old_dir = _HH / "agents" / renamed_from
                new_dir = _HH / "agents" / new_name
                if old_dir.exists() and not new_dir.exists():
                    await _asyncio.to_thread(_shutil.move, str(old_dir), str(new_dir))
                    logger.info(
                        "Rename: moved host data dir %s → %s", old_dir, new_dir,
                    )
            except Exception as exc:
                logger.warning(
                    "Rename: failed to move host data for '%s' → '%s': %s",
                    renamed_from, new_name, exc,
                )
        _asyncio.create_task(_rename_bg())

    return web.json_response(agent)


async def handle_agents_delete(request: web.Request) -> web.Response:
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    # Same auth-source fix as the update handler above:
    # request["current_user"] (dict) + "sub" field, NOT request._user
    # + "id".
    req_user = request.get("current_user") or {}
    req_user_id = req_user.get("sub") or ""
    req_role = req_user.get("role") or ""
    if req_role != "admin" and agent.get("creator_id") and agent["creator_id"] != req_user_id:
        return web.json_response(
            {"error": "Only the agent's creator or an admin can delete this agent"},
            status=403,
        )

    # delete_agent() wipes ~/.logos/agents/<name>/. (The v1 runtime also
    # destroyed the agent's OpenShell sandbox here; no backend until E2.)
    auth_db.delete_agent(aid)
    return web.Response(status=204)


# ── Agent tools ──────────────────────────────────────────────────────────────


async def handle_agent_tools_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/tools — state for the T pill.

    ``toolsets`` is empty until the E2 sandbox backend can be asked what
    the agent has. ``presets`` (v1 OpenShell network presets, removed in
    S1.6) stays in the response as empty lists because the dashboard
    still reads it.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)

    # ── Application layer: sandbox toolsets + availability ──
    #
    # The agent's own sandbox is the only source of truth for what
    # toolsets hermes actually has. The v1 probe (openshell sandbox exec)
    # went with OpenShell 0.0.x in S1.5; until E2 there is no sandbox to
    # ask, so the list is empty and ``error`` explains why.
    available_toolsets: list[dict] = []
    toolsets_source = "unavailable"
    toolsets_error: Optional[str] = (
        "No agents are running yet: the v2 sandbox backend arrives in epic E2."
    )

    return web.json_response({
        "toolsets": {
            "available": available_toolsets,
            "source": toolsets_source,
            "error": toolsets_error,
        },
        "presets": {"applied": [], "available": []},
    })


# ── Per-agent channel credentials ────────────────────────────────────────────
# Backend for the Agent editor → Channels subtab. Each agent owns its own
# Telegram/Discord/Slack/WhatsApp bot tokens; the adapter-per-credential
# lifecycle in gateway/run.py spawns one adapter instance per enabled row.


def _scrub_credential_row(row: dict) -> dict:
    """Return the row with the raw token replaced by a masked preview.

    The token is a secret; the UI shows a short fingerprint so the user
    can tell rows apart ("is this the prod one?") without the full
    value being exposed in HTTP responses or browser devtools.
    """
    tok = row.get("token") or ""
    masked = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "••••"
    return {**{k: v for k, v in row.items() if k != "token"}, "token_preview": masked}


def _refresh_sandbox_channel_env(request: web.Request, agent_name: str) -> bool:
    """Best-effort hot-refresh of a running sandbox's channel credentials
    after the UI edits a row (LOG-44.3.4).

    The v1 OpenShell executor did this; there is no sandbox backend until
    E2, so this returns False. The DB row is the source of truth and the
    sandbox picks the change up when it is (re)created.
    """
    del request, agent_name
    return False


async def handle_agent_channels_list(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/channels

    Returns the agent's credential rows with tokens masked — the UI
    never needs the raw value after a token is saved, only the
    preview for visual identification.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        rows = auth_db.list_agent_channel_credentials(agent_id=aid)
    except Exception as exc:
        logger.exception("channels list failed for agent=%s", aid)
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"credentials": [_scrub_credential_row(r) for r in rows]})


async def handle_agent_channels_post(request: web.Request) -> web.Response:
    """POST /admin/agents/{id}/channels

    Body: ``{"platform": "telegram", "token": "xxx", "label": "prod", "enabled": true}``

    Upserts a credential row on the (agent_id, platform, label) key. When
    label is omitted, "default" is used — matching the single-bot-per-agent
    shape produced by the env→DB migration in gateway/run.py. Optionally
    validates the token with the platform API before storing, so the UI
    can show the caller a "Connected as @hermesbot" confirmation in the
    same round-trip.

    Side effect: once stored, the adapter lifecycle picks it up on the
    next gateway restart. Hot-reconnect is a follow-up (it'd need to
    disconnect the old adapter and spawn one for the new row).
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    platform = (body.get("platform") or "").strip().lower()
    token = (body.get("token") or "").strip()
    label = (body.get("label") or "default").strip() or "default"
    enabled = bool(body.get("enabled", True))
    validate = bool(body.get("validate", False))

    if not platform:
        return web.json_response({"error": "platform is required"}, status=400)
    if not token:
        return web.json_response({"error": "token is required"}, status=400)

    # Optional validation before persisting — reuses the existing
    # messaging catalogue definitions (auth.test / getMe / etc).
    validation_result = None
    if validate:
        try:
            from gateway.services import validate_messaging_credential, MESSAGING_INTEGRATIONS
            env_var_by_platform = {
                "telegram": "TELEGRAM_BOT_TOKEN",
                "discord":  "DISCORD_BOT_TOKEN",
                "slack":    "SLACK_BOT_TOKEN",
                "whatsapp": "WHATSAPP_TOKEN",
            }
            env_var = env_var_by_platform.get(platform)
            if env_var and env_var in MESSAGING_INTEGRATIONS:
                validation_result = await validate_messaging_credential(env_var, token)
                if not validation_result.get("ok"):
                    return web.json_response(
                        {"error": "validation_failed", "detail": validation_result},
                        status=400,
                    )
        except Exception as exc:
            logger.exception("channels validate failed")
            return web.json_response({"error": str(exc)}, status=500)

    metadata = (validation_result or {}).get("details") or None

    try:
        row = auth_db.upsert_agent_channel_credential(
            agent_id=aid,
            platform=platform,
            token=token,
            label=label,
            enabled=enabled,
            metadata=metadata,
        )
    except Exception as exc:
        logger.exception("channels upsert failed")
        return web.json_response({"error": str(exc)}, status=500)

    logger.info(
        "channels_post(agent=%s, platform=%s, label=%s): stored row %s",
        aid, platform, label, row.get("id"),
    )

    # LOG-44.3: bot connection happens in-sandbox via hermes's own
    # platform adapters, not in the Logos gateway process.
    # _refresh_sandbox_channel_env below pushes the new token into the
    # sandbox's .env and bounces hermes so it picks the bot up.
    connect_result = None

    # The v1 gateway granted the `messaging` toolset + a platform
    # network preset here. Presets were removed in S1.6; in v2 network
    # access comes from the agent's own policy (agents/<name>.yaml).
    # The key stays in the response for the dashboard.
    access_result = None

    # Fire-and-forget the sandbox .env refresh (see delete handler
    # for why — synchronous call blocks the event loop for ~30s).
    asyncio.create_task(
        asyncio.to_thread(_refresh_sandbox_channel_env, request, agent["name"])
    )

    return web.json_response({
        "credential": _scrub_credential_row(row),
        "validated": validation_result,
        "connected": connect_result,
        "access": access_result,
        "sandbox_refresh_scheduled": True,
    })


async def handle_agent_channels_delete(request: web.Request) -> web.Response:
    """DELETE /admin/agents/{id}/channels/{cred_id}"""
    aid = request.match_info["id"]
    cred_id = request.match_info["cred_id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    row = auth_db.get_agent_channel_credential(cred_id)
    if not row or row.get("agent_id") != aid:
        return web.json_response({"error": "not_found"}, status=404)
    # LOG-44.3: in-sandbox hermes is what runs the bot. Dropping the DB
    # row + refreshing the sandbox .env (below) is enough to stop the
    # poller — no Logos-side adapter to disconnect.
    try:
        auth_db.delete_agent_channel_credential(cred_id)
    except Exception as exc:
        logger.exception("channels delete failed")
        return web.json_response({"error": str(exc)}, status=500)

    # Fire-and-forget the sandbox .env refresh so the HTTP delete
    # returns immediately (a no-op until the E2 sandbox backend).
    # The DB row is already gone; the sandbox picks up the token
    # removal on this refresh or the next respawn, whichever comes
    # first.
    asyncio.create_task(
        asyncio.to_thread(_refresh_sandbox_channel_env, request, agent["name"])
    )

    return web.json_response({"ok": True, "sandbox_refresh_scheduled": True})


async def handle_agent_channels_toggle(request: web.Request) -> web.Response:
    """POST /admin/agents/{id}/channels/{cred_id}/toggle

    Body: ``{"enabled": true|false}``
    """
    aid = request.match_info["id"]
    cred_id = request.match_info["cred_id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    row = auth_db.get_agent_channel_credential(cred_id)
    if not row or row.get("agent_id") != aid:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    enabled = bool(body.get("enabled"))
    try:
        auth_db.set_agent_channel_credential_enabled(cred_id, enabled)
    except Exception as exc:
        logger.exception("channels toggle failed")
        return web.json_response({"error": str(exc)}, status=500)

    # LOG-44.3: enable/disable flows through the sandbox .env refresh
    # below. Adding the token back / pulling it out there is what
    # starts / stops hermes's in-sandbox bot poller.

    # Fire-and-forget the sandbox .env refresh (see delete handler
    # for why — synchronous call blocks the event loop for ~30s).
    asyncio.create_task(
        asyncio.to_thread(_refresh_sandbox_channel_env, request, agent["name"])
    )

    refreshed = auth_db.get_agent_channel_credential(cred_id)
    return web.json_response({
        "credential": _scrub_credential_row(refreshed or row),
        "sandbox_refresh_scheduled": True,
    })


# ── Per-agent cloud-tool env credentials (plan-C, 2026-04-21) ──────────────
# Handlers for the /setup slash command in chat: save/delete env vars that
# land in the sandbox's .env and persist in agent_env_credentials for
# respawn replay. Mirrors the agent_channels_* handlers above but with
# (env_var, value) semantics instead of (platform, label, token).


def _mask_env_value(value: str) -> str:
    """Return a visual-identifier preview of a credential value without
    leaking it. Long values show first-4 + ••• + last-4, short ones just
    get fully masked. Never send the full value back to the browser.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}•••{value[-4:]}"


def _scrub_env_cred_row(row: dict) -> dict:
    """Shape an agent_env_credentials row for the UI — drop the raw value
    and substitute a preview."""
    if not row:
        return {}
    out = dict(row)
    out["value_preview"] = _mask_env_value(out.pop("value", "") or "")
    return out


async def handle_agent_env_credentials_list(request: web.Request) -> web.Response:
    """GET /api/admin/agents/{id}/env-credentials

    Returns the agent's env credential rows with values masked. Used by
    the chat-side /setup card to know which vars are Logos-managed.
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        rows = auth_db.list_agent_env_credentials(agent_id=aid)
    except Exception as exc:
        logger.exception("env-credentials list failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({
        "credentials": [_scrub_env_cred_row(r) for r in rows],
    })


async def handle_agent_env_credentials_post(request: web.Request) -> web.Response:
    """POST /api/admin/agents/{id}/env-credentials

    Body: ``{"env_var": "FIRECRAWL_API_KEY", "value": "fc-xxx",
             "description": "Firecrawl cloud API"}``

    Upserts on (agent_id, env_var). Persists to agent_env_credentials
    AND rewrites the sandbox .env in place via
    ``refresh_channel_credentials`` (which re-runs ``build_channel_extra_env``
    — that now pulls from both credential tables — and restarts hermes
    so the new env is loaded).
    """
    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    env_var = (body.get("env_var") or "").strip().upper()
    value   = (body.get("value") or "").strip()
    description = body.get("description") or None
    if not env_var or not value:
        return web.json_response(
            {"error": "env_var_and_value_required"}, status=400,
        )
    # Basic shape validation — env var names must look like identifiers.
    # Prevents accidental injection of whole lines ("KEY=x\nOTHER=y") and
    # any env-name that would choke the .env parser inside the sandbox.
    if not env_var.replace("_", "").isalnum() or env_var[0].isdigit():
        return web.json_response(
            {"error": "invalid_env_var_name",
             "detail": "Must match [A-Z_][A-Z0-9_]* (all caps recommended)."},
            status=400,
        )
    # Messaging tokens live in agent_channel_credentials — reject them
    # here so the user doesn't end up with two copies of a bot token
    # that can drift out of sync.
    _MESSAGING_ENVS = {
        "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN",
        "WHATSAPP_TOKEN", "MATRIX_ACCESS_TOKEN", "SIGNAL_TOKEN",
    }
    if env_var in _MESSAGING_ENVS:
        return web.json_response(
            {"error": "use_channel_credentials",
             "detail": f"{env_var} is a messaging token — set it via the Messaging tab so Logos can manage the platform adapter."},
            status=400,
        )

    try:
        row = auth_db.save_agent_env_credential(
            aid, env_var, value,
            enabled=True, description=description,
        )
    except Exception as exc:
        logger.exception("env-credentials save failed")
        return web.json_response({"error": str(exc)}, status=500)

    # Fire-and-forget the sandbox .env refresh — same pattern as the
    # channel-credential handlers. refresh_channel_credentials re-runs
    # build_channel_extra_env (which reads from both tables) and
    # restarts hermes. If the sandbox isn't up yet, this silently
    # fails and the key lands on next spawn via build_channel_extra_env.
    asyncio.create_task(
        asyncio.to_thread(_refresh_sandbox_channel_env, request, agent["name"])
    )

    return web.json_response({
        "credential": _scrub_env_cred_row(row),
        "sandbox_refresh_scheduled": True,
    })


async def handle_agent_env_credentials_delete(request: web.Request) -> web.Response:
    """DELETE /api/admin/agents/{id}/env-credentials/{env_var}

    Drops the row from agent_env_credentials and refreshes the sandbox
    .env so hermes loses the key immediately. Uses env_var as the URL
    segment (not id) because it's the natural key the UI has.
    """
    aid = request.match_info["id"]
    env_var = (request.match_info.get("env_var") or "").strip().upper()
    if not env_var:
        return web.json_response({"error": "env_var_required"}, status=400)
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    try:
        auth_db.delete_agent_env_credential_by_var(aid, env_var)
    except Exception as exc:
        logger.exception("env-credentials delete failed")
        return web.json_response({"error": str(exc)}, status=500)

    asyncio.create_task(
        asyncio.to_thread(_refresh_sandbox_channel_env, request, agent["name"])
    )
    return web.json_response({"ok": True, "sandbox_refresh_scheduled": True})


# ── User ↔ platform identity links ────────────────────────────────────────
# Maps a Logos user to their identity on each messaging platform so
# dispatch rows show real names and agent-edit permissions are scoped.


async def handle_platform_links_list(request: web.Request) -> web.Response:
    """GET /api/admin/platform-links[?user_id=...]"""
    user_id = request.query.get("user_id")
    try:
        rows = auth_db.list_user_platform_links(user_id=user_id or None)
    except Exception as exc:
        logger.exception("platform-links list failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"links": rows})


async def handle_platform_links_post(request: web.Request) -> web.Response:
    """POST /api/admin/platform-links

    Body: ``{"user_id": "...", "platform": "telegram", "platform_uid": "123456789", "display_name": "Alex"}``
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id = (body.get("user_id") or "").strip()
    platform = (body.get("platform") or "").strip().lower()
    platform_uid = str(body.get("platform_uid") or "").strip()
    display_name = (body.get("display_name") or "").strip() or None
    if not user_id or not platform or not platform_uid:
        return web.json_response({"error": "user_id, platform, and platform_uid required"}, status=400)
    try:
        row = auth_db.upsert_user_platform_link(user_id, platform, platform_uid, display_name)
    except Exception as exc:
        logger.exception("platform-links upsert failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"link": row})


async def handle_platform_links_delete(request: web.Request) -> web.Response:
    """DELETE /api/admin/platform-links/{id}"""
    lid = request.match_info["id"]
    try:
        auth_db.delete_user_platform_link(lid)
    except Exception as exc:
        logger.exception("platform-links delete failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True})


# ── Internal sandbox-facing endpoints ──────────────────────────────────────
# Called by agent code running inside OpenShell sandboxes, NOT by the
# dashboard or CLI. Reached via host.openshell.internal:8091/api/internal/*.
# No auth — the L7 proxy + network policy is the trust boundary.


# ── Embedding stats + backfill ────────────────────────────────────────────


async def handle_internal_embedding_stats(request: web.Request) -> web.Response:
    """GET /api/internal/embedding-stats — coverage report."""
    try:
        runner = request.app.get("runner") if request.app else None
        db = getattr(runner.session_store, "_db", None) if runner else None
        if not db:
            return web.json_response({"error": "session DB not available"}, status=503)
        return web.json_response(db.embedding_stats())
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_internal_embedding_backfill(request: web.Request) -> web.Response:
    """POST /api/internal/embedding-backfill — embed unembedded messages.

    Runs synchronously (blocking) for up to `batch_size` messages.
    Call repeatedly until coverage reaches 100%.
    """
    import asyncio as _asyncio
    try:
        batch = int(request.query.get("batch", "50"))
    except ValueError:
        batch = 50
    try:
        runner = request.app.get("runner") if request.app else None
        db = getattr(runner.session_store, "_db", None) if runner else None
        if not db:
            return web.json_response({"error": "session DB not available"}, status=503)
        loop = _asyncio.get_event_loop()
        count = await loop.run_in_executor(None, lambda: db.backfill_embeddings(batch_size=batch))
        stats = db.embedding_stats()
        return web.json_response({"embedded_this_batch": count, **stats})
    except Exception as exc:
        logger.exception("embedding backfill failed")
        return web.json_response({"error": str(exc)}, status=500)


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


async def handle_agent_logs_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/logs?tail=N — return the last N lines of
    ``~/.logos/agents/<name>/logs/agent.log``.

    Query params:
      tail   Number of trailing lines (default 200, max 2000).
      errors If set ("1", "true"), return errors.log instead of agent.log.

    Returns JSON::

        {
          "agent": "Adam",
          "path": "/home/.../Adam/logs/agent.log",
          "lines": ["2026-04-14 11:24:52 INFO ...", ...],
          "truncated": false,     // true when file had more lines than tail
          "size_bytes": 12345,
          "mtime_iso": "2026-04-14T11:24:52"
        }

    403 when the caller isn't admin/operator. 404 when the agent exists
    but has no log file yet (new agent, no dispatches). 204 (empty body)
    when the log rotated/is empty.
    """
    # RBAC: only admins + operators can read raw logs. Auth middleware
    # stores decoded claims under ``request["current_user"]`` — mirror
    # the other admin handlers in this module rather than inventing a
    # new auth shape.
    user = request.get("current_user") or {}
    role = user.get("role", "")
    if role not in ("admin", "operator"):
        return web.json_response({"error": "forbidden", "role_seen": role}, status=403)

    aid = request.match_info["id"]
    agent = auth_db.get_agent(aid)
    if not agent:
        return web.json_response({"error": "agent_not_found"}, status=404)
    name = agent.get("name") or ""
    if not name:
        return web.json_response({"error": "agent_has_no_name"}, status=400)

    try:
        tail = int(request.query.get("tail", "200"))
    except (TypeError, ValueError):
        tail = 200
    tail = max(1, min(tail, 2000))

    errors_only = request.query.get("errors", "").lower() in ("1", "true", "yes")
    log_name = "errors.log" if errors_only else "agent.log"

    import pathlib as _pathlib
    import datetime as _dt
    logos_home = _pathlib.Path(
        os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME")
        or str(_pathlib.Path.home() / ".logos")
    )
    log_path = logos_home / "agents" / name / "logs" / log_name

    # The v1 runtime pulled the sandbox's hermes log dir into the host
    # copy before reading; gone with OpenShell 0.0.x (S1.5). Reads the
    # on-disk copy only.

    if not log_path.exists():
        return web.json_response({
            "agent": name,
            "path": str(log_path),
            "lines": [],
            "truncated": False,
            "size_bytes": 0,
            "mtime_iso": None,
            "exists": False,
        })

    try:
        stat = log_path.stat()
        # Read only the tail of the file to keep memory bounded — even
        # for multi-GB logs we pull the last ~256KB and slice to the
        # requested line count, never the whole file.
        READ_MAX = 512 * 1024
        size = stat.st_size
        with log_path.open("rb") as f:
            if size > READ_MAX:
                f.seek(size - READ_MAX)
                # Skip partial first line
                f.readline()
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        tail_lines = all_lines[-tail:] if len(all_lines) > tail else all_lines
        truncated = (len(all_lines) > tail) or (size > READ_MAX)
        return web.json_response({
            "agent": name,
            "path": str(log_path),
            "lines": tail_lines,
            "truncated": truncated,
            "size_bytes": size,
            "mtime_iso": _dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "exists": True,
            "log_name": log_name,
        })
    except Exception as exc:
        logger.exception("handle_agent_logs_get failed for %s", name)
        return web.json_response({"error": "read_failed", "detail": str(exc)[:200]}, status=500)


# ── Per-agent lifetime state ─────────────────────────────────────────────
#
# Three read-only endpoints that back the "🧠 Mind" modal on each agent
# card. They expose what persists across sessions and makes a named agent
# feel like a continuing entity: curated memories, past session
# metadata, and rolled-up activity counters. Session transcripts are
# intentionally NOT exposed here — listing is enough for v1, and a full
# transcript viewer can be added later once we decide how to clamp big
# ones (some agents have 2k-message histories).
#
# All three require admin/operator for the same reason logs do: memory
# entries can contain user-private context, and the session metadata
# reveals which chats the agent has had.


def _resolve_agent_dir(agent_id: str):
    """Look up an agent by id, return (name, host_dir, None) on success
    or (None, None, err_response) on failure.

    Shared preamble for the state handlers below — agents live under
    ``~/.logos/agents/<name>/`` where ``<name>`` is the DB name (not
    the id). Rejects ids that don't resolve to a real agent.
    """
    import pathlib as _pathlib
    agent = auth_db.get_agent(agent_id)
    if not agent:
        return None, None, web.json_response({"error": "agent_not_found"}, status=404)
    name = agent.get("name") or ""
    if not name:
        return None, None, web.json_response({"error": "agent_has_no_name"}, status=400)
    logos_home = _pathlib.Path(
        os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME")
        or str(_pathlib.Path.home() / ".logos")
    )
    return name, logos_home / "agents" / name, None


async def handle_agent_memories_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/memories — curated memory entries.

    Reads ``MEMORY.md`` and ``USER.md`` from
    ``~/.logos/agents/<name>/memories/`` — the same directory the
    executor uploads into each sandbox on spawn. Entries are separated
    by the ``\\n§\\n`` delimiter the memory tool uses (see
    ``tools/memory_tool.py``).

    Response shape::

        {
          "agent": "Hermes",
          "memory": {
            "path": "/home/.../Hermes/memories/MEMORY.md",
            "entries": ["fact one", "fact two", ...],
            "size_bytes": 1234,
            "mtime_iso": "2026-04-15T22:30:10",
            "exists": true
          },
          "user": { ...same shape... }
        }

    ``exists: false`` with ``entries: []`` when a file hasn't been
    written yet — expected for fresh agents and rendered by the UI as
    "no memories yet" rather than an error.
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err

    import datetime as _dt
    mem_dir = agent_dir / "memories"

    def _read(path):
        if not path.exists():
            return {"path": str(path), "entries": [], "size_bytes": 0,
                    "mtime_iso": None, "exists": False}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            # Entry delimiter matches tools/memory_tool.py ENTRY_DELIMITER.
            entries = [e.strip() for e in text.split("\n§\n") if e.strip()]
            st = path.stat()
            return {
                "path": str(path),
                "entries": entries,
                "size_bytes": st.st_size,
                "mtime_iso": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "exists": True,
            }
        except Exception as exc:
            return {"path": str(path), "entries": [], "size_bytes": 0,
                    "mtime_iso": None, "exists": True, "error": str(exc)[:200]}

    return web.json_response({
        "agent": name,
        "memory": _read(mem_dir / "MEMORY.md"),
        "user":   _read(mem_dir / "USER.md"),
    })


async def handle_agent_skills_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/skills — agent-authored skills list.

    Walks the host mirror at ``~/.logos/agents/<name>/skills/`` (populated
    by the v1 runtime's periodic sync from the sandbox's
    ``$HERMES_HOME/skills/``). Each SKILL.md is parsed for its YAML
    frontmatter; body preview is the first 400 chars of the non-
    frontmatter content. Entries sort newest-first by mtime, capped at
    500 to bound response size.

    Response shape::

        {
          "agent": "Hermes",
          "root":  "/home/.../Hermes/skills",
          "exists": true,
          "skills": [
            {
              "name": "haiku-writer",
              "description": "Composes three-line haiku in 5-7-5",
              "category": "writing",
              "version": "1.0.0",
              "path": "skills/writing/haiku-writer/SKILL.md",
              "size_bytes": 842,
              "mtime_iso": "2026-04-22T13:55:12",
              "body_preview": "## When to Use\\nUser asks for a haiku..."
            }
          ]
        }
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err

    import datetime as _dt
    import re as _re
    import yaml as _yaml

    skills_dir = agent_dir / "skills"
    if not skills_dir.is_dir():
        return web.json_response({
            "agent": name, "root": str(skills_dir),
            "exists": False, "skills": [],
        })

    # SKILL.md canonical frontmatter fence: starts with `---\n`, ends
    # with `\n---\n`. Allow optional leading whitespace / BOM but nothing
    # fancier — hermes-agent's _validate_frontmatter is equally strict.
    _FM_RE = _re.compile(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", _re.DOTALL)
    MAX_BYTES = 256 * 1024

    rows = []
    for skill_md in skills_dir.rglob("SKILL.md"):
        try:
            st = skill_md.stat()
        except OSError:
            continue
        entry = {
            "path": str(skill_md.relative_to(agent_dir)),
            "size_bytes": st.st_size,
            "mtime_iso": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "name": skill_md.parent.name,
            "description": "",
            "category": "",
            "version": "",
            "body_preview": "",
        }
        if st.st_size > MAX_BYTES:
            entry["error"] = "too_large"
            rows.append(entry)
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            entry["error"] = f"read_failed: {str(exc)[:120]}"
            rows.append(entry)
            continue

        m = _FM_RE.match(text)
        if not m:
            entry["error"] = "no_frontmatter"
            entry["body_preview"] = text[:400].strip()
            rows.append(entry)
            continue

        fm_raw, body = m.group(1), m.group(2)
        try:
            fm = _yaml.safe_load(fm_raw) or {}
        except _yaml.YAMLError as exc:
            entry["error"] = f"yaml_error: {str(exc)[:120]}"
            entry["body_preview"] = body[:400].strip()
            rows.append(entry)
            continue

        if isinstance(fm, dict):
            entry["name"] = (fm.get("name") or entry["name"]).strip() or entry["name"]
            entry["description"] = (fm.get("description") or "").strip()
            entry["version"] = str(fm.get("version") or "").strip()
            # Category: frontmatter wins, else parent-of-SKILL.md's
            # parent dir (skills/<category>/<name>/SKILL.md layout).
            cat = ""
            meta = fm.get("metadata")
            if isinstance(meta, dict):
                hermes_meta = meta.get("hermes")
                if isinstance(hermes_meta, dict):
                    cat = str(hermes_meta.get("category") or "").strip()
            if not cat and skill_md.parent.parent != skills_dir:
                cat = skill_md.parent.parent.name
            entry["category"] = cat

        entry["body_preview"] = body[:400].strip()
        rows.append(entry)

    # Newest first, cap at 500 so a runaway skills tree doesn't blow
    # the JSON response.
    rows.sort(key=lambda r: r.get("mtime_iso", ""), reverse=True)
    rows = rows[:500]

    return web.json_response({
        "agent": name,
        "root":  str(skills_dir),
        "exists": True,
        "skills": rows,
    })


async def handle_agent_sessions_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/sessions?limit=N&offset=K — session list.

    Returns metadata (not transcripts) for every
    ``session_*.json`` under ``~/.logos/agents/<name>/sessions/``,
    sorted newest-first by ``last_updated``. Paginated because an
    active agent can accumulate hundreds of sessions over time.

    Per-session shape::

        {
          "id": "20260414_222925_3e0d43",
          "started": "2026-04-14T22:29:25.xxx",
          "last_updated": "2026-04-14T22:30:01.xxx",
          "model": "qwen3.5-9b",
          "msg_count": 8,
          "preview": "Hi!"   // first user message, truncated to 120 chars
        }

    Top-level wrapper carries ``total`` so the UI can render "showing
    50 of 217" without a second call.
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err

    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 500))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0

    sess_dir = agent_dir / "sessions"
    if not sess_dir.is_dir():
        return web.json_response({
            "agent": name, "sessions": [], "total": 0,
            "offset": offset, "limit": limit, "exists": False,
        })

    # Sort by mtime desc for newest-first without opening each file.
    # A follow-up pass reads the JSON for the slice we're returning to
    # pull out model + msg_count + preview. Opening the whole directory
    # would be wasteful for agents with thousands of sessions.
    import json as _json
    try:
        files = sorted(
            sess_dir.glob("session_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        return web.json_response({"error": "list_failed", "detail": str(exc)[:200]}, status=500)

    total = len(files)
    page = files[offset:offset + limit]
    sessions = []
    for p in page:
        item = {"id": p.stem.replace("session_", ""), "file": p.name}
        try:
            data = _json.loads(p.read_text(encoding="utf-8", errors="replace"))
            item["started"] = data.get("session_start")
            item["last_updated"] = data.get("last_updated")
            item["model"] = data.get("model")
            msgs = data.get("messages") or []
            item["msg_count"] = len(msgs)
            # Preview = first non-system message content, truncated.
            # System prompts are noise in a list view; the model identity
            # is already its own field.
            first_user = next(
                (m for m in msgs if (m.get("role") or m.get("from")) not in (None, "system")),
                None,
            )
            if first_user:
                c = first_user.get("content") or ""
                if isinstance(c, list):
                    c = " ".join(str(x.get("text", x)) for x in c if x)
                c = str(c).strip().replace("\n", " ")
                item["preview"] = c[:120] + ("…" if len(c) > 120 else "")
            else:
                item["preview"] = ""
        except Exception as exc:
            item["error"] = str(exc)[:160]
        sessions.append(item)

    return web.json_response({
        "agent": name,
        "sessions": sessions,
        "total": total,
        "offset": offset,
        "limit": limit,
        "exists": True,
    })


async def handle_agent_activity_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/activity — roll-up stats for the Mind-modal Overview tab.

    Aggregates ``dispatches`` + ``cost_log`` into a single per-agent
    summary: total dispatches, dispatches in the last 24h, cumulative
    tokens, estimated cost, last-seen timestamp, and the most-used
    model over the agent's lifetime. Single SQL round-trip — cheap
    enough for the modal to call every time it opens.
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err
    aid = request.match_info["id"]

    import datetime as _dt
    cutoff_24h = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)).timestamp()

    try:
        iso24 = _dt.datetime.fromtimestamp(cutoff_24h, _dt.timezone.utc).isoformat()
        with auth_db._conn() as conn:
            cur = conn.cursor()
            total = cur.execute(
                "SELECT COUNT(*) FROM dispatches WHERE agent_id = ?", (aid,)
            ).fetchone()[0]
            last24 = cur.execute(
                "SELECT COUNT(*) FROM dispatches WHERE agent_id = ? AND started_at >= ?",
                (aid, iso24),
            ).fetchone()[0]
            last_seen = cur.execute(
                "SELECT MAX(started_at) FROM dispatches WHERE agent_id = ?", (aid,)
            ).fetchone()[0]
            top_row = cur.execute(
                "SELECT model, COUNT(*) AS n FROM dispatches WHERE agent_id = ? "
                "GROUP BY model ORDER BY n DESC LIMIT 1",
                (aid,),
            ).fetchone()
            top_model = top_row["model"] if top_row else None
            cost_row = cur.execute(
                "SELECT COALESCE(SUM(input_tokens),0) AS in_tok, "
                "       COALESCE(SUM(output_tokens),0) AS out_tok, "
                "       COUNT(*) AS n "
                "FROM cost_log WHERE agent_id = ?",
                (aid,),
            ).fetchone()
            in_tok, out_tok, cost_n = cost_row["in_tok"], cost_row["out_tok"], cost_row["n"]
    except Exception as exc:
        logger.exception("handle_agent_activity_get failed for %s", name)
        return web.json_response({"error": "query_failed", "detail": str(exc)[:200]}, status=500)

    return web.json_response({
        "agent": name,
        "dispatches_total": total,
        "dispatches_last_24h": last24,
        "last_seen": last_seen,
        "top_model": top_model,
        "input_tokens_total": in_tok,
        "output_tokens_total": out_tok,
        "cost_log_rows": cost_n,
    })


async def handle_agent_sandbox_files_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/sandbox-files?path=… — directory listing from
    INSIDE the agent's sandbox (LOG-49.1/49.3).

    The v1 implementation ran ``openshell sandbox exec`` against the
    live sandbox. That runtime was removed in S1.5; until the E2 sandbox
    backend exists this answers 503 ``no_agents_running``.
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err

    return web.json_response(
        {"error": "no_agents_running",
         "message": "No agents are running yet: the v2 sandbox backend arrives in epic E2."},
        status=503,
    )


async def handle_agent_sandbox_file_download(request: web.Request) -> web.StreamResponse:
    """GET /admin/agents/{id}/sandbox-files/download?path=… — stream a
    single file from INSIDE the sandbox (LOG-49.2).

    The v1 implementation used ``openshell sandbox download``; removed in
    S1.5. Until the E2 sandbox backend this answers 503
    ``no_agents_running`` after the usual auth / path checks.
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err

    path = (request.rel_url.query.get("path") or "").strip()
    if not path or not path.startswith("/") or ".." in path.split("/"):
        return web.json_response({"error": "invalid_path"}, status=400)

    return web.json_response(
        {"error": "no_agents_running",
         "message": "No agents are running yet: the v2 sandbox backend arrives in epic E2."},
        status=503,
    )


async def handle_agent_file_download(request: web.Request) -> web.StreamResponse:
    """GET /admin/agents/{id}/files/download?rel_path=... — stream a
    single file from the per-agent host dir (LOG-49 part 1).

    Companion to ``handle_agent_files_get``: the listing endpoint shows
    what's on disk, this one lets the admin actually fetch a file the
    agent produced (scripts it wrote, cron configs it set up, outputs
    it generated, etc.).

    Safety:
      - admin/operator only (same gate as the list endpoint)
      - rel_path is resolved under the agent_dir and rejected if it
        escapes (symlink or `..` traversal) — returns 400 not 404 to
        make the failure loud during dev
      - 100MB cap to stop accidental huge-file downloads stalling the
        gateway; returns 413 with a clear message for anything bigger
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err

    rel_path = (request.rel_url.query.get("rel_path") or "").strip()
    if not rel_path:
        return web.json_response({"error": "rel_path required"}, status=400)

    from pathlib import Path as _Path
    try:
        # Resolve both sides so symlinks are collapsed, then ensure the
        # resolved target is under the agent dir. Rejects `../` tricks
        # and symlinks pointing outside the agent's sandbox mirror.
        agent_root = agent_dir.resolve()
        target = (agent_dir / rel_path).resolve()
        target.relative_to(agent_root)  # raises ValueError on escape
    except Exception:
        return web.json_response({"error": "path_escape"}, status=400)

    if not target.is_file():
        return web.json_response({"error": "not_found"}, status=404)

    MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100MB
    try:
        size = target.stat().st_size
    except OSError as exc:
        return web.json_response({"error": "stat_failed", "detail": str(exc)[:200]}, status=500)
    if size > MAX_DOWNLOAD_BYTES:
        return web.json_response({
            "error": "file_too_large",
            "size_bytes": size,
            "max_bytes": MAX_DOWNLOAD_BYTES,
        }, status=413)

    # Stream the file with a Content-Disposition so browsers save it.
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="{_Path(rel_path).name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
    await resp.prepare(request)
    with target.open("rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            await resp.write(chunk)
    await resp.write_eof()
    return resp


async def handle_agent_files_get(request: web.Request) -> web.Response:
    """GET /admin/agents/{id}/files — flat listing of the agent's on-disk directory.

    Walks ``~/.logos/agents/<name>/`` and returns every file with a
    relative path, size, and mtime. No content — that would turn into
    a DoS vector on a multi-gig log file. The UI renders this as a
    sortable table so the user can eyeball "what's backed up for this
    agent" at a glance.

    Skips anything larger than 100MB from the listing (lets us cap the
    response size even if the agent hoards huge files).
    """
    user = request.get("current_user") or {}
    if user.get("role", "") not in ("admin", "operator"):
        return web.json_response({"error": "forbidden"}, status=403)

    name, agent_dir, err = _resolve_agent_dir(request.match_info["id"])
    if err is not None:
        return err

    if not agent_dir.is_dir():
        return web.json_response({
            "agent": name, "root": str(agent_dir), "files": [], "exists": False,
        })

    import datetime as _dt
    MAX_ENTRIES = 5000
    files = []
    try:
        for p in agent_dir.rglob("*"):
            if len(files) >= MAX_ENTRIES:
                break
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({
                "rel_path": str(p.relative_to(agent_dir)),
                "size_bytes": st.st_size,
                "mtime_iso": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
    except Exception as exc:
        logger.exception("handle_agent_files_get failed for %s", name)
        return web.json_response({"error": "walk_failed", "detail": str(exc)[:200]}, status=500)

    # Newest first — the surfaced files should match what the agent
    # has recently touched, not the alphabetical top of the tree.
    files.sort(key=lambda f: f["mtime_iso"] or "", reverse=True)

    return web.json_response({
        "agent": name,
        "root": str(agent_dir),
        "files": files,
        "truncated": len(files) >= MAX_ENTRIES,
        "exists": True,
    })
