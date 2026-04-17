# Logos Work Queue
> Compiled 2026-04-17 from `TASKS.md`, validated against the codebase.
> Jira-style backlog, sorted by importance. **Today's recommendation at the bottom.**

---

## Legend

**Priority** — P0 (blocker) · P1 (high) · P2 (medium) · P3 (low/polish)
**Effort** — XS (<30m) · S (30m–2h) · M (2h–1d) · L (1–3d) · XL (3d+)
**Status** — DONE · PARTIAL · OPEN · BLOCKED

---

## Audit findings (the big direction-change)

**LOG-24 (sandbox worker WS regression) is essentially DONE.** The team picked **option B** (per-task `openshell sandbox exec`) — *not* the NemoClaw port-forward (option C) that TASKS.md describes as the path forward. `TunnelWebSocket` is gone, `/ws/worker` route is deleted, `OpenShellExecutor.spawn` no longer launches a worker, `WorkerRegistry.dispatch_task` shells out per task. TASKS.md has been corrected to mark this resolved. What remains is **verification + stale-doc cleanup**, not a 1–3 day refactor.

Tailwind audit confirmed: `assets/tailwind.css` mtime is `2026-04-06`, while `main_app.html` was modified `2026-04-16`. Stale by 10 days.

GHCR workflow file exists but installer wiring (`fresh-install.sh` pull, `_DEFAULT_IMAGE` swap) is still missing.

All other tickets in the original queue are confirmed **OPEN / NOT STARTED**.

**Strategic addition (2026-04-17):** New ticket **LOG-44 · Migrate from Hermes-as-library to Hermes-as-server (autonomy unlock)**. After re-reading the upstream `hermes-agent` repo at `knowledge-repos/hermes-agent/`, we confirmed Hermes ships as a complete autonomous agent platform (HTTP gateway, 15 native channel adapters, built-in cron, boot hooks, pairing, sessions). Logos currently uses only `AIAgent.run_conversation()` per-task — wasting all the autonomy primitives. The decision is to swap each sandbox's entrypoint from `sleep infinity` to `hermes gateway run`, with Logos staying as the multi-tenant orchestrator above N autonomous Hermes sandboxes. NemoClaw is the per-sandbox packaging reference (single-agent), not a replacement for Logos. See LOG-44 below for the phased plan.

**LOG-44 Phase 1 status update (2026-04-17 afternoon):** Code-complete in worktree `logos-log44-hermes-server` branch `log44-hermes-server-prototype` (5 commits: `9f60d7ab` → `ac04ff13`). Ready for live testing behind two env flags:
- `LOGOS_HERMES_SERVER_MODE=1` — `spawn()` launches `hermes gateway run` in each new sandbox
- `LOGOS_DISPATCH_V2=1` — `_handle_chat` routes to HTTP-in-sandbox dispatcher when both set

All validated against live `hermes-henry` sandbox — 0.17s cold-start, real chat through `inference.local`. Remaining Phase 1 = user-driven live integration test (restart gateway with flags, destroy+recreate an agent, send a chat).

---

## P0 — Blockers / strategic

### LOG-44 · Migrate to Hermes-as-server per sandbox (autonomy unlock)
**Effort:** XL (2–3 weeks across phases) · **Type:** Architecture · **Status:** OPEN · **Owner:** —

**Goal:** Each agent's sandbox runs `hermes gateway run` long-lived, exposing an OpenAI-compatible HTTP API on localhost. Logos dispatches via HTTP-into-sandbox instead of stdin/stdout subprocess. Per-agent cron, boot hooks, native channel adapters all become available.

**Architecture target:**
```
Logos = orchestrator + multi-user web UI + auth + MCP server lifecycle +
        docker container plumbing + sandbox lifecycle (OpenShell) +
        cross-agent comparison + world view + agent registry
                          │
                          ├─ sandbox A: `hermes gateway run` (autonomous agent)
                          ├─ sandbox B: `hermes gateway run` (autonomous agent)
                          └─ sandbox C: `hermes gateway run` (autonomous agent)
```

**What stays in Logos** (unchanged or near-unchanged):
- Auth, RBAC, multi-user web UI
- MCP server lifecycle + per-agent MCP exposure
- Docker container / sandbox lifecycle (OpenShell executor)
- Agent registry, world view, comparison UI, soul library
- Cross-agent session_search aggregation
- Setup wizard, model routing config

