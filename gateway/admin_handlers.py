"""Admin handlers: machines, routing policies, user-policy assignment, routing resolver."""

import logging
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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base}/health", timeout=aiohttp.ClientTimeout(total=3)
            ) as r:
                return {"status": "ok" if r.ok else "degraded", "http": r.status}
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

    updates = {k: body[k] for k in ("name", "endpoint_url", "description", "enabled") if k in body}
    if updates:
        auth_db.update_machine(mid, **updates)
        auth_db.write_audit_log(
            request["current_user"]["sub"], "update_machine",
            target_type="machine", target_id=mid,
            metadata=updates, ip_address=request.remote,
        )

    machine = auth_db.get_machine(mid)
    machine["capabilities"] = auth_db.get_machine_capabilities(mid)
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
