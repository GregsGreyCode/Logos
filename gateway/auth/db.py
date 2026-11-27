"""SQLite persistence for auth: users, refresh_tokens, user_settings, audit_logs,
agents, approval_requests."""

import json
import logging
import re
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

-- ── User ↔ platform identity links ────────────────────────────────────
-- Maps a Logos user to their identity on each messaging platform
-- (Telegram user_id, Discord user_id, Slack member_id, etc.) so
-- dispatch_platform_message can resolve an inbound chat user to
-- a Logos user, and the Runs tab shows a name instead of a numeric platform ID.
--
-- Also tracks ownership attribution: agents created by a user
-- show that user as the owner for any inbound messages on the
-- platform, which drives per-user agent-edit permissions.

CREATE TABLE IF NOT EXISTS user_platform_links (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    platform_uid    TEXT NOT NULL,
    display_name    TEXT,
    created_at      INTEGER NOT NULL,
    UNIQUE(platform, platform_uid)
);
CREATE INDEX IF NOT EXISTS idx_upl_user     ON user_platform_links(user_id);
CREATE INDEX IF NOT EXISTS idx_upl_platform ON user_platform_links(platform, platform_uid);

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
                        CHECK (deploy_mode IN ('external', 'docker')),
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

-- ── Per-agent channel credentials ─────────────────────────────────────────
-- One row per (agent, platform, label) — lets a single agent own multiple
-- bot tokens on the same platform (e.g. a prod + staging Telegram bot),
-- and lets different agents own different tokens on the same platform
-- (the original motivation: "Hermes gets this Telegram bot, Henry gets
-- that one, route by token not by chat_id").
--
-- The token column is stored in cleartext, same as the legacy global
-- credentials dict in platform_settings.feature_flags. Logos is
-- single-host self-hosted; filesystem perms are the trust boundary.
-- Row-level encryption (Fernet) can layer in later without changing
-- the schema if the threat model tightens.
--
-- label: human-friendly distinguisher for the agent's channels tab
-- ("prod", "staging", "family"). Free text; UNIQUE constraint is
-- (agent_id, platform, label), so the same label is fine across agents.
--
-- metadata: JSON blob for platform-specific extras captured at validate
-- time (e.g. Telegram bot username, Discord application id, Slack team).
-- Lets the UI render "Connected as @hermesbot (Hermes)" without hitting
-- the platform API on every page load.

CREATE TABLE IF NOT EXISTS agent_channel_credentials (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT 'default',
    token       TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    metadata    TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE(agent_id, platform, label)
);
CREATE INDEX IF NOT EXISTS idx_acc_agent    ON agent_channel_credentials(agent_id);
CREATE INDEX IF NOT EXISTS idx_acc_platform ON agent_channel_credentials(platform);

-- Per-agent cloud-tool credentials — one row per (agent, env_var) pair.
-- Stored separately from agent_channel_credentials because the value
-- semantics differ: channel creds map to a platform + label so we can
-- model multiple bot tokens per platform ("work telegram", "personal
-- telegram"), whereas env creds are simple env-var → value pairs. Both
-- tables feed into the same .env injection path at spawn time via
-- ``build_channel_extra_env`` (extended 2026-04-21).
--
-- Use case: setting FIRECRAWL_API_KEY / FAL_KEY / OPENROUTER_API_KEY /
-- etc. per-agent so each agent's sandbox gets its own keys. Keys live
-- where hermes actually reads them (/tmp/hermes-srv-home/.env) rather
-- than in Logos's gateway os.environ (which never reached the
-- sandbox). Replaces the dead TOOL_INTEGRATIONS / get_tool_readiness
-- path that pretended to configure cloud tools for hermes but never
-- actually forwarded the keys.

CREATE TABLE IF NOT EXISTS agent_env_credentials (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    env_var     TEXT NOT NULL,
    value       TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    description TEXT,                   -- human hint ("Firecrawl cloud API"); nullable
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE(agent_id, env_var)
);
CREATE INDEX IF NOT EXISTS idx_aec_agent   ON agent_env_credentials(agent_id);
CREATE INDEX IF NOT EXISTS idx_aec_env_var ON agent_env_credentials(env_var);
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
            # v1 per-user routing profile. Unused since S1.6 (routing_policies
            # is gone, see drop_machines_routing_v1); the column stays so
            # existing rows keep lining up. No FK: its old target table no
            # longer exists.
            "ALTER TABLE users ADD COLUMN policy_id TEXT",
            # v7: per-agent toolset config (JSON array of enabled toolset names)
            "ALTER TABLE agents ADD COLUMN toolsets TEXT",
            # v8: per-agent sprite selection (0..7). NULL falls back to name-hash
            # in AgentSprite.js so pre-v8 agents still render.
            "ALTER TABLE agents ADD COLUMN char_index INTEGER",
            # v9: per-agent OpenShell 0.0.x route binding. Unused since S1.5
            # (the route table is gone, see drop_model_routes_v1); the
            # column stays so existing rows/ALTERs keep lining up. No FK:
            # its old target table no longer exists.
            "ALTER TABLE agents ADD COLUMN model_route_id TEXT",
            # v10: per-agent OpenShell network policy presets (JSON array).
            # Unused since S1.6 removed the v1 preset machinery; kept so
            # existing rows keep lining up.
            "ALTER TABLE agents ADD COLUMN applied_presets TEXT",
            # v17: per-agent website blocklist. Unused since S1.6; kept
            # for the same reason as applied_presets.
            "ALTER TABLE agents ADD COLUMN website_blocklist TEXT",
            # v21: per-MCP-server auto_grant toggle. When True, the
            # server's tools are injected into every agent's effective
            # toolset automatically at spawn/refresh time. When False,
            # the agent has to go through the request_mcp_access flow
            # for a per-session grant. Column default is NULL so the
            # create path can distinguish "never set" (apply deploy-
            # mode default) from an explicit user choice.
            "ALTER TABLE mcp_servers ADD COLUMN auto_grant INTEGER",
            # v25: per-user agent limit (LOG-25.5). NULL = unlimited;
            # integer N caps how many agents a non-admin user can create
            # via handle_agents_post. Admins always bypass the check.
            "ALTER TABLE users ADD COLUMN max_agents INTEGER",
            # v26: per-user daily budget cap (LOG-25.6). NULL = no cap.
            # Mirrors agents.daily_budget_usd but applies at the user
            # level, summed across every agent the user has dispatched.
            "ALTER TABLE users ADD COLUMN daily_budget_usd REAL",
            # v26b: add user_id to cost_log so per-user rollups don't
            # need a JOIN to dispatches/agents. NULL on historical rows
            # (pre-migration) — they just don't contribute to new caps.
            "ALTER TABLE cost_log ADD COLUMN user_id TEXT",
            # v27 (Logos v2 ST1): the dashboard's per-person options, as JSON
            # (chat panel behaviour, Show thinking, Show scheduled jobs).
            "ALTER TABLE user_settings ADD COLUMN ui_prefs TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists

        # v21 backfill: pre-migration rows have auto_grant=NULL. Give
        # docker-deployed servers the opt-in default (user clicked
        # Deploy — that's consent). External-URL servers stay off
        # until the user explicitly toggles auto-grant in the UI.
        try:
            conn.execute(
                "UPDATE mcp_servers SET auto_grant=1 "
                "WHERE auto_grant IS NULL AND deploy_mode='docker'"
            )
            conn.execute(
                "UPDATE mcp_servers SET auto_grant=0 "
                "WHERE auto_grant IS NULL"
            )
        except sqlite3.OperationalError:
            pass

        # v11: dispatch activity ledger (M8 Phase B). Durable record of
        # every task dispatch — who, what agent, which model, origin,
        # timing, token counts, and outcome. Feeds the Admin Activity tab.
        # STAMP columns (soul, toolsets_snapshot, user_message,
        # tool_sequence) turn each row into a full run record so the
        # Runs tab can answer "what did this agent actually do" without
        # a second lookup.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id                 TEXT PRIMARY KEY,
                task_id            TEXT NOT NULL,
                agent_id           TEXT,
                sandbox_name       TEXT,
                model              TEXT,
                origin             TEXT NOT NULL DEFAULT 'user_chat',
                origin_detail      TEXT,
                session_id         TEXT,
                user_id            TEXT,
                prompt_tokens      INTEGER,
                completion_tokens  INTEGER,
                elapsed_s          REAL,
                status             TEXT NOT NULL DEFAULT 'running',
                error              TEXT,
                started_at         INTEGER NOT NULL,
                ended_at           INTEGER,
                soul               TEXT,
                toolsets_snapshot  TEXT,
                user_message       TEXT,
                tool_sequence      TEXT,
                policy_snapshot    TEXT
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

            -- LOG-60.1: per-event agent trace. One row per meaningful
            -- stream frame emitted during a dispatch (tool_start,
            -- tool_end, thinking, memory_write, task_result, plus
            -- synthetic dispatch_start/dispatch_end bookends). The
            -- dispatches table captures the coarse per-run summary;
            -- this table captures the fine-grained timeline within
            -- each run so the UI can render a step-by-step trace
            -- without shelling into the sandbox to read session JSON.
            -- run_id matches dispatches.id (the ``dsp_*`` prefix).
            -- parent_run_id is reserved for LOG-60.5 delegation
            -- traces; NULL for top-level runs. payload is JSON-
            -- encoded event metadata (tool args, tool results,
            -- thinking text, error messages) after secret redaction,
            -- capped at 16 KB — oversized content is truncated with
            -- a "...[truncated]" marker rather than stashed in a
            -- sidecar file (the sidecar path is reserved in the
            -- schema but unused until 57.4+ when we know we need it).
            CREATE TABLE IF NOT EXISTS agent_events (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                INTEGER NOT NULL,
                agent_id          TEXT,
                run_id            TEXT NOT NULL,
                parent_run_id     TEXT,
                event_type        TEXT NOT NULL,
                tool_name         TEXT,
                status            TEXT,
                duration_ms       INTEGER,
                payload           TEXT,
                payload_blob_path TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_aevent_run    ON agent_events(run_id, ts);
            CREATE INDEX IF NOT EXISTS idx_aevent_agent  ON agent_events(agent_id, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_aevent_type   ON agent_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_aevent_tool   ON agent_events(tool_name);
        """)

        # v12: migrate per-agent char_index from the legacy 0..23 encoding
        # (slot index into a 24-variant sheet) to the new 0..95 encoding
        # (body*12 + skin*4 + hair for an 8×3×4 variant matrix).
        # Legacy mapping:
        #   old 0..7   → body N, skin 0 (light), hair 0 (original)   = N * 12
        #   old 8..15  → body N, skin 1 (medium), hair 0             = N * 12 + 4
        #   old 16..23 → body N, skin 2 (dark),   hair 2 (180°)      = N * 12 + 10
        # Guarded by a schema_flags row so it only runs once.
        # Logos v2 ST2: each agent's colour on the dashboard (its dot and, later, its floating mark),
        # the same for everyone; set by the people who may use that agent.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_colours (
                name       TEXT PRIMARY KEY,
                colour     TEXT NOT NULL,
                set_by     TEXT,
                updated_at INTEGER
            )
        """)
        # Voice V1.2: each agent's voice on the house speaker, chosen in Settings by the people who
        # may use that agent (a Piper voice id). Without a row, the definition's speaker_voice.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_voices (
                name       TEXT PRIMARY KEY,
                voice      TEXT NOT NULL,
                set_by     TEXT,
                updated_at INTEGER
            )
        """)
        # V3.4: its French voice, for answers in French ("" in voice = the definition's English one).
        try:
            conn.execute("ALTER TABLE agent_voices ADD COLUMN voice_fr TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Voice VO.2: each person's voice print (speaker embeddings and their strictness, never audio),
        # made in Settings › Your voice and managed only by that person (gateway/voice_id.py).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_prints (
                username   TEXT PRIMARY KEY,
                print      TEXT NOT NULL,
                updated_at INTEGER
            )
        """)
        # F38: invitations (People › Add a person). Only a hash of each link's secret is kept; a link
        # works once, until it expires or is cancelled. ``options`` says what to set up for the person.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash  TEXT UNIQUE NOT NULL,
                created_by  TEXT,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL,
                used_at     INTEGER,
                revoked_at  INTEGER,
                options     TEXT
            )
        """)
        # F38: what was set up for each person added from the dashboard, step by step (shown on their page).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS people_setup (
                username    TEXT PRIMARY KEY,
                steps       TEXT NOT NULL,
                updated_at  INTEGER
            )
        """)
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

        # v18 (agent_b_defaults_v1) applied v1 capability defaults to
        # empty-toolset agents. The capability catalogue was removed in
        # S1.6, so the backfill is gone; nothing reads the flag any more.

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

        # v16 (browserless_preset_default_v1) stamped a v1 network preset
        # onto agents.applied_presets. Presets were removed in S1.6.

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

        # v20: migrate mcp_servers.deploy_mode CHECK constraint from
        # ('k8s', 'external') to ('external', 'docker'). The k8s deploy path
        # has been removed along with the rest of the legacy sandbox
        # runtimes; existing 'k8s' rows are downgraded to 'external' and
        # their k8s_namespace/k8s_image cleared so the catalogue survives
        # but the user knows the deploy is broken until they re-wire it.
        # SQLite can't alter a CHECK constraint in-place so we table-swap.
        _migrate_mcp_deploy_mode_v1(conn)

        # v21: add STAMP columns to dispatches so each row is a full
        # run record (soul, toolsets_snapshot, user_message,
        # tool_sequence). Additive ALTERs, idempotent-by-checking.
        _migrate_dispatches_stamp_v1(conn)

        # v22: drop the dead workflow + action_policy tables now that the
        # UI and Python API have been removed. Idempotent via schema_flags.
        # Other column residues (agents.action_policy_id,
        # agent_runs.action_policy_id/snapshot + workflow_run_id) stay —
        # they have no FK constraint pointing at a dropped table, so
        # rebuilding those tables buys us nothing.
        already_v22 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'drop_workflow_action_policy_tables_v1'"
        ).fetchone()
        if not already_v22:
            for table in (
                "workflow_step_runs",
                "workflow_runs",
                "workflow_definitions",
                "action_policies",
            ):
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                except sqlite3.OperationalError as exc:
                    logger.warning("drop_workflow_action_policy_tables_v1: DROP %s: %s", table, exc)
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("drop_workflow_action_policy_tables_v1", "1", int(time.time() * 1000)),
            )
            logger.info("drop_workflow_action_policy_tables_v1: removed workflow_* + action_policies tables")

        # v23: drop STAMP run records (S1.4). agent_runs never received a
        # row in production: nothing called start_run/finish_run. The
        # dispatches ledger already carries soul, model, toolsets and the
        # tool sequence for every chat turn. Idempotent via schema_flags.
        already_v23 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'drop_agent_runs_v1'"
        ).fetchone()
        if not already_v23:
            conn.execute("DROP TABLE IF EXISTS agent_runs")
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("drop_agent_runs_v1", "1", int(time.time() * 1000)),
            )
            logger.info("drop_agent_runs_v1: removed agent_runs table")

        # v24: drop the OpenShell 0.0.x model_routes table (S1.5). The
        # agents.model_route_id / fallback_route_id columns are left in
        # place (unused) rather than rebuilding agents. But on DBs where
        # model_route_id was added with ``REFERENCES model_routes(id)``,
        # SQLite (foreign_keys=ON) refuses every INSERT/DELETE on agents
        # once the parent table is gone ("no such table: main.model_routes").
        # So first strip that FK clause from the stored CREATE TABLE text via
        # writable_schema, the procedure SQLite documents for removing
        # FOREIGN KEY constraints without touching on-disk data
        # (https://sqlite.org/lang_altertable.html#otheralter), then drop.
        already_v24 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'drop_model_routes_v1'"
        ).fetchone()
        if not already_v24:
            _strip_agents_model_route_fk(conn)
            conn.execute("DROP TABLE IF EXISTS model_routes")
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("drop_model_routes_v1", "1", int(time.time() * 1000)),
            )
            logger.info("drop_model_routes_v1: removed model_routes table")

        # v22b: rebuild `users` to drop the `action_policy_id` column, whose
        # FK pointed at the `action_policies` table just dropped above.
        # Why: with PRAGMA foreign_keys=ON, any INSERT into users fails with
        # "no such table: main.action_policies" on a fresh install because
        # the column's FK target is gone. The old migration note claimed
        # "leaving [column residues] NULL-only is harmless" — it isn't for
        # columns whose FK target was dropped. Separate schema_flag so this
        # runs even on DBs that already had v22 applied.
        already_v22b = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'drop_users_action_policy_id_column_v1'"
        ).fetchone()
        if not already_v22b:
            has_col = any(
                row[1] == "action_policy_id"
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            )
            if has_col:
                conn.execute("PRAGMA foreign_keys=OFF")
                try:
                    conn.executescript(
                        """
                        CREATE TABLE users__new (
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
                            locked_until       INTEGER,
                            policy_id          TEXT
                        );
                        INSERT INTO users__new
                            (id, email, username, password_hash, role, status,
                             display_name, created_at, last_login,
                             failed_login_count, locked_until, policy_id)
                        SELECT id, email, username, password_hash, role, status,
                               display_name, created_at, last_login,
                               failed_login_count, locked_until, policy_id
                          FROM users;
                        DROP TABLE users;
                        ALTER TABLE users__new RENAME TO users;
                        """
                    )
                finally:
                    conn.execute("PRAGMA foreign_keys=ON")
                logger.info("drop_users_action_policy_id_column_v1: rebuilt users table")
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("drop_users_action_policy_id_column_v1", "1", int(time.time() * 1000)),
            )

        # v27: drop v1 multi-machine routing (S1.6): machines, their
        # capabilities/claims, routing profiles + rules, the routing log
        # and cloud_providers. users.policy_id is left in place (unused)
        # rather than rebuilding users, but its ``REFERENCES
        # routing_policies(id)`` clause has to go first: with
        # foreign_keys=ON SQLite refuses writes to users once the parent
        # table is missing. Same writable_schema procedure as
        # drop_model_routes_v1. Children are dropped before parents.
        # Runs after v22b, whose rebuild used to re-add that FK.
        already_v27 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'drop_machines_routing_v1'"
        ).fetchone()
        if not already_v27:
            _strip_fk_clause(conn, "users", "routing_policies",
                             "drop_machines_routing_v1")
            for table in _DROPPED_ROUTING_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("drop_machines_routing_v1", "1", int(time.time() * 1000)),
            )
            logger.info("drop_machines_routing_v1: removed machines/routing/cloud_providers tables")

        # v28: collapse the four v1 roles into two (S1.6). operator and
        # viewer both become plain users; admin stays admin. The CHECK on
        # users.role still lists the old names (changing it would need a
        # table rebuild) but nothing writes them any more, and
        # rbac.normalize_role() applies the same mapping to any stale
        # role claim in an already-issued token.
        already_v28 = conn.execute(
            "SELECT value FROM schema_flags WHERE key = 'collapse_roles_v1'"
        ).fetchone()
        if not already_v28:
            cur = conn.execute(
                "UPDATE users SET role = 'user' WHERE role NOT IN ('admin', 'user')"
            )
            conn.execute(
                "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
                ("collapse_roles_v1", "1", int(time.time() * 1000)),
            )
            if cur.rowcount:
                logger.info("collapse_roles_v1: mapped %d operator/viewer user(s) to 'user'",
                            cur.rowcount)

        # v23: collapse dead per-channel `hermes-<platform>` toolset aliases
        # on existing agents.toolsets JSON columns into `hermes-cli`. The
        # aliases were identical clones with no differentiated behavior;
        # the catalogue now only exposes `hermes-cli` + `hermes-acp`, so
        # stored agent configs referencing the old names would resolve to
        # empty tool lists at runtime. Idempotent via schema_flags.
        _migrate_collapse_hermes_channel_toolsets_v1(conn)

        # v24 (prune_orphan_agent_toolsets_v1) pruned agents.toolsets
        # against the v1 static toolset catalogue. That catalogue went
        # with tools/ and core/ in S1.7 — v2 agents get their tools from
        # upstream hermes inside the sandbox — so the migration is gone.


