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

**LOG-44 Phase 1 status update (2026-04-17 evening):** ✅ **Live-validated end-to-end** on branch `log44-hermes-server-prototype` (rebased onto latest main; 5 feature commits on top). Behind two env flags:
- `LOGOS_HERMES_SERVER_MODE=1` — `spawn()` launches `hermes gateway run` in each new sandbox
- `LOGOS_DISPATCH_V2=1` — `_handle_chat` routes to HTTP-in-sandbox dispatcher when both set

Proven: dispatch_task_v2 → openshell exec (inner netns) → hermes @ 127.0.0.1:8642 → `https://inference.local/v1` (openshell cluster inference) → LAN LM Studio (qwen3.5-9b). Real chat response streamed back through SSE as task_result frame. **Phase 1 is done; moving to Phase 2.**

Gotchas surfaced during validation (all fixed in-branch — see commit log + memory `log44_phase1_live_validation.md`):
- Hermes listens in sandbox's inner netns — only `openshell sandbox exec` reaches it (kubectl exec can't).
- `enable_hermes_server_mode()` must be invoked via the spawn path so the setup dict is persisted to executor state; manual invocation leaves dispatch_v2 unable to find the key. Resurrect path should be audited.
- Hermes has a second auth layer (user allowlist) on top of the bearer token; `.env` must include `GATEWAY_ALLOW_ALL_USERS=true` or every /v1/runs returns 401.
- `launch_hermes_gateway` must source `.env` before `nohup hermes` — background shell drops non-exported vars.
- Openshell cluster inference only accepts provider types `openai/anthropic/nvidia`; `generic` is rejected. The config key for OpenAI-compatible upstream is `OPENAI_BASE_URL` (not `base_url`).

---

## P0 — Blockers / strategic

### LOG-44 · Migrate to Hermes-as-server per sandbox (autonomy unlock)
**Effort:** XL (2–3 weeks across phases) · **Type:** Architecture · **Status:** PHASE 1 DONE — Phase 2+ OPEN · **Owner:** —

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
| 44.1 | **Bootstrap** — Hermes server in one sandbox ✅ **DONE 2026-04-17** | M–L | Shipped via `hermes_server_mode.py` + `worker_registry_v2.py`. Sandbox image already ships hermes binary (`knowledge-repos/hermes-agent` build), so no new Dockerfile — we write config.yaml + .env into `/tmp/hermes-srv-home/` and launch `nohup hermes gateway run` from spawn. Dispatch is via `openshell sandbox exec` (chosen over unix socket — transport's NAT'd into the inner netns anyway, and exec gives us stdin/stdout framing for the SSE parse). Live chat validated end-to-end. | Socket-vs-exec answered: exec. |
| 44.2 | **Per-agent config + autonomy primitives** — boot hooks DONE 2026-04-18; cron proof blocked by sandbox image | M | Logos writes per-agent `/sandbox/.hermes/config.yaml` at spawn (shipped 44.1). Boot hooks: `deploy_boot_md` uploads `souls/<name>/boot.md` → `/tmp/hermes-srv-home/BOOT.md` at spawn (commit `408ad04`); hermes's built-in `boot_md` hook runs the agent on its contents at every `gateway:startup`. Live-verified — a test `BOOT.md` instructed the agent to call `write_file` for `~/outputs/boot_log.md`, hook fired, file appeared with correct ISO timestamp. Souls README at `souls/README.md` documents the convention. Cron proof blocked: sandbox image is missing `croniter` and openshell's network policy blocks `pip install` from inside — see LOG-34 for the image fix. | Decided: no separate Hermes profile/skin layer; souls map 1:1 via `system_prompt` + optional `boot.md`. |
| 44.3 | **Channel adapters in-sandbox** | M–L | Yesterday's per-agent channel credentials (`agent_channel_credentials` table) push INTO each sandbox config. Each agent IS its own TG/Discord bot. OpenShell network policy permits per-agent egress. Logos's `gateway/channels/*` demotes to credential-storage + setup-UI. | Whether Logos retains a central "channel router" for agents that don't have their own bot vs full delegation |
| 44.4 | **Sessions + memory reconciliation** — A.1 DONE 2026-04-18; A.3–A.5 tracked as LOG-52 | M | Option A picked: Hermes owns per-agent SessionDB, Logos fans out HTTP queries. A.1 (/v1/runs auto-loads history from SessionDB via LOG-51.2 monkeypatch) shipped commit `3eb6d5f` — within-chat continuity works on v2. A.2 was a no-op on inspection (Logos session_id already stable per chat; earlier fragmentation was chat_id regeneration from lost localStorage). Memory tool unification still open; moves to LOG-44.5. | Decided: Hermes-owns-with-aggregation. See LOG-52 for A.3 (sandbox query endpoints), A.4 (aggregator), A.5 (retire Logos local transcripts). |
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

### LOG-50 · Persist LOG-44 env flags across `logos gateway restart` — **DONE (2026-04-18)**
**Effort:** XS (15–30m) · **Type:** DX/bug · **Status:** DONE · **Surfaced:** 2026-04-17 post-Phase-1 validation

`~/.logos/env` is now auto-sourced on start/restart (openshell convention). Two load points so both spawn paths cover it:

- `logos_cli/gateway.py::run_gateway_detached` overlays KEY=VALUE lines into the subprocess `env` dict *after* `os.environ.copy()` and before Popen — shell exports still win (no override).
- `gateway/run.py` calls `load_dotenv(~/.logos/env, override=False)` alongside the existing `~/.logos/.env` load for direct `python -m gateway.run` launches.

Verified: shell env stripped of both flags, gateway spawned via `run_gateway_detached()` shows both `LOGOS_HERMES_SERVER_MODE=1` and `LOGOS_DISPATCH_V2=1` in `/proc/<pid>/environ`, sourced from the file.

**Option (2) remains OPEN** — flipping the feature-gate defaults so v2 is the only path is still LOG-44.6 cleanup work.

**Trip-wire test:** restart gateway with no explicit env, confirm chat reaches hermes-in-sandbox (i.e. `_use_v2=True` in the dispatch log line).

---

### LOG-51 · v2 cancel parity + Live-Executions Stop button — **DONE (2026-04-18)**
**Effort:** S (30m–2h) · **Type:** Feature/bug · **Status:** DONE (51.6 pending live-tool observation) · **Surfaced:** 2026-04-18 immediately after LOG-44 Phase 1 landed on main · **Prereq for:** any meaningful long-running agent use on v2

