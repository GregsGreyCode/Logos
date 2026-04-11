# Missing — large architectural gaps

**Purpose**: Features that are scaffolded in the data model and partially started, but **not built out to a usable state**. This is a standing doc — not audit-scoped, not tied to any one session.

**When to add**: Something large is architecturally scaffolded (tables exist, FK relationships are in place, some code paths are written) but the UI / control surface is missing or incomplete, AND the gap is big enough to need planning rather than a quick fix.

**When NOT to add**:
- Small bugs → [TASKS.md](../TASKS.md)
- Speculative future features with no current scaffolding → out of scope
- UI polish (rename, rearrange, small flows) → audit punch list, not this

**Established**: 2026-04-11 during the UI audit pass 3. Originally surfaced from cross-referencing [pass1_db_inventory.md](audit/pass1_db_inventory.md) against [pass1_ui_inventory.md](audit/pass1_ui_inventory.md).

---

## M1 — Editable Tools (T) in the Chats STAMP pill

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

**State today**: 960px Phaser canvas lives in the Agents tab, sharing space with the Create-Agent form. Canvas shrinks (16rem) when the form is open, grows (24rem) when closed. Agent sprites walk around the world with a real local-time day/night cycle (commit `24e3ad8`).

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

**State today**: The `/admin/agents` endpoint returns two booleans per agent — `worker_connected` and `worker_healthy` — which the UI reads in ~8 places to decide whether to render an agent as "chat-ready" (green pill, drag-enabled, etc.). Those booleans currently reflect **WebSocket reverse-connection state** (from the `WorkerRegistry` keyed on `sandbox_name`). When the TASKS.md #24 refactor lands, they'll be redefined to reflect **port-forward reachability + HTTP /health probe** under the same names (Approach A — zero UI churn) so the refactor doesn't block on UI work.

Approach A is the minimum; **M7 is the follow-up that makes the sandbox health surface actually informative for users**.

**What's scaffolded**:
- `/admin/agents` endpoint already returns per-agent status; the shape is well-known and consumed by 8 UI sites.
- M6 unified logging (shipped) already captures probe-response events as structured records, so a historical latency chart is one query away.
- NemoClaw's `agents/hermes/manifest.yaml` already specifies the health probe contract (`GET http://localhost:8642/health → {"status":"ok","platform":"hermes-agent"}`) and timeout (90s).

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

**What's missing**:

1. **A reflection scheduler**. Periodically wakes each agent and dispatches a synthetic "system" turn that asks the agent to review its recent conversations, extract durable learnings, write memories, and optionally flag evolution proposals. Shape is basically:
   ```python
   async def run_reflection_cycle(agent):
       last_n_turns = load_recent_chat_turns(agent.id, hours=24)
       if not last_n_turns: return   # nothing to reflect on
       task = {
           "type": "task",
           "task_id": new_uuid(),
           "message": REFLECTION_PROMPT_TEMPLATE.format(turns=last_n_turns),
           "history": [],
           "context_prompt": agent.soul_prompt + REFLECTION_SOUL_SUFFIX,
           "_internal_origin": "self_reflection",
       }
       await worker_registry.dispatch_task(agent.sandbox_name, task, ...)
   ```

2. **A cadence model**. Options:
   - Fixed interval (every N hours) — simplest, boring
   - Activity-triggered (run N minutes after the last user turn in a session) — reflects fresh context
   - Nightly (one reflection per agent per day at 3am) — matches human rhythm, cheapest
   - User-configurable per agent (soul manifest has `reflection_cadence: hourly|nightly|never`)
   
   Probably start with "nightly, opt-out per agent" and expand from there.

3. **Reflection prompt template** — separate concern from the dispatch machinery. Needs to be authored with care: should reinforce the agent's persona/soul, summarize what happened, ask for takeaways, authorize memory writes. Likely lives in `souls/<name>.md` or a shared `REFLECTION.md` skill.

4. **Dispatch origin integration**. Reflection dispatches must be tagged `origin='self_reflection'` in the Phase B ledger so the user can see "Tali self-reflected 7 times this week" as distinct from "Tali answered 42 user messages". Without the ledger, reflections vanish into the same SSE firehose as chats and the user can't tell they're happening.