**What moves into per-agent Hermes** (was reimplemented in Logos):
- The chat dispatch loop itself (Hermes serves `/v1/chat/completions`)
- Per-agent cron jobs
- Per-agent channel adapters (TG/Discord/Slack run in-sandbox as that agent's bot)
- Per-agent memories, sessions, plans, skills
- Boot hooks — autonomous startup behaviors per agent
- Pairing flow for DM users

**Phased plan:**

| # | Phase | Effort | Goal | Decision points |
|---|---|---|---|---|
| 44.1 | **Bootstrap** — Hermes server in one sandbox | M–L | New `Dockerfile.hermes-sandbox` installs hermes binary; entrypoint = `hermes gateway run`; `WorkerRegistry.dispatch_task` switches to HTTP POST (via `sandbox exec curl` to localhost:8642 OR unix socket). Chat works end-to-end with current UX. | Unix socket (cleaner) vs TCP+socat (NemoClaw shows it works) |
| 44.2 | **Per-agent config + autonomy primitives** | M | Logos writes per-agent `/sandbox/.hermes/config.yaml` at spawn (model, soul, system prompt, tools). Boot hooks (boot.md) wired. One scheduled cron job fires inside the sandbox as proof. | How souls map onto Hermes profiles/skins |
| 44.3 | **Channel adapters in-sandbox** | M–L | Yesterday's per-agent channel credentials (`agent_channel_credentials` table) push INTO each sandbox config. Each agent IS its own TG/Discord bot. OpenShell network policy permits per-agent egress. Logos's `gateway/channels/*` demotes to credential-storage + setup-UI. | Whether Logos retains a central "channel router" for agents that don't have their own bot vs full delegation |
| 44.4 | **Sessions + memory reconciliation** | M | Hermes owns per-agent SessionDB; Logos's session_search aggregates across sandboxes via per-sandbox HTTP. Memory tool unification (Hermes `builtin_memory_provider` vs Logos memory tools). | Hermes-owns-with-aggregation (recommended) vs Logos-owns-with-mount |
| 44.5 | **Tool/skill reconciliation** | M–L | Logos-specific tools migrate to Hermes plugin system. Logos still owns MCP server lifecycle and exposes them to each sandbox via Hermes's MCP config. Skills hub aligns with Hermes's plugin layout. | Which Logos tools are obsoleted by Hermes equivalents |
| 44.6 | **Cleanup** | S–M | Delete `docker/sandbox_worker.py` (no longer needed). Delete superseded Logos channel adapters. Deprecate or rescope Logos cron (cross-agent jobs only). | Keep `/setup` wizard logic on Logos side or migrate any of it into hermes onboarding |

**Acceptance for "this is done":**
- An agent with a cron entry in its Hermes config wakes up, decides to send a Telegram message, and does so without any Logos gateway involvement in the request path.
- Logos web UI shows the agent's activity (via session aggregation across sandboxes).
- Restart of Logos gateway does NOT interrupt agents' in-flight work or scheduled jobs.

**Risks / unknowns:**
- Hermes upstream may require version pinning to avoid surprise behavior changes per release.
- Per-sandbox memory baseline goes up (each runs full Hermes vs cold Python).
- Hermes's HTTP API surface may be missing things Logos relies on (e.g. specific streaming event types).
- Reconciling Hermes's `souls`-equivalent (profiles/skins) with Logos's existing soul library may need a translation layer.

**Don't start until:** LOG-24.v1 confirms current Plan A-prime chat actually works — we want a green baseline before swapping the dispatch model.

---

### LOG-24 · Verify Plan A-prime end-to-end + clean up stale WS references
**Effort:** S (30m–2h) · **Type:** Verification + cleanup · **Status:** PARTIAL · **Prereq for:** LOG-44

The transport refactor itself shipped (commits sometime between 2026-04-12 and 2026-04-16). What's left:

| # | Sub-task | Effort |
|---|---|---|
| 24.v1 | Run `/setup` end-to-end on a fresh state-dir, confirm chat works through Plan A-prime | S |
| 24.v2 | Inspect `worker_connected` / `worker_healthy` UI fields — green or grey? Wire to dispatch readiness if needed | XS |
| 24.c1 | Decide fate of `gateway/worker.py` + `logos_cli/main.py:2128 --connect` flag (legacy headless WS CLI — separate feature; delete or keep?) | XS |
| 24.c2 | Strip `/ws/worker` references from docs — *deprioritized: most of these will be rewritten by LOG-44 anyway. Skip unless trivial.* | XS |

Demoted from P0/L to P0/S — was the right ticket all along, but the heavy lifting is done.

---

## P1 — High priority

### LOG-25 · Multi-user hardening
**Effort:** L (2–3d) · **Type:** Feature/Security · **Status:** OPEN (foundation done, all sub-items pending) · **Cross-ref:** Sub-items 25.1/25.3 will need re-thinking after LOG-44.4 (sessions move into per-agent Hermes — "user can see own sessions" becomes "user can see sessions across the agents they own/share")

Audit confirmed every sub-item below is unstarted in code. RBAC scaffolding is there (`require_permission` decorator), but UI tabs render unconditionally and session listings don't filter by user.

| # | Sub-task | Effort | Notes |
|---|---|---|---|
| 25.1 | Per-user chat isolation (`user` role only sees own sessions) | M | No `user_id` filter on session list endpoints today |
| 25.2 | UI role gating (hide Admin/Config tabs from `user`/`viewer`) | S | `main_app.html` has `can('manage_*')` for actions but tabs are always rendered |
| 25.3 | Agent sharing rules (private vs shared visibility + edit perms) | M | `shared` column exists on agents table; not enforced on list endpoints |
| 25.4 | Settings scoping (admin-only: model routes, tools, policies) | S | UI side only — backend enforces |
| 25.5 | Per-user agent limits (`max_agents` column on users) | S | Column doesn't exist |
| 25.6 | Per-user daily budget caps (`daily_budget_usd` per user) | M | Column exists per-agent only |
| 25.7 | `/register` endpoint with optional approval gate | M | `allow_registration`/`require_approval` flags exist in `platform_settings`; route not wired |

### LOG-26 · Background embed-on-write for session search
**Effort:** M · **Type:** Feature · **Status:** OPEN · **Cross-ref:** Re-evaluate after LOG-44.4 — if Hermes owns per-agent sessions, embed-on-write moves into the per-sandbox layer; Logos's job becomes aggregating embeddings across sandboxes for cross-agent search.

`append_to_transcript()` in `core/state.py` writes to SQLite only — no embedding hook. Direct continuation of the `99748746` semantic search work from yesterday. Still worth doing pre-LOG-44 if you want passive recall in the next month — the work is small and the storage layer becomes a translation problem post-44.4, not a discard.

### LOG-27 · Auto-inject top-3 semantically similar past chats into prompt
**Effort:** M · **Type:** Feature · **Status:** OPEN · **Best-after:** LOG-26 · **Cross-ref:** Hermes upstream may already do this (its `agent/insights.py` etc.) — investigate during LOG-44.1 before duplicating.

Passive recall — agent gets relevant history without needing to call `session_search`. Embed coverage stays sparse without LOG-26, so do that first.

---

## P2 — Medium

### LOG-28 · Bidirectional reply push: web → Telegram
**Effort:** M · **Type:** Feature · **Status:** OPEN

`gateway/channels/telegram.py` is inbound-only today. Web replies on a TG-originated chat stay web-only.

### LOG-29 · Wire Telegram slash commands as `CommandHandler`s
**Effort:** S · **Type:** Bug · **Status:** OPEN

`/new`, `/reset`, `/model`, `/reasoning`, `/stop` are menu-hint stubs only — `CommandHandler = Any` per the audit, fall through as plain text.

### LOG-30 · Agent rename → auto-destroy old sandbox — **DONE (2026-04-17)**
**Effort:** S · **Type:** Bug · **Status:** DONE

`handle_agents_patch` now destroys the old `hermes-<old_name>` sandbox AND moves `~/.logos/agents/<old_name>/` → `<new_name>/` so memories follow the agent. Background task, PATCH response returns immediately.

### LOG-31 · GHCR installer wiring (continuation of #23)
**Effort:** M · **Type:** Infra · **Status:** PARTIAL

| # | Sub-task | Status |
|---|---|---|
| 31.1 | GHCR workflow file exists | DONE |
| 31.2 | `scripts/fresh-install.sh` pulls from GHCR (build = fallback) | DONE (2026-04-17) — ungated from `INSTALL_OPENSHELL=1`, tries GHCR first then falls back to local build |
| 31.3 | `gateway/executors/openshell.py::_DEFAULT_IMAGE` → GHCR tag | OPEN — still `hermes-sandbox:m12` |
| 31.4 | cosign signing | OPEN (defer to v1) |
| 31.5 | Trigger first publish run + confirm `:public` package | OPEN |

### LOG-32 · #17 Cache sandbox details (prevent blank-flash)
**Effort:** S · **Type:** Polish · **Status:** OPEN

Cache last-known sandbox values per-name in Alpine state; refresh-in-place on poll.

### LOG-46 · Wire sandbox auxiliary client to `inference.local`
**Effort:** S (30m–1h) · **Type:** Bug · **Status:** OPEN · **May be obsoleted by:** LOG-44.1

Warning observed inside sandbox 2026-04-17:
```
WARNING agent.auxiliary_client: Auxiliary auto-detect: no provider
available (tried: openrouter, nous, local/custom, openai-codex, api-key).
Compression, summarization, and memory flush will not work. Set
OPENROUTER_API_KEY or configure a local model in config.yaml
```

Main chat works fine — this is the upstream hermes auxiliary client that powers context compression, summarization, and memory flush. It looks at its own env-var chain (`OPENROUTER_API_KEY` / `AUXILIARY_PROVIDER` / `AUXILIARY_BASE_URL` / `AUXILIARY_MODEL` — exact names live in the sandbox image's `agent/auxiliary_client.py`, not this repo) and finds nothing because the sandbox only has the privacy-routed `https://inference.local/v1` channel wired for primary inference.

Fix direction: confirm the exact env names the upstream client reads, then set them in either the generated `instance-config.json` (uploaded by `OpenShellExecutor.spawn`) or the sandbox pod env so the auxiliary client reuses the `inference.local` path. Likely a ~10-line addition.

**Deprio note:** LOG-44 Phase 1 switches each sandbox to run the full `hermes gateway run` binary, at which point the auxiliary client is configured from Hermes's own config loader and this gap may disappear. Worth revisiting after LOG-44.1 lands rather than patching twice.

### LOG-33 · Thin desktop client (Tauri)
**Effort:** S (1–2h) · **Type:** Feature · **Status:** OPEN

WebView wrapper. ~3MB exe. Or Chrome PWA shortcut for zero build.

### LOG-47 · Per-agent sandbox snapshot + restore
**Effort:** M · **Type:** Feature · **Status:** OPEN · **Expands-with:** LOG-44.1

Today the sandbox is a passive exec environment (Plan A-prime) and most durable agent state lives on the host in `~/.logos/`. But the sandbox still has state worth preserving across destroy/recreate: the uploaded memories in `/tmp/hermes/memories/`, any workspace scratch files the agent wrote, browser cookies/session state, terminal history. Today there is no way to snapshot a running sandbox, export it, or restore it onto a fresh sandbox later.

After LOG-44.1 lands, Hermes-in-sandbox owns sessions / memories / plans / hermes-local DB — the sandbox becomes the source of truth, not a scratch space. Snapshot coverage needs to widen to those paths too, but the primitive is the same.

| # | Sub-task | Effort | Notes |
|---|----------|--------|-------|
| 47.1 | `openshell sandbox download <name>:<src> <dst>` wrapper | XS | Already exists as a CLI subcommand — wrap in Python as `executors/openshell.py::download_from_sandbox(...)`. |
| 47.2 | `SandboxSnapshotter.snapshot(agent_name, dest_dir)` helper | S | Tars `/tmp/hermes/`, any workspace dirs, selected hermes paths into `~/.logos/backups/<agent>/<timestamp>.tar.gz`. One source of truth for "what gets backed up" in a declarative list. |
| 47.3 | `/admin/agents/<id>/backup` HTTP POST + "Backup now" button | S | Manual trigger from Admin → Agents detail pane. Returns path + size. |
| 47.4 | Retention: keep last N per-agent | XS | Config `LOGOS_SANDBOX_BACKUP_RETENTION=10`; prune on write. |
| 47.5 | Restore flow: upload tarball → new sandbox | S | Destroy sandbox → spawn fresh → `openshell sandbox upload` contents back into the original paths → `openshell sandbox exec -- tar xf ...` if compression used. |
| 47.6 | Scheduled snapshots (cron integration) | S | Re-use existing `~/.logos/cron/` plumbing. Default: daily 03:00 local, per-agent. Toggle per agent in Admin UI. |
| 47.7 | LOG-44 widening: include `/sandbox/.hermes/`, hermes SessionDB, plan state | S | Update the declarative snapshot-paths list when LOG-44.1 merges. Same primitive; bigger scope. |

**Restore testing:** verify `snapshot → destroy → spawn → restore` round-trips cleanly on a warm agent (chat state should survive). Fail-safe: if restore fails, agent still spawns cleanly from its host-side `~/.logos/agents/<name>/memories/` — the snapshot is additive, never load-bearing.

**Not in scope:** off-host backup destinations (S3, rsync, borg, etc.) — start with local tarballs, let the user plug in their own rsync cron if they want offsite. Adding cloud backends is a future follow-up, not blocking this.

### LOG-45 · Pluggable runtime images per agent
**Effort:** M · **Type:** Feature · **Status:** OPEN · **Best-after:** LOG-44.1 (HTTP-in-sandbox contract makes this near-trivial)

Today `gateway/executors/openshell.py::_DEFAULT_IMAGE` is one hardcoded tag (`hermes-sandbox:m12`) used by every agent. Any sandbox image that honors Logos's dispatch contract could slot in instead (NemoClaw's Hermes binary, a Claude Code Agent SDK container, `mini-swe-agent`, Codex-style images, etc.), but there's no per-agent selection.

The pre-LOG-44 dispatch contract is opinionated (stdin/stdout JSON framing via `python3 /app/sandbox_worker.py`). Post-LOG-44.1 it becomes just "expose OpenAI-compat `/v1/chat/completions` on port 8642", which is a standard that off-the-shelf agent images already speak. That's why this ticket is best-scoped after LOG-44.1.

| # | Sub-task | Effort | Notes |
|---|----------|--------|-------|
| 45.1 | `agents.image` nullable column (migration) | XS | Falls back to `_DEFAULT_IMAGE` when null. |
| 45.2 | Thread through `InstanceConfig.image` → `OpenShellExecutor.spawn` | S | Replace `self.sandbox_image` with `config.image or self.sandbox_image`. |
| 45.3 | Blessed-image registry (YAML or DB) with runtime metadata | S | Name, tag, runtime kind (hermes / claude-sdk / custom), contract version, default soul compat. Used by the UI picker + a future `logos image sync` command. |
| 45.4 | `/admin/agents` image picker in the agent-edit form | S | Dropdown of registered images + manual tag entry. |
| 45.5 | Pre-spawn image availability check + auto-pull fallback | S | At spawn time, if the resolved image isn't on host, try `docker pull` before `_ensure_image_in_cluster`; surface as a `pre_spawn` sub-stage label. |
| 45.6 | Per-image network policy presets | M | Different runtimes need different policies (e.g. a Claude-SDK image needs `api.anthropic.com`, not `inference.local`). Gate behind the existing policy preset system. |
| 45.7 | Docs: "Adding a new runtime" walkthrough | S | Point at NemoClaw `Dockerfile` as the canonical reference post-LOG-44. |

**Open questions:**
- One image = one runtime kind, or can images advertise multiple modes? (YAGNI — start with 1:1.)
- Image registry source of truth: ship with Logos, or pull from a ghcr manifest? (Start shipped; migrate if community images emerge.)

---

## P3 — Low / polish

### LOG-34 · Real slim sandbox image (replace orphan Dockerfile)
**Effort:** L · **Type:** Infra · **Status:** OPEN · **Independent of LOG-24** (Plan A-prime kept the same image baseline)

Audit `/opt/hermes` to carve a 1–2 GB image. Migrate browser tools to `@playwright/mcp`. Delete the orphan `Dockerfile.hermes-sandbox`.

### LOG-35 · UI consistency micro-fixes
**Effort:** XS each · **Type:** Polish · **Status:** OPEN

| # | Item | Status |
|---|---|---|
| 35a | Audit log pagination right-aligned (match Runs tab) | OPEN |
| 35b | Runs origin badges: `platform_telegram` styled pill (match `user_chat`) | OPEN |
| 35c | Rebuild `assets/tailwind.css` (10-day stale per audit) | DONE (2026-04-17) — rebuilt via `npx tailwindcss@3`; 46.9KB → 52KB |

### LOG-36 · Sub-agent live execution: per-sub-agent boxes
**Effort:** M · **Type:** Polish · **Status:** OPEN

### LOG-37 · Periodic backfill cron for embeddings
**Effort:** S · **Type:** Feature · **Status:** OPEN · **Best-after:** LOG-26

### LOG-38 · Lightweight Python embedding fallback
**Effort:** M · **Type:** Feature · **Status:** OPEN

`sentence-transformers` not in pyproject. Currently embeddings silently return empty when no LM Studio/Ollama endpoint is reachable.

### LOG-39 · "Show hidden" toggle for soft-deleted sessions
**Effort:** S · **Type:** Feature · **Status:** OPEN

Soft-delete (`hidden=1`) ships. Toggle to show/restore doesn't.

### LOG-40 · Platform badge in chat header ("via Telegram")
**Effort:** XS · **Type:** Polish · **Status:** OPEN

### LOG-41 · `get_current_time` MCP tool
**Effort:** S · **Type:** Feature · **Status:** OPEN

### LOG-42 · `/setup` IANA timezone dropdown
**Effort:** S · **Type:** Feature · **Status:** OPEN (low priority — punt unless asked)

### LOG-43 · Trim Telegram command menu (drop `/update`, `/reload_mcp`, `/provider`)
**Effort:** XS · **Type:** Polish · **Status:** OPEN

---

## Documented limits (no work — record only)

- **LM Studio reasoning toggle** is detection-only; no parameter combo disables qwen3.5 thinking through OpenAI-compat.
- **Single-LM-Studio VRAM ceiling** — user-controlled config.
- **Worker WS frame parser blocks during inference** — superseded by Plan A-prime (whole transport replaced).

---

## Today's recommendation (2026-04-17, post-decision on LOG-44)

The strategic direction is set: **LOG-44 (Hermes-as-server per sandbox)** is the path to autonomy. Today is best spent (a) confirming current state isn't broken so we have a green baseline, then (b) starting the LOG-44.1 prototype.

### Shape D — "Baseline + LOG-44.1 prototype day" (recommended given direction lock-in)

**Morning (~1h):** LOG-24.v1 sanity check — fresh `/setup`, send a chat, confirm Plan A-prime works end-to-end. If anything fails, *that* becomes the day. If green, proceed to afternoon.

**Afternoon (~3–4h):** Begin LOG-44.1 prototype. Concrete first steps:
1. Read `knowledge-repos/hermes-agent/gateway/run.py` + `gateway/platforms/api_server.py` — understand the `hermes gateway run` HTTP surface (endpoints, streaming protocol, health check).
2. Write a throwaway `Dockerfile.hermes-server-test` based on `Dockerfile.hermes-upstream` that runs `hermes gateway run` instead of `sleep infinity`.
3. `docker build`, `docker run`, `curl localhost:8642/health`, `curl localhost:8642/v1/chat/completions` with a real LM Studio backend through `inference.local`.
4. Document findings in a new `docs/architecture/hermes-as-server-prototype.md` — what worked, what didn't, surprises, decisions to make for Phase 1 proper (unix socket vs TCP, auth model, config delivery).

**End of Friday:** green baseline + concrete first-hand knowledge of the upstream HTTP surface + a written prototype report. Monday opens with informed Phase 1 implementation, not blind exploration.

### Shape A (still valid) — "Close out #24 + memory continuation"
If you want momentum on existing-architecture features before the LOG-44 commitment, this still works:
- Morning: LOG-24.v1 + 24.v2 (~1h)
- Afternoon: LOG-26 (background embed-on-write, ~3h)
- LOG-26 is not wasted under LOG-44 — see cross-ref note on the ticket.

### Shape B (still valid) — "Quick wins Friday"
Same as before. Fine if you'd rather defer LOG-44 prep to Monday.

---

**My pick: Shape D.** You've made the strategic call — the cheapest way to de-risk it is a hands-on prototype today that returns concrete knowledge by EOD. Pure desk research is a worse use of the time; small commits clearing trivia (Shape B) is fine but leaves the big bet uninformed. Shape D gets you a written report Monday-you can read before writing the real code.