**Symptom.** The Stop button in the chat header still renders for v2 chats (v2 emits `task_started` with a `task_id`, so `activeTaskId` is set), but clicking it returns 404 because the v1 `WorkerRegistry.cancel_task` only knows its subprocess `_in_flight` dict. `worker_registry_v2.py` has no `cancel_task` at all. A user watching an agent iterate has no working way to stop it; the only ways out today are `openshell sandbox exec --name <sb> -- pkill -f "hermes gateway run"` (kills the whole hermes process, loses all in-flight work) or waiting for `max_iterations` to trip.

Live-Executions observability also needs a pass: the seed row ("thinking…") does render for v2 because `_session_status[session_key]` is populated before dispatch regardless of path, but per-tool updates depend on hermes upstream actually emitting `tool.start`/`tool.end` SSE events in the shape `worker_registry_v2.py` maps. Worth confirming on a live run before trusting the translation.

**Design — where the Stop control lives.**
Put it on each Live-Executions row, not just the chat header. Three reasons: (a) you're already looking at Live Executions while watching an agent iterate, (b) multi-agent case — chat-header Stop only targets the focused chat, (c) cron/boot-hook/channel-origin runs have no chat for the header button to attach to.

**Sub-tasks:**

| # | Item | Effort | Status |
|---|---|---|---|
| 51.1 | Probe upstream hermes for a run-cancel endpoint | XS | DONE — no cancel endpoint; only `/v1/responses` interrupts on disconnect, `/v1/runs` doesn't. Logos-side monkeypatch chosen over fork. |
| 51.2 | hermes cancel monkeypatch + wrapper launcher | S | DONE — `gateway/executors/hermes_cancel_monkeypatch.py` rebinds `APIServerAdapter._handle_runs` + `_handle_run_events` to mirror the `/v1/responses` disconnect-interrupt pattern. Delivered at spawn via `hermes_server_mode.deploy_cancel_monkeypatch`, launched via `python3 <patch>.py gateway run -v`. Live-verified: `logos.cancel_patch: SSE client disconnected, interrupted run …` fires on disconnect, subsequent runs still complete normally. |
| 51.3 | `worker_registry_v2.cancel_task(task_id)` + dispatcher routing | S | DONE — two-step termination (host `proc.terminate()` + in-sandbox `pkill -f /tmp/_disp_v2_client_<task_id>.py`). The pkill is load-bearing because `openshell sandbox exec` does NOT forward signals. `_handle_chat_cancel` tries v1 first, falls through to v2. |
| 51.4 | Thread `task_id` into `_session_status[session_key]` | XS | DONE — `http_api.py:4024` + serializer at `:1660`. |
| 51.5 | Per-row Stop button in Live-Executions UI | XS | DONE — red-outline micro-button in `main_app.html:1461`, `cancelLiveTask(taskId)` Alpine method, hidden via `x-show="s.task_id"` on legacy rows. |
| 51.6 | Confirm `tool.start`/`tool.end` events actually flow through v2 on a real run | XS | PENDING (observational) — open Live Executions on a hermes-hermes chat, count rows. If stuck on "thinking…", v2's SSE translation may be missing the `tool.*` mappings that `worker_registry_v2.py` expects. |

**Acceptance:**
- Stop button on a Live-Executions row for a v2 dispatch actually stops the agent (no more iteration after ~1s).
- Chat-header Stop also works (same endpoint).
- Each tool call the agent makes shows up in Live Executions as its own row, same UX as v1 used to have.

**Risk:** if hermes upstream has no graceful cancel, 51.2's fallback is heavy-handed (kill+restart of the in-sandbox hermes, losing any unflushed memory/session state). Document + surface in the UI as a warning on the Stop confirm, rather than silently destroying state.

**Upstream finding (2026-04-18):** hermes 0.7.0 exposes only `POST /v1/runs` + `GET /v1/runs/{id}/events` — no cancel endpoint, and the events handler does NOT interrupt the agent on client disconnect (only the sibling `/v1/responses` handler does that). Upstream already has the primitives (`agent.interrupt()` at `run_agent.py:3086` which the run loop honors at 20+ checkpoints including mid-LLM-stream HTTP-close at `run_agent.py:5139`), they just aren't wired on `/v1/runs`. So 51.2 ships as a **Logos-side monkeypatch** delivered at sandbox spawn — the sandbox image stays pristine (no hermes-agent fork), the patch rides in on upload, and it's `try/except AttributeError`-guarded so a hermes bump that renames internals makes cancel *stop working* rather than break boot.

**Temporary by design:** file an upstream issue on hermes-agent asking for either `POST /v1/runs/{id}/cancel` or SSE-disconnect-interrupt parity with `/v1/responses`. When that ships, delete the monkeypatch entirely — cancel becomes part of the runtime-agnostic contract Logos expects of any sandbox image. See LOG-45's contract list.

---

### LOG-55 · Sidestep Playwright for JSON-GET cases — **DONE (2026-04-18, pivoted)**
**Effort:** XS · **Type:** Feature · **Status:** DONE · **Shipped as:** soul-fragment guidance

Original plan was a dedicated `http_get` tool. Abandoned after implementation hit a toolset-registration wall: hermes's API-server platform picks its tool list from a static `hermes-cli` composite (see `hermes_cli/tools_config.py::_get_platform_tools`), and registering a new tool under an existing toolset (e.g. `browser`) breaks that toolset's subset-inference so it gets kicked out of `enabled_toolsets` entirely. Adding a properly-visible tool would require either rewriting `config.yaml` to explicitly enumerate every configurable toolset, or upstreaming the new tool into hermes's own composite — both bigger than the LOG-55 value.

