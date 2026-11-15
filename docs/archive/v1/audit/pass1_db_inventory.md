# Logos Data Model Inventory — Pass 1

**Date**: 2026-04-11
**Purpose**: Factual map of the database schema. Ground truth against which the UI is measured in pass 3.
**Tone**: Descriptive only. No "should" statements, no recommendations.
**Captured by**: exploration agent walking `gateway/auth/db.py` and tracing reader/writer code paths.
**Known inconsistencies to re-validate in pass 3**: raw counts in section 5 don't exactly match the bullet enumeration (e.g., "Direct" count of 9 vs. 11 bullets); `mcp_servers` is listed under both "Indirect" and "None". Preserve as-captured; re-count if load-bearing.

---

## 1. Schema location

All schema definitions are in a single file: **`gateway/auth/db.py:19-435`**.

Schema is defined as a single SQL string (`_SCHEMA`) executed at database initialization. Tables are created with `CREATE TABLE IF NOT EXISTS`. Migrations are handled separately in `_run_migrations()` at `db.py:448-480`, applying `ALTER TABLE` statements when columns are added to existing tables.

Migration history (v0–v9 tracked in comments):
- v0: initial schema
- v1–v9: incremental column additions (tracked in `db.py:452-475`)

Database path: `{hermes_home}/auth.db` (SQLite, WAL mode, foreign keys enabled).

---

## 2. Per-table inventory

### `users`
- **Columns**: `id` (PK, TEXT), `email` (UNIQUE), `username` (UNIQUE), `password_hash`, `role` (ENUM: admin/operator/user/viewer), `status` (ENUM: active/suspended/pending), `display_name`, `created_at`, `last_login`, `failed_login_count`, `locked_until`, `policy_id` (FK → `routing_policies`), `action_policy_id` (FK → `action_policies`)
- **Purpose**: Platform user accounts and role/status tracking.
- **Written by**: `db.create_user()` (`db.py:539`), `db.update_user()` (`db.py:596`), `db.record_failed_login()` (`db.py:571`), `db.assign_user_action_policy()` (`db.py:1620`)
- **Read by**: `db.get_user_by_email()` (`db.py:506`), `db.list_users()` (`db.py:635`), `admin_handlers.py`
- **UI reachability**: **Direct** — Admin → Users

### `refresh_tokens`
- **Columns**: `id` (PK), `user_id` (FK → `users`, CASCADE), `token_hash` (UNIQUE), `issued_at`, `expires_at`, `revoked` (bool), `ip_address`, `user_agent`
- **Purpose**: OAuth session tokens; revocation tracking.
- **Written by**: `db.store_refresh_token()` (`db.py:663`), `db.revoke_refresh_token()` (`db.py:691`)
- **Read by**: `db.get_refresh_token()` (`db.py:682`)
- **UI reachability**: **None** — backend session management only

### `user_settings`
- **Columns**: `user_id` (PK, FK → `users`, CASCADE), `default_soul`, `default_model`, `ui_theme`, `notification_telegram` (bool), `spawn_defaults`, `updated_at`
- **Purpose**: Per-user UI preferences and defaults.
- **Written by**: `db.create_user()` (initializes on user creation), `db.update_user_settings()` (`db.py:717`)
- **Read by**: `db.get_user_settings()` (`db.py:709`)
- **UI reachability**: **Read-only** — displayed in user profile; writes not exposed via nav

### `platform_settings`
- **Columns**: `id` (PK, singleton=1), `allowed_souls`, `default_tool_policy`, `allow_registration` (bool), `require_approval` (bool), `feature_flags` (JSON), `updated_at`
- **Purpose**: Global platform configuration and feature flags.
- **Written by**: `db.set_platform_feature_flag()` (`db.py:2103`), `db.mark_setup_completed()` (`db.py:2118`)
- **Read by**: `db.get_platform_feature_flags()` (`db.py:2089`), `db.is_setup_completed()` (`db.py:2114`)
- **UI reachability**: **Indirect** — setup wizard writes to `feature_flags`; no direct table UI

