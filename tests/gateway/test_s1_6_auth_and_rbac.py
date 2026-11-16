"""S1.6: presets, machines/routing and the four-role RBAC are gone.

Covers what must survive: login, logout, token refresh, /auth/me and admin
user create/list; the two-role permission table; the idempotent drop
migration (including the users.policy_id FK strip); and the empty shapes
the dashboard still fetches.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from gateway.auth import db as auth_db
from gateway.auth import rbac
from gateway.auth.handlers import (
    handle_login, handle_logout, handle_me, handle_refresh,
    handle_users_list, handle_users_patch, handle_users_post,
)
from gateway.auth.middleware import (
    _rate_store, auth_middleware, require_csrf, require_permission,
)
from gateway.auth.password import hash_password

GATEWAY_DIR = Path(__file__).resolve().parents[2] / "gateway"

DROPPED_TABLES = (
    "machines", "machine_capabilities", "machine_users", "routing_policies",
    "policy_rules", "routing_log", "cloud_providers",
)


@pytest.fixture
def db_home(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "_DB_PATH", None)
    monkeypatch.setenv("LOGOS_JWT_SECRET", "test-secret-s1-6")
    monkeypatch.delenv("LOGOS_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_INTERNAL_TOKEN", raising=False)
    _rate_store.clear()
    return tmp_path


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _users_sql(path) -> str:
    with sqlite3.connect(path) as c:
        return c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]


# ── RBAC ───────────────────────────────────────────────────────────────────

def test_two_roles_only():
    assert set(rbac.ROLE_PERMISSIONS) == {"admin", "user"}
    assert rbac.ROLES == ("admin", "user")


def test_legacy_roles_map_to_user():
    assert rbac.normalize_role("admin") == "admin"
    assert rbac.normalize_role("user") == "user"
    assert rbac.normalize_role("operator") == "user"
    assert rbac.normalize_role("viewer") == "user"
    assert rbac.normalize_role("root") == ""
    assert rbac.normalize_role(None) == ""
    # A stale "operator" claim gets user rights, not its old elevated ones.
    assert rbac.get_permissions("operator") == rbac.get_permissions("user")
    assert not rbac.has_permission("operator", "manage_users")
    assert not rbac.has_permission("operator", "decide_approvals")
    assert rbac.has_permission("viewer", "view_instances")
    assert rbac.get_permissions("root") == []


def test_admin_vs_user_permissions():
    assert rbac.has_permission("admin", "manage_users")
    assert rbac.has_permission("admin", "manage_machines")
    assert not rbac.has_permission("user", "manage_users")
    assert not rbac.has_permission("user", "manage_machines")
    assert rbac.has_permission("user", "view_runs")
    assert rbac.can_spawn("admin", {})
    assert rbac.can_spawn("user", {"user_accessible": True})
    assert not rbac.can_spawn("user", {})


def _referenced_permissions() -> set[str]:
    """Permission names handlers in gateway/ actually check."""
    pat = re.compile(
        r"(?:require_permission|has_permission|_has_perm|_require)\(\s*"
        r"(?:[\w.\[\]\"']+\s*,\s*)?\"([a-z_]+)\""
    )
    found: set[str] = set()
    for py in GATEWAY_DIR.rglob("*.py"):
        if py.name == "rbac.py":
            continue
        found.update(pat.findall(py.read_text(encoding="utf-8")))
    return found


def test_permission_table_matches_what_gateway_references():
    referenced = _referenced_permissions()
    assert "manage_users" in referenced and "view_instances" in referenced  # regex sanity
    known = set().union(*rbac.ROLE_PERMISSIONS.values())
    # Every permission a handler checks is grantable (no route locked for all).
    assert referenced - known == set()
    # Nothing left over from v1 (machine claims, routing profiles, presets).
    # The two spawn permissions are only reached through can_spawn().
    assert known - referenced == {"spawn_instance", "spawn_instance_restricted"}
    assert referenced <= rbac.ROLE_PERMISSIONS["admin"]


# ── Deleted modules stay deleted ───────────────────────────────────────────

def test_v1_policy_modules_are_gone():
    assert not (GATEWAY_DIR / "policies").exists()
    assert not (GATEWAY_DIR / "policies.py").exists()
    assert not (GATEWAY_DIR / "capabilities.py").exists()
    for name in ("list_machines", "create_policy", "list_cloud_providers",
                 "claim_machine", "resolve_policy_machines", "log_routing_decision",
                 "set_agent_applied_presets"):
        assert not hasattr(auth_db, name), name


# ── Migration ──────────────────────────────────────────────────────────────

def test_fresh_db_has_no_routing_tables(db_home):
    auth_db.init_db(db_home)
    path = db_home / "auth.db"
    assert _tables(path).isdisjoint(DROPPED_TABLES)
    assert "routing_policies" not in _users_sql(path)
    u = auth_db.create_user("a@example.com", "a", hash_password("pw-12345678"), role="admin")
    assert auth_db.delete_user(u["id"])


def _make_legacy_db(path) -> None:
    """A v1 install: routing tables populated, users.policy_id carries an FK
    to routing_policies, and an operator + a viewer exist."""
    with sqlite3.connect(path) as c:
        c.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user'
                    CHECK (role IN ('admin','operator','user','viewer')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended','pending')),
                display_name TEXT, created_at INTEGER NOT NULL, last_login INTEGER,
                failed_login_count INTEGER NOT NULL DEFAULT 0, locked_until INTEGER
            );
            CREATE TABLE machines (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, endpoint_url TEXT NOT NULL,
                description TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE machine_capabilities (
                id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                model_class TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 10,
                max_context INTEGER, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE routing_policies (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT,
                fallback TEXT NOT NULL DEFAULT 'any_available',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE policy_rules (
                id TEXT PRIMARY KEY,
                policy_id TEXT NOT NULL REFERENCES routing_policies(id) ON DELETE CASCADE,
                model_class TEXT NOT NULL,
                machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                rank INTEGER NOT NULL
            );
            CREATE TABLE machine_users (
                id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                priority INTEGER NOT NULL DEFAULT 100, created_at INTEGER NOT NULL
            );
            CREATE TABLE routing_log (
                id TEXT PRIMARY KEY, user_id TEXT, model_alias TEXT NOT NULL,
                model_class TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE cloud_providers (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL,
                api_key TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            ALTER TABLE users ADD COLUMN policy_id TEXT REFERENCES routing_policies(id);

            INSERT INTO machines VALUES ('m1', 'Local Node', 'http://localhost:1234/v1', '', 1, 0, 1, 1);
            INSERT INTO machine_capabilities VALUES ('c1', 'm1', 'general', 10, NULL, 1);
            INSERT INTO routing_policies VALUES ('p1', 'default', '', 'any_available', 1, 1);
            INSERT INTO policy_rules VALUES ('r1', 'p1', '*', 'm1', 1);
            INSERT INTO cloud_providers VALUES ('cp1', 'anthropic', 'Anthropic', 'sk-x', 1, 1);
            INSERT INTO routing_log VALUES ('l1', 'usr_admin', 'fast', 'lightweight', 1);
            INSERT INTO users (id, email, username, password_hash, role, created_at, policy_id)
                VALUES ('usr_admin', 'greg@example.com', 'greg', 'x', 'admin', 1, 'p1'),
                       ('usr_op', 'op@example.com', 'op', 'x', 'operator', 2, 'p1'),
                       ('usr_view', 'view@example.com', 'view', 'x', 'viewer', 3, NULL),
                       ('usr_user', 'bere@example.com', 'bere', 'x', 'user', 4, NULL);
            INSERT INTO machine_users VALUES ('mu1', 'm1', 'usr_admin', 100, 1);
            """
        )


