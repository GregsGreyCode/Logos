"""drop_model_routes_v1 (S1.5): the OpenShell 0.0.x model_routes table is
dropped, and the agents.model_route_id FK to it is stripped so agents
INSERT/DELETE keep working with foreign_keys=ON."""

from __future__ import annotations

import sqlite3

import pytest

from gateway.auth import db as auth_db


def _agents_sql(path) -> str:
    with sqlite3.connect(path) as c:
        return c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'"
        ).fetchone()[0]


def _has_table(path, name) -> bool:
    with sqlite3.connect(path) as c:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None


@pytest.fixture
def db_home(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "_DB_PATH", None)
    return tmp_path


def test_fresh_db_has_no_model_routes(db_home):
    auth_db.init_db(db_home)
    path = db_home / "auth.db"
    assert not _has_table(path, "model_routes")
    assert "model_routes" not in _agents_sql(path)
    a = auth_db.create_agent(name="fresh")
    auth_db.delete_agent(a["id"])


def test_legacy_db_migrates(db_home):
    path = db_home / "auth.db"
    # A pre-S1.5 install: model_routes exists and agents.model_route_id
    # was ALTER-added with an FK to it.
    with sqlite3.connect(path) as c:
        c.executescript(
            """
            CREATE TABLE model_routes (id TEXT PRIMARY KEY, provider TEXT, model TEXT);
            INSERT INTO model_routes VALUES ('mr_1', 'lmstudio', 'm');
            CREATE TABLE agents (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                soul_slug TEXT NOT NULL DEFAULT 'general', model TEXT,
                description TEXT, creator_id TEXT, shared INTEGER NOT NULL DEFAULT 1,
                toolsets TEXT, daily_budget_usd REAL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            ALTER TABLE agents ADD COLUMN model_route_id TEXT REFERENCES model_routes(id);
            INSERT INTO agents (id, name, created_at, updated_at, model_route_id)
                VALUES ('agent_old', 'old', 1, 1, 'mr_1');
            -- A real legacy DB has long since applied the B-tier defaults
            -- backfill (which opens its own connection per agent and would
            -- wait on the migration's write lock here).
            CREATE TABLE schema_flags (key TEXT PRIMARY KEY, value TEXT, applied_at INTEGER);
            INSERT INTO schema_flags VALUES ('agent_b_defaults_v1', '1', 1);
            """
        )
    assert "REFERENCES model_routes" in _agents_sql(path)

    auth_db.init_db(db_home)
    auth_db.init_db(db_home)  # idempotent

    assert not _has_table(path, "model_routes")
    assert "model_routes" not in _agents_sql(path)
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute(
            "SELECT value FROM schema_flags WHERE key='drop_model_routes_v1'"
        ).fetchone()[0] == "1"

    # Unused column is left in place with its data.
    assert auth_db.get_agent("agent_old")["model_route_id"] == "mr_1"
    # INSERT / DELETE on agents work with foreign_keys=ON.
    new = auth_db.create_agent(name="new")
    auth_db.delete_agent(new["id"])
    auth_db.delete_agent("agent_old")
    assert auth_db.get_agent("agent_old") is None