### `audit_logs`
- **Columns**: `id` (PK), `user_id` (FK → `users`, SET NULL), `action`, `target_type`, `target_id`, `metadata` (JSON), `ip_address`, `created_at`
- **Purpose**: Immutable audit trail for compliance.
- **Written by**: `db.write_audit_log()` (`db.py:732`), called from `admin_handlers.py`, `evolution_handlers.py`, `setup_handlers.py`
- **Read by**: `db.list_audit_logs()` (`db.py:760`), `http_api.py` (`handle_audit_log` endpoint)
- **UI reachability**: **Read-only** — Admin → Audit Log

### `machines`
- **Columns**: `id` (PK), `name` (UNIQUE), `endpoint_url`, `description`, `enabled` (bool), `sort_order`, `created_at`, `updated_at`, `default_model` (nullable), `api_key` (nullable, added v5–v6)
- **Purpose**: LM Studio / Ollama / local inference endpoints.
- **Written by**: `db.create_machine()` (`db.py:788`), `db.update_machine()` (`db.py:824`), `db.delete_machine()` (`db.py:839`), `db.reorder_machines()` (`db.py:814`)
- **Read by**: `db.list_machines()` (`db.py:806`), `openshell_routes.py`, `setup_handlers.py`
- **UI reachability**: **Direct** — Settings → Inference (local machines)

### `machine_capabilities`
- **Columns**: `id` (PK), `machine_id` (FK → `machines`, CASCADE), `model_class`, `priority`, `max_context` (nullable), `enabled` (bool), UNIQUE(`machine_id`, `model_class`)
- **Purpose**: Per-machine model class support and priority ranking.
- **Written by**: `db.set_machine_capabilities()` (`db.py:1325`)
- **Read by**: `db.get_machine_capabilities()` (`db.py:1316`), `openshell_routes.py`
- **UI reachability**: **Indirect** — editable from machine detail (Admin → Machines)

### `cloud_providers`
- **Columns**: `id` (PK), `provider`, `name`, `base_url`, `api_key`, `active_model`, `is_active` (bool), `enabled` (bool), `created_at`, `updated_at`
- **Purpose**: Cloud inference endpoints (OpenAI, Anthropic, etc.) with API keys.
- **Written by**: `db.create_cloud_provider()` (`db.py:846`), `db.set_active_cloud_provider()` (`db.py:894`)
- **Read by**: `db.list_cloud_providers()` (`db.py:866`), `db.get_active_cloud_provider()` (`db.py:904`)
- **UI reachability**: **Direct** — Settings → Inference (cloud providers)

### `agents`
- **Columns**: `id` (PK), `name` (UNIQUE), `soul_slug`, `model`, `description`, `creator_id` (FK → `users`, nullable), `shared` (bool), `toolsets`, `created_at`, `updated_at`, `char_index` (0–7 sprite, v8), `model_route_id` (FK → `model_routes`, nullable, v9)
- **Purpose**: Named agent definitions with soul + model route bindings.
- **Written by**: `db.create_agent()` (`db.py:912`), `db.update_agent()` (`db.py:961`), `db.delete_agent()` (`db.py:986`)
- **Read by**: `db.list_agents()` (`db.py:948`), `http_api.py`, `run.py`
- **UI reachability**: **Direct** — Agents tab

### `model_routes`
- **Columns**: `id` (PK), `provider`, `model`, `openshell_name` (UNIQUE), `openshell_port` (UNIQUE), `status` (ENUM: provisioning/ready/error/stopped), `status_detail`, `is_default` (bool), `is_primordial` (bool), `created_at`, `updated_at`
- **Purpose**: OpenShell gateway instances pinned to (provider, model) pairs; enables multi-model multi-agent concurrency.
- **Written by**: `db.create_model_route()` (`db.py:998`), `db.set_default_model_route()` (`db.py:1114`), `db.rename_model_route_openshell_name()` (`db.py:1090`), `openshell_routes.py`
- **Read by**: `db.list_model_routes()` (`db.py:1061`), `db.count_agents_using_route()` (`db.py:1140`)
- **UI reachability**: **Direct** — Settings → Routing **and** Admin → Model Routes (same table, two nav locations)

