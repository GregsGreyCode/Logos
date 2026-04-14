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

CREATE TABLE IF NOT EXISTS cloud_providers (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    name         TEXT NOT NULL,
    base_url     TEXT,
    api_key      TEXT,
    active_model TEXT,
    is_active    INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rlog_ts       ON routing_log(created_at);

CREATE TABLE IF NOT EXISTS agents (
    id                TEXT PRIMARY KEY,
    name              TEXT UNIQUE NOT NULL,
    soul_slug         TEXT NOT NULL DEFAULT 'general',
    model             TEXT,
    description       TEXT,
    creator_id        TEXT REFERENCES users(id),
    shared            INTEGER NOT NULL DEFAULT 1,
    toolsets          TEXT,
    daily_budget_usd  REAL,          -- NULL = no cap; else refuse dispatch once today's cost_log sum exceeds this value
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

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

CREATE TABLE IF NOT EXISTS mcp_servers (
    id              TEXT PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    catalogue_id    TEXT,
    source          TEXT NOT NULL DEFAULT 'ui'
                        CHECK (source IN ('ui', 'external')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'deploying', 'running', 'stopped', 'error', 'external')),
    deploy_mode     TEXT NOT NULL DEFAULT 'external'
                        CHECK (deploy_mode IN ('k8s', 'external')),
    url             TEXT,
    token           TEXT,
    k8s_namespace   TEXT,
    k8s_image       TEXT,
    config_json     TEXT NOT NULL DEFAULT '{}',
    tools_filter    TEXT NOT NULL DEFAULT '{}',
    category        TEXT NOT NULL DEFAULT 'general',
    description     TEXT,
    auto_wire       INTEGER NOT NULL DEFAULT 1,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    last_error      TEXT
);

-- ── Platform routing ───────────────────────────────────────────────────────
-- Maps an inbound platform conversation (chat/user/global) to the agent
-- whose sandbox should handle it. Inserted by the setup wizard (one
-- 'global' row per enabled platform → first agent) and editable from the
-- Admin → Platforms tab.
--
-- scope:
--   'global'  → fallback for any conversation on this platform
--   'chat'    → specific chat_id (e.g. a Telegram group, a Discord channel)
--   'user'    → specific user_id (DMs, identified user across chats)

CREATE TABLE IF NOT EXISTS platform_routing (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    scope       TEXT NOT NULL CHECK (scope IN ('global', 'chat', 'user')),
    scope_id    TEXT NOT NULL DEFAULT '',
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    UNIQUE(platform, scope, scope_id)
);

-- ── Model routes ───────────────────────────────────────────────────────────
-- One row per (provider, model) pair the user has provisioned. Each route is
-- backed by a dedicated OpenShell gateway pinned to that single model via
-- `openshell inference set`. Agents bind to a route via agents.model_route_id;
-- the OpenShell executor reads the route's openshell_name to know which
-- gateway to spawn the sandbox in. Lets multiple agents target different
-- models simultaneously without OpenShell's "one forced model" design
-- becoming a bottleneck.

CREATE TABLE IF NOT EXISTS model_routes (
    id              TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,                      -- 'lmstudio' / 'openai' / 'anthropic' / etc
    model           TEXT NOT NULL,                      -- 'openai/gpt-oss-20b'
    openshell_name  TEXT NOT NULL UNIQUE,               -- sanitized model name, e.g. 'qwen-qwen3-5-9b'
    openshell_port  INTEGER NOT NULL UNIQUE,            -- 9090, 9091, 9092, ...
    status          TEXT NOT NULL DEFAULT 'provisioning', -- 'provisioning' | 'ready' | 'error' | 'stopped'
    status_detail   TEXT,                               -- last error / phase note (truncated to 500 chars)
    is_default      INTEGER NOT NULL DEFAULT 0,         -- exactly one row should have is_default=1
    is_primordial   INTEGER NOT NULL DEFAULT 0,         -- DEPRECATED: always 0 in new rows (primordial concept removed)
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    UNIQUE(provider, model)
);
CREATE INDEX IF NOT EXISTS idx_mroutes_status ON model_routes(status);
CREATE INDEX IF NOT EXISTS idx_mroutes_default ON model_routes(is_default);
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
            # v6: machine api key (optional bearer token for auth-protected local servers)
            "ALTER TABLE machines ADD COLUMN api_key TEXT",
            # v7: per-agent toolset config (JSON array of enabled toolset names)
            "ALTER TABLE agents ADD COLUMN toolsets TEXT",
            # v8: per-agent sprite selection (0..7). NULL falls back to name-hash
            # in AgentSprite.js so pre-v8 agents still render.
            "ALTER TABLE agents ADD COLUMN char_index INTEGER",
            # v9: per-agent OpenShell route binding. Each agent's sandbox is
            # spawned inside the OpenShell gateway named in the linked
            # model_routes row. NULL means "use the default route" (the row
            # with is_default=1) and is the path the gateway-side resolver
            # takes when an agent was created before this column existed
            # or when the user explicitly wants the platform default.
            "ALTER TABLE agents ADD COLUMN model_route_id TEXT REFERENCES model_routes(id)",
            # v10: per-agent network policy presets layered on top of the
            # baseline in gateway/policies/openshell_default.yaml. JSON
            # array of preset names from gateway/policies/presets/*.yaml
            # (e.g. ["github", "slack"]). NULL means "no presets applied,
            # baseline only". Read by gateway.policies.get_applied_presets()
            # and merged into the effective policy at spawn time + when the
            # Tools editor UI toggles presets (MISSING.md M10 scope items
            # 4-5). Use get_agent_applied_presets() / set_agent_applied_presets()
            # below for JSON-aware access instead of reading the column
            # directly.
            "ALTER TABLE agents ADD COLUMN applied_presets TEXT",
            # v17: per-agent website blocklist (Layer 1 of URL control).
            # JSON {"enabled": bool, "patterns": [...glob...]} read by hermes
            # tools/website_policy.py before every browser navigation. Lives
            # next to applied_presets since both are policy-shaped fields the
            # capability system writes to.
            "ALTER TABLE agents ADD COLUMN website_blocklist TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists

        # v11: dispatch activity ledger (M8 Phase B). Durable record of
        # every task dispatch — who, what agent, which model, origin,
        # timing, token counts, and outcome. Feeds the Admin Activity tab.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id                TEXT PRIMARY KEY,
                task_id           TEXT NOT NULL,
                agent_id          TEXT,
                sandbox_name      TEXT,
                model             TEXT,
                origin            TEXT NOT NULL DEFAULT 'user_chat',
                origin_detail     TEXT,
                session_id        TEXT,
                user_id           TEXT,
                prompt_tokens     INTEGER,
                completion_tokens INTEGER,
                elapsed_s         REAL,
                status            TEXT NOT NULL DEFAULT 'running',
                error             TEXT,
                started_at        INTEGER NOT NULL,
                ended_at          INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_disp_agent   ON dispatches(agent_id);
            CREATE INDEX IF NOT EXISTS idx_disp_origin  ON dispatches(origin);
            CREATE INDEX IF NOT EXISTS idx_disp_ts      ON dispatches(started_at);
            CREATE INDEX IF NOT EXISTS idx_disp_status  ON dispatches(status);

            -- Cost ledger — one row per agent dispatch that consumed cloud
            -- tokens. Local models (lmstudio/ollama) also get rows with
            -- cost_usd=0 so activity is visible; cost only accrues for
            -- cloud providers. Input columns are the raw counts straight
            -- from the model response (Anthropic: input_tokens, cache_read,
            -- cache_creation; OpenAI: prompt_tokens, completion_tokens).
            -- cost_usd is computed at insert time using gateway.pricing
            -- so historical rows stay accurate if pricing later shifts.
            CREATE TABLE IF NOT EXISTS cost_log (
                id             TEXT PRIMARY KEY,
                ts             INTEGER NOT NULL,
                agent_id       TEXT,
                agent_name     TEXT,
                session_id     TEXT,
                task_id        TEXT,
                provider       TEXT,
                model          TEXT NOT NULL,
                input_tokens   INTEGER NOT NULL DEFAULT 0,
                output_tokens  INTEGER NOT NULL DEFAULT 0,
                cache_read_tok INTEGER NOT NULL DEFAULT 0,
                cache_write_tok INTEGER NOT NULL DEFAULT 0,
                cost_usd       REAL NOT NULL DEFAULT 0,
                pricing_known  INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_cost_agent ON cost_log(agent_id);
            CREATE INDEX IF NOT EXISTS idx_cost_ts    ON cost_log(ts);
            CREATE INDEX IF NOT EXISTS idx_cost_model ON cost_log(model);
        """)

        # v12: migrate per-agent char_index from the legacy 0..23 encoding
        # (slot index into a 24-variant sheet) to the new 0..95 encoding
        # (body*12 + skin*4 + hair for an 8×3×4 variant matrix).
        # Legacy mapping:
        #   old 0..7   → body N, skin 0 (light), hair 0 (original)   = N * 12
        #   old 8..15  → body N, skin 1 (medium), hair 0             = N * 12 + 4
        #   old 16..23 → body N, skin 2 (dark),   hair 2 (180°)      = N * 12 + 10
        # Guarded by a schema_flags row so it only runs once.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_flags (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                applied_at INTEGER
            );
        """)
        already = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'char_index_v2'"
        ).fetchone()
        if not already:
            conn.execute("""
                UPDATE agents SET char_index = CASE
                    WHEN char_index BETWEEN 0  AND 7  THEN  char_index       * 12
                    WHEN char_index BETWEEN 8  AND 15 THEN (char_index - 8)  * 12 + 4
                    WHEN char_index BETWEEN 16 AND 23 THEN (char_index - 16) * 12 + 10
                    ELSE char_index
                END
                WHERE char_index IS NOT NULL AND char_index BETWEEN 0 AND 23
            """)
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("char_index_v2", "1", int(time.time() * 1000)),
            )

        # v13: migrate char_index from the 8×3×4 (0..95) encoding to the
        # new 8×4×5 (0..159) encoding. Old skin 0/1/2 map to new skin
        # 0/2/3 (new skin 1 is a freshly-added mid-light tone that no
        # existing agent can be on). Old hair 0..3 all map to new hair 0
        # (original) — the old sheet's non-zero hair was a 90°/180°/270°
        # hue shift of the source, which is not the same palette as the
        # new theme colours, so collapsing to Original is the safest
        # visual. Agents can re-pick a theme in the picker if desired.
        already_v3 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'char_index_v3'"
        ).fetchone()
        if not already_v3:
            conn.execute("""
                UPDATE agents SET char_index = (
                    (char_index / 12) * 20
                    + CASE (char_index % 12) / 4
                        WHEN 0 THEN 0
                        WHEN 1 THEN 2
                        WHEN 2 THEN 3
                      END * 5
                    + 0
                )
                WHERE char_index IS NOT NULL AND char_index BETWEEN 0 AND 95
            """)
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("char_index_v3", "1", int(time.time() * 1000)),
            )

        # v18: apply B-tier capability defaults to agents that spawned with
        # empty toolsets (e.g., Adam — created after the v15 backfill stamped
        # its flag but before handle_agents_post wired capability defaults).
        # Narrower than v15: only touches agents where toolsets is literally
        # '[]' or NULL, so agents the user has deliberately customised stay
        # untouched. Idempotent via schema_flags.
        already_v18 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'agent_b_defaults_v1'"
        ).fetchone()
        if not already_v18:
            empties = conn.execute(
                "SELECT id, name FROM agents WHERE toolsets IS NULL OR toolsets = '' OR toolsets = '[]'"
            ).fetchall()
            if empties:
                # Import lazily so the migration module stays dependency-lite.
                try:
                    from gateway import capabilities as _caps
                    fixed = 0
                    for row in empties:
                        try:
                            _caps.apply_initial_defaults(row["id"])
                            fixed += 1
                        except Exception as exc:
                            logger.warning(
                                "agent_b_defaults_v1: agent %s failed: %s",
                                row["name"], exc,
                            )
                    logger.info(
                        "agent_b_defaults_v1: stamped %d empty-toolset agent(s) with B-tier defaults",
                        fixed,
                    )
                except Exception as exc:
                    logger.warning(
                        "agent_b_defaults_v1: could not import capabilities: %s — skipping",
                        exc,
                    )
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("agent_b_defaults_v1", "1", int(time.time() * 1000)),
            )

        # v19: add daily_budget_usd column + fallback_route_id column to
        # existing agents tables. Cost-tracker feature: a cap on how much
        # a cloud-backed agent can spend per rolling 24h. When breached,
        # dispatch refuses (or falls back to a local route if one is
        # configured). Existing agents get NULL (no cap) by default.
        already_v19 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'agent_budget_columns_v1'"
        ).fetchone()
        if not already_v19:
            # ALTER TABLE ADD COLUMN is idempotent via try/except because
            # SQLite will raise "duplicate column" on re-run.
            for col_ddl in (
                "ALTER TABLE agents ADD COLUMN daily_budget_usd REAL",
                "ALTER TABLE agents ADD COLUMN fallback_route_id TEXT",
            ):
                try:
                    conn.execute(col_ddl)
                except Exception as exc:
                    if "duplicate column" not in str(exc).lower():
                        logger.warning("v19 migration: %s failed: %s", col_ddl, exc)
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("agent_budget_columns_v1", "1", int(time.time() * 1000)),
            )
            logger.info("v19: added daily_budget_usd + fallback_route_id to agents")

        # v16: apply the `browserless` preset to every existing agent so the
        # local browser tool works out of the box. Pairs with the matching
        # default in handle_agents_post (new agents get it too) and with the
        # browserless.yaml preset that targets host.openshell.internal:3000.
        # Idempotent — skips rows that already have it. Safe even if no
        # browserless container is running; the network grant just sits there
        # unused until one is.
        already_v16 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'browserless_preset_default_v1'"
        ).fetchone()
        if not already_v16:
            import json as _json
            try:
                rows = conn.execute(
                    "SELECT id, applied_presets FROM agents"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            updated = 0
            for row in rows:
                raw = row["applied_presets"] or "[]"
                try:
                    presets = _json.loads(raw) if isinstance(raw, str) else list(raw)
                    if not isinstance(presets, list):
                        presets = []
                except (ValueError, TypeError):
                    presets = []
                if "browserless" not in presets:
                    presets.append("browserless")
                    conn.execute(
                        "UPDATE agents SET applied_presets = ? WHERE id = ?",
                        (_json.dumps(presets), row["id"]),
                    )
                    updated += 1
            logger.info(
                "browserless_preset_default_v1: stamped %d agent(s) with the browserless preset",
                updated,
            )
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("browserless_preset_default_v1", "1", int(time.time() * 1000)),
            )

        # v15: backfill agents.toolsets when empty. Agents created before
        # the default-enabled toolset expansion (or with explicit empty
        # selection) hit "model promised to use a tool but didn't" because
        # AIAgent received an empty enabled_toolsets list. Stamp them with
        # a sane default — the union of enforced + default_enabled from
        # gateway/souls/general/soul.manifest.yaml — so existing agents
        # work without a manual edit. Per-soul defaults could be richer,
        # but the General soul's list covers the common case and Greg
        # confirmed "give them everything by default" for now.
        already_v15 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'agent_toolsets_backfill_v1'"
        ).fetchone()
        if not already_v15:
            import json as _json
            default_toolsets = _json.dumps([
                "web", "memory", "clarify", "session_search", "todo",
                "file", "world", "browser", "image_gen", "tts",
                "delegation", "terminal",
            ])
            cur = conn.execute(
                "UPDATE agents SET toolsets = ? "
                "WHERE toolsets IS NULL OR toolsets = '' OR toolsets = '[]'",
                (default_toolsets,),
            )
            logger.info(
                "agent_toolsets_backfill_v1: stamped %d agent(s) with default toolsets",
                cur.rowcount,
            )
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("agent_toolsets_backfill_v1", "1", int(time.time() * 1000)),
            )

        # v14: backfill agents.model_route_id from the default route. Agents
        # created before handle_agents_post auto-binding (or before any
        # routes were provisioned) carry NULL here and look "unattached"
        # on the Dashboards bound-agents column even though the executor
        # is happily resolving the default at spawn time. Make the DB the
        # source of truth so the count is accurate. Idempotent: only
        # touches NULL rows, only runs once.
        already_v4 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'agent_route_backfill_v1'"
        ).fetchone()
        if not already_v4:
            default_row = conn.execute(
                "SELECT id FROM model_routes WHERE is_default = 1 LIMIT 1"
            ).fetchone()
            if default_row:
                cur = conn.execute(
                    "UPDATE agents SET model_route_id = ? WHERE model_route_id IS NULL",
                    (default_row["id"],),
                )
                logger.info(
                    "agent_route_backfill_v1: bound %d agent(s) to default route %s",
                    cur.rowcount, default_row["id"],
                )
            # Stamp the flag even if no default route exists so we don't
            # re-scan on every startup. If a default is provisioned later,
            # the auto-bind in handle_agents_post covers new agents and
            # existing nulls remain rare (only the explicit-no-route case).
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("agent_route_backfill_v1", "1", int(time.time() * 1000)),
            )


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