def test_legacy_db_migrates(db_home):
    path = db_home / "auth.db"
    _make_legacy_db(path)
    assert "REFERENCES routing_policies" in _users_sql(path)

    auth_db.init_db(db_home)
    auth_db.init_db(db_home)  # idempotent

    assert _tables(path).isdisjoint(DROPPED_TABLES)
    assert "routing_policies" not in _users_sql(path)
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        flags = dict(c.execute("SELECT key, value FROM schema_flags").fetchall())
        assert flags["drop_machines_routing_v1"] == "1"
        assert flags["collapse_roles_v1"] == "1"
        # Unused column is left in place with its data.
        assert c.execute("SELECT policy_id FROM users WHERE id='usr_admin'").fetchone()[0] == "p1"

    # Users survive; operator and viewer became plain users.
    roles = {u["username"]: u["role"] for u in auth_db.list_users(limit=50)[0]}
    assert roles == {"greg": "admin", "op": "user", "view": "user", "bere": "user"}

    # INSERT / UPDATE / DELETE on users work with foreign_keys=ON even though
    # the FK's parent table is gone.
    new = auth_db.create_user("n@example.com", "n", hash_password("pw-12345678"))
    auth_db.update_user(new["id"], display_name="N")
    assert auth_db.delete_user(new["id"])
    assert auth_db.delete_user("usr_op")


