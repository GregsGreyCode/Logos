"""S1.9: first-run setup is account creation only.

Fresh DB -> POST /api/setup/complete creates the admin (+ optional second
user) -> both can log in; a second call is refused; the wizard's
provisioning/benchmark endpoints are gone.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import http_api, setup_handlers
from gateway.auth import db as auth_db
from gateway.auth.handlers import handle_login, handle_me
from gateway.auth.middleware import (
    _PUBLIC_PATHS, _rate_store, auth_middleware, require_csrf, require_permission,
)
from gateway.auth.password import hash_password, verify_password

GATEWAY_DIR = Path(__file__).resolve().parents[2] / "gateway"

GREG = {"name": "Greg", "email": "greg@example.com", "password": "correct-horse"}
BERE = {"name": "Béré", "email": "bere@example.com", "password": "battery-staple"}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "_DB_PATH", None)
    monkeypatch.setenv("LOGOS_JWT_SECRET", "test-secret-s1-9")
    monkeypatch.delenv("LOGOS_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_INTERNAL_TOKEN", raising=False)
    _rate_store.clear()
    auth_db.init_db(tmp_path)
    return tmp_path


def _app() -> web.Application:
    """The setup + auth routes wired the way start_http_api wires them."""
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/setup", http_api._handle_setup_page)
    app.router.add_get("/api/setup/status", http_api._handle_setup_status)
    app.router.add_post("/api/setup/complete", setup_handlers.handle_setup_complete)
    app.router.add_post(
        "/api/setup/wipe",
        require_csrf(require_permission("manage_platform")(http_api._handle_setup_wipe)),
    )
    app.router.add_post("/auth/login", handle_login)
    app.router.add_get("/auth/me", handle_me)
    return app


def _with_client(coro_fn):
    async def _main():
        jar = aiohttp.CookieJar(unsafe=True)  # test server is on an IP address
        async with TestClient(TestServer(_app()), cookie_jar=jar) as client:
            return await coro_fn(client, jar)
    return asyncio.run(_main())


def _cookie(jar, name):
    for c in jar:
        if c.key == name:
            return c.value
    return None


async def _login(client, identifier, password):
    return await client.post("/auth/login", json={"email": identifier, "password": password})


# ── The story's acceptance flow ────────────────────────────────────────────

def test_fresh_db_setup_creates_admin_and_login_works(fresh_db):
    async def flow(client, jar):
        r = await client.get("/api/setup/status")
        assert await r.json() == {"completed": False}
        r = await client.get("/setup")
        assert r.status == 200 and "Create your account" in await r.text()

        r = await client.post("/api/setup/complete", json=GREG)
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True and body["second_user"] is None
        assert body["user"]["email"] == "greg@example.com"
        assert body["user"]["username"] == "greg"
        assert body["user"]["role"] == "admin"

        r = await client.get("/api/setup/status")
        assert await r.json() == {"completed": True}

        # The response logged the admin in: the dashboard's first call works.
        r = await client.get("/auth/me")
        assert r.status == 200
        assert (await r.json())["user"]["email"] == "greg@example.com"

        # And a cold login works too, by email or by username.
        jar.clear()
        assert (await client.get("/auth/me")).status == 401
        r = await _login(client, "greg@example.com", "correct-horse")
        assert r.status == 200
        login = await r.json()
        assert login["setup_required"] is False
        assert login["user"]["display_name"] == "Greg"
        jar.clear()
        assert (await _login(client, "greg", "correct-horse")).status == 200
        jar.clear()
        assert (await _login(client, "greg", "wrong-password")).status == 401

    _with_client(flow)
    users, total = auth_db.list_users(page=1, limit=10)
    assert total == 1 and users[0]["role"] == "admin"


def test_setup_with_optional_second_user(fresh_db):
    async def flow(client, jar):
        r = await client.post("/api/setup/complete", json={**GREG, "second": BERE})
        assert r.status == 200
        body = await r.json()
        assert body["second_user"]["email"] == "bere@example.com"
        assert body["second_user"]["username"] == "bere"   # accent folded
        assert body["second_user"]["role"] == "user"

        jar.clear()
        r = await _login(client, "bere@example.com", "battery-staple")
        assert r.status == 200
        login = await r.json()
        assert login["user"]["role"] == "user"
        assert login["user"]["display_name"] == "Béré"
        assert login["setup_required"] is False
        jar.clear()
        assert (await _login(client, "greg@example.com", "correct-horse")).status == 200

    _with_client(flow)
    _, total = auth_db.list_users(page=1, limit=10)
    assert total == 2


def test_second_call_to_setup_is_refused(fresh_db):
    async def flow(client, jar):
        assert (await client.post("/api/setup/complete", json=GREG)).status == 200
        jar.clear()

        intruder = {"name": "Mallory", "email": "m@example.com", "password": "hunter2-hunter2"}
        r = await client.post("/api/setup/complete", json=intruder)
        assert r.status == 409
        assert (await r.json())["error"] == "setup_already_completed"
        assert _cookie(jar, "access_token") is None   # no session handed out

        # Same answer when replaying the original payload.
        r = await client.post("/api/setup/complete", json={**GREG, "second": BERE})
        assert r.status == 409

        # /setup bounces to the dashboard once completed.
        r = await client.get("/setup", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/"

    _with_client(flow)
    # Nothing changed: one user, original password, no Mallory, no Béré.
    users, total = auth_db.list_users(page=1, limit=10)
    assert total == 1
    assert auth_db.get_user_by_email("m@example.com") is None
    assert auth_db.get_user_by_email("bere@example.com") is None
    admin = auth_db.get_user_by_email("greg@example.com")
    assert verify_password(admin["password_hash"], "correct-horse")


def test_already_set_up_install_is_unaffected(fresh_db):
    """setup_completed true + existing users: setup neither runs nor edits."""
    auth_db.create_user("old@example.com", "old", hash_password("old-password"), role="admin")
    auth_db.mark_setup_completed()

    async def flow(client, jar):
        assert (await client.post("/api/setup/complete", json=GREG)).status == 409
        r = await _login(client, "old@example.com", "old-password")
        assert r.status == 200 and (await r.json())["setup_required"] is False

    _with_client(flow)
    assert auth_db.get_user_by_email("greg@example.com") is None


# ── Seeded admin, validation, wipe ─────────────────────────────────────────

def test_setup_takes_over_the_seeded_default_admin(fresh_db):
    """An admin seeded from LOGOS_ADMIN_EMAIL before setup must be replaced by setup,
    not left beside the real one with its old password."""
    from gateway.auth.password import hash_password
    auth_db.create_user(email="admin@example.com", username="admin",
                        password_hash=hash_password("admin"), role="admin", display_name="Admin")
    seeded = auth_db.get_primary_admin()
    assert seeded["email"] == "admin@example.com"

    async def flow(client, jar):
        assert (await client.post("/api/setup/complete", json=GREG)).status == 200
        jar.clear()
        assert (await _login(client, "admin@example.com", "admin")).status == 401
        assert (await _login(client, "admin", "admin")).status == 401
        assert (await _login(client, "greg@example.com", "correct-horse")).status == 200

    _with_client(flow)
    users, total = auth_db.list_users(page=1, limit=10)
    assert total == 1 and users[0]["id"] == seeded["id"]
    assert users[0]["username"] == "greg" and users[0]["display_name"] == "Greg"


@pytest.mark.parametrize("payload, error", [
    ({**GREG, "name": "  "}, "invalid_name"),
    ({**GREG, "email": "not-an-email"}, "invalid_email"),
    ({**GREG, "password": "short"}, "password_too_short"),
    ({**GREG, "password": None}, "password_too_short"),
    ({**GREG, "second": {**BERE, "password": "short"}}, "password_too_short"),
    ({**GREG, "second": {**BERE, "email": ""}}, "invalid_email"),
    ({**GREG, "second": {**BERE, "email": GREG["email"]}}, "duplicate_email"),
    ({**GREG, "second": {**BERE, "name": "greg"}}, "duplicate_name"),
    ({**GREG, "second": "bere"}, "invalid_account"),
])
def test_invalid_input_is_rejected_and_writes_nothing(fresh_db, payload, error):
    async def flow(client, jar):
        r = await client.post("/api/setup/complete", json=payload)
        assert r.status == 400
        body = await r.json()
        assert body["error"] == error and body["detail"]
        assert _cookie(jar, "access_token") is None
        r = await client.get("/api/setup/status")
        assert await r.json() == {"completed": False}

    _with_client(flow)
    assert auth_db.list_users(page=1, limit=10)[1] == 0


def test_blank_second_account_means_none(fresh_db):
    blank = {"name": "", "email": " ", "password": ""}

    async def flow(client, jar):
        r = await client.post("/api/setup/complete", json={**GREG, "second": blank})
        assert r.status == 200 and (await r.json())["second_user"] is None

    _with_client(flow)
    assert auth_db.list_users(page=1, limit=10)[1] == 1


def test_non_object_body_is_rejected(fresh_db):
    async def flow(client, jar):
        r = await client.post("/api/setup/complete", json=["greg"])
        assert r.status == 400
        r = await client.post("/api/setup/complete", data=b"{not json")
        assert r.status == 400

    _with_client(flow)
    assert not auth_db.is_setup_completed()


def test_username_from_name():
    f = setup_handlers.username_from_name
    assert f("Greg", "g@example.com") == "greg"
    assert f("Béré D.", "b@example.com") == "bere-d"
    assert f("日本", "nihon.go@example.com") == "nihon-go"
    assert f("!!!", "@") == "user"
    # Always passes the /auth/register username rule.
    for name in ("Greg", "Béré D.", "a b_c", "x" * 200):
        u = f(name, "e@example.com")
        assert u.replace("_", "").replace("-", "").isalnum() and len(u) <= 64


def test_factory_reset_then_setup_again(fresh_db):
    """The admin 'Factory reset' action still works: wipe -> setup re-runs."""
    async def flow(client, jar):
        assert (await client.post("/api/setup/complete", json={**GREG, "second": BERE})).status == 200
        csrf = {"X-CSRF-Token": _cookie(jar, "csrf_token")}

        r = await client.post("/api/setup/wipe", json={"confirm": "nope"}, headers=csrf)
        assert r.status == 400
        r = await client.post("/api/setup/wipe", json={"confirm": "WIPE"}, headers=csrf)
        assert r.status == 200
        assert (await r.json())["deleted"]["users"] == 2
        assert await (await client.get("/api/setup/status")).json() == {"completed": False}

        jar.clear()
        assert (await _login(client, "greg@example.com", "correct-horse")).status == 401
        fresh = {"name": "Greg", "email": "greg@new.example", "password": "another-pass"}
        assert (await client.post("/api/setup/complete", json=fresh)).status == 200
        assert (await client.get("/auth/me")).status == 200

    _with_client(flow)
    users, total = auth_db.list_users(page=1, limit=10)
    assert total == 1 and users[0]["email"] == "greg@new.example"


def test_wipe_requires_admin(fresh_db):
    async def flow(client, jar):
        assert (await client.post("/api/setup/complete", json={**GREG, "second": BERE})).status == 200
        jar.clear()
        assert (await _login(client, "bere@example.com", "battery-staple")).status == 200
        csrf = {"X-CSRF-Token": _cookie(jar, "csrf_token")}
        r = await client.post("/api/setup/wipe", json={"confirm": "WIPE"}, headers=csrf)
        assert r.status == 403

    _with_client(flow)
    assert auth_db.is_setup_completed()


# ── The wizard is gone ─────────────────────────────────────────────────────

REMOVED_ENDPOINTS = (
    "probe", "scan", "pull", "compare", "compare/cancel-server", "test",
    "model-catalog", "validate-provider", "prewarm", "preload-model",
    "progress", "session", "env-probe", "reset",
)


def test_wizard_endpoints_are_gone():
    src = (GATEWAY_DIR / "http_api.py").read_text(encoding="utf-8")
    routed = set(re.findall(r'"/api/setup/([a-z/-]+)"', src))
    assert routed == {"status", "complete", "wipe"}
    assert "/api/model-catalog" not in src   # fed only the wizard's model picker
    assert "/api/model-catalog" not in _PUBLIC_PATHS
    for name in REMOVED_ENDPOINTS:
        assert f"/api/setup/{name}" not in _PUBLIC_PATHS
    assert {"/setup", "/api/setup/status", "/api/setup/complete"} <= _PUBLIC_PATHS
    assert "/api/setup/wipe" not in _PUBLIC_PATHS


def test_setup_module_is_account_creation_only():
    handlers = [n for n in dir(setup_handlers) if n.startswith("handle_")]
    assert handlers == ["handle_setup_complete"]
    src = (GATEWAY_DIR / "setup_handlers.py").read_text(encoding="utf-8")
    assert len(src.splitlines()) < 400
    for gone in ("spawn_sandbox", "provision_route", "prewarm", "searxng", "subprocess",
                 "_HARD_", "_BENCH_", "ClientSession"):
        assert gone not in src, gone


def test_setup_page_only_asks_for_accounts():
    html = (GATEWAY_DIR / "html" / "setup.html").read_text(encoding="utf-8")
    assert len(html.splitlines()) < 400
    assert set(re.findall(r"/api/[a-z/_-]+", html)) == {"/api/setup/complete"}
    assert "/static/tailwind.css" in html
    for gone in ("benchmark", "Benchmark", "sandbox", "Telegram", "Discord", "soul", "Ollama", "LM Studio"):
        assert gone not in html, gone
    # The dashboard no longer offers the "re-run wizard" action or its endpoint.
    main = (GATEWAY_DIR / "html" / "main_app.html").read_text(encoding="utf-8")
    assert "/api/setup/reset" not in main and "resetSetup" not in main
    assert "/api/setup/wipe" in main


# ── No default admin; setup needs this machine or the one-time token ─────────

def test_no_default_admin_is_seeded():
    assert not (GATEWAY_DIR / "seed.py").exists()
    assert "run_seed" not in (GATEWAY_DIR / "http_api.py").read_text(encoding="utf-8")


def test_setup_token_gate(fresh_db, tmp_path):
    token = setup_handlers.ensure_setup_token(tmp_path)
    assert token and (tmp_path / ".setup_token").read_text() == token
    assert oct((tmp_path / ".setup_token").stat().st_mode & 0o777) == "0o600"
    assert setup_handlers.ensure_setup_token(tmp_path) == token            # stable across restarts
    assert setup_handlers.setup_allowed("127.0.0.1", None)                 # this machine
    assert setup_handlers.setup_allowed("::1", None)
    assert not setup_handlers.setup_allowed("192.168.1.50", None)          # the LAN needs the token
    assert not setup_handlers.setup_allowed("192.168.1.50", "wrong")
    assert not setup_handlers.setup_allowed("192.168.1.50", "")
    assert setup_handlers.setup_allowed("192.168.1.50", token)
    setup_handlers.complete_setup(GREG)
    assert not (tmp_path / ".setup_token").exists()                         # used up
    assert not setup_handlers.setup_allowed("192.168.1.50", token)
    assert setup_handlers.ensure_setup_token(tmp_path) is None


def test_bad_emails_are_rejected(fresh_db):
    for email in ("@", "a@", "@b.com", "a@b", "a b@c.com"):
        with pytest.raises(setup_handlers.SetupError):
            setup_handlers.complete_setup({**GREG, "email": email})
