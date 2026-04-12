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
| M1 — Editable Tools (T) in STAMP pill | **UI SHIPPED, RUNTIME BLOCKED ON M11** | T pill dropdown UI shipped as part of M10 Phase 1 item 5 (2026-04-12) — lists toolsets + network policy presets, writes to DB, pushes policy changes via `openshell policy set`. Infrastructure-layer presets take runtime effect today. Application-layer toolset toggles land on `agents.toolsets` but won't actually gate tool invocations until M11 ships an in-sandbox agent that honors `enabled_toolsets`. |
| M2 — Editable Policy (P) + approvals | **NOT STARTED** | P pill is a display-only `<span>` at `main_app.html:639`. Blocked on M11 (need the in-sandbox agent first) + M10 Phase 1 items 6-7 (Policy editor UI + in-sandbox approval callback, not yet built). |
| M3 — Per-user inference routing | **NOT STARTED** | `machine_users` table exists, zero UI. No claimMachine JS, no My Inference tab. |
| M4 — Multi-user UX polish | **NOT STARTED** | No owner badges, no shared toggle, no per-user chat filter. |
| M5 — World view first-class surface | **PARTIALLY SHIPPED** | Phase A thought bubble from M8 landed (`AgentSprite._updateBubble` — 💭 scale-pulse when `active_tasks > 0`). Full-tab / click-to-enter-chat / multi-user world still pending. |
| M6 — Unified observability | **DONE (MVP)** | `JsonRedactingFormatter` + `_SessionFilter` + `set_log_context` in `gateway/run.py`. `unified.jsonl` actively writing. `logos debug tail` CLI works. 4 of 5 minimum-viable items landed. |
| M7 — Sandbox health UX | **PREMISE OUTDATED** | Original premise assumed a NemoClaw port-forward + HTTP /health probe. Plan A-prime uses per-task `openshell sandbox exec` instead, so the rename/probe design needs to be re-grounded on what Plan A-prime actually offers as a health signal. Field rename and richer observability goals still valid. |
| M8 — Dispatch activity ledger | **PHASE A SHIPPED, PHASE B NOT STARTED** | `WorkerRegistry._active_tasks` counter, `admin_handlers` surfaces it, world view renders thought bubble. `dispatches` table, origin tagging, Admin → Activity tab still pending. |
| M9 — Autonomous activity (visibility) | **NOT STARTED** | The three consolidation mechanisms exist (memory nudge + skill nudge + pre-reset flush) but are invisible in the UI. Memory writes during chats don't actually happen today because M10 blocks them. |
| M10 — Restore AIAgent inside the sandbox | **PARTIALLY SHIPPED, ITEMS 1-3 REVERTED** | Phase 1 items 4-5 shipped on 2026-04-12 (network policy presets + `gateway/policies.py` management module + per-agent `applied_presets` DB column + admin Tools endpoints + T pill dropdown UI — all agent-runtime-agnostic, stay in place). Items 1-3 (Dockerfile + sandbox_worker.py rewrites + SOUL.md/memories upload changes) **reverted during the build-test cycle** — first build produced a 4.26 GB image because `COPY . /app/` + `pip install -e ".[messaging]"` bundled the entire Logos Python package (gateway/, logos_cli/, cron/, acp_adapter/) into the sandbox, which is both bloated and an architectural error (host-side code inside the security boundary). **Superseded by M11** — image-per-agent-release pattern. |
| M11 — Agents as versioned drop-in sandbox images | **NOT STARTED** | Replaces M10 items 1-3. Sandbox image is a versioned upstream agent runtime (Hermes, Claude Code, OpenClaw, etc.), referenced by tag, **not bundled by Logos**. Proves one agent works end-to-end; multi-agent then comes free from spawning a second sandbox. Narrow 2-3 day scope for the proof-of-concept. |

**Recommended next execution target**: **M11 — prove one full agent runs inside a sandbox from a versioned drop-in image.** The M10 Phase 1 build-test cycle on 2026-04-12 surfaced that bundling Logos's Hermes fork into the sandbox via `pip install -e .` was the wrong architectural direction — the right shape is the NemoClaw-style "sandbox image = the agent runtime release, Logos references it by tag." M10 Phase 1 items 4-8 (network policies, DB, editor UI) stay intact because they're infrastructure-layer, not runtime-layer. **Unblocks the runtime half of M1 / M2 / M9.**

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

**Status**: NOT STARTED. Verified: P pill is `<span data-testid="stamp-p">` at `gateway/html/main_app.html:639`, display-only, no click handler, no slide-out. Blocked on M10 items 1-3 + 7 (restore in-sandbox agent loop so `action_policy` is consulted, plus the in-sandbox approval callback for per-tool-call gating) to give per-tool approval enforcement a runtime path.

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

**Status**: PREMISE OUTDATED, needs re-grounding. The original M7 draft assumed the TASKS.md #24 refactor would land as a NemoClaw-style port-forward + HTTP /health probe architecture — under that design, "sandbox health" meant "can the gateway HTTP-GET `/health` on a forwarded port and does the agent reply with `{"status":"ok"}`". **Plan A-prime instead landed as per-task `openshell sandbox exec` subprocesses**, which doesn't have a port-forward or an HTTP endpoint to probe. What constitutes "sandbox healthy" under Plan A-prime is different: the sandbox CR is in `phase=ready` (per `openshell sandbox list`) and the last `dispatch_task` invocation returned a clean `task_result`. The field rename and richer observability goals from M7's "What's missing" list are still valid, but they need to be re-grounded on the per-task-exec model rather than port-forward probes. Low priority until M10 items 1-3 land — after that, sandbox health will include "can the agent loop inside the sandbox reach its self-directed tools" which IS probeable.

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

## M10 — Restore `AIAgent.run_conversation` inside the OpenShell sandbox