def _migrate_collapse_hermes_channel_toolsets_v1(conn) -> None:
    """Rewrite agents.toolsets to drop dead hermes-<channel> aliases.

    The `hermes-telegram` / `-discord` / `-slack` / `-whatsapp` / `-signal`
    / `-homeassistant` / `-email` / `-gateway` entries used to live in
    core.toolsets.TOOLSETS as clones of hermes-cli. They're gone now;
    any agent whose toolsets JSON still references one would get an
    empty tool list when resolved. Replace them in place with
    hermes-cli and dedupe.
    """
    already = conn.execute(
        "SELECT value FROM schema_flags WHERE key = 'collapse_hermes_channel_toolsets_v1'"
    ).fetchone()
    if already:
        return

    import json as _json
    DEAD_ALIASES = {
        "hermes-telegram", "hermes-discord", "hermes-slack",
        "hermes-whatsapp", "hermes-signal", "hermes-homeassistant",
        "hermes-email", "hermes-gateway",
    }
    rewritten = 0
    for row in conn.execute("SELECT id, toolsets FROM agents").fetchall():
        raw = row["toolsets"]
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        new_list: list[str] = []
        seen: set[str] = set()
        changed = False
        for name in parsed:
            if not isinstance(name, str):
                new_list.append(name)
                continue
            repl = "hermes-cli" if name in DEAD_ALIASES else name
            if repl != name:
                changed = True
            if repl not in seen:
                seen.add(repl)
                new_list.append(repl)
            else:
                changed = True  # dedup also counts as a change worth writing
        if changed:
            conn.execute(
                "UPDATE agents SET toolsets=? WHERE id=?",
                (_json.dumps(new_list), row["id"]),
            )
            rewritten += 1

    conn.execute(
        "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
        ("collapse_hermes_channel_toolsets_v1", "1", int(time.time() * 1000)),
    )
    if rewritten:
        logger.info(
            "collapse_hermes_channel_toolsets_v1: rewrote %d agent row(s)", rewritten,
        )


