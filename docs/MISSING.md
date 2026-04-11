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
```

**Recommended tackling order**: **M6 → M3 → M4 → M2 → M1 → M5.**

Rationale: **M6 goes first** because it pays for itself the next time anything breaks, and because every subsequent M depends on being able to reason about what happened across components. Then M3 unlocks M4; M2 is user-visibility-critical once M4 is real; M1 is the last polish on the STAMP pill once everything else is in place; M5 is the long arc that benefits from everything else first.