### `routing_policies`
- **Columns**: `id` (PK), `name` (UNIQUE), `description`, `fallback` (default `'any_available'`), `created_at`, `updated_at`
- **Purpose**: Inference routing rules (legacy; superseded by `action_policies` for behavior enforcement).
- **Written by**: `db.create_policy()` (`db.py:1353`), `db.update_policy()` (`db.py:1385`)
- **Read by**: `db.list_policies()` (`db.py:1377`), `db.get_user_policy()` (`db.py:584`)
- **UI reachability**: **None** — defined but not exposed in current nav (superseded by `action_policies`; kept for backward compatibility)

### `policy_rules`
- **Columns**: `id` (PK), `policy_id` (FK → `routing_policies`, CASCADE), `model_class`, `machine_id` (FK → `machines`, CASCADE), `rank`, UNIQUE(`policy_id`, `model_class`, `rank`)
- **Purpose**: Individual routing rules within a `routing_policy`.
- **Written by**: `db.set_policy_rules()` (`db.py:1438`)
- **Read by**: `db.get_policy_rules()` (`db.py:1425`), `db.resolve_policy_machines()` (`db.py:1456`)
- **UI reachability**: **Indirect** — via `routing_policies` (which has no direct UI)

### `machine_users`
- **Columns**: `id` (PK), `machine_id` (FK → `machines`, CASCADE), `user_id` (FK → `users`, CASCADE), `priority`, `created_at`, UNIQUE(`machine_id`, `user_id`)
- **Purpose**: User claims on specific local machines (multi-tenancy).
- **Written by**: `db.claim_machine()` (`db.py:1244`), `db.unclaim_machine()` (`db.py:1264`)
- **Read by**: `db.list_machine_claims()` (`db.py:1272`), `db.list_user_machines()` (`db.py:1285`)
- **UI reachability**: **None** (complex CRUD methods exist but no nav surface — claims are populated by backend setup flows)

### `routing_log`
- **Columns**: `id` (PK), `user_id`, `model_alias`, `model_class`, `machine_id`, `machine_name`, `layer`, `instance_name`, `created_at`
- **Purpose**: Audit trail of routing decisions for debugging.
- **Written by**: `db.log_routing_decision()` (`db.py:1492`)
- **Read by**: `db.list_routing_log()` (`db.py:1511`), `http_api.py` (`handle_routing_log` endpoint)
- **UI reachability**: **Read-only** — Settings → Routing (debug log section)

### `action_policies`
- **Columns**: `id` (PK), `name` (UNIQUE), `description`, `network_policy`, `network_allowlist` (JSON), `filesystem_policy`, `exec_policy`, `write_policy`, `provider_policy`, `secret_policy`, `created_at`, `updated_at`
- **Purpose**: Behavior enforcement policies (network/filesystem/execution/write/provider/secret access control).
- **Written by**: `db.create_action_policy()` (`db.py:1548`), `db.update_action_policy()` (`db.py:1592`)
- **Read by**: `db.list_action_policies()` (`db.py:1584`), `db.get_user_action_policy_row()` (`db.py:1628`)
- **UI reachability**: **Direct** — Admin → Security

### `approval_requests`
- **Columns**: `id` (PK), `session_id`, `user_id` (FK → `users`, nullable), `tool_name`, `tool_args`, `tool_args_hash`, `action_type`, `status` (ENUM: pending/approved/rejected/timeout/cancelled), `policy_id`, `requested_at`, `decided_at`, `decided_by`, `decision_note`, `expires_at`
- **Purpose**: Pending/resolved approval gates for policy-gated tool calls.
- **Written by**: `db.create_approval_request()` (`db.py:1645`), `db.resolve_approval_request()` (`db.py:1723`), `db.expire_stale_approvals()` (`db.py:1741`)
- **Read by**: `db.find_approved_request()` (`db.py:1679`), `db.list_approval_requests()` (`db.py:1695`)
- **UI reachability**: **Direct** — Admin → Approvals