def _migrate_dispatches_stamp_v1(conn) -> None:
    """Add STAMP columns to the dispatches table if missing.

    Each is a single ALTER TABLE ADD COLUMN — reversible by dropping
    the columns if we ever roll back. Safe to run repeatedly: we probe
    the current column set first and only ALTER the missing ones.
    """
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(dispatches)").fetchall()
    }
    adds = [
        ("soul", "TEXT"),
        ("toolsets_snapshot", "TEXT"),
        ("user_message", "TEXT"),
        ("tool_sequence", "TEXT"),
        ("policy_snapshot", "TEXT"),
    ]
    for name, sqltype in adds:
        if name in cols:
            continue
        conn.execute(f"ALTER TABLE dispatches ADD COLUMN {name} {sqltype}")


# Dropped by drop_machines_routing_v1, children before parents.
_DROPPED_ROUTING_TABLES = (
    "machine_capabilities",
    "machine_users",
    "policy_rules",
    "routing_log",
    "routing_policies",
    "machines",
    "cloud_providers",
)


def _strip_fk_clause(conn: sqlite3.Connection, table: str, parent: str,
                     migration: str) -> None:
    """Remove ``REFERENCES <parent>(id)`` from ``table``'s stored schema.

    Rewrites only the schema text (column data is untouched), bumps
    schema_version so open connections reload, and verifies with
    ``PRAGMA integrity_check``. No-op when the clause is absent.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if not row or not row[0]:
        return
    old_sql = row[0]
    new_sql = re.sub(
        r"\s+REFERENCES\s+[\"'`\[]?%s[\"'`\]]?\s*\(\s*id\s*\)" % re.escape(parent),
        "", old_sql, flags=re.IGNORECASE)
    if new_sql == old_sql:
        return
    conn.commit()
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema=ON")
    try:
        conn.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (new_sql, table),
        )
        conn.execute(f"PRAGMA schema_version={int(version) + 1}")
        conn.commit()
    finally:
        conn.execute("PRAGMA writable_schema=OFF")
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        logger.warning("%s: integrity_check after FK strip: %s",
                       migration, result[0] if result else None)


def _strip_agents_model_route_fk(conn: sqlite3.Connection) -> None:
    """Remove ``REFERENCES model_routes(id)`` from the agents table schema.

    Part of drop_model_routes_v1.
    """
    _strip_fk_clause(conn, "agents", "model_routes", "drop_model_routes_v1")


def _migrate_mcp_deploy_mode_v1(conn) -> None:
    """Re-create mcp_servers with the new deploy_mode CHECK constraint.

    Idempotent via schema_flags.mcp_deploy_mode_v1. Copies every existing
    row, rewriting deploy_mode='k8s' → 'external' and nulling the
    k8s_namespace/k8s_image columns on those rows (the data is dead: the
    k8s deploy path has been removed).
    """
    already = conn.execute(
        "SELECT value FROM schema_flags WHERE key = 'mcp_deploy_mode_v1'"
    ).fetchone()
    if already:
        return

    # Check whether the constraint already matches target (fresh DB from
    # new schema). If the current CHECK mentions 'docker', skip the
    # recreate and just stamp the flag.
    existing_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='mcp_servers'"
    ).fetchone()
    needs_recreate = True
    if existing_sql and existing_sql[0]:
        sql_text = existing_sql[0]
        if "'docker'" in sql_text and "'external'" in sql_text and "'k8s'" not in sql_text:
            needs_recreate = False

    if needs_recreate:
        # Rewrite k8s → external and clear the now-meaningless k8s_* columns
        conn.execute(
            "UPDATE mcp_servers SET deploy_mode='external', "
            "k8s_namespace=NULL, k8s_image=NULL "
            "WHERE deploy_mode='k8s'"
        )

        conn.executescript("""
            CREATE TABLE mcp_servers_new (
                id              TEXT PRIMARY KEY,
                name            TEXT UNIQUE NOT NULL,
                catalogue_id    TEXT,
                source          TEXT NOT NULL DEFAULT 'ui'
                                    CHECK (source IN ('ui', 'external')),
                status          TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'deploying', 'running', 'stopped', 'error', 'external')),
                deploy_mode     TEXT NOT NULL DEFAULT 'external'
                                    CHECK (deploy_mode IN ('external', 'docker')),
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
            INSERT INTO mcp_servers_new SELECT
                id, name, catalogue_id, source, status, deploy_mode,
                url, token, k8s_namespace, k8s_image, config_json,
                tools_filter, category, description, auto_wire, enabled,
                created_at, updated_at, last_error
            FROM mcp_servers;
            DROP TABLE mcp_servers;
            ALTER TABLE mcp_servers_new RENAME TO mcp_servers;
        """)

    conn.execute(
        "INSERT INTO schema_flags (key, value, applied_at) VALUES (?, ?, ?)",
        ("mcp_deploy_mode_v1", "1", int(time.time() * 1000)),
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


def _storable_role(role: str) -> str:
    """Return ``admin`` or ``user`` for a role about to be written.

    v1 role names (operator, viewer) are mapped by rbac.normalize_role;
    anything else raises ValueError rather than being stored.
    """
    from gateway.auth.rbac import normalize_role
    normalized = normalize_role(role)
    if not normalized:
        raise ValueError(f"invalid_role: {role!r}")
    return normalized


def create_user(
    email: str,
    username: str,
    password_hash: str,
    role: str = "user",
    display_name: Optional[str] = None,
    status: str = "active",
) -> dict:
    role = _storable_role(role)
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


def update_user(user_id: str, **fields) -> Optional[dict]:
    allowed = {"role", "status", "display_name", "password_hash", "email", "username"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_user_by_id(user_id)
    if "role" in updates:
        updates["role"] = _storable_role(updates["role"])
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


def list_users(
    page: int = 1,
    limit: int = 20,
    role: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
) -> tuple[list[dict], int]:
    conditions, params = [], []
    if role:
        conditions.append("role = ?")
        params.append(role)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if q:
        # Match against the three fields an admin would scan: username,
        # display_name, and email.
        like = f"%{q}%"
        conditions.append(
            "(COALESCE(username,'') LIKE ? "
            "OR COALESCE(display_name,'') LIKE ? "
            "OR COALESCE(email,'') LIKE ?)"
        )
        params.extend([like, like, like])
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
    allowed = {"default_soul", "default_model", "ui_theme", "notification_telegram", "spawn_defaults", "ui_prefs"}
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

# ── Named agents ─────────────────────────────────────────────────────────────

def create_agent(name: str, soul_slug: str = "general", model: str = "",
                 description: str = "", creator_id: str = "", shared: bool = True,
                 toolsets: str = "", char_index: Optional[int] = None) -> dict:
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
                                   shared, toolsets, char_index,
                                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (aid, name, soul_slug, model or "", description or "", creator_id or None,
             1 if shared else 0, toolsets or "", ci,
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
               "char_index",
               "daily_budget_usd"}
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
    # Hard delete: the two orphan tables (dispatches,
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


# ── Platform routing ────────────────────────────────────────────────────────
# Picks the agent that should handle inbound messages for a given
# platform/scope. Resolution is most-specific-first: chat → user → global.

# ── User ↔ platform identity links ────────────────────────────────────────
# Resolves a platform chat user_id (Telegram number, Discord snowflake, …)
# to a Logos user so dispatch rows show real names and agent-edit
# permissions are scoped correctly.


# ── Per-agent channel credentials ────────────────────────────────────────────
# One token row per (agent, platform, label). The adapter lifecycle in
# platform_manager reads these rows and spawns one adapter instance per
# enabled row; inbound messages carry the owning agent_id so no
# platform_routing lookup is needed on the receive path.

# ── Per-agent env credentials (cloud-tool API keys) ─────────────────────────
# See the agent_env_credentials table definition above for rationale.
# Read path: the v1 sandbox runtime merged rows from here with rows from
# agent_channel_credentials into the sandbox's .env (removed in S1.5; the
# E2 sandbox backend re-implements env injection).


# ── Approval requests ────────────────────────────────────────────────────────

_APPROVAL_TTL_SECONDS = 300  # default 5 min


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


# ── Evolution ──────────────────────────────────────────────────────────────────

# ── MCP managed servers ─────────────────────────────────────────────────────


# ── Dispatch ledger (M8 Phase B) ───────────────────────────────────────────


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


# ── Agent events (LOG-60.1) ─────────────────────────────────────────────

_PAYLOAD_MAX_BYTES = 16 * 1024  # cap inline payload at 16 KB
_SECRET_KEY_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential|passwd|auth[_-]?header)\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-.]{8,})",
)
_BEARER_RE = re.compile(r"(?i)bearer\s+([A-Za-z0-9+/=_\-.]{8,})")


# ── Cost log ─────────────────────────────────────────────────────────────
# Insert + rollup queries feeding the Costs dashboard. The gateway
# writes one row per dispatch; the admin endpoint reads aggregates.

def get_platform_settings() -> dict:
    """Fetch the single platform_settings row (always id=1). Returns a
    dict with allow_registration, require_approval, allowed_souls,
    default_tool_policy, feature_flags, updated_at. Used by the
    /auth/register handler (LOG-25.7) to decide whether self-service
    registration is enabled and whether new users start `pending`."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM platform_settings WHERE id = 1"
        ).fetchone()
        return dict(row) if row else {
            "allow_registration": 0,
            "require_approval": 1,
        }