def get_primary_admin() -> Optional[dict]:
    """Return the original (oldest) admin user — used by setup to identify the seeded account."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE role = 'admin' ORDER BY created_at ASC LIMIT 1"
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


def delete_user(user_id: str) -> bool:
    """Delete a user account and all directly-owned data.

    Preserves audit_log rows (anonymised) so the audit trail remains intact.
    Returns True if a user row was actually deleted.
    """
    with _conn() as conn:
        conn.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return conn.total_changes > 0


def reset_user_data(user_id: str) -> None:
    """Wipe a user's run history and invalidate their active sessions.

    Clears agent_runs rows so the Runs tab is empty for that user,
    and deletes refresh_tokens so any current browser sessions are
    invalidated on next refresh.  User account and settings are kept.
    """
    with _conn() as conn:
        conn.execute("DELETE FROM agent_runs WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))


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
    allowed = {"name", "endpoint_url", "description", "enabled", "default_model", "api_key"}
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


# ── Cloud providers ──────────────────────────────────────────────────────────

def create_cloud_provider(provider: str, name: str, api_key: str = "",
                          base_url: str = "", active_model: str = "") -> dict:
    pid = _new_id("cprov")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO cloud_providers
               (id, provider, name, base_url, api_key, active_model, is_active, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?)""",
            (pid, provider, name, base_url, api_key, active_model, now, now),
        )
    return get_cloud_provider(pid)