**Status**: NOT STARTED, SCOPED. Validated against code 2026-04-12. The fix is **Option A** — run the full `AIAgent` per-dispatch inside the sandbox pod. The previous Option D in this ticket (a bidirectional stdin/stdout bridge protocol with host-side action tools) is discarded: its return channel was built on a false premise about stdin being usable after the task is delivered, and it gives up the security boundary the sandbox was meant to provide. Phase 1 is shippable in stages; items 1-3 of the scope breakdown below close the core gap in ~4-7 days, and the full Phase 1 (items 1-8, including Tools/Policy editor UIs and the in-sandbox approval callback) is ~2-3 weeks.

### State today — verified in code 2026-04-12

Every web-UI chat in OpenShell mode flows through this exact path:

1. **`gateway/http_api.py:_handle_chat:2789`** is the only `/chat` handler. Line 2832-2835 requires `agent_id` and explicitly refuses any in-process fallback: *"OpenShell-only routing: every chat must target a named agent that has its own sandbox worker. No in-process fallback."* Line 3089-3104 is equally explicit when the worker is unhealthy — *"Do NOT silently fall back to an in-process runner — that would run the user's message through the gateway process itself, bypassing every network/filesystem policy the sandbox was meant to enforce."* The handler emits a `sandbox_unavailable` error instead.