def test_roles_are_normalised_on_write(db_home):
    auth_db.init_db(db_home)
    u = auth_db.create_user("o@example.com", "o", "x", role="operator")
    assert u["role"] == "user"
    assert auth_db.update_user(u["id"], role="viewer")["role"] == "user"
    assert auth_db.update_user(u["id"], role="admin")["role"] == "admin"
    with pytest.raises(ValueError):
        auth_db.create_user("r@example.com", "r", "x", role="root")
    with pytest.raises(ValueError):
        auth_db.update_user(u["id"], role="superuser")


# ── Auth flow over HTTP ────────────────────────────────────────────────────

def _auth_app() -> web.Application:
    """The auth/user routes wired the way start_http_api wires them."""
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_post("/auth/login", handle_login)
    app.router.add_post("/auth/logout", handle_logout)
    app.router.add_post("/auth/refresh", handle_refresh)
    app.router.add_get("/auth/me", handle_me)
    _mu = require_permission("manage_users")
    app.router.add_get("/users", _mu(handle_users_list))
    app.router.add_post("/users", _mu(require_csrf(handle_users_post)))
    app.router.add_patch("/users/{id}", _mu(require_csrf(handle_users_patch)))
    return app


def _with_client(coro_fn):
    async def _main():
        jar = aiohttp.CookieJar(unsafe=True)  # test server is on an IP address
        async with TestClient(TestServer(_auth_app()), cookie_jar=jar) as client:
            return await coro_fn(client, jar)
    return asyncio.run(_main())


def _cookie(jar, name):
    for c in jar:
        if c.key == name:
            return c.value
    return None


def test_login_me_refresh_logout(db_home):
    auth_db.init_db(db_home)
    auth_db.create_user("greg@example.com", "greg", hash_password("correct-horse"),
                        role="admin", display_name="Greg")

    async def flow(client, jar):
        # Unauthenticated
        r = await client.get("/auth/me")
        assert r.status == 401

        # Wrong password, then right one (by username and by email)
        r = await client.post("/auth/login", json={"email": "greg", "password": "nope"})
        assert r.status == 401
        r = await client.post("/auth/login", json={"email": "greg@example.com",
                                                   "password": "correct-horse"})
        assert r.status == 200
        body = await r.json()
        assert body["user"]["username"] == "greg" and body["user"]["role"] == "admin"
        assert "password_hash" not in body["user"] and "policy_id" not in body["user"]
        assert _cookie(jar, "access_token") and _cookie(jar, "csrf_token")
        first_access = _cookie(jar, "access_token")
        first_refresh = _cookie(jar, "refresh_token")
        assert first_refresh

        # /auth/me
        r = await client.get("/auth/me")
        assert r.status == 200
        me = await r.json()
        assert me["user"]["email"] == "greg@example.com"
        assert me["permissions"] == rbac.get_permissions("admin")
        assert "manage_users" in me["permissions"]

        # Refresh rotates both tokens; the old refresh token is dead.
        r = await client.post("/auth/refresh")
        assert r.status == 200
        assert _cookie(jar, "refresh_token") != first_refresh
        assert _cookie(jar, "access_token") != first_access
        assert (await client.get("/auth/me")).status == 200
        r = await client.post("/auth/refresh", cookies={"refresh_token": first_refresh})
        assert r.status == 401

        # Logout clears all three cookies, so the session is over for
        # this client: no access token, nothing to refresh with.
        r = await client.post("/auth/logout")
        assert r.status == 204
        assert not _cookie(jar, "access_token")
        assert not _cookie(jar, "refresh_token")
        assert not _cookie(jar, "csrf_token")
        assert (await client.get("/auth/me")).status == 401
        assert (await client.post("/auth/refresh")).status == 401

    _with_client(flow)