def get_cloud_provider(provider_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM cloud_providers WHERE id = ?", (provider_id,)).fetchone()
        return dict(row) if row else None


def list_cloud_providers() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cloud_providers ORDER BY is_active DESC, created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def update_cloud_provider(provider_id: str, **fields) -> Optional[dict]:
    allowed = {"name", "base_url", "api_key", "active_model", "is_active", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_cloud_provider(provider_id)
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE cloud_providers SET {set_clause} WHERE id = ?",
            (*updates.values(), provider_id),
        )
    return get_cloud_provider(provider_id)


def delete_cloud_provider(provider_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM cloud_providers WHERE id = ?", (provider_id,))


def set_active_cloud_provider(provider_id: str) -> Optional[dict]:
    """Set one provider as active, deactivate all others."""
    with _conn() as conn:
        conn.execute("UPDATE cloud_providers SET is_active = 0, updated_at = ?",
                     (int(time.time() * 1000),))
        conn.execute("UPDATE cloud_providers SET is_active = 1, updated_at = ? WHERE id = ?",
                     (int(time.time() * 1000), provider_id))
    return get_cloud_provider(provider_id)


def get_active_cloud_provider() -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM cloud_providers WHERE is_active = 1 LIMIT 1").fetchone()
        return dict(row) if row else None


# ── Named agents ─────────────────────────────────────────────────────────────

def create_agent(name: str, soul_slug: str = "general", model: str = "",
                 description: str = "", creator_id: str = "", shared: bool = True,
                 toolsets: str = "", char_index: Optional[int] = None,
                 model_route_id: Optional[str] = None) -> dict:
    aid = _new_id("agent")
    now = int(time.time() * 1000)
    # Clamp char_index to the 0..159 sprite-sheet range, or store NULL so the
    # frontend falls back to its name-hash default (the picker's "?" slot
    # corresponds to this NULL state). Encoding: body*20 + skin*5 + hair
    # where body ∈ [0..7], skin ∈ [0..3], hair ∈ [0..4]. Max = 7*20+3*5+4 = 159.
    ci = int(char_index) if char_index is not None else None
    if ci is not None and not (0 <= ci <= 159):
        ci = None
    with _conn() as conn:
        conn.execute(
            """INSERT INTO agents (id, name, soul_slug, model, description, creator_id,
                                   shared, toolsets, char_index, model_route_id,
                                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (aid, name, soul_slug, model or "", description or "", creator_id or None,
             1 if shared else 0, toolsets or "", ci, model_route_id,
             now, now),
        )
    return get_agent(aid)


def get_agent(agent_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return dict(row) if row else None


def get_agent_by_name(name: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return dict(row) if row else None


def list_agents(user_id: str = "") -> list[dict]:
    """List agents visible to a user (shared + own). If no user_id, list all."""
    with _conn() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM agents WHERE shared = 1 OR creator_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def update_agent(agent_id: str, **fields) -> Optional[dict]:
    allowed = {"name", "soul_slug", "model", "description", "shared", "toolsets",
               "char_index", "model_route_id", "applied_presets", "website_blocklist",
               "daily_budget_usd", "fallback_route_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    # Normalize daily_budget_usd: accept 0/empty as "clear the cap"
    if "daily_budget_usd" in updates:
        v = updates["daily_budget_usd"]
        if v in (None, "", 0, 0.0):
            updates["daily_budget_usd"] = None
        else:
            try:
                updates["daily_budget_usd"] = max(0.0, float(v))
            except (TypeError, ValueError):
                updates["daily_budget_usd"] = None
    if "char_index" in updates:
        ci = updates["char_index"]
        try:
            ci = int(ci) if ci is not None else None
        except (TypeError, ValueError):
            ci = None
        if ci is not None and not (0 <= ci <= 159):
            ci = None
        updates["char_index"] = ci
    if not updates:
        return get_agent(agent_id)
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id = ?",
            (*updates.values(), agent_id),
        )
    return get_agent(agent_id)


def delete_agent(agent_id: str) -> None:
    # Hard delete: the three orphan tables (dispatches, agent_runs,
    # evolution_proposals) have no FK cascade because they're not owned
    # by the agent lifecycle, but leaving rows behind produces dangling
    # agent_id values in admin dashboards. Purge them here so "delete"
    # means delete. platform_routing cascades via FK (see schema).
    #
    # Also nuke the agent's on-disk session/memory/log directory at
    # ~/.logos/agents/<name>/. That dir survives across deployments
    # (lives in $HOME, not in any deployment-scoped path), and the
    # session_search tool reads from it — so without this cleanup, a
    # freshly-recreated agent with the same name would surface the old
    # transcripts as "memory" and confuse users into thinking memory
    # bled across deployments. Best-effort: log and continue if the
    # filesystem op fails (DB delete already succeeded).
    name = None
    with _conn() as conn:
        row = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row:
            name = row["name"]
        conn.execute("DELETE FROM dispatches WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM agent_runs WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM evolution_proposals WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    if name:
        try:
            import shutil
            from pathlib import Path
            agent_dir = Path.home() / ".logos" / "agents" / name
            if agent_dir.is_dir():
                shutil.rmtree(agent_dir)
                logger.info("delete_agent: wiped on-disk dir %s", agent_dir)
        except Exception as exc:
            logger.warning(
                "delete_agent(%s): failed to remove on-disk dir for %r: %s",
                agent_id, name, exc,
            )


def get_agent_applied_presets(agent_id: str) -> list[str]:
    """Return the list of network policy preset names applied to an agent.

    Returns an empty list for agents with no presets (either NULL in the
    DB or an explicit empty JSON array). Invalid JSON in the column is
    logged and treated as empty — callers should not crash on malformed
    state. See gateway/policies.py for the merge/apply/remove logic that
    consumes this.
    """
    agent = get_agent(agent_id)
    if not agent:
        return []
    raw = agent.get("applied_presets")
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "get_agent_applied_presets(%s): applied_presets is not valid JSON: %r",
            agent_id, raw,
        )
        return []
    if not isinstance(loaded, list):
        return []
    return [str(p) for p in loaded if isinstance(p, str)]


def set_agent_applied_presets(agent_id: str, presets: list[str]) -> None:
    """Replace the applied preset list for an agent with the given list.

    Deduplicates (preserving order) and coerces each entry to str.
    Callers are responsible for validating that each preset name
    matches a real file in gateway/policies/presets/ — this function
    stores whatever it's given. gateway.policies.set_applied_presets
    is the validating wrapper.
    """
    seen: set[str] = set()
    clean: list[str] = []
    for name in presets or []:
        s = str(name)
        if s in seen:
            continue
        seen.add(s)
        clean.append(s)
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET applied_presets = ?, updated_at = ? WHERE id = ?",
            (json.dumps(clean), int(time.time() * 1000), agent_id),
        )


# ── Model routes ────────────────────────────────────────────────────────────
# Each row corresponds to one OpenShell gateway pinned to a single
# (provider, model) combination. See gateway/openshell_routes.py for the
# subprocess wrappers that actually provision / restart / destroy the
# underlying gateways. The CRUD functions here only manage DB state — they
# never call the openshell CLI themselves.

def create_model_route(
    provider: str,
    model: str,
    openshell_name: str,
    openshell_port: int,
    status: str = "provisioning",
    status_detail: Optional[str] = None,
    is_default: bool = False,
    is_primordial: bool = False,
) -> dict:
    """Insert a new model_routes row. The (provider, model) UNIQUE constraint
    means re-provisioning the same model raises sqlite3.IntegrityError — the
    caller should handle that by looking up the existing row first."""
    rid = _new_id("mr")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO model_routes
               (id, provider, model, openshell_name, openshell_port,
                status, status_detail, is_default, is_primordial,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rid, provider, model, openshell_name, int(openshell_port),
             status, status_detail,
             1 if is_default else 0, 1 if is_primordial else 0,
             now, now),
        )
    return get_model_route(rid)


def get_model_route(route_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM model_routes WHERE id = ?", (route_id,)).fetchone()
        return dict(row) if row else None


def get_model_route_by_name(openshell_name: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM model_routes WHERE openshell_name = ?",
            (openshell_name,),
        ).fetchone()
        return dict(row) if row else None


def get_model_route_by_provider_model(provider: str, model: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM model_routes WHERE provider = ? AND model = ?",
            (provider, model),
        ).fetchone()
        return dict(row) if row else None


def get_default_model_route() -> Optional[dict]:
    """Return the row with is_default=1, or None if no default is set."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM model_routes WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_model_routes() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM model_routes ORDER BY is_default DESC, created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def update_model_route(route_id: str, **fields) -> Optional[dict]:
    """Partial update. Only the listed fields are mutable; created_at,
    openshell_name, openshell_port, provider, model are immutable after
    creation. Use set_default_model_route() to change is_default since
    it has cross-row semantics."""
    allowed = {"status", "status_detail"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_model_route(route_id)
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE model_routes SET {set_clause} WHERE id = ?",
            (*updates.values(), route_id),
        )
    return get_model_route(route_id)


def rename_model_route_openshell_name(
    route_id: str, new_openshell_name: str,
) -> Optional[dict]:
    """Rename the openshell_name on an existing route row.

    Kept for future use by a proper "destroy container + re-provision
    under new name" admin flow. The previous client-side-alias migration
    that used this helper was structurally broken (see the module
    docstring on gateway/openshell_routes.py) and has been removed.
    """
    now = int(time.time() * 1000)
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE model_routes SET openshell_name = ?, updated_at = ? WHERE id = ?",
            (new_openshell_name, now, route_id),
        )
        if cur.rowcount == 0:
            return None
    return get_model_route(route_id)


def set_default_model_route(route_id: str) -> Optional[dict]:
    """Mark exactly one route as default. Clears all other is_default flags
    in the same transaction."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM model_routes WHERE id = ?", (route_id,)
        ).fetchone()
        if not row:
            return None
        now = int(time.time() * 1000)
        conn.execute("UPDATE model_routes SET is_default = 0, updated_at = ?", (now,))
        conn.execute(
            "UPDATE model_routes SET is_default = 1, updated_at = ? WHERE id = ?",
            (now, route_id),
        )
    return get_model_route(route_id)


def delete_model_route(route_id: str) -> bool:
    """Delete a route. Caller must verify no agents are bound
    (count_agents_using_route(route_id) == 0) and that this isn't the
    last remaining route (see gateway.openshell_routes.destroy_route
    for the full guards)."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM model_routes WHERE id = ?", (route_id,))
        return cur.rowcount > 0


def count_agents_using_route(route_id: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE model_route_id = ?",
            (route_id,),
        ).fetchone()
        return int(row["n"] if row else 0)


def list_agents_using_route(route_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agents WHERE model_route_id = ? ORDER BY created_at",
            (route_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Platform routing ────────────────────────────────────────────────────────
# Picks the agent that should handle inbound messages for a given
# platform/scope. Resolution is most-specific-first: chat → user → global.

def upsert_platform_routing(
    platform: str,
    scope: str,
    scope_id: str,
    agent_id: str,
) -> dict:
    """Insert or update a routing rule. Returns the row."""
    if scope not in ("global", "chat", "user"):
        raise ValueError(f"invalid scope: {scope!r}")
    rid = _new_id("pr")
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO platform_routing (id, platform, scope, scope_id, agent_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, scope, scope_id) DO UPDATE SET
                   agent_id = excluded.agent_id,
                   created_at = excluded.created_at""",
            (rid, platform, scope, scope_id or "", agent_id, now),
        )
        row = conn.execute(
            "SELECT * FROM platform_routing WHERE platform = ? AND scope = ? AND scope_id = ?",
            (platform, scope, scope_id or ""),
        ).fetchone()
        return dict(row) if row else {}


def delete_platform_routing(routing_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM platform_routing WHERE id = ?", (routing_id,))


def list_platform_routing(platform: Optional[str] = None) -> list[dict]:
    """All routing rules, optionally filtered by platform."""
    with _conn() as conn:
        if platform:
            rows = conn.execute(
                "SELECT * FROM platform_routing WHERE platform = ? ORDER BY scope, scope_id",
                (platform,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM platform_routing ORDER BY platform, scope, scope_id"
            ).fetchall()
        return [dict(r) for r in rows]


def resolve_platform_routing(
    platform: str,
    chat_id: str = "",
    user_id: str = "",
) -> Optional[dict]:
    """Pick the agent for an inbound message. Most-specific match wins:
    chat > user > global. Returns the routing row (with agent_id) or None.
    """
    with _conn() as conn:
        # 1. exact chat match
        if chat_id:
            row = conn.execute(
                "SELECT * FROM platform_routing WHERE platform = ? AND scope = 'chat' AND scope_id = ?",
                (platform, chat_id),
            ).fetchone()
            if row:
                return dict(row)
        # 2. exact user match
        if user_id:
            row = conn.execute(
                "SELECT * FROM platform_routing WHERE platform = ? AND scope = 'user' AND scope_id = ?",
                (platform, user_id),
            ).fetchone()
            if row:
                return dict(row)
        # 3. global fallback
        row = conn.execute(
            "SELECT * FROM platform_routing WHERE platform = ? AND scope = 'global'",
            (platform,),
        ).fetchone()
        return dict(row) if row else None


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


# ── MCP managed servers ─────────────────────────────────────────────────────


def list_mcp_servers() -> list[dict]:
    """Return all managed MCP servers ordered by name."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_servers ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_mcp_server(server_id: str) -> dict | None:
    """Return a single MCP server by ID, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_servers WHERE id=?", (server_id,)
        ).fetchone()
    return dict(row) if row else None


def get_mcp_server_by_name(name: str) -> dict | None:
    """Return a single MCP server by name, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_servers WHERE name=?", (name,)
        ).fetchone()
    return dict(row) if row else None


def create_mcp_server(
    *,
    name: str,
    catalogue_id: str | None = None,
    source: str = "ui",
    deploy_mode: str = "external",
    url: str | None = None,
    token: str | None = None,
    k8s_namespace: str | None = None,
    k8s_image: str | None = None,
    config_json: str = "{}",
    tools_filter: str = "{}",
    category: str = "general",
    description: str | None = None,
    auto_wire: bool = True,
) -> dict:
    """Create a new managed MCP server record."""
    server_id = f"mcp_{uuid.uuid4().hex[:20]}"
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO mcp_servers
               (id, name, catalogue_id, source, status, deploy_mode,
                url, token, k8s_namespace, k8s_image,
                config_json, tools_filter, category, description,
                auto_wire, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (server_id, name, catalogue_id, source,
             "external" if deploy_mode == "external" else "pending",
             deploy_mode, url, token, k8s_namespace, k8s_image,
             config_json, tools_filter, category, description,
             1 if auto_wire else 0, now, now),
        )
    return get_mcp_server(server_id)


def update_mcp_server(server_id: str, **fields) -> dict | None:
    """Update fields on an MCP server record."""
    allowed = {
        "name", "status", "url", "token", "k8s_namespace", "k8s_image",
        "config_json", "tools_filter", "category", "description",
        "auto_wire", "enabled", "last_error", "deploy_mode",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_mcp_server(server_id)
    updates["updated_at"] = int(time.time() * 1000)
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE mcp_servers SET {set_clause} WHERE id=?",
            [*updates.values(), server_id],
        )
    return get_mcp_server(server_id)


def delete_mcp_server(server_id: str) -> bool:
    """Delete an MCP server record. Returns True if a row was deleted."""
    with _conn() as conn:
        cursor = conn.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
    return cursor.rowcount > 0


# ── Dispatch ledger (M8 Phase B) ───────────────────────────────────────────


def create_dispatch(
    task_id: str,
    agent_id: str = "",
    sandbox_name: str = "",
    model: str = "",
    origin: str = "user_chat",
    origin_detail: str = "",
    session_id: str = "",
    user_id: str = "",
) -> str:
    """Insert a new dispatch record at the start of a task. Returns the id."""
    import uuid
    dispatch_id = f"dsp_{uuid.uuid4().hex[:12]}"
    now_ms = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO dispatches
               (id, task_id, agent_id, sandbox_name, model, origin,
                origin_detail, session_id, user_id, status, started_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (dispatch_id, task_id, agent_id, sandbox_name, model, origin,
             origin_detail, session_id, user_id, "running", now_ms),
        )
    return dispatch_id


def complete_dispatch(
    dispatch_id: str,
    status: str = "ok",
    elapsed_s: float = 0.0,
    prompt_tokens: int = None,
    completion_tokens: int = None,
    error: str = None,
) -> None:
    """Update a dispatch record when the task finishes."""
    now_ms = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute(
            """UPDATE dispatches
               SET status=?, elapsed_s=?, prompt_tokens=?,
                   completion_tokens=?, error=?, ended_at=?
               WHERE id=?""",
            (status, elapsed_s, prompt_tokens, completion_tokens, error,
             now_ms, dispatch_id),
        )


def sweep_orphaned_dispatches() -> int:
    """Mark any ``status='running'`` dispatches as ``status='interrupted'``.

    Called exactly once on gateway startup. The dispatch lifecycle is:
      1. ``create_dispatch()`` inserts with status='running'
      2. Worker runs the task
      3. ``complete_dispatch()`` updates to 'ok' / 'error'

    If the gateway process dies between (1) and (3) — SIGKILL, restart,
    crash — the row is stuck at 'running' forever. Events → Activity
    shows them as active tasks even though nothing is executing.

    On startup, by definition no task we previously dispatched is still
    running in memory (the worker registry is built fresh and in-flight
    tasks don't survive a process restart). So any pre-existing
    'running' row is an orphan. Update it to 'interrupted' with
    ``error='gateway restarted before task completed'`` so it stops
    contaminating the "currently active" view.

    Returns the number of rows updated.
    """
    now_ms = int(time.time() * 1000)
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE dispatches
               SET status='interrupted',
                   error=COALESCE(error, 'gateway restarted before task completed'),
                   ended_at=?
               WHERE status='running'""",
            (now_ms,),
        )
        return cur.rowcount


def list_dispatches(
    agent_id: str = None,
    origin: str = None,
    status: str = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple:
    """Query the dispatch ledger. Returns (rows, total_count)."""
    where_parts = []
    params = []
    if agent_id:
        where_parts.append("agent_id = ?")
        params.append(agent_id)
    if origin:
        where_parts.append("origin = ?")
        params.append(origin)
    if status:
        where_parts.append("status = ?")
        params.append(status)
    where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM dispatches{where_clause}", params,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM dispatches{where_clause} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


# ── Cost log ─────────────────────────────────────────────────────────────
# Insert + rollup queries feeding the Costs dashboard. The gateway
# writes one row per dispatch; the admin endpoint reads aggregates.

def insert_cost_entry(
    agent_id: str = None,
    agent_name: str = None,
    session_id: str = None,
    task_id: str = None,
    provider: str = None,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.0,
    pricing_known: bool = False,
    ts: int = None,
) -> str:
    """Record one request's token usage + cost.

    Returns the inserted row id. Safe to call even when pricing is
    unknown — the row goes in with cost_usd=0 and pricing_known=0 so
    the dashboard can surface "N requests with unknown pricing" without
    silently dropping data.
    """
    import uuid as _uuid
    if ts is None:
        ts = int(time.time() * 1000)
    row_id = "cost_" + _uuid.uuid4().hex[:18]
    with _conn() as conn:
        conn.execute(
            "INSERT INTO cost_log ("
            "id, ts, agent_id, agent_name, session_id, task_id, provider, model, "
            "input_tokens, output_tokens, cache_read_tok, cache_write_tok, "
            "cost_usd, pricing_known) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, ts, agent_id, agent_name, session_id, task_id, provider, model,
             int(input_tokens or 0), int(output_tokens or 0),
             int(cache_read_tokens or 0), int(cache_write_tokens or 0),
             float(cost_usd or 0), 1 if pricing_known else 0),
        )
    return row_id


def cost_rollup(
    agent_id: str = None,
    since_ts: int = None,
    until_ts: int = None,
) -> dict:
    """Return summary stats for the Costs dashboard card.

    Shape:
      {
        "count": int,                  # total requests in window
        "total_usd": float,
        "avg_per_minute_usd": float,   # total / minutes covered
        "known_price_count": int,
        "unknown_price_count": int,
        "by_model": [{"model","count","total_usd"}],
        "last_request": {...} | None,
        "largest_request": {...} | None,
        "window": {"since": ..., "until": ...},
      }
    """
    where = []
    params: list = []
    if agent_id:
        where.append("agent_id = ?"); params.append(agent_id)
    if since_ts is not None:
        where.append("ts >= ?"); params.append(since_ts)
    if until_ts is not None:
        where.append("ts <= ?"); params.append(until_ts)
    wc = (" WHERE " + " AND ".join(where)) if where else ""

    with _conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) as c, COALESCE(SUM(cost_usd),0) as s, "
            f"MIN(ts) as mn, MAX(ts) as mx, "
            f"SUM(CASE WHEN pricing_known=1 THEN 1 ELSE 0 END) as kc "
            f"FROM cost_log{wc}", params,
        ).fetchone()
        count = row["c"]
        total = float(row["s"])
        mn = row["mn"]; mx = row["mx"]
        known_count = row["kc"] or 0
        minutes = max((mx - mn) / 60000, 1) if (mn and mx and count > 0) else 1
        avg_per_min = total / minutes if count > 0 else 0

        by_model = conn.execute(
            f"SELECT model, COUNT(*) as count, SUM(cost_usd) as total_usd "
            f"FROM cost_log{wc} GROUP BY model ORDER BY total_usd DESC LIMIT 10",
            params,
        ).fetchall()

        last = conn.execute(
            f"SELECT * FROM cost_log{wc} ORDER BY ts DESC LIMIT 1", params,
        ).fetchone()
        largest = conn.execute(
            f"SELECT * FROM cost_log{wc} ORDER BY cost_usd DESC LIMIT 1", params,
        ).fetchone()

    return {
        "count": count,
        "total_usd": total,
        "avg_per_minute_usd": avg_per_min,
        "known_price_count": known_count,
        "unknown_price_count": count - known_count,
        "by_model": [dict(r) for r in by_model],
        "last_request": dict(last) if last else None,
        "largest_request": dict(largest) if largest else None,
        "window": {"since": since_ts, "until": until_ts},
    }
