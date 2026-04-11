# Missing — large architectural gaps

**Purpose**: Features that are scaffolded in the data model and partially started, but **not built out to a usable state**. This is a standing doc — not audit-scoped, not tied to any one session.

**When to add**: Something large is architecturally scaffolded (tables exist, FK relationships are in place, some code paths are written) but the UI / control surface is missing or incomplete, AND the gap is big enough to need planning rather than a quick fix.

**When NOT to add**:
- Small bugs → [TASKS.md](../TASKS.md)
- Speculative future features with no current scaffolding → out of scope
- UI polish (rename, rearrange, small flows) → audit punch list, not this

**Established**: 2026-04-11 during the UI audit pass 3. Originally surfaced from cross-referencing [pass1_db_inventory.md](audit/pass1_db_inventory.md) against [pass1_ui_inventory.md](audit/pass1_ui_inventory.md).

---

## Status summary (verified 2026-04-11 end of session)

| Ticket | Status | Notes |
|---|---|---|
| M1 — Editable Tools (T) in STAMP pill | **NOT STARTED** | T pill is a display-only `<span>` at `main_app.html:569`. Blocked until M10 Option D gives STAMP-T runtime teeth. |
| M2 — Editable Policy (P) + approvals | **NOT STARTED** | P pill is a display-only `<span>` at `main_app.html:639`. Blocked until M10 Option D gives STAMP-P runtime teeth. |
| M3 — Per-user inference routing | **NOT STARTED** | `machine_users` table exists, zero UI. No claimMachine JS, no My Inference tab. |
| M4 — Multi-user UX polish | **NOT STARTED** | No owner badges, no shared toggle, no per-user chat filter. |
| M5 — World view first-class surface | **PARTIALLY SHIPPED** | Phase A thought bubble from M8 landed (`AgentSprite._updateBubble` — 💭 scale-pulse when `active_tasks > 0`). Full-tab / click-to-enter-chat / multi-user world still pending. |
| M6 — Unified observability | **DONE (MVP)** | `JsonRedactingFormatter` + `_SessionFilter` + `set_log_context` in `gateway/run.py`. `unified.jsonl` actively writing. `logos debug tail` CLI works. 4 of 5 minimum-viable items landed. |
| M7 — Sandbox health UX | **PREMISE OUTDATED** | Original premise assumed a NemoClaw port-forward + HTTP /health probe. Plan A-prime uses per-task `openshell sandbox exec` instead, so the rename/probe design needs to be re-grounded on what Plan A-prime actually offers as a health signal. Field rename and richer observability goals still valid. |
| M8 — Dispatch activity ledger | **PHASE A SHIPPED, PHASE B NOT STARTED** | `WorkerRegistry._active_tasks` counter, `admin_handlers` surfaces it, world view renders thought bubble. `dispatches` table, origin tagging, Admin → Activity tab still pending. |
| M9 — Autonomous activity (visibility) | **NOT STARTED** | The three consolidation mechanisms exist (memory nudge + skill nudge + pre-reset flush) but are invisible in the UI. Memory writes during chats don't actually happen today because M10 blocks them. |
| M10 — Plan A-prime bypasses agent loop | **NOT STARTED** | `sandbox_worker.py` is a naive chat forwarder with no `tools` payload. Option D (agent inside, action tools outside, STAMP-gated bridges) documented and recommended. Blocks M1, M2, M9. |

**Recommended next execution target**: **M10 via Option D**. It unblocks M1, M2, and M9, and it's the architecture that makes STAMP's T/P axes real governance instead of decorative labels. Estimated 3-5 days of focused work — scope as its own session.

---

## M1 — Editable Tools (T) in the Chats STAMP pill

**Status**: NOT STARTED. Verified: T pill is `<span data-testid="stamp-t">` at `gateway/html/main_app.html:569`, no `@click` handler, no dropdown, no toggle logic. Purely informational.

**State today**: The Chats tab has a STAMP governance pill row — **S**oul / **T**ools / **A**gent / **M**odel / **P**olicy. S and M are dropdowns users can change mid-chat. T displays a count but has no interaction.

**What's scaffolded**:
- `agents.toolsets` column stores per-agent toolset binding
- `mcp_servers` table is the catalog
- Backend code paths exist to attach/detach tools
- Settings → Tools has a global MCP server management surface

**What's missing**: A dropdown or drawer from the T pill that shows currently-attached tools and lets the user toggle them on/off for this agent. Needs to respect whatever governance rules apply (M2 may gate certain tool changes behind approvals).

**Why it's architecturally large**: Touches four things at once — the tools domain, the agents entity, the policy cross-cutting concern, and the per-user permission layer. Each needs its own decision (e.g., should toggling a tool trigger an approval request? is the per-agent binding user-scoped or shared?).

---

## M2 — Editable Policy (P) in the Chats STAMP pill + approval surfacing

**Status**: NOT STARTED. Verified: P pill is `<span data-testid="stamp-p">` at `gateway/html/main_app.html:639`, display-only, no click handler, no slide-out. Blocked on M10 Option D to give per-tool approval enforcement a runtime path.

**State today**: P pill shows the policy name badge, no interaction. Pending approvals are badged only on the top-level Admin tab and live in Admin → Approvals. A non-admin user in the middle of a chat has no way to see or respond to an approval for their own tool call — the chat just stalls.

**What's scaffolded**:
- `users.action_policy_id` → `action_policies` (the behavior-enforcement policy)
- `approval_requests` links policy + session + user + tool call
- Admin → Approvals handles the admin side completely
- The gateway already emits a `pendingApprovalCount` used by the Admin tab red badge

**What's missing**:
- Click P pill → policy detail slide-out showing what the agent is allowed to do (network/fs/exec/write/provider/secret), with approval history for this session
- Inline approval prompt in the chat when an agent's tool call is gated and waiting (today the user sees silence and no indication why)
- Non-admin path to see and respond to approvals for their own sessions
- Badge on individual chats in the sidebar list when that chat has a pending approval

**Why it's architecturally large**: Approvals are going to matter more and more. They sit at the junction of governance, workflows, and the chat experience. Every chat that touches a gated tool becomes an approval experience — if the UX is admin-only, users hit dead ends. Confirmed by Q4 in the audit: "approvals will matter more and more and users must be able to access it".

---

## M3 — Per-user inference routing

**Status**: NOT STARTED. Verified: no `my-inference` / `claim_machine` / `claimMachine` / `machine_users` references anywhere in `main_app.html`. Data-layer functions exist in `gateway/auth/db.py` but have zero UI consumers.

**State today**: `machines` (local LM Studio / Ollama endpoints) and `cloud_providers` (API-key backends) are global. Every user shares the same inference pool. The `machine_users` claims table exists with full CRUD methods (`claim_machine`, `unclaim_machine`, `list_machine_claims`, `list_user_machines`) — **no UI at all**. Claims are populated by backend setup flows only.

**What's scaffolded**:
- `machine_users (machine_id, user_id, priority, UNIQUE(machine_id, user_id))` — exactly the schema a per-user routing feature needs
- Data-layer functions exist (`gateway/auth/db.py:1244`, `:1264`, `:1272`, `:1285`)
- `users` table, roles, permissions — all multi-user-ready
- Routing resolver reads `machine_users` where it exists but doesn't prefer-claimed-over-shared yet