def test_admin_creates_and_lists_users_and_user_cannot(db_home):
    auth_db.init_db(db_home)
    auth_db.create_user("greg@example.com", "greg", hash_password("correct-horse"), role="admin")

    async def flow(client, jar):
        r = await client.post("/auth/login", json={"email": "greg", "password": "correct-horse"})
        assert r.status == 200
        csrf = {"X-CSRF-Token": _cookie(jar, "csrf_token")}

        # CSRF is still enforced
        r = await client.post("/users", json={"email": "b@example.com", "username": "bere",
                                              "password": "battery-staple"})
        assert r.status == 403

        r = await client.post("/users", headers=csrf, json={
            "email": "bere@example.com", "username": "bere", "password": "battery-staple"})
        assert r.status == 201
        created = (await r.json())["user"]
        assert created["role"] == "user"

        # v1 role names are stored as "user"; unknown ones are rejected.
        r = await client.post("/users", headers=csrf, json={
            "email": "op@example.com", "username": "op", "password": "battery-staple",
            "role": "operator"})
        assert r.status == 201 and (await r.json())["user"]["role"] == "user"
        r = await client.post("/users", headers=csrf, json={
            "email": "x@example.com", "username": "x", "password": "battery-staple",
            "role": "root"})
        assert r.status == 400 and (await r.json())["error"] == "invalid_role"
        r = await client.patch(f"/users/{created['id']}", headers=csrf, json={"role": "root"})
        assert r.status == 400

        r = await client.get("/users")
        assert r.status == 200
        listing = await r.json()
        assert listing["total"] == 3
        assert {u["username"] for u in listing["users"]} == {"greg", "bere", "op"}
        r = await client.get("/users?role=admin")
        assert [u["username"] for u in (await r.json())["users"]] == ["greg"]

        # The new plain user can log in and read /auth/me but not manage users.
        await client.post("/auth/logout")
        r = await client.post("/auth/login", json={"email": "bere", "password": "battery-staple"})
        assert r.status == 200
        me = await (await client.get("/auth/me")).json()
        assert me["user"]["role"] == "user"
        assert me["permissions"] == rbac.get_permissions("user")
        r = await client.get("/users")
        assert r.status == 403
        assert (await r.json())["requires"] == "manage_users"
        r = await client.post("/users", headers={"X-CSRF-Token": _cookie(jar, "csrf_token")},
                              json={"email": "z@example.com", "username": "z",
                                    "password": "battery-staple", "role": "admin"})
        assert r.status == 403

    _with_client(flow)


# ── Endpoints the dashboard still fetches ──────────────────────────────────

def test_removed_list_endpoints_answer_empty_shapes():
    from gateway import http_api

    def body(shape):
        req = make_mocked_request("GET", "/")
        resp = asyncio.run(http_api._empty_shape_handler(shape)(req))
        assert resp.status == 200
        return json.loads(resp.body)

    assert body({"machines": []}) == {"machines": []}

    # The routes themselves are registered with those shapes.
    src = (GATEWAY_DIR / "http_api.py").read_text(encoding="utf-8")
    for path, key in (
        ("/admin/machines", "machines"),
        ("/admin/cloud-providers", "providers"),
        ("/admin/recommended-models", "recommended"),
        ("/api/admin/services/local", "services"),
        ("/api/openshell/presets", "presets"),
        ("/admin/agents/{id}/capabilities", "capabilities"),
    ):
        m = re.search(
            r'add_get\("%s",\s*_mm\(_empty_shape_handler\(\s*(\{.*?\})\)\)\)' % re.escape(path),
            src, re.S)
        assert m, path
        assert json.loads(m.group(1))[key] == []


def test_agent_tools_endpoint_keeps_empty_presets(db_home):
    from gateway import admin_handlers

    auth_db.init_db(db_home)
    agent = auth_db.create_agent(name="tools-shape")
    req = make_mocked_request("GET", "/", match_info={"id": agent["id"]})
    resp = asyncio.run(admin_handlers.handle_agent_tools_get(req))
    data = json.loads(resp.body)
    assert data["presets"] == {"applied": [], "available": []}
    assert data["toolsets"]["available"] == []
