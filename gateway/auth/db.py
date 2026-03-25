"""SQLite persistence for auth: users, refresh_tokens, user_settings, audit_logs,
machines, machine_capabilities, routing_policies, policy_rules,
action_policies, approval_requests, workflow_definitions, workflow_runs,
workflow_step_runs."""

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DB_PATH: Optional[Path] = None

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id                 TEXT PRIMARY KEY,
    email              TEXT UNIQUE NOT NULL,
    username           TEXT UNIQUE NOT NULL,
    password_hash      TEXT NOT NULL,
    role               TEXT NOT NULL DEFAULT 'user'
                           CHECK (role IN ('admin','operator','user','viewer')),
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','suspended','pending')),
    display_name       TEXT,
    created_at         INTEGER NOT NULL,
    last_login         INTEGER,
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until       INTEGER
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT UNIQUE NOT NULL,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    ip_address  TEXT,
    user_agent  TEXT
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id               TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    default_soul          TEXT,
    default_model         TEXT,
    ui_theme              TEXT NOT NULL DEFAULT 'midnight',
    notification_telegram INTEGER NOT NULL DEFAULT 0,
    spawn_defaults        TEXT,
    updated_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_settings (
    id                 INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    allowed_souls      TEXT,
    default_tool_policy TEXT,
    allow_registration INTEGER NOT NULL DEFAULT 0,
    require_approval   INTEGER NOT NULL DEFAULT 1,
    feature_flags      TEXT,
    updated_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    metadata    TEXT,
    ip_address  TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_user   ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_rtk_user     ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_rtk_hash     ON refresh_tokens(token_hash);

INSERT OR IGNORE INTO platform_settings (id, updated_at) VALUES (1, unixepoch() * 1000);

CREATE TABLE IF NOT EXISTS machines (
    id           TEXT PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    endpoint_url TEXT NOT NULL,
    description  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS machine_capabilities (
    id          TEXT PRIMARY KEY,
    machine_id  TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    model_class TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 10,
    max_context INTEGER,
    enabled     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (machine_id, model_class)
);

CREATE TABLE IF NOT EXISTS routing_policies (
    id          TEXT PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    description TEXT,
    fallback    TEXT NOT NULL DEFAULT 'any_available',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_rules (
    id         TEXT PRIMARY KEY,
    policy_id  TEXT NOT NULL REFERENCES routing_policies(id) ON DELETE CASCADE,
    model_class TEXT NOT NULL,
    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    rank       INTEGER NOT NULL,
    UNIQUE (policy_id, model_class, rank)
);

CREATE TABLE IF NOT EXISTS machine_users (
    id          TEXT PRIMARY KEY,
    machine_id  TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    priority    INTEGER NOT NULL DEFAULT 100,
    created_at  INTEGER NOT NULL,
    UNIQUE (machine_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_mu_machine ON machine_users(machine_id);
CREATE INDEX IF NOT EXISTS idx_mu_user    ON machine_users(user_id);

CREATE TABLE IF NOT EXISTS routing_log (
    id           TEXT PRIMARY KEY,
    user_id      TEXT,
    model_alias  TEXT NOT NULL,
    model_class  TEXT NOT NULL,
    machine_id   TEXT,
    machine_name TEXT,
    layer        TEXT,
    instance_name TEXT,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cap_machine   ON machine_capabilities(machine_id);
CREATE INDEX IF NOT EXISTS idx_rules_policy  ON policy_rules(policy_id);
CREATE INDEX IF NOT EXISTS idx_rlog_user     ON routing_log(user_id);
CREATE INDEX IF NOT EXISTS idx_rlog_ts       ON routing_log(created_at);

-- ── Action policies ────────────────────────────────────────────────────────
-- What a user/session/agent is permitted to do (write, exec, provider, etc.)
-- Separate from routing_policies which govern machine/provider *selection*.

CREATE TABLE IF NOT EXISTS action_policies (
    id                TEXT PRIMARY KEY,
    name              TEXT UNIQUE NOT NULL,
    description       TEXT,
    network_policy    TEXT NOT NULL DEFAULT 'internet_enabled',
    network_allowlist TEXT NOT NULL DEFAULT '[]',
    filesystem_policy TEXT NOT NULL DEFAULT 'workspace_only',
    exec_policy       TEXT NOT NULL DEFAULT 'restricted',
    write_policy      TEXT NOT NULL DEFAULT 'auto_apply',
    provider_policy   TEXT NOT NULL DEFAULT 'any',
    secret_policy     TEXT NOT NULL DEFAULT 'tool_only',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- ── Approval requests ──────────────────────────────────────────────────────
-- Pending / resolved approval gates for policy-controlled tool calls.

CREATE TABLE IF NOT EXISTS approval_requests (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    user_id       TEXT,
    tool_name     TEXT NOT NULL,
    tool_args     TEXT NOT NULL,
    tool_args_hash TEXT NOT NULL,
    action_type   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','rejected','timeout','cancelled')),
    policy_id     TEXT,
    requested_at  INTEGER NOT NULL,
    decided_at    INTEGER,
    decided_by    TEXT,
    decision_note TEXT,
    expires_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appr_session ON approval_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_appr_status  ON approval_requests(status);
CREATE INDEX IF NOT EXISTS idx_appr_ts      ON approval_requests(requested_at);
CREATE INDEX IF NOT EXISTS idx_appr_lookup  ON approval_requests(session_id, tool_name, tool_args_hash, status);

-- ── Workflow system ─────────────────────────────────────────────────────────
-- workflow_definitions: reusable templates; steps stored as JSON.
-- workflow_runs:        execution instances (one per trigger).
-- workflow_step_runs:   per-step state for one run.

CREATE TABLE IF NOT EXISTS workflow_definitions (
    id           TEXT PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    description  TEXT,
    version      TEXT NOT NULL DEFAULT '1.0',
    steps_json   TEXT NOT NULL DEFAULT '[]',
    tags         TEXT NOT NULL DEFAULT '[]',
    created_by   TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id           TEXT PRIMARY KEY,
    workflow_id  TEXT NOT NULL REFERENCES workflow_definitions(id),
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','running','paused','success','failed','cancelled')),
    triggered_by TEXT,
    input_json   TEXT NOT NULL DEFAULT '{}',
    output_json  TEXT,
    error        TEXT,
    started_at   INTEGER,
    finished_at  INTEGER,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_step_runs (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id        TEXT NOT NULL,
    step_type      TEXT NOT NULL,
    step_name      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','running','success','failed','skipped','waiting_approval','cancelled')),
    parallel_group TEXT,
    depends_on     TEXT NOT NULL DEFAULT '[]',
    input_summary  TEXT,
    output_summary TEXT,
    approval_id    TEXT REFERENCES approval_requests(id),
    error          TEXT,
    started_at     INTEGER,
    finished_at    INTEGER,
    created_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wfrun_workflow ON workflow_runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_wfrun_status   ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_wfrun_ts       ON workflow_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_wfstep_run     ON workflow_step_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_wfstep_status  ON workflow_step_runs(status);

-- ── Agent run records ────────────────────────────────────────────────────────
-- One record per _run_agent invocation: tracks model, tools used, status, etc.

CREATE TABLE IF NOT EXISTS agent_runs (
    id                    TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    user_id               TEXT REFERENCES users(id) ON DELETE SET NULL,
    instance_name         TEXT,
    soul                  TEXT,
    model                 TEXT,
    provider              TEXT,
    action_policy_id      TEXT,
    action_policy_snapshot TEXT,
    workflow_run_id       TEXT,
    workspace_path        TEXT,
    status                TEXT NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running','success','failed','cancelled')),
    user_message          TEXT,
    tool_sequence         TEXT NOT NULL DEFAULT '[]',
    tool_detail           TEXT NOT NULL DEFAULT '[]',
    approval_ids          TEXT NOT NULL DEFAULT '[]',
    output_summary        TEXT,
    error                 TEXT,
    api_calls             INTEGER NOT NULL DEFAULT 0,
    started_at            INTEGER NOT NULL,
    finished_at           INTEGER,
    created_at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arun_session ON agent_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_arun_user    ON agent_runs(user_id);
CREATE INDEX IF NOT EXISTS idx_arun_status  ON agent_runs(status);
CREATE INDEX IF NOT EXISTS idx_arun_ts      ON agent_runs(created_at);

-- ── Evolution ─────────────────────────────────────────────────────────────────
-- Proposals generated by agents via the self-improvement skill, reviewed by users.

CREATE TABLE IF NOT EXISTS evolution_proposals (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL DEFAULT 'hermes',
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    diff_text       TEXT,
    target_files    TEXT NOT NULL DEFAULT '[]',
    proposal_type   TEXT NOT NULL DEFAULT 'improvement'
                        CHECK (proposal_type IN ('improvement','bugfix','refactor','new_feature')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','accepted','declined','questioned',
                                          'in_progress','merged','cancelled')),
    question_text   TEXT,
    answer_text     TEXT,
    frontier_model  TEXT,
    frontier_output TEXT,
    cron_job_id     TEXT,
    git_branch      TEXT,
    git_pr_url      TEXT,
    decided_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    decided_at      INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evo_status ON evolution_proposals(status);
CREATE INDEX IF NOT EXISTS idx_evo_ts     ON evolution_proposals(created_at);

CREATE TABLE IF NOT EXISTS evolution_settings (
    id                   INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled              INTEGER NOT NULL DEFAULT 0,
    schedule_label       TEXT NOT NULL DEFAULT '1 week',
    schedule_minutes     INTEGER NOT NULL DEFAULT 10080,
    git_remote_url       TEXT,
    git_username         TEXT,
    git_pat              TEXT,
    git_base_branch      TEXT NOT NULL DEFAULT 'main',
    frontier_model       TEXT NOT NULL DEFAULT 'claude-opus-4-6',
    frontier_api_key_env TEXT NOT NULL DEFAULT 'ANTHROPIC_API_KEY',
    max_pending          INTEGER NOT NULL DEFAULT 5,
    cron_job_id          TEXT,
    updated_at           INTEGER NOT NULL
);
INSERT OR IGNORE INTO evolution_settings (id, updated_at) VALUES (1, unixepoch() * 1000);
"""


def init_db(hermes_home: Path) -> None:
    global _DB_PATH
    _DB_PATH = hermes_home / "auth.db"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.executescript(_SCHEMA)
    _run_migrations()
    logger.info("Auth DB initialised at %s", _DB_PATH)


def _run_migrations() -> None:
    """Idempotent ALTER TABLE migrations for existing databases."""
    with _conn() as conn:
        for stmt in (
            "ALTER TABLE users ADD COLUMN policy_id TEXT REFERENCES routing_policies(id)",
            "ALTER TABLE machines ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
            # v2: action policies (behaviour enforcement, separate from routing policies)
            "ALTER TABLE users ADD COLUMN action_policy_id TEXT REFERENCES action_policies(id)",
            # v3: sandboxed execution workspace tracking
            "ALTER TABLE agent_runs ADD COLUMN workspace_path TEXT",
            # v4: multi-agent — which adapter produced this run
            "ALTER TABLE agent_runs ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'hermes'",
            # v5: machine default model
            "ALTER TABLE machines ADD COLUMN default_model TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


@contextmanager
def _conn():
    if _DB_PATH is None:
        raise RuntimeError("Auth DB not initialised — call init_db() first")
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:21]}"


# ── Users ──────────────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(username) = ?", (username.lower(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def create_user(
    email: str,
    username: str,
    password_hash: str,
    role: str = "user",
    display_name: Optional[str] = None,
    status: str = "active",
) -> dict:
    uid = _new_id("usr")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO users
               (id, email, username, password_hash, role, status, display_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, email.lower(), username, password_hash, role, status, display_name, now),
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, updated_at) VALUES (?, ?)",
            (uid, now),
        )
    return get_user_by_id(uid)


def update_last_login(user_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET last_login = ?, failed_login_count = 0, locked_until = NULL WHERE id = ?",
            (int(time.time() * 1000), user_id),
        )


def record_failed_login(user_id: str, count: int, locked_until: Optional[int]) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET failed_login_count = ?, locked_until = ? WHERE id = ?",
            (count, locked_until, user_id),
        )


def assign_user_policy(user_id: str, policy_id: Optional[str]) -> None:
    with _conn() as conn:
        conn.execute("UPDATE users SET policy_id = ? WHERE id = ?", (policy_id, user_id))


def get_user_policy(user_id: str) -> Optional[dict]:
    """Return the routing_policies row for this user, or None if unassigned."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT rp.* FROM routing_policies rp
               JOIN users u ON u.policy_id = rp.id
               WHERE u.id = ?""",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def update_user(user_id: str, **fields) -> Optional[dict]:
    allowed = {"role", "status", "display_name", "password_hash", "email", "username"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_user_by_id(user_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",
            (*updates.values(), user_id),
        )
    return get_user_by_id(user_id)


def list_users(
    page: int = 1,
    limit: int = 20,
    role: Optional[str] = None,
    status: Optional[str] = None,
) -> tuple[list[dict], int]:
    conditions, params = [], []
    if role:
        conditions.append("role = ?")
        params.append(role)
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM users {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM users {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


# ── Refresh tokens ─────────────────────────────────────────────────────────

def store_refresh_token(
    user_id: str,
    token_hash: str,
    expires_at: int,
    ip: Optional[str] = None,
    ua: Optional[str] = None,
) -> str:
    tid = _new_id("rtk")
    now = int(time.time())
    with _conn() as conn:
        conn.execute(
            """INSERT INTO refresh_tokens
               (id, user_id, token_hash, issued_at, expires_at, ip_address, user_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tid, user_id, token_hash, now, expires_at, ip, ua),
        )
    return tid


def get_refresh_token(token_hash: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None


def revoke_refresh_token(token_hash: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?",
            (token_hash,),
        )


def revoke_all_user_tokens(user_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?",
            (user_id,),
        )


# ── User Settings ───────────────────────────────────────────────────────────

def get_user_settings(user_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def update_user_settings(user_id: str, **fields) -> Optional[dict]:
    allowed = {"default_soul", "default_model", "ui_theme", "notification_telegram", "spawn_defaults"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE user_settings SET {set_clause} WHERE user_id = ?",
            (*updates.values(), user_id),
        )
    return get_user_settings(user_id)


# ── Audit Log ──────────────────────────────────────────────────────────────

def write_audit_log(
    user_id: Optional[str],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    try:
        lid = _new_id("aud")
        now = int(time.time() * 1000)
        with _conn() as conn:
            conn.execute(
                """INSERT INTO audit_logs
                   (id, user_id, action, target_type, target_id, metadata, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lid, user_id, action, target_type, target_id,
                    json.dumps(metadata) if metadata else None,
                    ip_address, now,
                ),
            )
    except Exception as exc:
        logger.warning("Failed to write audit log: %s", exc)


# ── Audit log query ────────────────────────────────────────────────────────

def list_audit_logs(
    page: int = 1,
    limit: int = 50,
    user_id: Optional[str] = None,
    action: Optional[str] = None,
) -> tuple[list[dict], int]:
    conditions, params = [], []
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if action:
        conditions.append("action = ?")
        params.append(action)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM audit_logs {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM audit_logs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


# ── Machines ──────────────────────────────────────────────────────────────────

def create_machine(name: str, endpoint_url: str, description: Optional[str] = None) -> dict:
    mid = _new_id("mach")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO machines (id, name, endpoint_url, description, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (mid, name, endpoint_url, description, now, now),
        )
    return get_machine(mid)


def get_machine(machine_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
        return dict(row) if row else None


def list_machines() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM machines ORDER BY sort_order, created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def reorder_machines(ordered_ids: list) -> None:
    """Set sort_order for each machine based on the given id ordering."""
    with _conn() as conn:
        for i, mid in enumerate(ordered_ids):
            conn.execute(
                "UPDATE machines SET sort_order = ?, updated_at = ? WHERE id = ?",
                (i, int(time.time() * 1000), mid),
            )


def update_machine(machine_id: str, **fields) -> Optional[dict]:
    allowed = {"name", "endpoint_url", "description", "enabled", "default_model"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_machine(machine_id)
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE machines SET {set_clause} WHERE id = ?",
            (*updates.values(), machine_id),
        )
    return get_machine(machine_id)


def delete_machine(machine_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM machines WHERE id = ?", (machine_id,))


# ── Machine user claims ────────────────────────────────────────────────────────

def claim_machine(machine_id: str, user_id: str, priority: int = 100) -> dict:
    """Claim or update a user's priority on a machine."""
    cid = _new_id("mu")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO machine_users (id, machine_id, user_id, priority, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(machine_id, user_id) DO UPDATE SET priority = excluded.priority""",
            (cid, machine_id, user_id, priority, now),
        )
        row = conn.execute(
            """SELECT mu.*, u.username, u.display_name, u.email
               FROM machine_users mu JOIN users u ON u.id = mu.user_id
               WHERE mu.machine_id = ? AND mu.user_id = ?""",
            (machine_id, user_id),
        ).fetchone()
        return dict(row) if row else {}


def unclaim_machine(machine_id: str, user_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM machine_users WHERE machine_id = ? AND user_id = ?",
            (machine_id, user_id),
        )


def list_machine_claims(machine_id: str) -> list[dict]:
    """Return all user claims for a machine, ordered by priority ascending."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT mu.*, u.username, u.display_name, u.email
               FROM machine_users mu JOIN users u ON u.id = mu.user_id
               WHERE mu.machine_id = ?
               ORDER BY mu.priority""",
            (machine_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_user_machines(user_id: str) -> list[dict]:
    """Return all machines claimed by a user, with their priority."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT mu.priority, m.*
               FROM machine_users mu JOIN machines m ON m.id = mu.machine_id
               WHERE mu.user_id = ?
               ORDER BY mu.priority""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_claims() -> list[dict]:
    """Return all machine→user claims for the MCP routing tool."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT mu.priority, mu.created_at,
                      m.id as machine_id, m.name as machine_name,
                      m.endpoint_url, m.enabled as machine_enabled,
                      u.id as user_id, u.username, u.display_name, u.email
               FROM machine_users mu
               JOIN machines m ON m.id = mu.machine_id
               JOIN users u ON u.id = mu.user_id
               ORDER BY m.name, mu.priority""",
        ).fetchall()
        return [dict(r) for r in rows]


# ── Machine capabilities ──────────────────────────────────────────────────────

def get_machine_capabilities(machine_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM machine_capabilities WHERE machine_id = ? ORDER BY priority",
            (machine_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_machine_capabilities(machine_id: str, capabilities: list) -> None:
    """Replace all capabilities for a machine atomically.

    Accepts either a list of model class strings (e.g. ["lightweight", "coding"])
    or a list of dicts with a "model_class" key.
    """
    with _conn() as conn:
        conn.execute("DELETE FROM machine_capabilities WHERE machine_id = ?", (machine_id,))
        for cap in capabilities:
            if isinstance(cap, str):
                cap = {"model_class": cap}
            cid = _new_id("cap")
            conn.execute(
                """INSERT INTO machine_capabilities
                   (id, machine_id, model_class, priority, max_context, enabled)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    cid, machine_id,
                    cap.get("model_class", "general"),
                    int(cap.get("priority", 10)),
                    cap.get("max_context"),
                    1 if cap.get("enabled", True) else 0,
                ),
            )


# ── Routing policies ──────────────────────────────────────────────────────────

def create_policy(
    name: str,
    description: Optional[str] = None,
    fallback: str = "any_available",
) -> dict:
    pid = _new_id("pol")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO routing_policies (id, name, description, fallback, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (pid, name, description, fallback, now, now),
        )
    return get_policy(pid)


def get_policy(policy_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM routing_policies WHERE id = ?", (policy_id,)
        ).fetchone()
        return dict(row) if row else None


def list_policies() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM routing_policies ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]


def update_policy(policy_id: str, **fields) -> Optional[dict]:
    allowed = {"name", "description", "fallback"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_policy(policy_id)
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE routing_policies SET {set_clause} WHERE id = ?",
            (*updates.values(), policy_id),
        )
    return get_policy(policy_id)


def delete_policy(policy_id: str) -> None:
    """Delete a policy and unassign it from all users atomically."""
    with _conn() as conn:
        conn.execute("UPDATE users SET policy_id = NULL WHERE policy_id = ?", (policy_id,))
        conn.execute("DELETE FROM routing_policies WHERE id = ?", (policy_id,))


def count_users_with_policy(policy_id: str) -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE policy_id = ?", (policy_id,)
        ).fetchone()[0]


def count_profiles_using_machine(machine_id: str) -> int:
    """Return the number of distinct routing profiles that have a rule pointing at this machine."""
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(DISTINCT policy_id) FROM policy_rules WHERE machine_id = ?",
            (machine_id,),
        ).fetchone()[0]


# ── Policy rules ──────────────────────────────────────────────────────────────

def get_policy_rules(policy_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT pr.*, m.name as machine_name
               FROM policy_rules pr
               LEFT JOIN machines m ON m.id = pr.machine_id
               WHERE pr.policy_id = ?
               ORDER BY pr.model_class, pr.rank""",
            (policy_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_policy_rules(policy_id: str, rules: list[dict]) -> None:
    """Replace all rules for a policy atomically."""
    with _conn() as conn:
        conn.execute("DELETE FROM policy_rules WHERE policy_id = ?", (policy_id,))
        for i, rule in enumerate(rules):
            rid = _new_id("rul")
            conn.execute(
                """INSERT INTO policy_rules (id, policy_id, model_class, machine_id, rank)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    rid, policy_id,
                    rule.get("model_class", "*"),
                    rule["machine_id"],
                    rule.get("rank", i + 1),
                ),
            )


def resolve_policy_machines(user_id: str, model_class: str) -> list[dict]:
    """Return ordered list of machines for a user+model_class, honouring wildcard rules.

    Returns empty list if the user has no policy assigned.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT policy_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or not row["policy_id"]:
            return []
        policy_id = row["policy_id"]
        # Exact class rules first, then wildcard "*" as supplement
        rows = conn.execute(
            """SELECT m.*, pr.rank, pr.model_class as rule_class
               FROM policy_rules pr
               JOIN machines m ON m.id = pr.machine_id
               WHERE pr.policy_id = ?
                 AND pr.model_class IN (?, '*')
                 AND m.enabled = 1
               ORDER BY CASE WHEN pr.model_class = ? THEN 0 ELSE 1 END, pr.rank""",
            (policy_id, model_class, model_class),
        ).fetchall()
        # Deduplicate by machine_id (exact-class rules win over wildcard)
        seen: set[str] = set()
        result = []
        for r in rows:
            d = dict(r)
            if d["id"] not in seen:
                seen.add(d["id"])
                result.append(d)
        return result


# ── Routing log ────────────────────────────────────────────────────────────

def log_routing_decision(
    user_id: str | None,
    model_alias: str,
    model_class: str,
    machine_id: str | None,
    machine_name: str | None,
    layer: str,
    instance_name: str | None = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO routing_log
               (id, user_id, model_alias, model_class, machine_id, machine_name, layer, instance_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_new_id("rlog"), user_id, model_alias, model_class,
             machine_id, machine_name, layer, instance_name, int(time.time())),
        )


def list_routing_log(
    *,
    user_id: str | None = None,
    since: int | None = None,
    until: int | None = None,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict], int]:
    """Return (rows, total) for the routing_log with optional filters."""
    conditions: list[str] = []
    params: list = []
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if since:
        conditions.append("created_at >= ?")
        params.append(since)
    if until:
        conditions.append("created_at <= ?")
        params.append(until)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit

    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM routing_log {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM routing_log {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


# ── Action policies ─────────────────────────────────────────────────────────

def create_action_policy(
    name: str,
    description: str = "",
    network_policy: str = "internet_enabled",
    network_allowlist: str = "[]",
    filesystem_policy: str = "workspace_only",
    exec_policy: str = "restricted",
    write_policy: str = "auto_apply",
    provider_policy: str = "any",
    secret_policy: str = "tool_only",
) -> dict:
    now = int(time.time() * 1000)
    pid = _new_id("ap")
    with _conn() as conn:
        conn.execute(
            """INSERT INTO action_policies
               (id, name, description, network_policy, network_allowlist,
                filesystem_policy, exec_policy, write_policy, provider_policy,
                secret_policy, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, name, description, network_policy, network_allowlist,
             filesystem_policy, exec_policy, write_policy, provider_policy,
             secret_policy, now, now),
        )
        row = conn.execute("SELECT * FROM action_policies WHERE id=?", (pid,)).fetchone()
    return dict(row)


def get_action_policy(policy_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM action_policies WHERE id=?", (policy_id,)
        ).fetchone()
    return dict(row) if row else None


def list_action_policies() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM action_policies ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def update_action_policy(policy_id: str, **fields) -> Optional[dict]:
    allowed = {
        "name", "description", "network_policy", "network_allowlist",
        "filesystem_policy", "exec_policy", "write_policy",
        "provider_policy", "secret_policy",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_action_policy(policy_id)
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [policy_id]
    with _conn() as conn:
        conn.execute(
            f"UPDATE action_policies SET {set_clause} WHERE id=?", values
        )
        row = conn.execute("SELECT * FROM action_policies WHERE id=?", (policy_id,)).fetchone()
    return dict(row) if row else None


def delete_action_policy(policy_id: str) -> bool:
    with _conn() as conn:
        affected = conn.execute(
            "DELETE FROM action_policies WHERE id=?", (policy_id,)
        ).rowcount
    return affected > 0


def assign_user_action_policy(user_id: str, policy_id: Optional[str]) -> None:
    """Assign (or clear) an action policy on a user."""
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET action_policy_id=? WHERE id=?", (policy_id, user_id)
        )


def get_user_action_policy_row(user_id: str) -> Optional[dict]:
    """Return the action_policies row for a user, or None if not assigned."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT ap.* FROM action_policies ap
               JOIN users u ON u.action_policy_id = ap.id
               WHERE u.id=?""",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


# ── Approval requests ────────────────────────────────────────────────────────

_APPROVAL_TTL_SECONDS = 300  # default 5 min


def create_approval_request(
    session_id: str,
    tool_name: str,
    tool_args: str,
    tool_args_hash: str,
    action_type: str,
    user_id: Optional[str] = None,
    policy_id: Optional[str] = None,
    expires_in: int = _APPROVAL_TTL_SECONDS,
) -> dict:
    now = int(time.time() * 1000)
    rid = _new_id("apr")
    expires_at = now + expires_in * 1000
    with _conn() as conn:
        conn.execute(
            """INSERT INTO approval_requests
               (id, session_id, user_id, tool_name, tool_args, tool_args_hash,
                action_type, status, policy_id, requested_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, session_id, user_id, tool_name, tool_args, tool_args_hash,
             action_type, "pending", policy_id, now, expires_at),
        )
        row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (rid,)).fetchone()
    return dict(row)


def get_approval_request(approval_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id=?", (approval_id,)
        ).fetchone()
    return dict(row) if row else None


def find_approved_request(
    session_id: str, tool_name: str, tool_args_hash: str
) -> Optional[dict]:
    """Return an approved, non-expired approval for this exact tool call."""
    now = int(time.time() * 1000)
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM approval_requests
               WHERE session_id=? AND tool_name=? AND tool_args_hash=?
                 AND status='approved' AND expires_at > ?
               ORDER BY decided_at DESC LIMIT 1""",
            (session_id, tool_name, tool_args_hash, now),
        ).fetchone()
    return dict(row) if row else None


def list_approval_requests(
    *,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict], int]:
    conditions: list[str] = []
    params: list = []
    if session_id:
        conditions.append("session_id=?")
        params.append(session_id)
    if status:
        conditions.append("status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM approval_requests {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM approval_requests {where} ORDER BY requested_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def resolve_approval_request(
    approval_id: str,
    status: str,  # 'approved' | 'rejected' | 'cancelled'
    decided_by: Optional[str] = None,
    note: Optional[str] = None,
) -> bool:
    """Approve or reject a pending request. Returns True if updated."""
    now = int(time.time() * 1000)
    with _conn() as conn:
        affected = conn.execute(
            """UPDATE approval_requests
               SET status=?, decided_at=?, decided_by=?, decision_note=?
               WHERE id=? AND status='pending'""",
            (status, now, decided_by, note, approval_id),
        ).rowcount
    return affected > 0


def expire_stale_approvals() -> int:
    """Mark expired pending requests as 'timeout'. Returns count updated."""
    now = int(time.time() * 1000)
    with _conn() as conn:
        affected = conn.execute(
            "UPDATE approval_requests SET status='timeout' WHERE status='pending' AND expires_at <= ?",
            (now,),
        ).rowcount
    return affected


# ── Workflow system ─────────────────────────────────────────────────────────

def create_workflow_definition(
    name: str,
    steps_json: str,
    description: str = "",
    version: str = "1.0",
    tags: str = "[]",
    created_by: Optional[str] = None,
) -> dict:
    wf_id = _new_id("wf")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO workflow_definitions
               (id,name,description,version,steps_json,tags,created_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (wf_id, name, description, version, steps_json, tags, created_by, now, now),
        )
    return get_workflow_definition(wf_id)


def get_workflow_definition(wf_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE id=?", (wf_id,)
        ).fetchone()
    return dict(row) if row else None


def list_workflow_definitions() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_definitions ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def update_workflow_definition(wf_id: str, **kwargs) -> Optional[dict]:
    allowed = {"name", "description", "version", "steps_json", "tags"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return get_workflow_definition(wf_id)
    fields["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as conn:
        conn.execute(
            f"UPDATE workflow_definitions SET {set_clause} WHERE id=?",
            list(fields.values()) + [wf_id],
        )
    return get_workflow_definition(wf_id)


def delete_workflow_definition(wf_id: str) -> bool:
    with _conn() as conn:
        affected = conn.execute(
            "DELETE FROM workflow_definitions WHERE id=?", (wf_id,)
        ).rowcount
    return affected > 0


def create_workflow_run(
    workflow_id: str,
    triggered_by: Optional[str] = None,
    inputs: Optional[dict] = None,
) -> str:
    run_id = _new_id("wfrun")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO workflow_runs
               (id,workflow_id,status,triggered_by,input_json,created_at)
               VALUES (?,?,?,?,?,?)""",
            (run_id, workflow_id, "pending", triggered_by,
             json.dumps(inputs or {}), now),
        )
    return run_id


def get_workflow_run(run_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE id=?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def list_workflow_runs(
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conditions: list[str] = []
    params: list = []
    if workflow_id:
        conditions.append("workflow_id=?")
        params.append(workflow_id)
    if status:
        conditions.append("status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM workflow_runs {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM workflow_runs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def update_workflow_run(run_id: str, **kwargs) -> None:
    allowed = {"status", "started_at", "finished_at", "output_json", "error"}
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as conn:
        conn.execute(
            f"UPDATE workflow_runs SET {set_clause} WHERE id=?",
            list(fields.values()) + [run_id],
        )


def create_workflow_step_run(run_id: str, step_def: Any) -> str:
    """Accepts a StepDefinition instance or dict."""
    step_id_val = step_def.id if hasattr(step_def, "id") else step_def["id"]
    step_type   = step_def.type.value if hasattr(step_def, "type") and hasattr(step_def.type, "value") else str(step_def.type if hasattr(step_def, "type") else step_def.get("type", ""))
    step_name   = step_def.name if hasattr(step_def, "name") else step_def.get("name", step_id_val)
    pg          = step_def.parallel_group if hasattr(step_def, "parallel_group") else step_def.get("parallel_group")
    deps        = json.dumps(step_def.depends_on if hasattr(step_def, "depends_on") else step_def.get("depends_on", []))
    row_id      = _new_id("wfstep")
    now         = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO workflow_step_runs
               (id,run_id,step_id,step_type,step_name,status,parallel_group,depends_on,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (row_id, run_id, step_id_val, step_type, step_name,
             "pending", pg, deps, now),
        )
    return row_id


def get_workflow_step_runs(run_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_step_runs WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_step_run(
    run_id: str,
    step_id: str,
    status: Optional[Any] = None,
    output_summary: Optional[str] = None,
    error: Optional[str] = None,
    started_at: Optional[int] = None,
    finished_at: Optional[int] = None,
    approval_id: Optional[str] = None,
) -> None:
    fields: dict = {}
    if status is not None:
        fields["status"] = status.value if hasattr(status, "value") else status
    if output_summary is not None:
        fields["output_summary"] = output_summary
    if error is not None:
        fields["error"] = error
    if started_at is not None:
        fields["started_at"] = started_at
    if finished_at is not None:
        fields["finished_at"] = finished_at
    if approval_id is not None:
        fields["approval_id"] = approval_id
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as conn:
        conn.execute(
            f"UPDATE workflow_step_runs SET {set_clause} WHERE run_id=? AND step_id=?",
            list(fields.values()) + [run_id, step_id],
        )


def create_workflow_approval(run_id: str, step_id: str, note: str) -> str:
    """Create an approval_requests record for a workflow approval step.

    Uses a synthetic tool_name so it surfaces alongside regular approvals.
    Expires in 72 hours.
    """
    approval_id = _new_id("appr")
    now = int(time.time() * 1000)
    expires = now + 72 * 3600 * 1000
    with _conn() as conn:
        conn.execute(
            """INSERT INTO approval_requests
               (id,session_id,tool_name,tool_args,tool_args_hash,action_type,
                status,requested_at,expires_at,decision_note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                approval_id,
                f"wf_{run_id}",          # session_id — identifies the run
                f"workflow_approval",    # tool_name
                json.dumps({"run_id": run_id, "step_id": step_id}),
                f"{run_id}_{step_id}",   # args hash
                "workflow_approval",     # action_type
                "pending",
                now,
                expires,
                note,
            ),
        )
    return approval_id


# ── Agent Runs ───────────────────────────────────────────────────────────────

def create_agent_run(
    session_id: str,
    user_id: Optional[str] = None,
    instance_name: Optional[str] = None,
    soul: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    action_policy_id: Optional[str] = None,
    action_policy_snapshot: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
    user_message: Optional[str] = None,
    workspace_path: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> str:
    run_id = _new_id("run")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (id,session_id,user_id,instance_name,soul,model,provider,
                action_policy_id,action_policy_snapshot,workflow_run_id,
                status,user_message,workspace_path,agent_id,started_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, session_id, user_id, instance_name, soul, model, provider,
             action_policy_id, action_policy_snapshot, workflow_run_id,
             "running", user_message, workspace_path, agent_id or "hermes", now, now),
        )
    return run_id


def set_agent_run_workspace(run_id: str, workspace_path: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE agent_runs SET workspace_path=? WHERE id=?",
            (workspace_path, run_id),
        )


def finish_agent_run(
    run_id: str,
    status: str,  # 'success' | 'failed' | 'cancelled'
    output_summary: Optional[str] = None,
    error: Optional[str] = None,
    api_calls: int = 0,
    model: Optional[str] = None,
    tool_sequence: Optional[list] = None,
    tool_detail: Optional[list] = None,
    approval_ids: Optional[list] = None,
) -> None:
    now = int(time.time() * 1000)
    fields: dict = {
        "status": status,
        "finished_at": now,
        "api_calls": api_calls,
    }
    if output_summary is not None:
        fields["output_summary"] = output_summary[:1000]
    if error is not None:
        fields["error"] = error[:500]
    if model is not None:
        fields["model"] = model
    if tool_sequence is not None:
        fields["tool_sequence"] = json.dumps(tool_sequence)
    if tool_detail is not None:
        fields["tool_detail"] = json.dumps(tool_detail)
    if approval_ids is not None:
        fields["approval_ids"] = json.dumps(approval_ids)
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as conn:
        conn.execute(
            f"UPDATE agent_runs SET {set_clause} WHERE id=?",
            list(fields.values()) + [run_id],
        )


def get_agent_run(run_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE id=?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def list_agent_runs(
    *,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conditions: list[str] = []
    params: list = []
    if user_id:
        conditions.append("user_id=?")
        params.append(user_id)
    if status:
        conditions.append("status=?")
        params.append(status)
    if session_id:
        conditions.append("session_id=?")
        params.append(session_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM agent_runs {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM agent_runs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


# ── Platform settings (singleton id=1) ────────────────────────────────────────

def get_platform_feature_flags() -> dict:
    """Return the platform feature_flags JSON dict (empty dict if unset)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT feature_flags FROM platform_settings WHERE id=1"
        ).fetchone()
    if row and row["feature_flags"]:
        try:
            return json.loads(row["feature_flags"])
        except Exception:
            return {}
    return {}


def set_platform_feature_flag(key: str, value) -> None:
    """Set a single key in platform feature_flags JSON."""
    flags = get_platform_feature_flags()
    flags[key] = value
    with _conn() as conn:
        conn.execute(
            "UPDATE platform_settings SET feature_flags=?, updated_at=? WHERE id=1",
            (json.dumps(flags), int(time.time() * 1000)),
        )


def is_setup_completed() -> bool:
    return bool(get_platform_feature_flags().get("setup_completed"))


def mark_setup_completed() -> None:
    set_platform_feature_flag("setup_completed", True)


def reset_setup_completed() -> None:
    set_platform_feature_flag("setup_completed", False)


def ensure_user_settings(user_id: str) -> None:
    """Insert a user_settings row if one doesn't exist yet."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (user_id, updated_at) VALUES (?, ?)",
            (user_id, int(time.time() * 1000)),
        )


# ── Evolution ──────────────────────────────────────────────────────────────────

def create_evolution_proposal(
    title: str,
    summary: str,
    diff_text: str | None = None,
    target_files: list[str] | None = None,
    proposal_type: str = "improvement",
    agent_id: str = "hermes",
) -> dict:
    now = int(time.time() * 1000)
    row_id = str(uuid.uuid4())
    with _conn() as conn:
        conn.execute(
            """INSERT INTO evolution_proposals
               (id, agent_id, title, summary, diff_text, target_files,
                proposal_type, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
            (
                row_id, agent_id, title, summary, diff_text,
                json.dumps(target_files or []),
                proposal_type, now, now,
            ),
        )
    return get_evolution_proposal(row_id)  # type: ignore[return-value]


def get_evolution_proposal(proposal_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM evolution_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["target_files"] = json.loads(d.get("target_files") or "[]")
    return d


def list_evolution_proposals(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conditions: list[str] = []
    params: list = []
    if status:
        conditions.append("status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM evolution_proposals {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM evolution_proposals {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["target_files"] = json.loads(d.get("target_files") or "[]")
        result.append(d)
    return result, total


def update_evolution_proposal(proposal_id: str, **fields) -> dict | None:
    """Update arbitrary columns on a proposal. Caller supplies only changed fields."""
    allowed = {
        "title", "summary", "diff_text", "target_files", "proposal_type",
        "status", "question_text", "answer_text", "frontier_model",
        "frontier_output", "cron_job_id", "git_branch", "git_pr_url",
        "decided_by", "decided_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_evolution_proposal(proposal_id)
    if "target_files" in updates:
        updates["target_files"] = json.dumps(updates["target_files"])
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE evolution_proposals SET {set_clause} WHERE id=?",
            list(updates.values()) + [proposal_id],
        )
    return get_evolution_proposal(proposal_id)


def get_evolution_settings() -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM evolution_settings WHERE id=1"
        ).fetchone()
    return dict(row) if row else {}


def update_evolution_settings(**fields) -> dict:
    allowed = {
        "enabled", "schedule_label", "schedule_minutes",
        "git_remote_url", "git_username", "git_pat", "git_base_branch",
        "frontier_model", "frontier_api_key_env", "max_pending", "cron_job_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE evolution_settings SET {set_clause} WHERE id=1",
            list(updates.values()),
        )
    return get_evolution_settings()