### `workflow_definitions`
- **Columns**: `id` (PK), `name` (UNIQUE), `description`, `version`, `steps_json` (JSON), `tags` (JSON), `created_by` (FK → `users`, nullable), `created_at`, `updated_at`
- **Purpose**: Reusable workflow templates.
- **Written by**: `db.create_workflow_definition()` (`db.py:1754`), `db.update_workflow_definition()` (`db.py:1790`)
- **Read by**: `db.list_workflow_definitions()` (`db.py:1782`)
- **UI reachability**: **Direct** — Admin → Workflows

### `workflow_runs`
- **Columns**: `id` (PK), `workflow_id` (FK → `workflow_definitions`), `status` (ENUM: pending/running/paused/success/failed/cancelled), `triggered_by`, `input_json` (JSON), `output_json` (JSON, nullable), `error` (nullable), `started_at`, `finished_at`, `created_at`
- **Purpose**: Execution instances of `workflow_definitions`.
- **Written by**: `db.create_workflow_run()` (`db.py:1813`), `db.update_workflow_run()` (`db.py:1865`)
- **Read by**: `db.list_workflow_runs()` (`db.py:1839`)
- **UI reachability**: **Read-only** — Admin → Runs (filtered by `workflow_id`)

### `workflow_step_runs`
- **Columns**: `id` (PK), `run_id` (FK → `workflow_runs`, CASCADE), `step_id`, `step_type`, `step_name`, `status`, `parallel_group`, `depends_on` (JSON), `input_summary`, `output_summary`, `approval_id` (FK → `approval_requests`, nullable), `error`, `started_at`, `finished_at`, `created_at`
- **Purpose**: Per-step state within one `workflow_runs` instance.
- **Written by**: `db.create_workflow_step_run()` (`db.py:1878`), `db.update_step_run()` (`db.py:1907`)
- **Read by**: `db.get_workflow_step_runs()` (`db.py:1898`)
- **UI reachability**: **Read-only** — nested display within workflow run detail

### `agent_runs`
- **Columns**: `id` (PK), `session_id`, `user_id` (FK → `users`, nullable), `instance_name`, `soul`, `model`, `provider`, `action_policy_id` (FK → `action_policies`, nullable), `action_policy_snapshot` (JSON), `workflow_run_id` (FK → `workflow_runs`, nullable), `workspace_path`, `status` (ENUM: running/success/failed/cancelled), `user_message`, `tool_sequence` (JSON), `tool_detail` (JSON), `approval_ids` (JSON), `output_summary`, `error`, `api_calls`, `started_at`, `finished_at`, `created_at`, `agent_id` (v4, TEXT soft-ref, NOT a FK)
- **Purpose**: Execution records for `_run_agent` invocations; tracks model, tools, approvals, status.
- **Written by**: `db.create_agent_run()` (`db.py:1973`), `db.finish_agent_run()` (`db.py:2011`)
- **Read by**: `db.list_agent_runs()` (`db.py:2056`)
- **UI reachability**: **Direct** — Admin → Runs (list/filter/detail)

### `evolution_proposals`
- **Columns**: `id` (PK), `agent_id` (default `'hermes'`), `title`, `summary`, `diff_text`, `target_files` (JSON), `proposal_type` (ENUM), `status` (ENUM), `question_text`, `answer_text`, `frontier_model`, `frontier_output`, `cron_job_id`, `git_branch`, `git_pr_url`, `decided_by` (FK → `users`, nullable), `decided_at`, `created_at`, `updated_at`
- **Purpose**: Code improvement proposals generated by agents; review gate.
- **Written by**: `db.create_evolution_proposal()` (`db.py:2137`), `db.update_evolution_proposal()` (`db.py:2201`)
- **Read by**: `db.list_evolution_proposals()` (`db.py:2174`)
- **UI reachability**: **Direct** — Settings → Proposals