def get_agent_colours() -> dict:
    """``{agent name: "#rrggbb"}`` for every agent someone has given a colour (ST2)."""
    with _conn() as conn:
        return {r["name"]: r["colour"] for r in conn.execute("SELECT name, colour FROM agent_colours")}


def set_agent_colour(name: str, colour: str, set_by: str) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO agent_colours (name, colour, set_by, updated_at) VALUES (?, ?, ?, ?)",
                     (name, colour, set_by, int(time.time() * 1000)))


def get_agent_voices() -> dict:
    """``{agent name: {"voice", "voice_fr", "set_by"}}`` for every agent someone chose a speaker voice
    for (Voice V1.2; French V3.4). "" or None: the definition's."""
    with _conn() as conn:
        return {r["name"]: {"voice": r["voice"] or None, "voice_fr": r["voice_fr"] or None, "set_by": r["set_by"]}
                for r in conn.execute("SELECT name, voice, voice_fr, set_by FROM agent_voices")}


def set_agent_voice(name: str, voice: str, set_by: str, lang: str = "en") -> None:
    """The agent's voice for ``lang`` ("en" or "fr"); ``voice`` "" goes back to the definition's."""
    column = "voice_fr" if lang == "fr" else "voice"
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO agent_voices (name, voice, voice_fr, set_by, updated_at) VALUES (?, '', '', ?, ?)",
                     (name, set_by, int(time.time() * 1000)))
        conn.execute(f"UPDATE agent_voices SET {column} = ?, set_by = ?, updated_at = ? WHERE name = ?",
                     (voice or "", set_by, int(time.time() * 1000), name))
        conn.execute("DELETE FROM agent_voices WHERE name = ? AND COALESCE(voice, '') = '' AND COALESCE(voice_fr, '') = ''",
                     (name,))