2. **Line 2961** resolves `target_worker = _sanitize_sandbox_name(f"hermes-{agent_name}")` — one sandbox per named agent. Multi-user / multi-agent routing happens at the Logos-gateway layer (the HTTP handler picks which sandbox to dispatch to based on the authenticated user's `agent_id` request parameter), not at the Hermes layer.

3. **Line 3112-3123** builds a task payload with `"type": "run_conversation"`, `toolsets`, and `max_iterations: 90`. All three fields are aspirational — the current sandbox worker ignores them. The `run_conversation` type name is a historical vestige from `gateway/worker.py:AgentWorker` (see below).

4. **Line 3146-3149** — `worker_result = await worker_registry.dispatch_task(target_worker, task_payload, timeout=600, on_stream_event=_on_worker_stream)`.

5. **`gateway/worker_registry.py:329-341`** spawns `openshell sandbox exec --no-tty --name <sandbox> -- python3 /app/sandbox_worker.py`, pipes the task JSON to stdin, and **closes stdin immediately** at line 379 — that EOF is what unblocks openshell's exec primitive. The comments at `worker_registry.py:25-33` and `sandbox_worker.py:14-30` document side-by-side tests proving an open stdin is unusable on this transport.

6. **`docker/sandbox_worker.py:249-365`** `_run_inference`: builds `messages = [system(context_prompt), *history, user(message)]`, POSTs to `inference.local/v1/chat/completions` with `{model, messages, stream, max_tokens}`, streams back `delta.content` + `delta.reasoning_content` as token/thinking events. **No `tools` array in the payload, no `tool_choice`, no `tool_calls` handling, no `AIAgent` import.** The worker exits after emitting the terminal `task_result`.

7. **`docker/Dockerfile.hermes-sandbox:38`** — the sandbox image installs only `aiohttp`. The Hermes source code (`agents/hermes/agent.py`, 5947 lines) is **not copied into the image**. The import would fail.

### What this means

Per web-UI chat turn, the sandbox runs:
- One Python process startup (~0.2s for aiohttp)
- One chat-completion HTTPS call to `inference.local`
- Token/thinking streaming back to the gateway
- Exit

No tool loop. No memory writes. No skill invocations. **No `memory_nudge` / `skill_nudge`** (they live at `agents/hermes/agent.py:4118-4140` inside `run_conversation`, which the worker never calls). No `RunRecorder`. No delegation. No workspace cleanup. No `ephemeral_system_prompt` interpretation beyond inserting it as a system message.

### Where the full `AIAgent` DOES run today

Grep `AIAgent\(` across the codebase returns **10 live call sites**, all host-side in-process:

- `gateway/run.py:3648` — `_run_agent` method, called from platform dispatch paths (Telegram, Discord, Slack, email, ACP)
- `gateway/run.py:741` — `_flush_memories_for_session` (session expire / `/reset` / `/resume`)
- `gateway/run.py:2645, 2849, 4507, 4649` — four other in-process instantiations (compression, background tasks, etc.)
- `cron/scheduler.py:258` — scheduled jobs
- `core/batch_runner.py:311` — batch processing
- `acp_adapter/session.py:203` — IDE integration (VS Code, Zed, JetBrains)
- `tools/delegate_tool.py:219` + `tools/handoff_tool.py:320` — in-agent sub-agent spawns
- `logos_cli/cli.py:1558, 3126` — direct CLI invocation
- `gateway/worker.py:219` — `AgentWorker._run_agent_sync`. **Half-built headless-worker infrastructure from the `docs/project/AGENT_WORKER.md` planning doc ("Status: Planning"). `grep AgentWorker` returns only the class definition and its own factory at line 263 — nothing else in the codebase imports or instantiates it.** It's a parked building block, not live code.

So the full agent loop runs for platform messages, cron, batch, ACP, CLI, flush, and delegation — just **not inside the OpenShell sandbox for web-UI chats**. M10 is specifically about making the web-UI path match the other entry points.

### The fix — Option A, validated 2026-04-12

Replace `sandbox_worker.py:_run_inference` with a ~50-line bootstrap that instantiates `AIAgent` and calls `run_conversation` per dispatch. The sandbox pod stays passive (`sleep infinity`). Dispatch still goes through `openshell sandbox exec --no-tty -- python3 /app/sandbox_worker.py`. Each dispatch runs a fresh Python process that:

1. Loads `/tmp/hermes/instance-config.json` (already uploaded at spawn time by `gateway/executors/openshell.py:866-872`)
2. Reads one task JSON from stdin — existing flow, unchanged
3. Instantiates `AIAgent` with callbacks that `emit()` JSON frames to stdout — same `emit()` helper and stdout protocol we have today:
   ```python
   from agents.hermes.agent import AIAgent
   agent = AIAgent(
       model=task["model"] or config["model"],
       base_url="https://inference.local/v1",
       api_key="unused",
       enabled_toolsets=task.get("toolsets") or ["hermes-cli"],
       session_id=task["session_id"],
       ephemeral_system_prompt=task.get("context_prompt"),
       max_iterations=task.get("max_iterations", 90),
       quiet_mode=True,
       tool_progress_callback=lambda t, p=None, a=None: emit({"type": "tool_progress", "task_id": task_id, "tool": t, "preview": p or ""}),
       tool_complete_callback=lambda cid, t, ok, ms, p=None: emit({"type": "tool_end", "task_id": task_id, "call_id": cid, "tool": t, "success": ok, "duration_ms": ms}),
       thinking_callback=lambda c: emit({"type": "thinking", "task_id": task_id, "content": c}),
   )
   ```
4. Runs it in a thread so streaming callbacks flush between turns:
   ```python
   result = await asyncio.get_event_loop().run_in_executor(None, lambda: agent.run_conversation(
       user_message=task["message"],
       conversation_history=task.get("history", []),
       task_id=task_id,
   ))
   ```
5. Emits the terminal `task_result` frame and exits.

The proven template is `gateway/worker.py:AgentWorker._run_agent_sync:186-246` — same shape, same callbacks, but dispatched via WebSocket instead of per-task exec. M10 takes the exec transport and points it at the same instantiation pattern.

The output frames the worker emits (`tool_progress`, `tool_end`, `thinking`, `token`, `task_result`) are **already parsed** by `gateway/worker_registry.py:dispatch_task` at line 449 — the parser was written for this case and has been waiting on the restoration.

### Validated 2026-04-12

Before committing to this plan, the viability of ephemeral `AIAgent` instantiation from inside a bare Python script was verified directly in `agents/hermes/agent.py`:

- **`__init__` signature** (lines 223-267): all parameters except `model` have defaults. `base_url` / `api_key` fall through to a provider router. `session_db`, `action_policy`, `iteration_budget`, `workspace_path` all optional.
- **`run_conversation` signature** (lines 4011-4019): synchronous, returns `{"final_response", "messages", "api_calls", "completed", "interrupted", ...}`. Takes `user_message`, `conversation_history`, `task_id`, optional `ephemeral_system_prompt` and `stream_callback`.
- **No host-gateway coupling**: grep for `^from gateway\.` / `^from logos\.` / `localhost:` / `127\.0\.0\.1` / `host\.docker\.internal` / `host\.openshell` across the 5947-line file returns **one match** — line 19, a docstring example. `AIAgent` has zero imports from `gateway.*` or `logos.*` and no hardcoded URLs to any host-side service. It can run anywhere Python can import `agent/`, `core/`, `tools/`, and the OpenAI SDK.
- **Context file and memory loading are gated on flags**: `skip_context_files=False` (default, line 257) loads SOUL.md / AGENTS.md / .cursorrules at line 1630. `skip_memory=False` (default, line 258) loads memories at line 700. For the sandbox case we keep both defaults — the agent has its own soul and memories inside the sandbox via an uploaded `$HERMES_HOME`.
- **Streaming is callback-based**: `tool_progress_callback`, `tool_complete_callback`, `thinking_callback`, `reasoning_callback`, `step_callback`. Each callback wires trivially to an `emit()` call that writes a JSON frame to stdout.
- **Filesystem needs inside the sandbox**: `$HERMES_HOME/logs/errors.log` (line 399), `$HERMES_HOME/sessions/` (line 646), `$HERMES_HOME/SOUL.md` + `memories/` + `skills/` if context files and memory are enabled. All under one env-configurable root. `AIAgent` creates missing directories at runtime (`mkdir(parents=True, exist_ok=True)`); only SOUL.md + seed memories need to be uploaded at spawn time.

### Why the previous Option D was wrong

Option D proposed a bidirectional stdin/stdout protocol: the sandbox emits a `tool_request` frame for any action tool, the gateway runs the tool host-side, then writes a `tool_grant` / `tool_denied` reply back to the subprocess's stdin. The sandbox's tool proxy awaits the response on stdin and continues the agent loop.

**That is physically impossible on the Plan A-prime transport.** `gateway/worker_registry.py:370-384` closes the subprocess's stdin *before* openshell's exec primitive will start the in-sandbox process — the EOF is load-bearing. Without it, the in-sandbox command sits in a gRPC wait state forever. The comments at `worker_registry.py:25-33` and `sandbox_worker.py:14-30` document the empirical side-by-side tests proving this. There is no way to keep stdin open as a return channel after the task is delivered. Option D's bridge would have killed itself on the first approval request.

**Even if stdin were usable**, running action tools on the host would invert the security story: `terminal_tool`, `browser_tool`, `delegate_tool` running in the gateway process would be unconstrained by Landlock, seccomp, capability drops, or the OpenShell network policy — exactly the layers the sandbox was meant to provide. Option D would give up the security boundary it was trying to codify. NVIDIA's NemoClaw (prior art — see below) takes the opposite stance: everything runs inside the sandbox, enforcement is at the infrastructure layer *below* the agent.

### Two-layer STAMP model — same outcome, cleaner implementation

The April 2026 migration plan (`docs/migration/logos-openshell-migration.md`, marked *"HISTORICAL — Migration largely complete"*) already committed to a two-layer enforcement split: *"Logos tool-level policy becomes a layer **above** OpenShell network policy. MCP tool access requests continue through the Logos gateway; outbound connections from MCP servers are governed by OpenShell egress rules. The combination is strictly stronger than either alone."* M10 implements that split:

- **S (Soul)** — unchanged. SOUL.md uploaded to `$HERMES_HOME/SOUL.md` at spawn (already happens at `gateway/executors/openshell.py:874-882`, destination path needs adjusting to match the new `$HERMES_HOME` layout).
- **T (Tools)** — two layers.
  - *Infrastructure* (NemoClaw-style, coarse, per-sandbox): which binaries are installed in the sandbox image + which network endpoints the OpenShell policy allows + binary-scoping via `/proc/<pid>/exe` SHA256. Hot-reloadable via `openshell policy set`.
  - *Application* (fine, per-agent, per-dispatch): which tool names are enabled per-agent, stored in `agents.toolsets` (column already exists), passed in the dispatch task payload to `AIAgent(enabled_toolsets=...)`. Honored by the agent's own tool registry at `agents/hermes/agent.py:586`. **No grant list, no bridge.**
- **A (Agent)** — runs inside the sandbox. Run history writes to `$HERMES_HOME/sessions/`, synced back to the host periodically via the daemon in scope item 8.
- **M (Model)** — unchanged. Sandbox calls `inference.local`, OpenShell's privacy router injects the provider credential at egress. Per-agent model binding already stored in `model_routes`.
- **P (Policy)** — stays in the existing `action_policies` + `approval_requests` tables. Consulted **inside the agent** via `AIAgent.__init__`'s `action_policy` parameter (`agent.py:265`), enforced in `_invoke_tool`. Per-tool-call approvals flow as an outbound HTTPS callback from the sandbox to a Logos-side approval endpoint (new named endpoint on the OpenShell L7 proxy, same mechanism as `inference.local`).

Each layer is independently buildable. M10 ships the agent-loop-inside-sandbox part; the policy expansion + preset system + editor UIs are the build-out on top.

### Scope breakdown — Phase 1, shippable in stages

| # | Item | Effort | Closes |
|---|---|---|---|
| 1 | **Sandbox image rebuild** — rewrite `docker/Dockerfile.hermes-sandbox` to install the Logos Python package so `from agents.hermes.agent import AIAgent` works inside the container. Crib the `uv pip install -e ".[all]"` pattern from `docker/Dockerfile.docker-sandbox:27-36`. Keep `CMD ["/app/entrypoint.sh"]` → `exec sleep infinity`. Audit `tools/` for required apt packages (shell for `terminal_tool`, etc.). Create `$HERMES_HOME` skeleton at `/sandbox/.hermes-data/{memories,skills,sessions,logs}` with sandbox-user ownership. | 2-4 days | M10 image prereq |
| 2 | **Sandbox worker rewrite** — replace `docker/sandbox_worker.py:_handle_task` with the `AIAgent.run_conversation` bootstrap. ~50 lines. Template: `gateway/worker.py:AgentWorker._run_agent_sync:186-246`. Protocol unchanged (same stdin task shape, same stdout frame types). | 1-2 days | **M10 core** |
| 3 | **Executor upload changes** — `gateway/executors/openshell.py:spawn()`: change SOUL.md upload destination from `/tmp/hermes/SOUL.md` (line 880) to `$HERMES_HOME/SOUL.md`, seed `memories/` from `~/.logos/instances/<agent>/memories/` if it exists, drop or repurpose the unused `gateway_url` field (line 744). | 1 day | M10 glue |
| 4 | **Policy expansion** — rewrite `gateway/policies/openshell_default.yaml` (currently 63 lines, 2 entries) with binary-scoped, L7-inspected entries for every endpoint the agent legitimately needs. Add `gateway/policies/presets/` with per-integration opt-in overlays (github, slack, discord, telegram, huggingface, pypi, npm — shapes portable from `knowledge-repos/NemoClaw/nemoclaw-blueprint/policies/presets/*.yaml`). Add `gateway/policies.py` module with `apply_preset(agent_id, preset_name)`, `get_applied_presets(agent_id)`, `compute_effective_policy(agent_id)` — Python port of `knowledge-repos/NemoClaw/src/lib/policies.ts`. Add `agents.applied_policies` column or join table. | 3-5 days | unblocks M1 infrastructure layer |
| 5 | **Tools editor UI** — dropdown behind the T pill in the Chats tab (the M1 surface). Reads `agents.toolsets` + `applied_policies`, lets the user toggle application-layer tool enable/disable and apply/remove infrastructure presets. Apply triggers `apply_preset()` + `openshell policy set` for runtime effect. | 3-5 days | **closes M1** |
| 6 | **Policy editor UI** — slide-out behind the P pill (the M2 surface). Shows the agent's `action_policy` with per-tool gating (auto / require-approval / deny), plus pending `approval_requests` for the current chat. | 3-5 days | closes M2 UI (pairs with item 7 for runtime) |
| 7 | **In-sandbox approval callback** — the one genuinely new bit of transport glue. Register a second named endpoint on the OpenShell L7 proxy (e.g. `logos-approval.local`, same mechanism as `inference.local`). Logos gateway exposes `POST /v1/approvals/decide` that holds the response open until the user decides in the chat UI. The agent inside the sandbox makes an outbound HTTPS call to this endpoint when `action_policy` requires approval, blocks on the response body `{"decision": "approve"\|"deny", "reason": "..."}`. Requires OpenShell L7 proxy support for a second named endpoint — if that needs an OpenShell-side change, the minimum-viable variant uses direct LAN IP egress through the network policy. | 2-3 days | closes M2 runtime + M9 chat visibility |
| 8 | **Sync-back daemon** — periodic `openshell sandbox download` pulls `$HERMES_HOME/memories/` + `$HERMES_HOME/skills/` out to `~/.logos/instances/<agent>/` on the host. Canonical durability copy across sandbox re-creation. On re-create, upload restores from the host copy. | 2-3 days | durability (not M10-blocking) |

**Phase 1 minimum** (shippable alone): items 1-3 close the core M10 gap — web chats run the full agent loop, memory writes happen during chats, nudges fire, delegation works from the web UI. **4-7 days of focused work.**

**Phase 1 full**: items 1-8, **2-3 weeks**. Closes M1, M2, M9 chat-path visibility, and adds durability.

### Why the scope is smaller than the earlier Option D estimate

The earlier "3-5 days" estimate covered Option D's bridge protocol — designing, implementing, and concurrency-testing a new bidirectional stdin/stdout message protocol with error paths, sandbox-side tool proxies, and host-side grant routing. That's no longer in scope. The sandbox worker rewrite replaces an existing file with a similar file using the same protocol frames. The Dockerfile work is "port NemoClaw's hardening patterns into Logos's directory". The policy work is "port NemoClaw's YAML structure into `gateway/policies/`". **None of it is novel architecture** — it's mechanical adaptation of patterns that exist and work in either Logos (the per-dispatch exec transport) or NemoClaw (the hardened image + policy language).

### Prior art — NemoClaw

NVIDIA's **NemoClaw** (`knowledge-repos/NemoClaw`) is a separate opinionated reference stack on top of OpenShell that implements exactly this architecture for Nous Research's upstream `hermes-agent`. Their `agents/hermes/` directory contains a complete hardened deployment:

- **`Dockerfile`** (124 lines) — image build with integrity-hashed config, immutable/writable dir split, SOUL.md seed + symlink, DAC hardening (root ownership, chmod 444), build-time patch for Hermes's `TelegramFallbackTransport` (which bypasses the L7 proxy via raw IPs)
- **`start.sh`** (411 lines) — runtime hardening entrypoint: capability drops via `capsh`, PATH hardening, ulimit, config integrity verification at startup, symlink validation, `chattr +i` immutable hardening, HTTP_PROXY setup, privilege separation via `gosu gateway`
- **`manifest.yaml`** — declarative agent integration contract (binary_path, gateway_command, health_probe, config paths, state_dirs, messaging platforms)
- **`policy-additions.yaml`** / **`policy-permissive.yaml`** — per-agent policy overlays
- **`decode-proxy.py`** (103 lines) — asyncio TCP proxy that URL-decodes paths before forwarding to the L7 proxy, working around Python httpx's URL-encoding of colons that would otherwise break OpenShell's `openshell:resolve:env:KEY` placeholder rewriting. **Directly reusable in Logos when we adopt the placeholder pattern for secrets.**
- **`plugin/__init__.py`** (164 lines) — important finding: the NemoClaw "plugin" for Hermes is **pure UX sugar**. It registers two tools (`nemoclaw_status`, `nemoclaw_info`) and a startup banner. It does NOT intercept other tool calls, does NOT enforce policy, does NOT bridge between sandbox and host. **All hardening is at the OpenShell layer, below Hermes.** The Hermes runtime inside the sandbox is unmodified upstream Hermes with one build-time patch. This is decisive confirmation that Logos does not need to modify `agents/hermes/agent.py` to adopt this model — the agent runs as-is, the sandbox boundary does the work.
- **`nemoclaw-blueprint/policies/openclaw-sandbox.yaml`** (204 lines) — the binary-scoped, L7-inspected reference network policy. Worked examples include Claude Code → `api.anthropic.com` (POST to inference paths only, TLS-terminated, binary-scoped to `/usr/local/bin/claude`), Sentry → GET-only (with comment explaining the threat model: unrestricted POST would be a generic exfiltration channel), and opt-in presets for every messaging / package / service integration.

**We do not adopt NemoClaw directly.** It targets Hermes ≥0.8.0 (Logos is on 0.7.x); it's explicitly single-user; it's single-agent (one always-on assistant per sandbox); its `nemoclaw/src/blueprint/ssrf.ts` SSRF check unconditionally rejects RFC1918 / CGNAT / loopback IPs, which breaks Logos's LM Studio on 192.168.x use case; it hard-codes NVIDIA Endpoints as the default provider; and STAMP, run replay, soul system, workflows, multi-user, evolution have no home in NemoClaw's mental model. **We port the patterns** — Dockerfile structure, hardening script, policy YAML language, preset model, config-generator pattern, decode-proxy — into Logos's own `docker/`, `gateway/policies/`, and `gateway/policies.py` directories, and maintain them as Logos code. Same goal, different vehicle.

### Dependencies

- **Blocks M1** (editable Tools pill) — the dropdown needs the in-sandbox agent to honor `enabled_toolsets` from the dispatch payload. Requires scope items 1-3.
- **Blocks M2** (editable Policy pill + approvals) — the editor needs the in-sandbox agent to consult `action_policy` and surface pending approvals. Requires scope items 1-3 + 7.
- **Blocks M9** (chat-path visibility) — memory writes have to happen during chats before they can be surfaced in the UI. Requires scope items 1-3.
- **No dependency on M8 Phase B** — the dispatch ledger can land before or after.
- **Not blocked by anything upstream** — M6 (unified logs) is done, TASKS.md #24 (Plan A-prime transport) is done, M8 Phase A (active_tasks + thought bubble) is done. M10 is the next unblocked execution target.

### Direction established

- **2026-04-11** — discovered during M9 scoping that `docker/sandbox_worker.py` bypasses `AIAgent.run_conversation`. Earlier Option D proposal (stdin-delivered grant protocol, action tools host-side) drafted and recommended.
- **2026-04-12 (morning)** — validated in code against `_handle_chat`, `worker_registry.dispatch_task`, the three Dockerfiles (`Dockerfile`, `Dockerfile.hermes-sandbox`, `Dockerfile.docker-sandbox`), the four executors (`openshell.py`, `docker.py`, `base.py`, `__init__.py`), all 10 `AIAgent(...)` call sites, and `AIAgent.__init__` + `run_conversation` signatures. Studied NemoClaw as prior art (full architecture read + code validation). Confirmed Option D's stdin bridge was physically impossible. Rewrote this section as Option A with verified scope breakdown.
- **2026-04-12 (evening)** — implemented Phase 1 items 1-5 on branch `m10-phase1-aiagent-in-sandbox`, then ran the build-test cycle. First `docker build` produced a 4.26 GB image because `COPY . /app/` + `pip install -e ".[messaging]"` bundled the entire Logos Python package (including `gateway/`, `logos_cli/`, `cron/`, `acp_adapter/`) into the sandbox. **Greg identified the architectural error**: the sandbox should contain the agent runtime (preferably a versioned upstream image like Nous Research's `hermes-agent`), not Logos's platform layer. "The image should honestly just be the original hermes repo as the gateway... that way we can just drop any agent into a sandbox and present multiple agents as options later in logos... multi agent just comes naturally by raising a new sandbox with a new agent or a sandbox of hermes a second time or third. We just need to prove it works once with a full agent sandboxed away." **Items 1-3 reverted, redirected to M11 (image-per-agent-release pattern)**. Items 4-5 (network policies, DB, editor UI) kept — they're infrastructure-layer and agent-runtime-agnostic.

---

## M11 — Agents as versioned drop-in sandbox images

**Status**: NOT STARTED. Supersedes M10 Phase 1 items 1-3 (reverted 2026-04-12 evening). The narrow proof-of-concept scope is ~2-3 days of focused work: prove one full agent runs inside a sandbox from a versioned image, then multi-agent follows for free from spawning a second sandbox.

### Why M10 items 1-3 went wrong

M10's Option A (full `AIAgent.run_conversation` inside the sandbox) was architecturally sound — the tool loop belongs inside the security boundary. But the **implementation** bundled the whole Logos Python package into the sandbox image (`COPY . /app/` + `uv pip install -e ".[messaging]"`), which meant:

1. **4.26 GB sandbox image** — most of it is host-side code and platform adapter deps the agent never runs (see below)
2. **Host-side code inside the security boundary** — `gateway/http_api.py`, `gateway/admin_handlers.py`, `gateway/policies.py`, `gateway/executors/openshell.py`, `gateway/auth/db.py`, `gateway/html/*` all ended up inside the sandbox. If the agent is compromised, the attacker has a copy of the admin HTTP handler surface sitting at `/app/gateway/`.
3. **Tight coupling to Logos releases** — every Logos release forces a sandbox image rebuild. When Nous Research ships Hermes 0.9.0, we'd have to pull it into the Logos fork, release a Logos version, rebuild the image, and redeploy — a multi-step release dance for a single upstream dependency bump.
4. **No path to multi-agent** — `sandbox_worker.py` hardcodes `from agents.hermes.agent import AIAgent`. Swapping to Claude Code or OpenClaw would require either parallel sandbox_worker variants per agent type or a runtime dispatch mechanism that doesn't exist yet. Both are architectural reworks, not configuration changes.
5. **Cross-layer dep bomb** — grep during the build-test revealed that `agent/auxiliary_client.py`, `tools/environments/singularity.py`, `tools/environments/base.py`, `tools/skills_tool.py`, and `tools/process_registry.py` all import from `logos_cli.config`, meaning the Hermes runtime has drifted into depending on the Logos CLI layer. This coupling would need to be unwound before any clean extraction is possible.

### The right shape

Each agent lives in its own **versioned upstream container image**, built from its own source (not the Logos repo). Logos references the image by tag, doesn't build or bundle it, and needs to know only the dispatch protocol. Version bumps = tag bumps, no Logos release.

**This is exactly NVIDIA NemoClaw's pattern** (prior art, already studied during M10 research). Their `nemoclaw-blueprint/blueprint.yaml:32` pins the sandbox image by sha256 digest: `ghcr.io/nvidia/openshell-community/sandboxes/openclaw@sha256:...`. Their `agents/hermes/manifest.yaml:15-24` declares Hermes as a drop-in agent with `binary_path: /usr/local/bin/hermes`, `gateway_command: "hermes gateway run"`, `health_probe.url: http://localhost:8642/health`, and `forward_ports: [8642]`. NemoClaw doesn't build Hermes — it references it.

### Three concrete consequences of the shape

1. **Agent updates = image tag bumps, no Logos rebuild.** When Nous Research releases `hermes-agent` 0.9.0 upstream, the Logos user pulls `nousresearch/hermes-agent:0.9.0` (or whatever registry the image lives at) and Logos's M11-era executor spawns it inside an OpenShell sandbox unchanged. Logos's code stays on 0.10.x.

2. **Multi-agent falls out for free.** Once one agent works inside a sandbox, spinning up a second is just calling `spawn()` with a different `agent.name` and possibly a different image tag. Different instances of the same agent image (Tali + Grace, both running Hermes with different souls) come for free. Different agent TYPES (Hermes + Claude Code + OpenClaw running concurrently) come for free too — the Logos dashboard's agent picker just needs to know which image to use for which agent type. Per Greg 2026-04-12 evening: *"multi agent just comes naturally by raising a new sandbox with a new agent or a sandbox of hermes a second time or third. We just need to prove it works once with a full agent sandboxed away."*

3. **Security boundary is actually meaningful.** Host-side Logos code (gateway, admin handlers, policies management, DB, HTTP API) doesn't leak into the sandbox. If an agent is compromised, blast radius is whatever the image contains plus whatever the OpenShell network policy allows — not "a copy of the entire Logos admin handler surface."

### The protocol question — how does Logos talk to the in-sandbox agent?

Two viable patterns, both proven in prior art:

- **Pattern A (port-forward HTTP)**: the agent inside the sandbox runs its own HTTP server on a known port (e.g. 8642 for Hermes, or whatever the agent's manifest declares in `forward_ports`). Logos's executor calls `openshell sandbox create --forward 8642` to expose that port to the host; chat dispatch is `POST http://127.0.0.1:<forwarded-port>/v1/chat/completions` (or whatever endpoint the agent's gateway exposes) from the Logos host to the in-sandbox agent's gateway. Streaming happens over SSE or chunked HTTP. **This is NemoClaw's pattern** (`agents/hermes/manifest.yaml:32-33`) and is also what Logos's existing `DockerSandboxExecutor` does (`gateway/executors/docker.py:138-151` — publishes port 8080 to localhost and polls `/health`).

- **Pattern B (stdin/stdout per-task exec)**: call `openshell sandbox exec --name <sandbox> -- <agent-binary> <chat-subcommand>` per chat turn. The agent binary has its own one-shot invocation mode that reads a task from stdin and emits events to stdout. This is what M10 Phase 1 items 1-3 attempted (via the Python `sandbox_worker.py` shim) — the shim approach is the architectural error we're undoing. A real Pattern B would delete the shim and invoke the agent binary directly.

**Pattern A is the right answer for M11** because:

- It matches NemoClaw's blessed NVIDIA reference architecture
- Logos's historical `DockerSandboxExecutor` already implements this shape — we can mine it for code
- Port forwarding is a well-known OpenShell feature (`--forward`)
- It doesn't require per-agent CLI glue — HTTP is a universal protocol
- Streaming tokens work naturally over SSE or chunked responses
- Multi-session within one sandbox works because HTTP is stateless per-request; just pass a `session_id` in the request body

### Scope — narrow proof-of-concept (~2-3 days)

1. **Pick the image source.** Easiest options in priority order:
   - **(a)** Use Logos's existing `docker/Dockerfile.docker-sandbox` as-is. It already builds a container running `python -m gateway.run` — effectively "a Logos gateway in a container", which is "a Hermes runtime with a gateway" because the gateway imports `AIAgent` and runs the tool loop in-process for `_run_agent` calls. Tag it `logos-hermes-runtime:<version>` or similar. Lowest risk — reuses proven working code.
   - **(b)** Build our own `logos-hermes-runtime:<version>` image from a clean subset of the Logos repo (just `agents/`, `agent/`, `tools/`, `core/`, and the `logos_cli.config` bits we can't easily separate yet). Slightly smaller image; requires the `logos_cli.config` cross-layer cleanup below.
   - **(c)** Use Nous Research's upstream `hermes-agent` image directly if they publish one, and accept that Logos's fork-specific additions don't apply. Longest-term correct; most work today.

    **Recommendation: start with (a).** It works today via `DockerSandboxExecutor` and proves the Pattern A dispatch model end-to-end. Swap to (b) or (c) in M11 Phase 2 once the primitive is working.

2. **Rewrite `gateway/executors/openshell.py:spawn()`** to:
   - Accept an image tag and a forwarded port (both from a per-agent config/manifest)
   - Call `openshell sandbox create --from <image> --forward <port>` — no `--policy` on the initial create (the effective policy merge already landed in M10 Phase 1 item 5 and stays)
   - Drop the `instance_config.json` upload path (that was for `sandbox_worker.py`'s config reading — not needed when the agent's own gateway reads config from wherever its image expects)
   - Drop the SOUL.md upload path (Hermes gateway inside the image reads SOUL.md from its own HERMES_HOME on container start, or we pass it via environment variable at sandbox create time)
   - Keep the `gateway.policies.write_effective_policy_to_tempfile(agent_id)` call for the policy file — that's infrastructure-layer and still applies

3. **Rewrite `gateway/worker_registry.py:dispatch_task`** to HTTP-POST to `http://127.0.0.1:<port>/chat` (or the appropriate endpoint) on the sandbox instead of `openshell sandbox exec python3 /app/sandbox_worker.py`. The streaming callback interface stays the same — forward whatever the agent's HTTP endpoint returns (SSE or chunked) to `on_stream_event`.

4. **Delete `docker/sandbox_worker.py`** — not needed. The agent's own gateway runs inside the sandbox.

5. **Delete `docker/Dockerfile.hermes-sandbox`** — replaced by `Dockerfile.docker-sandbox` (already exists) or a future `Dockerfile.hermes-runtime` once the cross-layer cleanup lands.

6. **Add a per-agent manifest** — just enough for the proof. Something like `agents/<name>/manifest.yaml` with `name`, `image`, `forward_port`, `health_probe.url`. Steal the shape from NemoClaw's `agents/hermes/manifest.yaml` but keep it minimal. A single file per agent type.

7. **Address the `logos_cli.config` cross-layer coupling** — move `get_hermes_home`, `load_env`, and `_ENV_VAR_NAME_RE` from `logos_cli/config.py` into `core/paths.py` or similar. Update the five affected files in `agent/` and `tools/`. Low-risk mechanical refactor, 5-10 line changes per file. **Can be deferred** if we pick image option (a) since the Logos gateway imports `logos_cli.config` fine from inside its own container.

8. **Build + smoke test end-to-end**: spawn one sandbox, send a chat, verify the full agent loop runs (memory write, tool invocation, nudges). This is the "prove it works once" milestone Greg scoped.

9. **Multi-agent smoke test**: spawn a second sandbox (same image, different `agent.name`), verify both work concurrently. This is the "multi-agent for free" validation — if it works for one, it should work for N.

### What's preserved from M10 Phase 1

Items 4-5 ship as-is. They're infrastructure-layer and agent-runtime-agnostic:

| Artifact | Stays |
|---|---|
| `gateway/policies.py` + the 6 presets in `gateway/policies/presets/` + the new `gateway/policies/openshell_default.yaml` baseline | ✓ Network policies apply to whichever agent runs inside the sandbox |
| `gateway/auth/db.py` v10 migration + `get_agent_applied_presets` / `set_agent_applied_presets` helpers | ✓ Per-agent applied-preset list survives the agent-runtime swap |
| `gateway/admin_handlers.py` new handlers (`handle_agent_tools_get`, `handle_agent_toolsets_toggle`, `handle_agent_presets_toggle`) + `gateway/http_api.py` route registrations | ✓ Still serve the T pill UI; preset toggles still push via `openshell policy set`; toolset toggles still write `agents.toolsets` (runtime effect returns once M11 ships) |
| `gateway/html/main_app.html` T pill dropdown UI | ✓ Users can toggle presets + toolsets today; preset runtime-effect works; toolset runtime-effect returns with M11 |
| `gateway/executors/openshell.py` non-reverted edits: `_GATEWAY_PORT` + `_WORKER_REGISTER_TIMEOUT` removal, `gateway_url` field drop from `instance_config`, `gateway.policies.write_effective_policy_to_tempfile(agent_id)` wire-up | ✓ Cleanup + effective-policy pass-through survives |
| MISSING.md M10 section | ✓ Historical narrative of "why Option D was wrong" stays as documentation |

### Dependencies

- **Blocks the runtime half of M1** — Tools editor toolset toggles land on `agents.toolsets` today (infrastructure-layer T pill works), but the in-sandbox agent won't honor `enabled_toolsets` until M11 ships a runtime that reads it.
- **Blocks the runtime half of M2 + all of M9 chat-path visibility** — same shape: memory writes during chats, tool invocations, and nudges all need an in-sandbox agent loop.
- **Does NOT block M3 / M4 / M5 / M7 / M8 Phase B** — those are independent tracks.
- **Depends on nothing upstream** — all the M10 infrastructure-layer pieces are shipped already; M11 is pure agent-runtime work.

### Open questions

1. **Which image option do we start with — (a) reuse `Dockerfile.docker-sandbox`, (b) build a cleaner subset, or (c) use upstream Nous Research directly?** (a) is fastest, (c) is most correct long-term.
2. **Does the in-sandbox agent's gateway need to route through `inference.local`, or does it make direct LLM calls?** If direct, credentials leak into the sandbox (bad). If through `inference.local`, we need to make sure the agent's gateway respects `HTTPS_PROXY` or `OPENAI_BASE_URL` env vars.
3. **How does per-agent config get into the sandbox?** NemoClaw uses the `instance-config.json` upload pattern (which we're dropping). Alternative: env vars set at `openshell sandbox create` time, or a small config file uploaded to a well-known path, or HTTP POST to the in-sandbox agent after spawn to configure it.
4. **Streaming responses**: HTTP SSE or chunked? NDJSON?
5. **Session state** — does the sandbox's in-memory session store persist across chats, or does each chat include full history in the task payload (stateless server)? Today Logos passes history per-chat, so stateless is the easy answer.
6. **Auth between Logos and the in-sandbox agent** — the forwarded port is local-only by default (127.0.0.1 binding), but should we add a bearer token in case of multi-user situations where different users' agents share a host? Probably punt to M11 Phase 2.

### Direction established

- **2026-04-12 (evening)** — during the M10 Phase 1 build-test cycle, the first `docker build` produced a 4.26 GB image that bundled the entire Logos package. Greg identified the architectural error and redirected scope: *"the image should honestly just be the original hermes repo as the gateway... that way we can just drop any agent into a sandbox and present multiple agents as options later in logos... The point is to just be able to drop the actual hermes agent and even be able to update it by just bringing up the new release of the agent and same with any agent... multi agent just comes naturally by raising a new sandbox with a new agent or a sandbox of hermes a second time or third. We just need to prove it works once with a full agent sandboxed away."* M10 Phase 1 items 1-3 reverted, this M11 section written to capture the new scope.

---

## Relationships

```
M6 (unified logs) ══════════→ DONE — MVP shipped, used daily now
                        └──→ unblocks every debugging session (already paid off)

TASKS.md #24 (chat transport) → DONE — Plan A-prime shipped end-to-end

M8 Phase A (active_tasks + bubble) → DONE — world view reflects live activity

─── blockers remain below this line ───

M10 Phase 1 items 4-5 (policies + UI) ══→ SHIPPED — network policies +
                                           presets + Tools editor UI +
                                           DB migration are agent-runtime-
                                           agnostic, ride through M11
                                           unchanged. T pill dropdown
                                           works for preset toggling
                                           at runtime today.

M10 Phase 1 items 1-3 (sandbox bundling) ═→ REVERTED 2026-04-12 evening.
                                             Superseded by M11 —
                                             bundling Logos's Python
                                             package into the sandbox
                                             was the wrong shape; the
                                             right shape is image-per-
                                             agent-release.

M11 (agents as drop-in sandbox images) ══╦══→ Sandbox image = versioned
                                           ║    upstream agent runtime,
                                           ║    Logos references by tag,
                                           ║    doesn't rebuild on agent
                                           ║    version bumps. Pattern A
                                           ║    port-forward HTTP dispatch.
                                           ║    Multi-agent comes free.
                                           ║
                                           ╠══→ UNBLOCKS runtime half of
                                           ║    M1 (toolsets honored in
                                           ║    the agent), runtime half
                                           ║    of M2 (action_policy
                                           ║    enforced), and M9 chat-
                                           ║    path visibility (memory
                                           ║    writes during chats).
                                           ╚══→ Proof-of-concept: one
                                               agent, one sandbox, one
                                               chat turn with tool use
                                               + memory write.

M1 (T editable) ──→ UI shipped (M10 item 5). Infrastructure-layer
                    toggles (presets) have runtime effect today via
                    gateway/policies.py + `openshell policy set`.
                    Application-layer toggles (toolsets) persist to DB
                    today; runtime effect blocked on M11.

M2 (P editable) ──→ Blocked on M11 (need in-sandbox agent first) +
                    M10 Phase 1 items 6-7 (Policy editor UI +
                    in-sandbox approval callback, not yet built).

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

M9 (activity visibility) ──→ depends on M11 (for chat-path visibility)
                         ──→ depends on M8 Phase B (for ledger tagging)
                         ──→ the tamagotchi / living-agent identity feature
```

**Recommended tackling order (updated 2026-04-12 evening)**:

**M11 proof-of-concept (2-3 days, one agent in one sandbox end-to-end) → multi-agent validation (hours — spawn a second sandbox, confirm both work) → M10 Phase 1 items 6-7 (Policy editor UI + in-sandbox approval callback, closes M2 + the remaining half of M9 chat visibility) → M8 Phase B → M3 → M4 → M7 → M5 (full-tab world) → M10 item 8 (sync-back daemon)**

Rationale: **M10 Phase 1 items 4-5 shipped** on 2026-04-12 (network policy presets, `gateway/policies.py`, DB migration, Tools editor backend and UI). Those are infrastructure-layer and agent-runtime-agnostic — they ride through M11 unchanged and already work for preset toggling today. **M10 items 1-3 were reverted** because bundling Logos's Python package into the sandbox image turned out to be the wrong shape after the first build produced a 4.26 GB image stuffed with host-side code. The replacement direction is **M11** — versioned upstream agent images referenced by tag, proven with one agent end-to-end first, multi-agent as the free consequence. Once M11 lands with a working in-sandbox Hermes, M1 and M2 become genuinely closable (Tools editor toolset toggles start having runtime effect; Policy editor runtime approvals land via items 6-7). M8 Phase B, M3/M4, M7, and M5 are independent tracks that can run in any order.

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