### `evolution_settings`
- **Columns**: `id` (PK, singleton=1), `enabled` (bool), `schedule_label`, `schedule_minutes`, `git_remote_url`, `git_username`, `git_pat`, `git_base_branch`, `frontier_model`, `frontier_api_key_env`, `max_pending`, `cron_job_id`, `updated_at`
- **Purpose**: Configuration for autonomous evolution (proposal generation schedule, Git integration).
- **Written by**: `db.update_evolution_settings()` (`db.py:2232`)
- **Read by**: `db.get_evolution_settings()` (`db.py:2224`)
- **UI reachability**: **Read-only** — Settings → Proposals (display only)

### `mcp_servers`
- **Columns**: `id` (PK), `name` (UNIQUE), `catalogue_id`, `source` (ENUM: ui/external), `status` (ENUM: pending/deploying/running/stopped/error/external), `deploy_mode` (ENUM: k8s/external), `url`, `token`, `k8s_namespace`, `k8s_image`, `config_json` (JSON), `tools_filter` (JSON), `category`, `description`, `auto_wire` (bool), `enabled` (bool), `created_at`, `updated_at`, `last_error`
- **Purpose**: MCP server instances and catalog entries; integration service keys.
- **Written by**: `db.create_mcp_server()` (`db.py:2279`), `db.update_mcp_server()` (`db.py:2315`)
- **Read by**: `db.list_mcp_servers()` (`db.py:2252`), `session.py`, `run.py`, `mcp_management.py`
- **UI reachability**: **Indirect** — Settings → Tools (agent's row inventory had this both Indirect and None — re-validate)

### `platform_routing`
- **Columns**: `id` (PK), `platform` (ENUM: web/telegram/discord/etc), `scope` (ENUM: global/chat/user), `scope_id` (empty for global), `agent_id` (FK → `agents`, CASCADE), `created_at`, UNIQUE(`platform`, `scope`, `scope_id`)
- **Purpose**: Maps inbound platform conversations to agent sandboxes (chat/user/global per platform).
- **Written by**: `db.upsert_platform_routing()` (`db.py:1162`), `db.delete_platform_routing()` (`db.py:1189`)
- **Read by**: `db.resolve_platform_routing()` (`db.py:1209`), `db.list_platform_routing()` (`db.py:1194`)
- **UI reachability**: **Direct** — Admin → Platforms **and** Settings → Channels (same table, two nav locations)

---

## 3. Foreign-key graph

```
users
  ├─→ refresh_tokens (user_id, CASCADE) [1:N]
  ├─→ user_settings (user_id, CASCADE) [1:1]
  ├─→ audit_logs (user_id, SET NULL) [1:N]
  ├─→ machine_users (user_id, CASCADE) [1:N]
  ├─→ agents (creator_id, nullable) [1:N]
  ├─→ approval_requests (user_id, nullable; decided_by, nullable) [1:N]
  ├─→ workflow_definitions (created_by, nullable) [1:N]
  └─→ evolution_proposals (decided_by, nullable) [1:N]

machines
  ├─→ machine_capabilities (machine_id, CASCADE) [1:N]
  ├─→ machine_users (machine_id, CASCADE) [1:N]
  └─→ policy_rules (machine_id, CASCADE) [1:N]

agents
  ├─→ creator_id → users [M:1]
  ├─→ model_route_id → model_routes [M:1, nullable]
  └─→ platform_routing (agent_id, CASCADE) [1:N]

model_routes
  └─→ agents (model_route_id) [1:N]

cloud_providers
  (no outbound FKs; referenced by agents.model field indirectly)

routing_policies [LEGACY]
  ├─→ policy_rules (policy_id, CASCADE) [1:N]
  └─→ users (policy_id, via assignment) [1:N]

action_policies [CURRENT]
  ├─→ users (action_policy_id, via assignment) [1:N]
  ├─→ agent_runs (action_policy_id, nullable) [1:N]
  └─→ approval_requests (policy_id, nullable) [1:N]

approval_requests
  ├─→ users (user_id, nullable; decided_by, nullable)
  └─→ workflow_step_runs (approval_id, nullable, backref)

workflow_definitions
  ├─→ workflow_runs (workflow_id) [1:N, no cascade]
  └─→ users (created_by, nullable)

workflow_runs
  ├─→ workflow_definitions (workflow_id)
  ├─→ workflow_step_runs (run_id, CASCADE) [1:N]
  └─→ agent_runs (workflow_run_id, nullable, backref)

agent_runs
  ├─→ users (user_id, nullable)
  ├─→ action_policies (action_policy_id, nullable)
  ├─→ workflow_runs (workflow_run_id, nullable)
  └─→ agents (agent_id — TEXT, NOT a FK — informal soft-ref)

evolution_proposals
  └─→ users (decided_by, nullable)

platform_routing
  └─→ agents (agent_id, CASCADE)

platform_settings, evolution_settings
  (singleton rows; no FKs)

mcp_servers
  (no FKs; consumed by session.py / run.py indirectly)
```

**Cascade impact**: Deleting a machine cascades to `machine_capabilities`, `machine_users`, `policy_rules`. Deleting a user cascades to `refresh_tokens`, `user_settings`. Deleting an agent cascades to `platform_routing`. Deleting a `workflow_definitions` row does **not** cascade to its runs (intentional, for audit preservation).

---

## 4. Schema surprises (factual only)

1. **`agent_runs.agent_id` is a soft reference** — TEXT column with default `'hermes'`, NOT a FK to `agents.id`. Added in v4 migration (`db.py:459`). Allows `agent_runs` to persist after agent deletion but breaks referential integrity for queries.

2. **`routing_policies` is vestigial** — table exists (`db.py:110-126`), still has writer code paths, zero UI surface. Superseded by `action_policies` (v2). Comments note the change (`db.py:454-455`). Kept for backward compatibility.

3. **`users` has two policy FKs** — `policy_id` (→ `routing_policies`, v1) **and** `action_policy_id` (→ `action_policies`, v2). These are semantically different (routing vs. behavior enforcement), but having both live creates confusion. Comments clarify intent (`db.py:183-200`).

4. **`model_routes.is_primordial` singleton not enforced by schema** — application code special-cases the primordial gateway at `openshell_routes.py` but nothing in the table prevents two primordial rows. Relies on app discipline.

5. **`platform_settings` and `evolution_settings` are singletons by CHECK constraint** — both use `id=1` with CHECK, and init code uses `INSERT OR IGNORE` to ensure one row exists. No enforcement if the constraint is bypassed.

6. **`workflow_definitions.steps_json` and `workflow_runs.input_json`/`output_json` are unvalidated free-form JSON** — no schema validation. Callers must enforce shape (e.g., `workflows/model.py`).

7. **`approval_requests.tool_args_hash` is not UNIQUE** — used for idempotency lookups but collisions are tolerated; deduplication happens at query time (`db.py:1679-1692`).

8. **No cascade `workflow_definitions` → `workflow_runs`** — runs are orphaned if the definition is deleted. Intentional (audit trail preservation), but cascade policy is inconsistent with other FKs.

9. **`machine_users` has no UI surface despite full CRUD** — `claim_machine`/`unclaim_machine` methods exist but are not exposed in nav; claims are created by backend setup flows.

---

## 5. Raw counts (as captured — re-validate in pass 3 if load-bearing)

- **Total tables**: 24
- **Direct UI reachability**: 9 (tally; bullet enumeration lists 11 — `users`, `machines`, `cloud_providers`, `agents`, `model_routes`, `action_policies`, `approval_requests`, `workflow_definitions`, `agent_runs`, `evolution_proposals`, `platform_routing`)
- **Read-only**: 4 (`user_settings`, `audit_logs`, `routing_log`, `evolution_settings`)
- **Indirect**: 5–6 (`machine_capabilities`, `machine_users`, `policy_rules`, `workflow_runs`, `workflow_step_runs`, `mcp_servers`)
- **None**: 4–6 (`refresh_tokens`, `platform_settings`, `routing_policies`, `mcp_servers` listed twice)