def get_voice_prints() -> dict:
    """``{username: print (dict)}`` for everyone who has made a voice print (Voice VO.2)."""
    with _conn() as conn:
        out = {}
        for r in conn.execute("SELECT username, print FROM voice_prints"):
            try:
                out[r["username"]] = json.loads(r["print"])
            except ValueError:
                continue
        return out


def set_voice_print(username: str, data: Optional[dict]) -> None:
    """``data`` None deletes it."""
    with _conn() as conn:
        if data is None:
            conn.execute("DELETE FROM voice_prints WHERE username = ?", (username,))
            return
        conn.execute("INSERT OR REPLACE INTO voice_prints (username, print, updated_at) VALUES (?, ?, ?)",
                     (username, json.dumps(data), int(time.time() * 1000)))


# ── Invitations and people's setup (F38) ─────────────────────────────────────

def create_invite(user_id: str, token_hash: str, created_by: Optional[str], expires_at: int,
                  options: Optional[dict] = None) -> dict:
    """A new invitation for ``user_id``; any earlier one still open for them is cancelled."""
    iid = _new_id("inv")
    now = int(time.time())
    with _conn() as conn:
        conn.execute("UPDATE invites SET revoked_at = ? WHERE user_id = ? AND used_at IS NULL AND revoked_at IS NULL",
                     (now, user_id))
        conn.execute("""INSERT INTO invites (id, user_id, token_hash, created_by, created_at, expires_at, options)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (iid, user_id, token_hash, created_by, now, expires_at, json.dumps(options or {})))
    return latest_invite(user_id)


def _invite_row(row) -> Optional[dict]:
    if not row:
        return None
    out = dict(row)
    try:
        out["options"] = json.loads(out.get("options") or "{}")
    except ValueError:
        out["options"] = {}
    return out


def find_open_invite(token_hash: str, now: Optional[int] = None) -> Optional[dict]:
    """The invitation with this hash, when it can still be used (not used, cancelled or expired)."""
    now = int(time.time()) if now is None else now
    with _conn() as conn:
        row = conn.execute("""SELECT * FROM invites WHERE token_hash = ? AND used_at IS NULL
                              AND revoked_at IS NULL AND expires_at > ?""", (token_hash, now)).fetchone()
    return _invite_row(row)


def use_invite(token_hash: str, now: Optional[int] = None) -> Optional[dict]:
    """Mark the invitation used, once: of two people opening the same link at the same moment only one
    gets it. Returns it, or None when it cannot be used."""
    now = int(time.time()) if now is None else now
    with _conn() as conn:
        cur = conn.execute("""UPDATE invites SET used_at = ? WHERE token_hash = ? AND used_at IS NULL
                              AND revoked_at IS NULL AND expires_at > ?""", (now, token_hash, now))
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM invites WHERE token_hash = ?", (token_hash,)).fetchone()
    return _invite_row(row)


def latest_invite(user_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM invites WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (user_id,)).fetchone()
    return _invite_row(row)


def revoke_invites(user_id: str) -> int:
    with _conn() as conn:
        cur = conn.execute("UPDATE invites SET revoked_at = ? WHERE user_id = ? AND used_at IS NULL AND revoked_at IS NULL",
                           (int(time.time()), user_id))
        return cur.rowcount


def get_people_setup(username: str) -> list:
    with _conn() as conn:
        row = conn.execute("SELECT steps FROM people_setup WHERE username = ?", (username,)).fetchone()
    try:
        return json.loads(row["steps"]) if row else []
    except ValueError:
        return []


def set_people_setup(username: str, steps: Optional[list]) -> None:
    """``steps`` None forgets them."""
    with _conn() as conn:
        if steps is None:
            conn.execute("DELETE FROM people_setup WHERE username = ?", (username,))
            return
        conn.execute("INSERT OR REPLACE INTO people_setup (username, steps, updated_at) VALUES (?, ?, ?)",
                     (username, json.dumps(steps), int(time.time() * 1000)))


def list_audit(actions: Optional[list] = None, limit: int = 100) -> list:
    """The newest audit rows (optionally only these actions), with who did it."""
    where, params = "", []
    if actions:
        where = "WHERE a.action IN (%s)" % ",".join("?" * len(actions))
        params = list(actions)
    with _conn() as conn:
        rows = conn.execute(f"""SELECT a.*, u.username AS by_username, u.display_name AS by_display_name
                                FROM audit_logs a LEFT JOIN users u ON u.id = a.user_id {where}
                                ORDER BY a.created_at DESC LIMIT ?""", [*params, int(limit)]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["metadata"] = json.loads(d.get("metadata") or "null")
        except ValueError:
            d["metadata"] = None
        out.append(d)
    return out