**What's missing**:
- UI for a user to register / claim their own inference server (LM Studio, Ollama, or cloud API key) alongside the shared pool
- Routing rule "use my personal machine first, fall back to shared"
- New Settings sub-tab ("My Inference" or similar) that surfaces `machine_users` per-user
- Admin visibility into per-user claims (detect contention, approve sharing, etc.)
- Per-user throttling / concurrency limits (because the whole point is "reduce parallel requests to machines not capable of doing them well")

**Why it's architecturally large**:
1. It's the explicit reason Settings → Routing and Admin → Model Routes were duplicated during development — work toward per-user routing was in-flight and got interrupted
2. Solves a real performance problem: contention on shared machines that can't sustain concurrent inference
3. Ties directly into multi-user polish (M4) — users aren't meaningfully separate until they have their own resources
4. Data model is ready; this is a UI + routing-resolver refactor, not a data migration
5. Cuts across four files that are currently duplicated across two templates

**Direction established**: Q2 answer — "users should be able to route to a different inference server (like their own) that way we can reduce parallel requests to machines not capable of doing so well".

---

## M4 — Multi-user UX polish

**Status**: NOT STARTED. Verified: no owner-badge / shared-toggle / created-by / shared_with_me references in `main_app.html`. The data shape is ready (`agents.creator_id`, `agents.shared`, `user_settings`) but zero UI consumers.

**State today**: The auth / permissions layer is real, but UX defaults still assume a single operator:
- `/chats` auto-selects the first agent on page load (era-3 residue)
- Chat history has no per-user filter
- Agent pill bar in Chats has no owner / sharing UX — every agent looks equally "yours"
- Agents default to `shared=True` but there's no UI toggle per-agent
- No user-switcher or active-user indicator beyond the account menu dropdown
- No "created by" surface on agent cards
- `user_settings` table is read-only from the UI — users can't change their own defaults

**What's scaffolded**:
- `users` table with roles
- `agents.creator_id` FK to users
- `agents.shared` bool
- `user_settings` table with per-user defaults (default soul, default model, UI theme, etc.)

**What's missing**:
- Owner badge on agent cards + Chats pill bar
- "Shared with me" vs "created by me" filter in Agents tab
- Per-agent shared/private toggle in Create Agent form
- Per-user chat history filter in Chats sidebar
- "Created by" column in agent/run detail views
- New Settings → My Account sub-tab for user-level preferences (writes to `user_settings`)
- Policy and approvals visibility for non-admin users (ties into M2)

**Why it's architecturally large**: Touches every primary tab (Agents, Chats, Settings). All the data is there; none of the surfaces expose it. This is the biggest coherent UX shape-change after M3, and the two should likely be planned together since M3 provides the infrastructure M4 needs.

**Direction established**: Q5 answer — HEAVY. Multi-user polish is first-class, not a nice-to-have.

---

## M5 — World view as a first-class, persistent surface

**Status**: PARTIALLY SHIPPED. The M8 Phase A thought-bubble animation (`AgentSprite._updateBubble` at `gateway/world/AgentSprite.js:~170` + `_startBusyTween` at ~560) made the world view actually reflect live agent state for the first time — previously it was just walking sprites. That's a big piece of "world as a real surface not a decoration", but M5's full scope (full-tab mode, click-to-enter-chat, multi-user awareness) is still not built.

**State today**: 960px Phaser canvas lives in the Agents tab, sharing space with the Create-Agent form. Canvas shrinks (16rem) when the form is open, grows (24rem) when closed. Agent sprites walk around the world with a real local-time day/night cycle (commit `24e3ad8`). Agents in flight render a pulsing 💭 thought bubble driven by `inst.active_tasks > 0` (commit `bbee66a`).

**What's scaffolded**:
- Phaser scene setup at `gateway/templates/main_app.html:2431`
- `_worldAgentList()` helper at `main_app.html:6596`
- Day/night cycle tied to browser local time
- Agent state visualization (sprites, positions)

**What's missing**:
- The world should breathe — full tab or fullscreen mode, not sharing space with CRUD (pass 3 S2 addresses the split; this goes further)
- Clicking an agent in the world should do something meaningful (open their chat, show state panel, enter "shadow" mode)
- Agents should react to real state: typing/thinking indicator when mid-inference, idle posture when sleeping, unhappy when they've hit an error
- Multi-user awareness — see other users' agents alive in the same world
- Persistence across tab switches — a small world preview should be available from Chats too, not only the Agents tab
- Per-agent avatar / sprite customization beyond the 0–7 char_index