**What actually shipped:** soul-fragment + preset-doc guidance pointing agents at `execute_code` (already in every agent's toolset) for HTTP fetches via stdlib `urllib`. Zero plumbing, zero new toolsets, same user-facing outcome — agent can hit SearxNG/REST APIs without needing Chromium.

- `souls/_shared/workspace.md` grew a "Web search and HTTP fetches" section with a complete urllib-based SearxNG pattern and a clear "reach for `execute_code` first" rule.
- `gateway/policies/presets/searxng.yaml` header rewritten to recommend the execute_code path (cheap, no Chromium) over the browser_navigate recipe (still documented for rendered-page cases).

**Caveats:**
- Leans on prompt compliance. Agents that don't follow guidance will still try browser_navigate first.
- Context-window cost of the extra guidance: ~300 tokens per prompt (we pay this regardless of whether the agent uses it). Acceptable.
- If we ever want a real first-class `http_get` tool, it ships with LOG-44.5 (tool/skill reconciliation) when we redo the Logos→hermes toolset handoff properly.

---

### LOG-56 · Bake Playwright browsers into the sandbox image
**Effort:** S (30m–2h) · **Type:** Infra · **Status:** OPEN · **Surfaced:** 2026-04-18 same session as LOG-55 · **Cross-ref:** LOG-34 (must-include deps list)

The `browser` toolset (`browser_navigate`, `browser_click`, `browser_vision`, `browser_snapshot`, etc.) relies on Playwright driving Chromium. Neither the Python package nor the browser binaries are in `hermes-sandbox:m12`. `playwright install chromium` downloads ~150 MB from `playwright.azureedge.net` — blocked by the sandbox's network policy, so agents can't self-recover at runtime.

**Fix:** add to the image build (LOG-34 scope):
```
RUN pip install playwright
RUN playwright install --with-deps chromium
```
`--with-deps` pulls in the Debian `libs` Chromium needs (glibc ⋅ atk ⋅ libx11 ⋅ etc). Image size grows by ~400 MB — acceptable for a pre-packaged workstation image, worth trimming if we drop to a slimmer browser later (`@playwright/mcp` path in LOG-34).

**Acceptance:** fresh-spawned sandbox, agent asks to navigate to a URL, first `browser_navigate` call succeeds with no install-time network activity.

**Relationship to LOG-55:** LOG-55 sidesteps Chromium for simple JSON GETs (covers SearxNG, most APIs). LOG-56 enables the full browser tool for rendered-page use cases. Ship both — they're complementary.

---

### LOG-54 · Fix hermes context compression in Logos-managed sandboxes
**Effort:** S (30m–2h) · **Type:** Bug · **Status:** OPEN · **Surfaced:** 2026-04-18 testing LOG-44.4 A.1 on a 207-msg session

**Symptom.** A chat turn on the Hermes agent (207 messages loaded from SessionDB via the A.1 monkeypatch) ran for the full 10-min dispatch timeout. In-sandbox session log shows qwen3.5-9b emitted `write_file({})` with empty args six times in a row, got an error each time, then two empty-response turns, then gateway timed out. Classic "model capacity exceeded" failure mode.

**What should have happened.** Hermes has a built-in `ContextCompressor` (`run_agent.py:1342-1494`) that automatically summarizes old turns when prompt size hits 50% of the model's context window (`protect_last_n=20` keeps recent messages verbatim). With that working, 207 messages → summary + last 20 → fits comfortably in any sane context window. Compression didn't happen (or happened too late), so the full 207 messages hit the model.

**Three suspects:**
1. **Our `config.yaml` has no `compression:` block.** Hermes defaults *should* apply without config, but worth confirming by explicitly writing the section in `_build_config_yaml`.
2. **Auxiliary client failed silently.** Compression needs an LLM call (to the auxiliary summarizer model) to produce the condensed message. If that call errors against `inference.local` (wrong endpoint, timeout, provider-type rejection), compression silently skips. The in-sandbox log has `Auxiliary auto-detect: using main provider custom (qwen3.5-9b)` — using the same model as main, probably fine, but no success/fail line confirming the summarizer actually ran.
3. **Model context-length detection defaults to 128K.** Log: `Could not detect context length for model 'qwen3.5-9b' at https://inference.local/v1 — defaulting to 128,000 tokens (probe-down).` Most qwen3.5-9b deploys ship 32K, not 128K. With the fake 128K ceiling, compression's 50% threshold fires at 64K tokens — way past the real 32K limit, so the prompt OOMs at the model before compression kicks in.

**Fix direction:** pin context length in the generated `config.yaml` (e.g. `model.context_length: 32768`) so compression thresholds are computed against reality, not a probe-down default. Also emit the `compression:` block explicitly so future hermes defaults changes can't regress us. Possibly expose the context_length as a per-model override in Logos's model_routes table.

**Acceptance:** 200+ message session produces a prompt the model can handle without looping on malformed tool calls. Gateway dispatch completes in <5 min for a 1200-word essay even on a deeply populated session.

**Related:** LOG-34 (image rebuild can include context_length metadata per known model). LOG-44.4 A.1 (which loads the full history — correct behaviour, not a bug once compression works).

---

### LOG-53 · UI pre-flight probe for hallucinated file paths
**Effort:** S (30m–2h) · **Type:** Polish/defense · **Status:** OPEN · **Surfaced:** 2026-04-18 during live testing · **Relates:** `feat(chat): click agent-cited file paths to download them` (commit `0eb596f`), `fix(souls): forbid citing fake file paths` (commit `ec653c8`)

**Symptom observed live.** User asked Hermes for a 1000-word essay on minoxidil. Agent produced the essay in its reply, did NOT call `write_file`, then said *"I've saved it to /tmp/hermes/minoxidil_infant_essay.md"*. The linkified path rendered as a clickable anchor via `_linkifySandboxPaths` (main_app.html:8920). User clicked, browser hit `/admin/agents/<id>/sandbox-files/download?path=…`, gateway correctly returned 404 (file didn't exist), user got a broken-link UX — "site not available" style error — rather than an inline indication that the agent had hallucinated.

Fix at the soul layer already shipped (`souls/_shared/workspace.md` now forbids citing paths the agent didn't actually write). This ticket is the **belt-and-suspenders** defense: don't trust prompt compliance, verify the file exists before advertising the click.

**Sub-tasks:**

| # | Item | Effort | Notes |
|---|---|---|---|
| 53.1 | `_linkifySandboxPaths` schedules a HEAD (or small GET with a sentinel) per distinct path when the chat bubble mounts | XS | Alpine `x-init` on the bubble, or a post-render pass. De-duplicate — one probe per path even if cited twice in the same reply. |
| 53.2 | Cache probe results in an Alpine Map keyed on `{agent_id}:{path}`, TTL ~30s | XS | Avoid re-probing on scroll or re-render. Invalidate when the user navigates or when a tool event for that path lands on the SSE stream (means the file was just written/modified — probe it fresh). |
| 53.3 | When probe returns 404, swap the anchor's style class (dim text, strikethrough, ⚠ icon) and set tooltip to *"Agent cited this path but the file isn't there — it may have been hallucinated or deleted"* | XS | Keep the anchor clickable (user might still want to hit it and see the raw 404) but visually communicate missing. |
| 53.4 | Apply the same probe on history replay (loading an old chat) so past hallucinations are visibly flagged, not just new ones | XS | `renderMsg` is called for both live and replayed messages; the probe path is the same. One flag to gate (skip probing on bulk history reload if it's slow; probe on viewport). |
| 53.5 | Ignore-list for paths that are real but temporary (e.g. `/tmp/_disp_v2_client_*.py` from LOG-51.3, short-lived per-dispatch scratch) | XS | Not citable by agent replies in normal flow; only included for completeness. Regex-skip on known Logos-internal prefixes. |

**Non-goals:**
- Don't try to detect hallucination *before* the reply renders — waiting for `write_file` tool events to land before rendering the anchor would feel sluggish. Probe-after-render is fine; anchors are styled immediately, updated on probe resolve.
- Don't auto-offer to regenerate or re-save — too surgery-y for a catch-hallucination UX. Surface the issue, let the user ask the agent to actually save.

**Acceptance:**
- Replay the minoxidil session: the `/tmp/hermes/minoxidil_infant_essay.md` anchor in Hermes's reply renders with strikethrough and a tooltip explaining the file is missing.
- A fresh turn where the agent calls `write_file` then cites the path: anchor renders normally, probe returns 200, user can click and download.
- Probing doesn't DoS the gateway (cache + de-dupe per chat bubble).

---

### LOG-52 · Cross-agent session aggregator + retire Logos local transcripts
**Effort:** M (2h–1d) · **Type:** Architecture · **Status:** OPEN · **Surfaced:** 2026-04-18 during LOG-44.4 A.1 research · **Blocks:** clean closure of LOG-44.4

Followup to LOG-44.4 A.1 (hermes `/v1/runs` auto-loads history from SessionDB — shipped in commit `3eb6d5f`). A.1 gave us within-chat continuity for v2 dispatches; this ticket does the rest of Option A ("Hermes owns sessions, Logos aggregates"):

| # | Sub-task | Effort | Notes |
|---|---|---|---|
| 52.1 | Monkeypatch hermes: `GET /v1/sessions` (list) + `GET /v1/sessions/{id}` (full transcript) + `GET /v1/sessions/search?q=…` (FTS via `messages_fts`) | S | Thin wrappers over SessionDB + messages_fts. Deliver via the same spawn-time upload pipe as LOG-51.2 (extend `hermes_cancel_monkeypatch.py`, rename to `hermes_logos_monkeypatch.py` at that point). Schema already has full metadata: id/source/model/system_prompt/message_count/tokens/cost/title. |
| 52.2 | Logos session_search fans out via HTTP to every running sandbox's GET /v1/sessions/search and merges | S | Current session_search reads `~/.logos/sessions/*` + its SQLite. Swap the backend for a fan-out aggregator keyed on the agent registry (`openshell_instances.json` gives sandbox names + hermes_server_setup/base_url + api_key). Cache per-sandbox responses for ~30s so rapid UI refreshes don't DoS each agent. |
| 52.3 | Logos UI session list pulls from aggregator instead of local storage | S | Sidebar "Recent chats" currently reads from Logos sessions. Repoint at `/admin/sessions` backed by aggregator. List shape largely the same — add an `agent_id` field so cross-agent display can group/filter. |
| 52.4 | Retire Logos's local transcript writes (`append_to_transcript`) + LOG-26 embed-on-write for v2 chats | XS | Under Option A, Logos no longer writes its own transcript copy for v2 dispatches — hermes owns it. Append becomes a no-op guarded by `hermes_server_setup present on worker`. Embedding moves into hermes (next follow-up, not this ticket). |
| 52.5 | Logos's `~/.logos/sessions/*` + `sessions.json` become append-only legacy state | XS | New chats don't write here; existing entries are read-only for chat_id → session_id lookups. Eventually prune (follow-up ticket). |

**Open questions:**
- **Cross-agent search UX:** does the search box show "Hermes said X about minoxidil" (agent-scoped result) or "minoxidil was mentioned in session Y" (session-scoped with agent as metadata)? Lean toward agent-scoped.
- **Auth on the new sandbox query endpoints:** same API_SERVER_KEY Bearer that /v1/runs uses, reachable only via openshell exec from the Logos host. Don't need per-user scoping yet (multi-user hermes-in-sandbox is LOG-44.x territory).
- **What happens when a sandbox is down:** aggregator degrades — returns results from available sandboxes + a "stale for agent X" marker. Don't block the search on a single unreachable agent.

**Acceptance:**
- User sends "find our past minoxidil conversation" in search — results come from hermes SessionDB via fan-out, not Logos's local store.
- Deleting a chat in the UI deletes from hermes SessionDB, not from Logos local (LOG-44.4 A.5).
- Logos's local `~/.logos/sessions/` stops growing for new chats.

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

### LOG-25 · Multi-user hardening — **DONE (2026-04-17)**
**Effort:** L (2–3d) · **Type:** Feature/Security · **Status:** DONE · **Cross-ref:** Sub-items 25.1/25.3 will need re-thinking after LOG-44.4 (sessions move into per-agent Hermes — "user can see own sessions" becomes "user can see sessions across the agents they own/share")

| # | Sub-task | Effort | Status |
|---|---|---|---|
| 25.1 | Per-user chat isolation (`user` role only sees own sessions) | M | DONE — `/admin/dispatches` route gate relaxed to `view_runs` + handler applies `user_id` filter for non-admin/operator |
| 25.2 | UI role gating (hide Admin/Config tabs from `user`/`viewer`) | S | DONE — Config leak (`\|\| can('view_evolution')`) removed; admin sub-tabs were already guarded |
| 25.3 | Agent sharing rules (private vs shared visibility + edit perms) | M | DONE — list_agents + patch/delete guards already enforced shared/creator; added `_handle_chat` check that returns 404 for non-creator on unshared agents |
| 25.4 | Settings scoping (admin-only: model routes, tools, policies) | S | DONE — existing `can('manage_*')` pattern covers it; backend already enforced via `require_permission` decorators |
| 25.5 | Per-user agent limits (`max_agents` column on users) | S | DONE — v25 migration + count_agents_by_creator + 429 `agent_limit_reached` in `handle_agents_post`; admins bypass |
| 25.6 | Per-user daily budget caps (`daily_budget_usd` per user) | M | DONE — v26/v26b migrations + user_cost_rollup_24h + `_handle_chat` user-scoped gate before the per-agent gate; cost_log.user_id attribution via dispatch task_id lookup |
| 25.7 | `/register` endpoint with optional approval gate | M | DONE — `POST /auth/register` backend + inline login-page form (mode toggle) + Admin → Users registration-settings card (allow_registration + require_approval checkboxes). Approval flow reuses existing Admin → Users table (update_user already accepts status) |

### LOG-26 · Background embed-on-write for session search — **DONE (2026-04-17)**
**Effort:** M · **Type:** Feature · **Status:** DONE · **Cross-ref:** Re-evaluate after LOG-44.4 — if Hermes owns per-agent sessions, embed-on-write moves into the per-sandbox layer; Logos's job becomes aggregating embeddings across sandboxes for cross-agent search.

`gateway/session.py::append_to_transcript` now fires a background thread after each SQLite insert that looks up the inserted row's id and calls `embed_message()` — so new user/assistant messages are searchable via semantic session_search without waiting for the backfill cron. Best-effort: when the embedding backend is offline (LM Studio not loaded, Ollama not started), the row stays unembedded and the periodic backfill picks it up later. Tool calls / tool results excluded (same filter as backfill). Also stripped the now-redundant synchronous embed block from `run.py::dispatch_platform_message` — centralised in append_to_transcript means every call site (web, platform, cron, tests) gets embeds for free.

### LOG-27 · Auto-inject top-3 semantically similar past chats into prompt
**Effort:** M · **Type:** Feature · **Status:** OPEN · **Best-after:** LOG-26 · **Cross-ref:** Hermes upstream may already do this (its `agent/insights.py` etc.) — investigate during LOG-44.1 before duplicating.

Passive recall — agent gets relevant history without needing to call `session_search`. Embed coverage stays sparse without LOG-26, so do that first.

---

### LOG-57 · Per-agent chat page UX redesign: one active conversation + history drawer
**Effort:** M (phased) · **Type:** Feature/UX · **Status:** OPEN · **Surfaced:** 2026-04-18 · **Cross-ref:** LOG-44.4 (session storage moves to hermes), LOG-52 (cross-agent aggregator), LOG-33 (desktop client), LOG-39 (platform-session hidden toggle generalised here)

**Motivation.** Current chat page (`gateway/html/main_app.html`) treats "topics" as parallel live threads — sidebar list with identical affordances for active vs. past. In reality, `+ New Topic` is just `session_store.reset_session()`; each agent runs exactly one hermes process and can only handle one live conversation at a time. Only `+ Agent` creates something genuinely parallel (new sandbox + new hermes). Multi-topic UI is a convention carried over from Claude/ChatGPT that doesn't match Hermes's actual shape.

Investigation surfaced: web conversations are **localStorage-only**; platform sessions are **DB-backed**; both render in the same flat list. Naming drifts across chat / topic / session / conversation. No rename, pin, archive, or live-vs-past distinction. `✕` hard-deletes.

**Phased plan — can stop after any phase:**

| # | Effort | What lands | Risk |
|---|---|---|---|
| 57.1 Honest UX, no backend | S | User-facing copy unifies on "conversation". Sidebar splits into *Active* (single prominent live item) + *History* (collapsible drawer). "+ New Topic" → "Reset context". Platform sessions move to a separate **Channels** tab (no longer interleaved). | Low — HTML/Alpine only. |
| 57.2 Server-backed conversations | M | New `conversations` table: `id (== session_id), agent_id, title, pinned, archived_at, created_at, updated_at`. Endpoints: `GET/PATCH/DELETE /admin/agents/{id}/conversations[/cid]`. Rename lands here (durable). Soft-archive default; separate **Purge** action for hard delete. Silent migration of localStorage chats + toast. Metadata only — transcripts stay in hermes SessionDB (layer over LOG-52's aggregator; do not duplicate). | Medium (migration). |
| 57.3 Restore-as-active + title on backgrounding | S–M | Archived rows render as read-only transcripts; explicit **Restore as active** button calls `POST .../conversations/{cid}/activate`. Titles proposed on backgrounding (≥2 exchanges) via one extra hermes turn asking for a 2–5 word title. Optional fallback: propose if active >20min AND ≥4 exchanges AND no title yet. | Low-medium. |

**Channels tab (separate surface, not mixed with conversations).**

Heavy platform use expected; conversations are **per-human** not per-platform-thread; user wants to **pull a transcript into the desktop's active slot** and continue there.
- Channels page per agent: rows grouped by platform (TG/DC/…), each row = one external human.
- "Pull into active slot" on any row routes the transcript into the desktop's active conversation.
- **Crux decision still open:** when a pulled-in channel thread receives a new external message, should it (A) append to the live desktop transcript — desktop becomes a real platform client; (B) buffer + show a "new message on TG" banner; (C) read-only fork — new messages continue on the server-side original. Must decide before 57.3 ships.

**Confirmed decisions (2026-04-18 design session):**
- Soft-archive by default; separate Purge action for hard delete.
- Platform sessions live on a separate Channels surface.
- Conversations are per-human on platform threads.
- Title fires on backgrounding (avoids mid-flow retitles).
- Migration of localStorage chats is silent + toast.

**Still-open:**
- Rename in 57.1 (localStorage, dies on browser reset) or wait for 57.2? Depends on how far apart phases ship.
- TG-live-pull crux (A/B/C above).
- Whether current localStorage holds meaningful data that justifies migration care, or Logos is still solo enough that loose migration is fine.

**Acceptance (after all phases):**
- Opening an agent shows one conspicuously-live conversation + collapsible history drawer with auto-titled/renamed past entries.
- Channels is a distinct tab; external threads are per-human; can be pulled into the active desktop slot.
- "Reset context" does exactly what it says; no mystery about where the old topic went.
- Soft-delete is reversible; purge is the only destructive action.

---

### LOG-59 · Surface truncation + tool errors on the v2 dispatch path
**Effort:** S (30m–2h) · **Type:** Bug/Reliability · **Status:** OPEN · **Surfaced:** 2026-04-18 · **Cross-ref:** LOG-58 (instruction-side sibling), LOG-54 (prompt-side compression), LOG-53 (UI path probe)

**Motivation.** LOG-58 covers what the *agent* must do (finish the reply + dual output). This ticket covers what the *stack* must do when the agent can't — detect and surface failures instead of presenting a partial reply as complete. From the 2026-04-18 essay incident (see LOG-58 Observed block):
- The streamed reply was cut mid-sentence by a token limit, but no UI signal told the user it was cut.
- The agent said "Yes, I finished the essay" — whether hallucinated or simply unaware — and Logos had no ground truth to contradict it.
- `write_file` failed silently in an earlier attempt; the error flag *was* present in the SSE stream but nothing surfaced it.

These are three small, obvious code gaps in the v2 dispatch path. All point at `gateway/worker_registry_v2.py` + `gateway/executors/hermes_server_mode.py`.

**Sub-items:**

| # | Item | File:line | What | Effort |
|---|---|---|---|---|
| 59.1 | Pass `max_tokens` to hermes `/v1/runs` + expose in config.yaml | `gateway/worker_registry_v2.py:155-159`, `gateway/executors/hermes_server_mode.py:210-250` | Add `max_tokens` to the POST body; add `max_completion_tokens:` to generated `config.yaml`. Default ~8192. Expose as a per-agent override (feeds into LOG-48 pricing-aware caps eventually). | XS |
| 59.2 | Detect `finish_reason: "length"` and emit `task_truncated` frame | `gateway/worker_registry_v2.py:218-220, 227-231` | On `run.completed`, read `finish_reason`; if `length`, emit a synthetic `task_truncated` SSE frame. UI renders a red "response was cut off" banner on the message. Kills the "Yes I finished" ambiguity — a truncated reply is visibly truncated. | XS |
| 59.3 | Surface `tool.end` errors prominently | `gateway/worker_registry_v2.py:214-217` | Already captures `error: true` per event. Log at WARNING level; emit a `tool_error` SSE frame the UI can render as a red inline marker on the offending tool card. For `write_file`, this is also the ground-truth signal that LOG-53's path-probe can cross-check against. | XS |

**Cross-ref detail:**
- **LOG-54** — prompt-side (compression fires too late due to wrong context-length default). LOG-59.1 is response-side (no output cap passed through). Both contribute to "essay gets cut off"; both need fixing; not duplicates.
- **LOG-53** — UI pre-flight probe for hallucinated paths. LOG-59.3 is the protocol-side signal that a `write_file` actually failed; LOG-53 is the UI-side defense when the agent hallucinates a write it never attempted. Complementary.
- **LOG-58** — instruction-side rule that the agent must produce complete dual output. LOG-59 is the infrastructure that makes rule violations visible when they happen.

**Acceptance:**
- Default-settings essay-length responses complete without silent truncation.
- When a response *is* truncated, the UI shows a visible "response cut off at N tokens" banner — no partial reply is ever presented as complete.
- A failed `write_file` renders a red error indicator on its tool row in Live Executions, not a silent success; console/log shows WARNING with the error detail.

**Non-goals:**
- Don't auto-retry truncated runs (agent may be mid-thought; retry is the user's call).
- Don't block tool errors from reaching the agent — the agent's own recovery logic is fine, the UI just can't be blind to what happened.

---

## P2 — Medium

### LOG-28 · Bidirectional reply push: web → Telegram — **DONE (2026-04-17)**
**Effort:** M · **Type:** Feature · **Status:** DONE

When a web user replies in a chat originated from a platform adapter (Telegram today; Discord/Slack/WhatsApp when those adapters are wired), the agent's final reply is now mirrored back through the in-process adapter via `adapter.send(chat_id, content)`. Client passes `platform` + `platform_chat_id` hints on the `/chat` POST; server mirrors after the final message SSE event. Best-effort: failures log + swallow, the web UI already has the reply.

### LOG-29 · Wire Telegram slash commands as `CommandHandler`s — **DONE (2026-04-17)**
**Effort:** S · **Type:** Bug · **Status:** DONE

Specific `CommandHandler`s for /help /status /stop /new /reset /model registered before the catch-all in `gateway/channels/telegram.py`; each reaches into gateway state (session_store, auth.db) for real local semantics instead of falling through to the LLM. Other commands still fall through.

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

### LOG-32 · #17 Cache sandbox details (prevent blank-flash) — **DONE (2026-04-17)**
**Effort:** XS · **Type:** Polish · **Status:** DONE

`loadSandboxes()` now mutates `selectedSandbox` in place using `Object.assign` so the 3s poll no longer leaves the detail panel holding a stale reference from the previous list. Also stopped wiping the list to `[]` on transient fetch errors — the last successful snapshot stays rendered until the next successful poll.

### LOG-46 · Wire sandbox auxiliary client to `inference.local` — **DONE (2026-04-17)**
**Effort:** S (30m–1h) · **Type:** Bug · **Status:** DONE · **May be obsoleted by:** LOG-44.1

`OpenShellExecutor.spawn` now seeds `OPENAI_BASE_URL=https://inference.local/v1` + `OPENAI_API_KEY=lm-studio` into `_service_env` (both spawn paths). The sandbox_worker reads `instance-config.env` on startup and sets those vars, which triggers the upstream auxiliary_client's `_try_custom_endpoint` branch — so compression / summarization / memory flush now route through the same privacy-routed inference channel as primary dispatch. `setdefault` so any user-configured override still wins.

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

### LOG-33 · Thin desktop client (Tauri) — **SCAFFOLDED (2026-04-17)**
**Effort:** S (1–2h) · **Type:** Feature · **Status:** SCAFFOLDED (builds locally, cross-platform CI ready; mobile/Android left for follow-up)

Tauri v2 project under `desktop/` with:
- `src-tauri/tauri.conf.json` — window points at `http://localhost:8091/login` by default
- `src-tauri/src/lib.rs` — persists the gateway URL to app-data so users can override without rebuilding; `get_gateway_url` / `set_gateway_url` commands for a future Settings UI
- Icons generated from `assets/logo.png` via `cargo tauri icon`
- `.github/workflows/desktop-build.yml` matrix-builds AppImage/.deb (Linux), .exe/.msi (Windows), .dmg (macOS arm+x64) via `tauri-apps/tauri-action@v0` on tag push. Artefacts attach to draft releases.

To build locally: `cd desktop && cargo tauri build`. Android build flow is a future ticket.

### LOG-48 · Budget-cap tightness: unpriced-model fallback + pre-call estimation
**Effort:** M · **Type:** Feature/Security · **Status:** OPEN

Today's budget gate (agents.daily_budget_usd, users.daily_budget_usd) has two known gaps, surfaced while landing LOG-25.6:

1. **Unpriced-model silent under-count.** When `pricing.cost_for_usage(model, …)` returns `None` (model not in our cached OpenRouter pricing table — brand-new Anthropic/OpenAI release, custom local model, etc.), `insert_cost_entry` writes the row with `cost_usd=0, pricing_known=0`. The budget gate sums cost_usd, so these rows don't contribute, and an over-cap user can keep dispatching indefinitely as long as the model stays unpriced. The pricing cache refreshes every 24h so a fix lands eventually, but the gap is real.

2. **Post-hoc enforcement only.** Current flow is `dispatch → API call → insert_cost_entry → NEXT dispatch checks rollup`. So one call can push the user over by its own cost — the cap only stops the *subsequent* dispatch. For tighter control we'd need a **pre-call estimator** that takes the prompt (+ context) through the same token counter the API uses, multiplies by our cached rate, and refuses if the new-call's projected cost would push the 24h rollup past the cap.

**Fix directions:**

- **For (1):** change `pricing_known=0` rows to a hard refuse option when a model's price is unknown AND the caller is over a minimum-usage threshold (e.g. >$0.01/day already). Alternative: maintain a manual price-fallback table for the top Anthropic/OpenAI SKUs so new releases aren't silently free. `pyproject.toml` could pin a curated fallback JSON updated per-release.

- **For (2):** wire `tiktoken` (for OpenAI/Anthropic) and `transformers.AutoTokenizer` (for open models) into a `pricing.estimate_cost(prompt, history, max_output_tokens, model)` helper. Call BEFORE the dispatch lands in `_handle_chat`. Reject with the same `budget_exceeded` error shape users already see. Trade-off: tokenizer load adds ~50-200 ms per call on first hit (cached afterwards).

**Scope note:** a `pricing_known=0` refuse-by-default toggle + a pre-call estimator together give "provable-cap" behaviour — cannot be over by more than one API response length. Worth shipping both or neither; one without the other still leaks.

**Ideally gated off-by-default** via `users.strict_budget_enforcement` flag (new column) or a global `LOGOS_STRICT_BUDGET_ENFORCEMENT=1` env var so existing installs don't suddenly start refusing traffic when the cache lags.

### LOG-49 · Agent-created files visible + downloadable (beyond host-side dir) — **DONE (2026-04-17)**
**Effort:** M · **Type:** Feature · **Status:** DONE · **Related:** LOG-47 (snapshot)

Users want to see + download files the agent creates inside its sandbox at arbitrary paths — e.g. `~/generate_agentic_newsletter.py`, `~/cron/agentic_newsletter.cron`, `~/hermes/newsletter_output/*`. Today only files under `~/.logos/agents/<name>/` on the HOST show in the Mind → Files tab, and those come from opinionated syncs (memories, logs, sessions). Raw agent-created scratch files live INSIDE the sandbox and don't surface.

**DONE this session:**
- `GET /admin/agents/{id}/files/download?rel_path=…` streams individual files from the host-side per-agent dir with path-traversal safety + 100MB cap. UI adds a download (↓) button per file in the Mind modal's Files tab.

**Also done (LOG-49.1/49.2/49.3):**
- `GET /admin/agents/{id}/sandbox-files?path=…` lists entries inside the live sandbox via `openshell sandbox exec find`; returns `{path, parent, entries: [{name,type,size,mtime}], roots: [known useful dirs]}` for a quick-jump UI.
- `GET /admin/agents/{id}/sandbox-files/download?path=…` streams a single file out of the sandbox via `openshell sandbox download` → tempdir → streamed → cleaned up. Same 100MB cap + path-traversal checks as the host-side endpoint.
- New "Sandbox" tab in the Mind modal with quick-jump buttons for `/root`, `/home/agent`, `/tmp/hermes`, `/tmp`, `/sandbox`, `/workspace`, `/app`; breadcrumb with `..` to go up; dirs click-through; files hover-download (↓).

**49.4 explicitly skipped** — periodic sync would only be worth doing if on-demand exec proved slow. Cold-start is ~200ms, felt responsive in testing. Revisit if sandbox exec latency becomes a problem on large dirs.

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

**Runtime contract (what any logos-compatible sandbox image must expose):**
- OpenAI-compatible `POST /v1/runs` → 202 with `{run_id}`, and `GET /v1/runs/{id}/events` SSE stream of lifecycle events (`tool.start`, `tool.end`, `message.delta`, `reasoning.available`, `run.completed`, `run.failed`). Non-negotiable — this is how Logos dispatches.
- **Graceful cancel** — either `POST /v1/runs/{id}/cancel` *or* SSE-disconnect-interrupts-agent on the events stream. Without this, the Logos Stop button is cosmetic. Today hermes-agent 0.7.0 requires a Logos-side monkeypatch for this (see LOG-51); that workaround gets deleted when upstream ships the capability. Images built on other runtimes (Claude SDK container, mini-swe-agent, etc.) have to implement it too.
- **Health probe** at `/health` returning 200 when ready to accept runs. Used by `wait_for_hermes_health` today; generalise to `wait_for_runtime_health` when 45.2 lands.

**Open questions:**
- One image = one runtime kind, or can images advertise multiple modes? (YAGNI — start with 1:1.)
- Image registry source of truth: ship with Logos, or pull from a ghcr manifest? (Start shipped; migrate if community images emerge.)
- How does the blessed-image registry record *contract version*? (Annotation on image metadata, version-range check in `wait_for_runtime_health`? Defer until second runtime lands.)

---

### LOG-58 · Runtime-injected prompt must guarantee reply completion + file-and-screen dual output
**Effort:** S (30m–2h) · **Type:** Feature/UX · **Status:** OPEN · **Surfaced:** 2026-04-18 · **Cross-ref:** LOG-53 (hallucinated-path pre-flight — addresses the other half of the same failure class), LOG-55 (workspace.md guidance pattern)

**Motivation.** Two observed failure modes, same root cause:
- Agent generates substantive output (essay, code, document), claims it was saved, never calls `write_file` → user clicks the linkified path and hits a 404. (Minoxidil incident; LOG-53 is the UI-side defense.)
- Agent *does* call `write_file` but truncates or omits the content from its chat reply, forcing the user to dig in the Sandbox tab to read what they asked for.

Default agent behavior isn't "finish the reply + emit through both channels." Runtime injection needs to make it so.

**Observed 2026-04-18 (concrete incident).** User asked Hermes for a 1000-word essay on "the state of getting a job in 2026 in the AI space." Three failures fired in one exchange:
- Agent streamed ~1000 words over ~1 minute and was **cut mid-sentence** at `"more people can"`. Retry *also* truncated mid-URL at `https://www.nucamp.co/blog/top-10-ai-skills-employers-are-hiring-for-in-2026-with`.
- When asked "did you finish?", agent replied **"Yes, I finished the essay"** — but the stream had been cut by a token limit; the agent either didn't know or lied.
- Agent volunteered: *"I apologize for the technical issues. Let me try a different approach and create the cited essay directly in my response instead of saving it to a file"* — `write_file` had failed earlier and the agent silently fell back to screen-only output with no error surfaced to the user.

This incident motivated LOG-59 (the infrastructure-side counterpart of this ticket: the stack must *detect* truncation and tool errors, not just rely on the agent to behave correctly).

**Requirement.** The runtime-injected prompt (shared soul fragment or per-agent hermes `system_prompt`) must enforce:
- **(a) Finish the reply.** Artifact-class outputs (multi-paragraph text, lists, code, documents) are emitted *in full* in the chat reply before the turn ends. No "see the file" substitutions, no truncation.
- **(b) Dual output.** The same content is also written via `write_file` to a stable, citeable path under the agent's workspace, and the cited path is returned in the reply.
- Exemptions: single-paragraph conversational answers, short clarifications — the rule applies to artifact-class outputs only.

**Where to inject:**
1. **`souls/_shared/workspace.md`** — loaded by every soul at spawn; add a "Dual-output rule" section. Consistent with LOG-53 / LOG-55 delivery.
2. **Per-agent hermes config `system_prompt`** written at spawn by `_build_config_yaml` — if soul-fragment compliance proves insufficient. Heavier.

Lean (1) first; promote to (2) if non-compliance persists.

**Acceptance:**
- "Write a 1000-word essay on X" → reply contains the full essay **and** a real file path that the click-to-download anchor resolves against.
- "Quick Python snippet" → snippet inline in reply; `write_file` not required (conversational class).
- Minoxidil regression check: agent either doesn't claim to save, or saves correctly.

**Non-goals:**
- No tool-level enforcement (auto-mirror every reply to disk) — would clutter workspace and conflict with agents that have their own file conventions.
- No requirement to save conversational replies — artifact-class only.

---

## P3 — Low / polish

### LOG-34 · Real slim sandbox image (replace orphan Dockerfile)
**Effort:** L · **Type:** Infra · **Status:** OPEN · **Independent of LOG-24** (Plan A-prime kept the same image baseline)

Audit `/opt/hermes` to carve a 1–2 GB image. Migrate browser tools to `@playwright/mcp`. Delete the orphan `Dockerfile.hermes-sandbox`.

**Must-include deps surfaced by downstream tickets:**
- `croniter` (nice-to-have, NOT blocking) — hermes's cron works fine without it for `every 30m`, `2h`, ISO-timestamp, one-shot-duration schedules (see `cron/jobs.py::parse_schedule`). croniter is only required to parse the five-field cron-expression syntax (`0 9 * * *`). Agents using the natural-language forms don't care. Ship it to widen the accepted syntax; don't ship it and `*/5 * * * *` raises a clean `ValueError` that the agent will retry with "every 5m".
- `playwright install chromium` — MUST ship. Without the Chromium binary the `browser` toolset's `browser_navigate` / `browser_click` / `browser_vision` fail on first use, and the sandbox's network policy blocks both `npm` and Playwright's download CDN so agents cannot self-recover at runtime. Reported live: user asked Hermes to search via SearxNG, agent got stuck in a pip/npm/wget install-attempt loop that all 403'd. See LOG-55 for the tool-layer workaround (http_get) that avoids Chromium entirely for the common JSON-GET cases.

### LOG-35 · UI consistency micro-fixes
**Effort:** XS each · **Type:** Polish · **Status:** OPEN

| # | Item | Status |
|---|---|---|
| 35a | Audit log pagination right-aligned (match Runs tab) | DONE (2026-04-17) — justify-between layout with "Showing X-Y of Z" counter on left, pager on right |
| 35b | Runs origin badges: `platform_telegram` styled pill (match `user_chat`) | DONE (2026-04-17) — unified blue pill for user_chat + platform_\*; text humanized ("platform_telegram" → "Telegram", "user_chat" → "Web chat") |
| 35c | Rebuild `assets/tailwind.css` (10-day stale per audit) | DONE (2026-04-17) — rebuilt via `npx tailwindcss@3`; 46.9KB → 52KB |

### LOG-36 · Sub-agent live execution: per-sub-agent boxes
**Effort:** M · **Type:** Polish · **Status:** OPEN

Ticket deepened 2026-04-17 after attempted start — the per-sub-agent UI requires backend event-threading that doesn't exist today. `tools/delegate_tool.py` runs children in-process inside the sandbox and the children's tool-call events (tool_start/tool_end) never flow back through the parent's stdout protocol to the gateway's SSE stream. So the UI has nothing to group.

| # | Sub-task | Effort | Notes |
|---|---|---|---|
| 36.1 | `delegate_tool.py` accepts a `tool_event_cb(subagent_id, event_dict)` param that the child AIAgent calls from its own tool-event hooks | S | Child agents must be run with their own event sinks — today they run silent. |
| 36.2 | `docker/sandbox_worker.py` wraps the parent's stdout-JSON framing to emit child events with a `subagent_id` field tagging the origin | XS | Single extra key on each event; keep existing tool_start/tool_end shape. |
| 36.3 | `gateway/worker_registry.py::dispatch_task` forwards `subagent_id` into the SSE event dict on its way to `_handle_chat` | XS | Already passes arbitrary dict keys through; just don't strip. |
| 36.4 | `main_app.html`: new `_liveSubagents` Alpine structure keyed by `subagent_id` (parent = null); render a small header per sub-agent + its own tool-call list | S | Also update `_liveTools` access paths to include `subagent_id`. |
| 36.5 | Delegation summary row in the parent's live-tools panel stays; clicking it expands to show the sub-agent details if we want to hide by default | XS | UX polish; optional. |

### LOG-37 · Periodic backfill cron for embeddings
**Effort:** S · **Type:** Feature · **Status:** OPEN · **Best-after:** LOG-26

### LOG-38 · Lightweight Python embedding fallback
**Effort:** M · **Type:** Feature · **Status:** OPEN

`sentence-transformers` not in pyproject. Currently embeddings silently return empty when no LM Studio/Ollama endpoint is reachable.

### LOG-39 · "Show hidden" toggle for soft-deleted sessions — **DONE (2026-04-17)**
**Effort:** S · **Type:** Feature · **Status:** DONE

`/api/platform-sessions?include_hidden=1` returns soft-deleted rows (with `hidden: true` stamped), and `POST /api/platform-sessions/{id}/restore` flips `hidden=0`. Sidebar has a "Show hidden" checkbox that re-queries with the flag; hidden rows render dim with an `hidden` pill + a Restore (↺) button on hover.

### LOG-40 · Platform badge in chat header ("via Telegram") — **DONE (2026-04-17)**
**Effort:** XS · **Type:** Polish · **Status:** DONE

Cyan "via Telegram" / "via Discord" / "via Slack" / "via WhatsApp" pill in the chat header next to the agent name, shown only when `chat._platform` is set.

### LOG-41 · `get_current_time` MCP tool — **DONE (2026-04-17)**
**Effort:** S · **Type:** Feature · **Status:** DONE

New `gateway/mcp_logos/tools/time.py` exposing `get_current_time(timezone=None)` on the in-process logos MCP server. Returns iso / epoch_s / timezone / display / fallback. Registered as auto_approve (read-only). Useful for scheduling, relative dates, deadline checks.

### LOG-42 · `/setup` IANA timezone dropdown — **DONE (2026-04-17)**
**Effort:** S · **Type:** Feature · **Status:** DONE

Optional tz selector on the Account step, populated from `Intl.supportedValuesOf('timeZone')` with a 16-zone curated fallback for older browsers. Value stored in `localStorage.logos_tz`; default "" means auto-detect from the browser. Server-side persistence deferred — world-view use case is browser-local.

### LOG-43 · Trim Telegram command menu (drop `/update`, `/reload_mcp`, `/provider`) — **DONE (2026-04-17)**
**Effort:** XS · **Type:** Polish · **Status:** DONE

Dropped `/update`, `/reload_mcp`, `/provider`, and `/personality` from the `set_my_commands` registration in `gateway/channels/telegram.py`. Landed alongside LOG-29 since both touched the same block.

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