5. **World-view visual affordance**. When an agent is in a reflection cycle, the thought bubble from M8 Phase A should render with a distinct glyph (🌙 for nightly reflection?) or color so the user can tell "Tali is thinking *about her memories*, not about a user message". Pure UX polish on top of the counter.

6. **A memory-write channel that's visible to the user**. If the agent writes a new memory during reflection, the user should SEE that happen — a toast notification, a notification dot on the agent's avatar, a "Tali wrote 2 new memories" card in a feed somewhere. Otherwise reflection is invisible and might as well not be happening, UX-wise. This is the whole *point* of the tamagotchi identity — you watch your agents grow.

7. **Safety / cost guards**. Reflection is an LLM call that costs compute (even on local LM Studio) and writes durable state. Guards:
   - Global "pause all reflection" switch in Admin → Settings
   - Per-agent budget ("no more than N reflections per day")
   - Dry-run mode that captures the proposed memory writes as evolution proposals instead of auto-applying them

**Why it's architecturally large**:

1. Needs an entirely new cron-like scheduler that isn't the current `cron/scheduler.py` (that one is user-authored one-off jobs, not a periodic system loop).
2. Requires a new "internal" dispatch origin the frontend and ledger both understand.
3. The reflection prompt design is a product-shaping decision, not a plumbing decision — it changes how the agent thinks of itself and what memories it forms.
4. Touches soul manifests, memory write path, evolution_proposals table, notification UI — it's a cross-cutting feature that spans most of the app.
5. User-facing discoverability (M9 #6) is where most of the UX work lives — make reflection feel like the agent *living*, not like a hidden backend cron.

**Dependency**: Must land **after** M8 Phase B (the dispatch ledger). Without a way to tag and count self-reflections, the feature is invisible to the user and no analytics can tell it from user traffic. Strong preference for Phase C to be scoped as its own focused session, not piggy-backed onto a plumbing commit.

**Direction established**: 2026-04-11 session — user observation that the tamagotchi / living-agent identity requires autonomous agent behavior, currently impossible. This is the M-feature that most directly shapes the product identity of Logos vs other agent frameworks.

---

## Relationships

```
M1 (T editable) ─┐
                 ├─→ both feed the Chats STAMP pill becoming fully interactive
M2 (P editable) ─┘

M3 (per-user routing) ──→ M4 (multi-user polish) [M3 is the infrastructure M4 needs]
                      └─→ fixes the dupe-surface accident root cause

M5 (world as surface) ──→ extends pass 3 S2 (CRUD slide-out)

M6 (unified logs) ──→ unblocks every future debugging session
                 ──→ prerequisite for operating M3+M4 at multi-user scale

M7 (sandbox health UX) ──→ depends on TASKS.md #24 refactor (port-forward + /health probe)
                      ──→ depends on M6 (log-stream as data source for latency sparklines)
                      ──→ makes the new architecture self-documenting in the UI

M8 (dispatch ledger) ──→ Phase A shipped (in-memory counter + world thought bubble)
                     ──→ Phase B depends on M6 (ledger cross-references log by task_id)
                     ──→ Phase B unlocks M9 (self-reflection needs the ledger to be
                         distinguishable from user chats)

M9 (self-reflection) ──→ depends on M8 Phase B
                     ──→ the tamagotchi / living-agent identity feature
                     ──→ product-shaping, not plumbing — scope as its own session
```

**Recommended tackling order**: **M6 → #24 refactor → M7 → M3 → M4 → M2 → M1 → M5 → M8 Phase B → M9.**

Rationale: **M6 goes first** because it pays for itself the next time anything breaks, and because every subsequent M depends on being able to reason about what happened across components. **TASKS.md #24** (the NemoClaw-pattern refactor) unblocks chat end-to-end and forces the sandbox transport question. **M7** then accurately surfaces the new transport's health in the UI. Then M3 unlocks M4; M2 is user-visibility-critical once M4 is real; M1 is the last polish on the STAMP pill once everything else is in place; M5 is the long arc that benefits from everything else first. **M8 Phase B** lands after the UI polish cluster because the ledger is lower-stakes than the interactive surfaces and can be retrofitted without disrupting anything already shipped. **M9** is last because it's the most product-shaping feature and deserves the richest context — it's also the one most likely to reveal further infrastructure needs once prototyped.

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