**Why it's architecturally large**: The tamagotchi-agents vision is the Logos product differentiator. "Named agents as persistent entities" is a first-class project memory. The world view is the visual expression of that — but today it's a decoration, not a surface you can interact with. M5 is the long-term direction beyond pass 3 S2 (which just gets the world out of the CRUD form's way).

---

## M6 — Unified observability / single source of logs

**Status**: DONE (MVP). Verified 2026-04-11: `~/.logos/logs/unified.jsonl` is 4.3MB and actively growing. `JsonRedactingFormatter`, `_SessionFilter`, and `set_log_context` all present in `gateway/run.py` at lines 312/351/368. `./venv/bin/logos debug tail` CLI works. The "CLI spinner bleeds over stdout log" leftover is cosmetic and the JSON sink bypasses it entirely. Used extensively during this session to diagnose the `registered_at` crash, the `-g` routing bug, the qwen reasoning stream separation verification, and the `_flush_memories_for_session` discovery — it paid for itself multiple times today.

**State today**: Logos events are scattered across **at least 6 disjoint log sources**, none of which agree with each other or are easily correlated:

| Source | Where it lives | What it captures | Why it's useless alone |
|---|---|---|---|
| Logos gateway stdout | Wherever `logos gateway run` is redirected (typically none in dev) | Banner + spinner animation + agent tool-call pretty-print | **Dominated by ANSI escape sequences, swallows Python `logger` output** — standard library log calls don't visibly appear |
| Python `logging` module calls across `gateway/*.py` | Either stdout (masked by spinner) or a file nobody configured | Worker registration, dispatch, disconnect, routing events | The real events are there but there's no deterministic sink |
| Sandbox `/tmp/worker.log` inside each k3s pod | Inside the sandbox pod's local `/tmp` | 7 lines if the worker's stuck, more if it's processing | Not tailed centrally, wiped on pod restart, not structured |
| Cluster container logs (`docker logs openshell-cluster-*`) | Docker | Internal k3s + openshell-server output | Forgotten about entirely unless you think to check it |
| k3s pod logs (`kubectl logs` inside the cluster container) | Nested inside the cluster container | Per-pod stdout from agent worker + supervisor | Requires exec'ing into the cluster container to use |
| `audit_logs` SQLite table | `auth.db` | Admin action trail | Not runtime events |

**Known pain (documented from a real debugging session on 2026-04-11)**: A worker registration regression took hours to even localize because the Python `logger.info("Worker registered: %s")` call at `gateway/worker_registry.py:130` could not be found in any visible log output. The CLI pretty-printer in `logos_cli/gateway.py` was masking stdlib logger output, leaving only spinner animation + tool-call status lines visible. The debugging session ended unresolved partly because there was no way to correlate events across `sandbox_worker.py → TunnelWebSocket frame layer → NAT path → gateway handler` in one place.

**What's scaffolded**:
- Python `logging` is used throughout (gateway, worker_registry, admin_handlers, openshell_routes, sandbox_worker). It's consistent, just unaggregated.
- `audit_logs` table exists and captures admin mutations with user_id + action + metadata.
- Sandbox workers already emit structured-ish log lines (`asctime name level message`).
- The gateway→worker WebSocket channel already exists and could relay sandbox log events upstream.

**MVP status (as of 2026-04-11)**: 4 of 5 minimum-viable items landed. The core loop — structured logs → single sink → correlation IDs → `logos debug tail` — works end-to-end. Ship-ready for future debugging sessions.

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | Structured format | **DONE** | `JsonRedactingFormatter` in `gateway/run.py` — emits one JSON object per log record with `ts`, `level`, `logger`, `msg`, correlation IDs, and a `source` tag. Runs messages through `agent.redact.redact_sensitive_text` to strip secrets. |
| 2 | Single sink | **DONE** | `~/.logos/logs/unified.jsonl` — `RotatingFileHandler` attached to the root logger in `gateway/run.py` alongside the existing `gateway.log` / `errors.log` handlers. Captures every `logging.getLogger(...).info/warning/error` call in the gateway process. 10MB × 5 rotation. |
| 3 | Correlation IDs | **DONE** | Five contextvars (`session_id`, `task_id`, `user_id`, `worker_id`, `chat_id`) with a public `set_log_context(**kwargs)` helper in `gateway/run.py`. Injected into every `LogRecord` by `_SessionFilter`. Wired into `_handle_chat` at `gateway/http_api.py:2810` as the first call site — generates a per-dispatch UUID `task_id` that propagates through the whole chat turn. |
| 4 | `logos debug tail` | **DONE** | `logos_cli/debug.py` + argparse registration in `logos_cli/main.py`. Pretty-prints records with colored source/level tags, supports `--filter key=value` (with wildcards, `!=` negation), `--level`, `--since 5m`, `--follow`, `--raw`, `--all`. Handles log rotation cleanly in follow mode. |
| 5 | CLI spinner / log stream separation | **partial** | The unified JSON sink is unaffected by the CLI spinner because it's a direct `FileHandler` attached to the root logger — spinner output goes to stdout, structured logs go to the file. The spinner still masks `logger.info` calls on stdout when running `logos gateway run` interactively, but `logos debug tail` bypasses that entirely. Full fix (moving spinner to stderr) is a minor polish item. |

**How to use it (for future sessions)**:
```bash
# Follow the unified log live
logos debug tail --follow

# Just the recent warnings and errors
logos debug tail --level WARNING --lines 100

# Everything for a specific chat turn
logos debug tail --all --filter task_id=abc123def

# Only gateway events, no aiohttp access noise
logos debug tail --filter logger!=aiohttp.access --follow

# Only events from a given worker
logos debug tail --filter worker_id=hermes-hemette --all

# Since the last gateway restart (relative)
logos debug tail --since 5m
```

**End-to-end verification** (2026-04-11):
1. Restart gateway with `./venv/bin/python -m gateway.run`
2. `~/.logos/logs/unified.jsonl` appears with JSON-lines records immediately
3. `logos debug tail` pretty-prints them with colors and filtering
4. Unit test in `gateway/run.py` confirms `set_log_context` → `_SessionFilter` → `JsonRedactingFormatter` wiring works (5 of 5 contextvars propagate correctly through a synthetic log record).

**Stretch items still open** (not MVP-blocking):
- Sandbox worker log forwarding to gateway's `unified.jsonl` — scaffolded via `_SandboxJsonFormatter` in `docker/sandbox_worker.py:27` writing to `/tmp/worker.jsonl` inside the sandbox, but **not yet forwarded upstream**. Blocked on TASKS.md #24 (sandbox worker registration regression) being fixed first; once workers can register, the gateway can stream `/tmp/worker.jsonl` records into unified.jsonl over the existing WS channel. The formatter exists so the JSON shape is already compatible.
- Correlation ID plumbing **beyond** `_handle_chat`. Currently only the main chat dispatch path sets context. Other entry points (platform webhooks, workflow runs, cron, admin mutations) still emit records with `"-"` defaults. Each additional entry point is a one-liner `set_log_context(...)` call at the top of its handler. See the `_handle_chat` wiring in `gateway/http_api.py:2810` as the pattern.
- Loki / Grafana / OpenTelemetry stretch goals (see below).
- CLI polish: filter out `aiohttp.access` noise by default (currently dominates `logos debug tail` output when the web UI is open).

**Stretch goals** (not required for the MVP, but worth keeping in mind):

6. Ship to Loki / Grafana Tempo via Vector or Fluent Bit for long-term retention + search UI.
7. OpenTelemetry spans for proper cross-component tracing (spans → correlation IDs for free, with parent/child relationships).
8. A `/admin/logs` web view that tails the unified stream with filter pills + a search bar.
9. Sandbox workers expose a tiny HTTP endpoint `/healthz` inside the pod so the supervisor can poll liveness independently of the WS registration.

**Why it's architecturally large**:

1. It touches **every process** in the system (gateway, workers, cluster containers, messaging adapters, cron, evolution). Retrofitting JSON logging isn't a one-file change.
2. Requires a **transport decision** (file append vs. broker vs. gRPC vs. sidecar daemon). Each has trade-offs on durability, latency, and ops burden.
3. Correlation IDs need **plumbing at every boundary** — they must be injected at chat-request time and propagated through every subsequent call (task dispatch, tool invocation, inference proxy, response stream).
4. The CLI display layer needs to be **visibly separated from the log layer**. That's a refactor of `logos_cli/gateway.py`'s pretty-printer.
5. **It must be in place BEFORE the next hard debugging session.** Without it, future "workers don't work" or "chats hang" incidents will burn the same hours this one did. M6 is infrastructure debt that pays for itself on the first bug.

**Direction established**: The 2026-04-11 worker-registration debugging session made the case inescapable. See TASKS.md for the accumulated findings from that day (sandbox TunnelWebSocket → gateway register reply is silently dropped somewhere in the NAT path, diagnosis stalled for lack of observability, not for lack of effort).

---

## M7 — Sandbox health observability in the UI

**Status**: PREMISE OUTDATED, needs re-grounding. The original M7 draft assumed the TASKS.md #24 refactor would land as a NemoClaw-style port-forward + HTTP /health probe architecture — under that design, "sandbox health" meant "can the gateway HTTP-GET `/health` on a forwarded port and does the agent reply with `{"status":"ok"}`". **Plan A-prime instead landed as per-task `openshell sandbox exec` subprocesses**, which doesn't have a port-forward or an HTTP endpoint to probe. What constitutes "sandbox healthy" under Plan A-prime is different: the sandbox CR is in `phase=ready` (per `openshell sandbox list`) and the last `dispatch_task` invocation returned a clean `task_result`. The field rename and richer observability goals from M7's "What's missing" list are still valid, but they need to be re-grounded on the per-task-exec model rather than port-forward probes. Low priority until M10 Option D lands — after that, sandbox health will include "can the agent loop inside the sandbox reach its self-directed tools" which IS probeable.

**State today**: The `/admin/agents` endpoint returns two booleans per agent — `worker_connected` and `worker_healthy` — which the UI reads in ~8 places to decide whether to render an agent as "chat-ready" (green pill, drag-enabled, etc.). Under Plan A-prime those booleans now reflect **state-file entry presence + phase=="ready"** (from `_SandboxHealthEntry` in `gateway/worker_registry.py`). The old reverse-WebSocket semantics are gone, the new semantics are "does the state file show this sandbox as Ready". M8 Phase A added `active_tasks` as a third field that actually reflects real-time activity.

**What's scaffolded**:
- `/admin/agents` endpoint already returns per-agent status; the shape is well-known and consumed by 8 UI sites.
- M6 unified logging (shipped) already captures structured records, so a historical latency chart is one query away if probe events start getting emitted.
- `WorkerRegistry._active_tasks` (from M8 Phase A) gives us the first real "is this agent currently doing something" signal.

**What's missing**:
1. **Rename the fields** from `worker_connected` / `worker_healthy` to `sandbox_reachable` / `sandbox_api_healthy`. The "worker" vocabulary is a lie after #24 — there is no worker, there's a port-forward + HTTP probe. Clearer naming reduces future confusion.
2. **Add `sandbox_phase`** (from `openshell sandbox get`) — "Ready", "Provisioning", "Error", "Stopped". Lets the UI distinguish "sandbox is still spawning" from "sandbox spawned but /health is failing" from "sandbox was deleted".
3. **Add `api_latency_ms`** — p50 of the last N health probes. Lets users see at a glance which agent's sandbox is slow before they try to chat with it.
4. **Add `last_probe_ts`** so the UI can show "checked 2s ago" / "stale" / "never probed" states instead of a boolean that silently ages.
5. **Add `api_version`** — whatever Hermes reports on `/health` or `/v1/models`. Surfaces version drift across sandboxes (useful during upgrade rolls).
6. **Update all UI call sites** to read the new field names. ~8 places in `main_app.html` — each is a one-line find-and-replace plus optionally a new pill rendering the latency/phase.
7. **Add a dedicated health tile to Admin → Sandboxes** that shows a per-sandbox mini-dashboard: phase + latency sparkline (fed by M6's `~/.logos/logs/unified.jsonl` filtered on `event=sandbox_probe`) + last-N failed probes with reason.

**Why it's architecturally large**:

1. Field rename touches every UI consumer (~8 files/blocks) plus the admin handler plus any tests. Backwards-compat matters if external tooling reads `/admin/agents`.
2. Requires a **probe-result event stream** emitted to `unified.jsonl`, not just the current boolean flip-flop. That's a small but real backend addition.
3. The sparkline + "checked 2s ago" UX is a meaningful visual design exercise, not just a field rename.
4. Integration with M6 is load-bearing — the richer UI pulls live data from the unified log, which has to be a first-class consumer API, not just a file humans grep.

**Dependency**: Must land **after** TASKS.md #24 is stable (Approach A is deployed and user-validated). Don't try to combine them — the refactor is already large and the UI rename would thrash 8 unrelated files during the hard transport swap.

**Direction established**: 2026-04-11 session — user raised the question during the TASKS.md #24 planning: *"if we don't have workers anymore do we change the UI for sandboxes to give a different indication that its working?"* — a good instinct. Approach A defers the rename; M7 plans the proper upgrade.

---

## M8 — Dispatch activity ledger

**Status**: PHASE A SHIPPED (commit `bbee66a`), PHASE B NOT STARTED. Verified: `gateway/worker_registry.py:139` has `self._active_tasks: Dict[str, int]`, `dispatch_task` increments/decrements, `admin_handlers.handle_agents_list` emits `active_tasks` on each agent record, `gateway/world/AgentSprite.js:_updateBubble` renders the 💭 bubble. The `dispatches` table, per-caller origin tagging, and Admin → Activity tab are all still pending.

**State today**: Phase A of the dispatch-activity work shipped — `WorkerRegistry` tracks an in-memory `_active_tasks` counter that goes up when `dispatch_task` enters and down when it exits, `admin_handlers.handle_agents_list` surfaces it as `active_tasks` on each agent record, and the world-view Phaser `AgentSprite` renders a 💭 thought-bubble with a subtle scale pulse whenever `active_tasks > 0`. The user can now see at a world-view glance "Tali is thinking about something right now" without guessing.

**What's missing (Phase B — the durable ledger)**:

1. **A `dispatches` table** in `auth.db`:
   ```
   id TEXT PRIMARY KEY
   agent_id TEXT           -- who was dispatched (hermes-<name>)
   route_id TEXT           -- model_routes.id at dispatch time
   model TEXT              -- the model string (denorm for easy group-by after route edits)
   origin TEXT             -- 'user_chat' | 'platform:discord' | 'cron' | 'workflow:<id>:<step>' | 'delegate:<parent_agent>'
   origin_detail TEXT      -- free-form JSON: user_id, chat_id, workflow_run_id, cron_job_id, etc.
   prompt_tokens INT       -- from the final task_result (if the worker reports it)
   completion_tokens INT
   elapsed_s REAL
   status TEXT             -- 'running' | 'ok' | 'error' | 'timeout'
   error TEXT              -- short error string if status != ok
   started_at INT          -- epoch ms
   ended_at INT
   ```

2. **Wrap `dispatch_task` at the gateway level** (not inside the registry) so the ledger row is written by whoever knows the origin. The registry is origin-agnostic — it shouldn't learn about "did this come from a chat, a cron job, or a platform message"; that's the caller's context. Each of the 4 real dispatch sites (`gateway/http_api.py:_handle_chat`, `gateway/run.py:dispatch_platform_message`, `cron/scheduler.py:run_job`, `workflows/engine.py:_run_agent`) gets a helper that inserts the `dispatches` row at entry, updates it at exit, and calls through to `worker_registry.dispatch_task`. The `delegate_tool.py` path doesn't go through `dispatch_task` at all (in-process thread-pool) — either record it separately with `origin='delegate'` or mark it explicitly out-of-scope for the ledger.

3. **Audit existing code paths** to plumb the right `origin_detail`:
   - `_handle_chat` has access to `request["current_user"]`, agent_id, session_id — trivial.
   - `dispatch_platform_message` has platform name + user_id + chat_id on the `MessageEvent.source` dataclass. Today that's **discarded** before `dispatch_task` is called (see the audit report above) — Phase B is the reason to stop discarding it.
   - `cron/scheduler.py` has the `origin` dict on the job record already — just needs to be passed through.
   - `workflows/engine.py` knows the `run_id` and `step_id` at step dispatch time.

4. **Admin → Activity tab**: new UI page that queries the ledger and shows:
   - Per-agent histogram of dispatches over the last 24h, split by origin
   - "Most-used model" per agent
   - Average elapsed_s per agent per origin (user vs cron is usually a dramatic split)
   - Top errors per agent
   - Filter by origin / agent / date range

5. **Retention policy**: a cron job that prunes rows older than N days (default 30) to keep the ledger cheap. Rollups for longer-term "you made 2,847 dispatches last month" come in Phase C or later.

**Why it's architecturally medium**:

1. New table + migration + CRUD helpers in `auth/db.py` (~50 lines, low risk).
2. Every dispatch site needs a wrapper; care needed to ensure the wrapper is robust to worker errors (don't leave `status='running'` ledger rows after a crash).
3. The wrapper has to be placed at the *caller* not the registry, which means 4 touch sites — small per site but they have to be kept in sync.
4. Rendering per-agent stats in the UI is a new page with a new API endpoint.

**Dependency**: Must land **after** M6 unified log is stable (it is) so the ledger and the log can cross-reference by `task_id` and `agent_id`. No hard ordering against #24 — the refactor already shipped.

**Direction established**: 2026-04-11 session — user observed agents autonomously writing memories mid-chat and asked *"should we have a way of counting how many requests are made by us and land on a model and the agent landing on a model?"* Phase A is the visual indicator; Phase B is the durable data for the analytics/observability story.

---

## M9 — Autonomous agent activity (discoverability, not cadence)

**Status**: NOT STARTED, AND partially blocked on M10. Two separate points here:
1. The three underlying consolidation mechanisms (memory nudge `agent.py:4118`, skill nudge `agent.py:4131`, pre-reset flush `run.py:720`) all exist in code and were verified 2026-04-11. This M-ticket is about making them *visible and consistent*, not about adding new triggers.
2. **The nudges don't actually fire during Plan A-prime chats** because `sandbox_worker.py` doesn't invoke `AIAgent.run_conversation`. Until M10 is fixed, nudges are dead code for web-UI chats. The pre-reset flush still works because it's an in-process path that goes around the sandbox entirely. M9's "visible memory writes" feature depends on memory writes actually *happening* during chats, so M9 is blocked on M10 for the chat path (but not for the flush path).

**State today — two corrections to earlier misreadings of this file**: Logos **already has** a layered autonomous-consolidation system. Three mechanisms, together they cover active-session incremental consolidation AND end-of-session final consolidation — the "cadence" problem I wrote this M-ticket around in the first draft doesn't actually exist. What IS missing is visibility and dispatch-path consistency, not triggers.

The three mechanisms:

1. **In-turn memory nudge** (`agents/hermes/agent.py:4118`). Every N user turns (default 10, configurable via soul's `memory.nudge_interval`), the next user message gets a `[System: Consider whether there's anything worth saving to your memories.]` footer injected before the agent sees it. Counter resets when the memory tool is actually called. Runs through whatever dispatch path the chat turn uses (Plan A-prime sandbox for primary chats). **Completely automatic, completely invisible to the user.**

2. **In-turn skill nudge** (`agents/hermes/agent.py:4131`). Same pattern, default 15 tool-loop iterations. Fires when the previous task involved many tool calls — the signal "there might be a reusable pattern here worth capturing".

3. **Pre-reset memory flush** (`gateway/run.py:720 _flush_memories_for_session`), fires on three triggers (session expiry watcher, `/reset` command, `/resume` command). Spins up a **temporary in-process `AIAgent`** with `enabled_toolsets=["memory", "skills"]` to review the full transcript and save memories/skills before the session is cleared. The injected system turn is word-for-word:

```
[System: This session is about to be automatically reset due to
inactivity or a scheduled daily reset. The conversation context
will be cleared after this turn.

Review the conversation above and:
1. Save any important facts, preferences, or decisions to memory
   (user profile or your notes) that would be useful in future sessions.
2. If you discovered a reusable workflow or solved a non-trivial
   problem, consider saving it as a skill.
3. If nothing is worth saving, that's fine — just skip.

Do NOT respond to the user. Just use the memory and skill_manage
tools if needed, then stop.]
```

An earlier audit of this file wrongly claimed "no agent in Logos ever initiates activity on its own." The audit was looking at `worker_registry.dispatch_task` call sites — the flush mechanism doesn't go through that path (it creates a fresh in-process `AIAgent` instead), and the in-turn nudges live inside `run_conversation` which only grep hit as "it's a Python function, not a scheduled job". Both misreadings are corrected here.

Together, the three mechanisms form a nicely layered system:

| Mechanism | Trigger | Cadence | Dispatch path |
|---|---|---|---|
| Memory nudge | user turn | every 10 user turns | piggy-backs on user chat → sandbox (Plan A-prime) |
| Skill nudge | user turn after long tool loop | every 15 tool iterations | same |
| Pre-reset flush | session expires / `/reset` / `/resume` | one-shot on session end | **in-process on host, bypasses sandbox** |

The nudges handle "incremental consolidation during active use", the flush handles "final consolidation when the session ends". **There is no gap that needs a new nightly-reflection scheduler** — adding one would be duplicative of the nudge mechanism. That's what the original draft of this M-ticket wrongly proposed. What's actually missing is discoverability and dispatch-path consistency.

**What's actually missing** (the real M9 scope):

1. **Discoverability — the whole point of the tamagotchi identity is that you can SEE your agents live.** Today, all three consolidation mechanisms are completely invisible to the user. The nudges inject a system footer into a user turn and the agent's response may or may not include a memory tool call — the user just sees the response, never knows a nudge fired. The flush runs entirely out-of-band, no chat UI surface. No SSE event tagged "self-reflection" in the unified log, no increment on the `active_tasks` counter from M8 Phase A (because the flush bypasses `dispatch_task` and nudges happen inside an already-counted user turn), no thought-bubble animation specific to reflection, no toast notification when a memory is actually written. An agent can be nudged 30 times and save 8 memories across a day and the user has zero indication it's happening.

2. **Architectural divergence on the flush path**: the flush uses `runtime_kwargs` + in-process `AIAgent` (hits the provider API directly on the host network at `$OPENAI_BASE_URL`) while the primary chat path uses `openshell sandbox exec` per-task via `https://inference.local/v1` (Plan A-prime, TASKS.md #24). That means the flush is **NOT subject to** the sandbox's network policy, filesystem isolation, or the worker-registry's activity counter. A reflection that writes memories to `~/.logos/memories/` runs with full host access, while the conversation that triggered the reflection ran in an isolated sandbox. That's a split worth making explicit and deciding about intentionally — either route the flush through the sandbox too for consistency, or document why it runs on the host (speed? no need for isolation since it doesn't execute user code?). Confirmed empirically: the env has `OPENAI_BASE_URL=http://192.168.1.117:1234/v1` (direct LAN LM Studio), and `AIAgent.__init__` does `OpenAI(base_url=...)` + `self.client.responses.stream(...)` from the gateway process.

3. **Memory writes are invisible to the user when they happen**. When `memory_tool` actually persists a new entry to `MEMORY.md` / `USER.md`, nothing surfaces in the chat UI or the world view. Toast notifications, a "💭 Tali saved a new memory: '…'" feed card, or a subtle animation on the agent's sprite — any of those would turn the invisible background work into a visible product moment. This is where most of the "living agent" UX feel actually comes from; without it the mechanism might as well not exist.

4. **World-view affordance during reflection**. M8 Phase A renders a 💭 thought bubble when `active_tasks > 0`, but the flush path never increments that counter (it bypasses `dispatch_task`), so reflections are invisible in the world view too. The nudges DO show the bubble because they happen inside an active user turn that IS counted — but a casual observer can't tell the difference between "agent is thinking about your message" and "agent is handling a nudge within your message". A distinct glyph or color for mid-nudge vs mid-flush vs mid-chat would let you glance at the world and see what KIND of cognition is happening.

**What this M-ticket should actually do**:

1. **Route the flush through `worker_registry.dispatch_task`** so it inherits the sandbox isolation AND increments the `active_tasks` counter. A new `origin="session_flush"` tag (feeding into M8 Phase B's dispatch ledger) lets the ledger distinguish reflection traffic from user chat traffic. If running inside the sandbox is too slow or loses host-local memory file access, document the decision instead and add the counter increment via a different mechanism (a manual counter bump around the in-process AIAgent).

2. **Tag nudges in the ledger**. When M8 Phase B lands, the per-turn dispatch row should carry a `had_memory_nudge: bool` / `had_skill_nudge: bool` flag so the user can query "how often is Tali being nudged, and how often does she act on it?"

3. **Surface memory writes live**. The memory tool handler emits an event whenever it writes a new entry; the chat UI subscribes and shows an inline "💭 Tali saved a memory" card next to the assistant turn that triggered the write. Or a top-level toast. Or a badge on the agent's avatar that clears when you click through. Multiple options, small UX exercise.

4. **Distinct world-view glyph/color for reflection vs chat** (polish on top of M8 Phase A).

5. **Safety / cost guards**: Admin → Settings pause-all switch for the flush path; per-agent opt-out on nudges (soul config already supports `nudge_interval: 0` to disable, but no UI for it); dry-run mode that captures proposed memory writes as evolution proposals instead of auto-applying them.

**Why it's architecturally medium**:

1. Routing the flush through `dispatch_task` is a real decision point (sandbox vs host) but the code change itself is small.
2. The visible-when-written feature is the bulk of the UX work — event plumbing from `memory_tool` → SSE → chat UI.
3. The ledger integration is trivial once M8 Phase B exists.
4. No new cadences to design. No new scheduler. The triggers are all already there.

**Dependency**: Phase A of M8 shipped (active_tasks counter + thought bubble). **Must land after M8 Phase B (dispatch ledger)** so reflections are distinguishable from user traffic in the analytics layer.

**Direction established**: 2026-04-11 session — user observed qwen reasoning mid-flush in LM Studio and asked about it, exposing the flush mechanism. Same user then correctly pointed out that Hermes already has in-turn memory/skill nudges built in and rejected the "add nightly cadence" premise of the first draft of this ticket. This corrected version scopes M9 to discoverability, dispatch-path consistency, and user-visible memory writes — NOT to adding new triggers.

---

## M10 — Plan A-prime bypasses the Hermes agent loop entirely

**Status**: NOT STARTED. Discovered 2026-04-11 during M9 scoping. The most architecturally significant finding in the doc — blocks M1, M2, M9's chat-path discoverability, and the tamagotchi product identity generally. Recommended fix: Option D below (agent inside, action tools outside, STAMP-gated bridges). Estimated 3-5 days of focused work.

**State today — a fundamental finding surfaced while scoping M9**: Plan A-prime's `docker/sandbox_worker.py` is a **naive chat-completion forwarder**. It builds `messages = [system(context_prompt), ...history, user(message)]`, sends `{model, messages, stream, max_tokens}` to `inference.local/v1/chat/completions`, and streams back `delta.content` + `delta.reasoning_content` as token/thinking events. **There is no `tools` field in the payload, no `tool_choice`, no `tool_calls` handling in the response loop, and no invocation of `AIAgent.run_conversation` anywhere in the sandbox path.**

**Concrete implications for every primary chat** (user sends message to Tali/Grace via the browser):

| Feature | Active? |
|---|---|
| Soul text as system prompt | Yes (via `context_prompt`) |
| Conversation history | Yes |
| Reasoning content streaming (`reasoning_content`) | Yes |
| `memory_tool` — any call | **No** (not in payload, no handler) |
| `skill_manage` / `skills_list` / `skill_view` | **No** |
| `memory_nudge` (every 10 user turns, `agent.py:4118`) | **No** — fires inside `AIAgent.run_conversation`, which the worker never invokes |
| `skill_nudge` (every 15 tool iterations) | **No** — same |
| `delegate_tool` (sub-agent spawn) | **No** |
| `terminal_tool` (shell exec) | **No** |
| `browser_tool` | **No** |
| `schedule_cronjob` / `list_cronjobs` / `remove_cronjob` | **No** |
| `knowledge_search` / `knowledge_add` | **No** |
| Every other tool declared in `tools/` | **No** |
| RunRecorder / `agent_runs` table writes | **No** (recorder init lives inside `AIAgent.run_conversation`) |
| Workspace TTL cleanup trigger | **No** |

The full `AIAgent` class (`agents/hermes/agent.py`, ~4500 lines) only runs in three places:
1. `_flush_memories_for_session` — session expiry / `/reset` / `/resume`, in-process on the host (bypasses sandbox — see M9)
2. Direct CLI invocations (`hermes` at the terminal — out-of-scope for the web UI)
3. Legacy paths that predate Plan A-prime and may be partially dead

**What this means concretely**: the "Hermes" your agents present as is currently *just the soul prompt in a system message followed by a bare LM Studio chat*. No tool use. No memories written during chats. No self-improvement. No delegation. No workspace management. No knowledge search. The entire multi-thousand-line agent loop is sitting unused for every single web-UI chat you have.

This **was not the case** pre-Plan-A. The old reverse-WebSocket worker design ran the full `AIAgent` inside the sandbox — the sandbox was the home of the agent loop, not just a chat-completion proxy. Plan A and Plan A-prime both stripped that out to get the transport working, with the intention of adding it back. It wasn't added back.

**How we got here** (for the record so this doesn't get mis-blamed on a later hand): the original Plan A and Plan A-prime refactors intentionally kept `sandbox_worker.py` minimal — a single `_run_inference` call — because the priority was "make the dispatch transport work at all". The idea was to restore full agent functionality after the transport was proven. The transport IS proven now. This is that restoration work.

**Four paths forward**, with Option D as the recommended choice after the user articulated the trust-boundary model it codifies:

### Option D — Agent inside, action tools outside, STAMP-gated bridges (RECOMMENDED)

**The principle**: what crosses the sandbox boundary is an explicit, user-governed decision — not an implicit consequence of where code happens to live. The sandbox IS the permission boundary. STAMP's T and P axes become functional governance instead of decorative labels.

**What lives inside the sandbox**:
- `AIAgent.run_conversation` and its tool-loop
- Self-directed tools (the ones the agent uses to grow and remember): `memory_tool`, `skill_manager_tool`, `skill_view`, `skills_list`
- Memory/skill files persist on the sandbox pod filesystem
- Soul, reasoning, context, conversation state, run recorder

**What lives outside the sandbox**:
- Action tools that touch the real world: `terminal_tool`, `browser_tool`, `delegate_tool`, `knowledge_search`, `knowledge_add`, `schedule_cronjob`, `platform_send`, `home_message`, any future "do something external" tool
- These execute on the host, gated by STAMP-T (grant list per agent) and STAMP-P (approval policy per tool)

**How the bridge works**: the stdin/stdout JSON protocol grows three new message types:

| Direction | Type | Purpose |
|---|---|---|
| sandbox → gateway | `tool_request` | "Execute `terminal_tool('ls -la')` on the host and return the result" |
| gateway → sandbox | `tool_grant` | "Approved — here's the result" (delivered via stdin) |
| gateway → sandbox | `tool_denied` | "User denied / policy rejected" (via stdin) |

Gateway-side handling:
1. Reads `tool_request` from subprocess stdout (interleaved with token/thinking/task_result events)
2. Checks the STAMP-T grant list for this agent — is the requested tool even in the granted set?
3. Checks `action_policies` (STAMP-P): auto / require-approval / deny
4. If require-approval: writes a row to `approval_requests`, emits an SSE event to the chat UI, blocks on user response
5. On approval: executes the tool on the host, captures output
6. Writes `tool_grant` (or `tool_denied`) back to the subprocess stdin
7. The agent's in-sandbox tool proxy receives the result and continues its loop

The agent inside the sandbox has **proxy implementations** of each host-side tool — same function signature, but instead of executing locally they serialize the call and `await` a result on stdin. Transparent to the agent loop.

**STAMP mapping** (why this is the right architecture):

- **S (Soul)** — stays inside the sandbox as it does today
- **T (Tools)** — now has teeth. The STAMP-T pill becomes an editable grant list, user decides which tools cross the boundary. **First time T is a real governance axis and not a decorative label.** M1 in this file ("editable T in the STAMP pill") becomes concretely buildable.
- **A (Agent)** — lives inside, run history writes to a sandbox-local DB or syncs back periodically
- **M (Model)** — same as today, sandbox calls `inference.local` for inference
- **P (Policy)** — now has teeth for action tools: per-tool approval policy wired into the existing `approval_requests` table. M2 ("editable P in the STAMP pill") also becomes concretely buildable.

This is the version where STAMP is the user's real governance interface for the sandbox trust boundary, not just vocabulary.

**Persistence model for memory/skill files** (inside-the-sandbox writes that need to survive pod destruction):
- Memories/skills write to `/tmp/hermes/memories/` + `/tmp/hermes/skills/` inside the pod
- Survive across dispatches naturally (pod runs `sleep infinity`, filesystem persists)
- Do NOT survive pod destruction or gateway restart without sync-back
- **Gateway sync-back daemon**: periodically calls `openshell sandbox download` to pull the files out to `~/.logos/memories/<agent_name>/`, canonical copy on the host. On pod re-create, `openshell sandbox upload` restores them. The pod is the live copy during a session; the host is the durable backup. This sync-back is itself a controlled boundary crossing — exactly the kind of thing the Option D trust model is designed to make explicit.

**Pros**:
- Correct trust boundary: compromise containment for the agent, user governance for real-world actions
- Makes STAMP's T and P axes functional, unblocks M1 and M2
- Memory/skill writes happen during chats (M10's core fix)
- Action tools stay on the host where they have the filesystem and network access they need
- User has explicit control over what tools each agent can touch — aligns with the multi-user / multi-agent product identity

**Cons**:
- Largest lift of the four options — 3-5 days spanning sandbox image, protocol extension, agent rewrite, sync-back daemon, and STAMP UI work
- The protocol extension is the biggest risk: interleaving `tool_request` frames with the existing token/thinking/task_result stream needs careful testing against concurrent dispatches
- Sync-back has edge cases (crashed pod mid-write, two dispatches touching the same memory file, gateway restart during sync)

**Scope breakdown** (~3-5 days):
1. Extend `docker/Dockerfile.hermes-sandbox` to include `agents/hermes/agent.py` + `tools/memory_tool.py` + `tools/skill_manager_tool.py` + their imports (pure Python, manageable)
2. Rewrite `docker/sandbox_worker.py` as a thin bootstrap: load config, instantiate `AIAgent` with proxy tools for the action surface, call `run_conversation`, emit events to stdout
3. Extend the stdin/stdout JSON protocol with `tool_request` / `tool_grant` / `tool_denied` types. Update the protocol doc.
4. Extend `gateway/worker_registry.py dispatch_task` to handle `tool_request` messages from stdout: check STAMP-T grants, route through `action_policies`, block on user approval when required, execute the tool on the host, reply via the subprocess stdin
5. Build the tool proxy framework inside the sandbox: a base class that serializes `tool_request` and awaits a `tool_grant`/`tool_denied` on stdin
6. Build the STAMP-T grant editor UI (closes M1)
7. Build the STAMP-P approval-policy editor UI (closes M2)
8. Sync-back daemon for memory/skill files (periodic `openshell sandbox download` → `~/.logos/memories/<agent>/`)

**Dependency**: Nothing else blocks this. M1, M2, M9 all become concretely buildable AFTER M10-via-Option-D lands because they depend on the STAMP grant/policy system having teeth.

---

### Option A: Full AIAgent in the sandbox (architecturally correct, biggest lift)

Replace `sandbox_worker._run_inference` with `AIAgent.run_conversation`. Import `AIAgent` and its dependencies into `Dockerfile.hermes-sandbox`. Every tool the agent might call needs to be available inside the sandbox (filesystem-wise and import-wise). The agent runs fully inside the pod; tool calls execute inside the pod; memory writes go to paths inside the pod and need to sync back to the host for persistence.

- **Pros**: architecturally pure. Tool calls are truly isolated. Matches the pre-Plan-A design.
- **Cons**: large scope. Every tool needs to be shipped into the sandbox image (current image is minimal: Python + aiohttp). Memory/skills need persistent mount or sync-back. Workspace paths inside the sandbox vs host filesystem diverge. The sandbox image balloons from ~250MB to probably >1GB. Cold-start time on first spawn jumps significantly.
- **Estimated scope**: 2-3 days of careful work. High risk of "now we have tool-call bugs in the sandbox environment" follow-ups.

### Option B: AIAgent in the gateway, inference via sandbox (hybrid, pragmatic)

Run `AIAgent` in the gateway process (like `_flush_memories_for_session` already does). Tool calls execute on the host. When the agent needs to call the LLM, route that call through the sandbox — either via the existing `openshell sandbox exec` per-task path (the agent becomes a gateway-side loop that farms each chat-completion call out to the sandbox for isolation), OR by having the agent hit `https://inference.local/v1` directly from the host (bypassing the sandbox entirely, like flush already does).

- **Pros**: restores full agent functionality fast. Tool calls inherit the gateway's filesystem access so `memory_tool`, `skill_manager_tool`, `workspace`, etc. all just work. No sandbox image bloat. Low cold-start cost. Reuses the proven `_flush_memories_for_session` pattern.
- **Cons**: tool execution is on the host, not sandboxed. If the user wanted the sandbox to be a security boundary for tools (terminal, browser, filesystem), this path doesn't provide that. The sandbox becomes essentially a proxy for inference calls only — its isolation becomes cosmetic for the chat path.
- **Estimated scope**: 1 day. The machinery is already built (`_flush_memories_for_session` proves the shape works).

### Option C: Add a tool-loop to `sandbox_worker.py` without pulling in full AIAgent (middle ground)

Keep `sandbox_worker.py` lightweight but extend `_run_inference` to parse `tool_calls` from the LM Studio response and execute a whitelist of sandbox-safe tools inline (memory_tool, skill_manage). Keep terminal/browser/delegate out of scope for the sandbox. Send `tools=[...]` in the payload so the model can actually request tool use.

- **Pros**: sandbox stays slim. Memory and skill consolidation work during chats (the user's original expectation). No host-side agent loop. Scope-bounded.
- **Cons**: reimplements a fraction of `AIAgent.run_conversation` in `sandbox_worker.py` — duplication risk. Any new tool the user wants in chats needs to be ported over. The "full Hermes experience" is partially delivered — the most-important tools work but the long tail doesn't. Memory_tool and skill_manage need to be importable inside the sandbox image (they're both pure Python so that's easy).
- **Estimated scope**: ~1 day. Well-bounded. Doesn't preclude doing Option A or B later.

**Recommended tackling order** (updated after user framing): **Option D**. A, B, and C are all weaker answers to the same question — they compromise the trust boundary in different ways. Option D maps the split onto the user's actual mental model (sandbox = agent self, host = real-world actuators, STAMP = governance of the bridge) and is the architecture that makes the multi-user, multi-agent product identity work correctly. It's the biggest lift but it's also the one that doesn't leave something important broken.

**Dependency**: Must land before M9 (visible memory writes). M9 depends on memory writes actually *happening* during chats, which requires this restoration. M1 (editable STAMP-T) and M2 (editable STAMP-P) become concretely buildable *after* Option D because they depend on the grant/policy system having runtime teeth.

**Direction established**: 2026-04-11 session — user asked to ship "visible memory writes" and during scoping I discovered that memory writes don't happen during chats at all because the sandbox worker doesn't run the agent loop. I presented three options (A: full agent in sandbox, B: agent on host, C: minimal tool loop in sandbox). User responded with the correct fourth framing: "from a protective save policy drive perspective it's probably better to have the entire agent on the inside. But with regards to tooling, apart from ones the agent needs to improve and remember, tools to act should probably be available on the outside and confirmed by users to be given to the agent. Part of the STAMP model." That framing became Option D above, and supersedes the earlier recommendation of Option B.

---

## Relationships

```
M6 (unified logs) ══════════→ DONE — MVP shipped, used daily now
                        └──→ unblocks every debugging session (already paid off)

TASKS.md #24 (chat transport) → DONE — Plan A-prime shipped end-to-end

M8 Phase A (active_tasks + bubble) → DONE — world view reflects live activity

─── blockers remain below this line ───

M10 (sandbox_worker ⟷ AIAgent) ══╦══→ Option D: agent inside, tools outside,
                                  ║     STAMP-gated bridges
                                  ║
                                  ╠══→ BLOCKS M1 (STAMP-T has no runtime teeth
                                  ║                until Option D lands)
                                  ╠══→ BLOCKS M2 (STAMP-P has no runtime teeth
                                  ║                until Option D lands)
                                  ╚══→ BLOCKS M9 chat-path visibility
                                        (memory writes don't happen during
                                         chats without the agent loop)

M1 (T editable) ─┐
                 ├──→ depend on M10 Option D
M2 (P editable) ─┘

M3 (per-user routing) ──→ M4 (multi-user polish) [M3 is the infrastructure M4 needs]
                      └──→ fixes the dupe-surface accident root cause

M5 (world as surface) ──→ extends pass 3 S2 (CRUD slide-out)
                      ──→ Phase A (M8's thought bubble) already ships the
                          "live agent state" half

M7 (sandbox health UX) ──→ premise needs re-grounding on Plan A-prime's
                            per-task exec model, not the old port-forward design
                      ──→ depends on M6 (shipped — log-stream as data source)
                      ──→ depends on M8 Phase A (active_tasks is the first
                          real "is agent busy" signal — shipped)

M8 Phase B (ledger) ──→ depends on M6 (shipped)
                    ──→ unlocks M9 analytics surface

M9 (activity visibility) ──→ depends on M10 Option D (for chat-path visibility)
                         ──→ depends on M8 Phase B (for ledger tagging)
                         ──→ the tamagotchi / living-agent identity feature
```

**Recommended tackling order (updated end of 2026-04-11)**:

**M10 (Option D) → M1 + M2 (both become buildable after Option D) → M8 Phase B → M9 → M3 → M4 → M7 → M5 (full-tab world)**

Rationale: **M6 is already done** and paid for itself multiple times today. **TASKS.md #24 is already done** — Plan A-prime ships end-to-end. **M8 Phase A is already done** — the thought bubble makes live agent state visible for the first time. The next focused chunk is **M10 via Option D**, because it unblocks THREE other tickets (M1, M2, M9's chat-path discoverability) and is the architecture that makes STAMP's T/P axes real governance. After Option D lands, M1 and M2 become concrete UI work; after that, the dispatch ledger (M8 Phase B) and visible memory writes (M9) are the next natural cluster. Multi-user (M3 → M4) and world-view polish (M5 full-tab) can run in parallel with the Option D work or follow after. M7 needs re-grounding before it can move forward — its original premise (NemoClaw port-forward + HTTP probe) was superseded by Plan A-prime's per-task exec model.

---

## Navigation consolidation — not yet scoped as an M-ticket

**State today**: The 5-tab navbar (Agents, Chats, Compare, Settings, Admin) was audited in `docs/audit/pass3_ui_audit.md` and judged to "mostly work" against the 8-domain model sketched in pass2. The audit focused on:

- **S1** (shipped): removed dupe surfaces from Settings — deleted Routing sub-tab, moved Benchmark into Inference, moved Debug into Admin → Model Routes
- **S2** (pending): Agents tab CRUD slide-out so the world breathes full-width
- **S3/S4** (pending, = MISSING.md M3/M4): multi-user polish

**What the audit did NOT cover** — and what's been raised since:

1. Whether **Admin → Sandboxes** and **Admin → Model Routes** should merge into a single "Dashboard" page. Today they're sibling sub-tabs inside Admin that are obviously related (sandboxes are the things that run inside routes) but render as two separate tables. A Dashboard that shows "routes → sandboxes → workers → dispatches" as a single nested view would compress two sub-tabs into one and make the containment relationship visually obvious.

2. Whether **Admin → Security** (which internally is `adminTab='action-policies'`) is a subsystem that belongs at the Admin top level or should be demoted / merged into Settings. The audit noted action-policies are "subsystems not settings" but didn't recommend a move.

3. Whether the **Compare tab** should be a top-level navbar item at all, or a mode you toggle inside Chats. Right now Compare is the 3rd of 5 tabs, which gives it equal weight to the core Chats flow — but it's used far less often and only makes sense once you have ≥2 agents. Moving it to a "Compare mode" button inside Chats frees a top-level slot.

4. Whether there should be a new top-level **Dashboard** tab that replaces Admin's current tabular sub-tabs with a single live overview page (active agents, active tasks, recent dispatches, route health) — the home-base for a running Logos install.

This isn't in the M-series because it's UX direction, not a missing capability. When it's ready to execute it should become M10 (or get folded into M5 "world as first-class surface", which already has a claim on the navbar-consolidation space). Concrete proposals welcome — the audit deliberately stopped short of this layer because pass3 judged the current navbar "good enough to unblock pass 3's punch list".
