# Platforms as Gateway-Mediated Transports — Design

**Version 0.1 | April 2026**
**Status:** DRAFT — for review
**Companion to:** `docs/migration/logos-openshell-migration.md`
**Target:** Logos v0.11.x

| | |
|---|---|
| **Scope** | Remove the in-process agent path from platform adapters (Telegram, Discord, Slack, WhatsApp, Email, HomeAssistant). Platforms become gateway-mediated transports that dispatch inbound messages to sandbox workers and expose outbound send operations as MCP tools. |
| **Depends on** | Phase A (strict `/chat` sandbox routing) — already shipped in `e01457a`. Phases 5.5 + 5.7 additionally depend on the Logos Capabilities MCP Server — see `docs/migration/logos-capabilities-mcp-server.md`. |
| **Blocks** | Full deletion of `runner._run_agent` from `gateway/run.py` |

---

## 1. Why This Doc Exists

The OpenShell-only pivot (shipped in `e01457a`) made web chat strict: `/chat` now requires a named agent and dispatches to its sandbox worker via WebSocket. No in-process fallback.

**Platform adapters were not migrated in that pivot.** `gateway/platforms/base.py` still calls `self._message_handler(event)`, which is bound to `runner._handle_message`, which internally calls `runner._run_agent(...)` — the exact in-process path we just deleted from `/chat`. Every inbound Telegram/Discord/Slack message still runs the agent loop *inside the gateway process*, with the gateway's full privileges, bypassing every sandbox policy.

The same security argument applies: **in-process agents are dangerous** because they read the gateway's env vars (API keys), query the auth DB directly, touch any file the gateway can, and bypass every network policy. The platform path is the last remaining in-process execution site and needs to close.

The user explicitly said platforms are non-functional today ("I dont think we even have the new providers working yet for telegram discord and cron") and that it's acceptable to break them during the pivot, but the re-implementation should be designed up front rather than hacked in piece by piece.

This document is that design.

---

## 2. What a "Platform" Actually Does

Each platform adapter has two independent responsibilities. It's crucial to separate them because they map to different architectural layers.

### 2.1 Inbound (user → gateway)

A user sends a message on Telegram. The adapter receives it, wraps it in a `MessageEvent`, and hands it to the gateway. The gateway needs to:

1. Decide which named agent should handle the message
2. Build session context (history, identity, platform metadata)
3. Dispatch the message to that agent's sandbox worker
4. Receive the agent's final response
5. Ask the adapter to send that response back to the Telegram channel

Today, step 3 is "run the agent loop in this Python process" — that's the dangerous part. The target is "dispatch via WorkerRegistry.dispatch_task over WebSocket to the sandbox" — exactly the same mechanism `/chat` already uses.

### 2.2 Outbound (sandbox → user)

A sandbox agent, partway through its tool loop, decides to send a proactive message (scheduled reminder, cron result, tool notification). It cannot call the Telegram API directly — its network policy blocks outbound traffic to `api.telegram.org`, and even if it could, it doesn't have the bot token. The token lives in the gateway's env.

The sandbox must ask the gateway to send on its behalf. The cleanest way to do that today is through an **MCP tool** exposed by the gateway's MCP gateway service. The sandbox calls something like `platform.send_message(platform="telegram", channel="home", text="...")`, the MCP gateway receives that call over its already-open WebSocket, locates the Telegram adapter in the runner, and calls `adapter.send(...)` with the gateway-held bot token. From the sandbox's point of view it's just a normal tool call. From the gateway's point of view it's a credential-scoped operation.

---

## 3. Target Architecture

### 3.1 Inbound flow

```
┌─────────┐
│ Telegram│
│ user    │
└────┬────┘
     │ DM / group message
     ▼
┌───────────────────────┐
│ TelegramAdapter       │  gateway/platforms/telegram.py
│ - receives update     │
│ - builds MessageEvent │
└────────────┬──────────┘
             │ .handle_message(event)
             ▼
┌───────────────────────────┐
│ BaseAdapter               │  gateway/platforms/base.py
│ ._process_message_bg()    │
│ - typing indicator        │
│ - await _message_handler  │
└────────────┬──────────────┘
             │ _message_handler = runner.dispatch_platform_message
             ▼
┌────────────────────────────────┐
│ GatewayRunner                  │  gateway/run.py
│ .dispatch_platform_message()   │  ← NEW, replaces _handle_message
│ 1. resolve target named agent  │
│ 2. build session + context     │
│ 3. build task payload          │
│ 4. worker_registry.dispatch_task
└────────────┬───────────────────┘
             │ over /ws/worker WebSocket
             ▼
┌───────────────────────────┐
│ OpenShell sandbox worker  │  docker/sandbox_worker.py
│ - runs AIAgent loop       │
│ - streams events back     │
│ - returns final_response  │
└────────────┬──────────────┘
             │ task_result via WebSocket
             ▼
       (control returns to
        dispatch_platform_message)
             │
             ▼
┌───────────────────────────┐
│ BaseAdapter.send(...)     │  sends final_response to Telegram
└───────────────────────────┘
```

**Key observation:** `runner._handle_message` becomes `runner.dispatch_platform_message` — same signature-shaped interface, same pre/post-processing, but the agent execution is delegated. The runner no longer runs any agent code itself.

### 3.2 Outbound flow

```
┌────────────────────────┐
│ OpenShell sandbox      │
│ AIAgent loop           │
│                        │
│ tool call:             │
│ platform.send_message  │
│   platform="telegram"  │
│   channel="home"       │
│   text="done!"         │
└────────────┬───────────┘
             │ MCP tool call via /mcp/platform/send_message
             │ (HTTP, scoped to MCP gateway endpoint by policy)
             ▼
┌─────────────────────────────┐
│ Logos gateway               │
│ MCP gateway service         │  gateway/mcp_service.py
│ - receives tool call        │
│ - looks up adapter by name  │
│ - calls adapter.send(...)   │
│   with gateway-held token   │
└────────────┬────────────────┘
             │
             ▼
┌────────────┐
│ Telegram   │
│ bot API    │
└────────────┘
```

**Key observation:** The bot token never leaves the gateway. The sandbox calls a scoped MCP tool and the tool does the sending. This is the same security model as the Privacy Router for inference — credentials stay in the gateway, sandboxes call mediated endpoints.

---

## 4. Design Decisions

### 4.1 Which agent handles an inbound message?

A Telegram bot has one identity per bot token. A Slack workspace can have many channels, each of which could logically belong to a different agent. A Discord server is in between.

**Decision:** Introduce a **`platform_routing` table** in `auth.db`:

```sql
CREATE TABLE platform_routing (
    id           TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,          -- telegram, discord, slack, ...
    scope        TEXT NOT NULL,          -- 'global' | 'channel' | 'user'
    scope_id     TEXT,                   -- channel_id, user_id, or NULL for global
    agent_id     TEXT NOT NULL REFERENCES agents(id),
    created_at   INTEGER NOT NULL,
    UNIQUE(platform, scope, scope_id)
);
```

Resolution precedence: exact channel match → exact user match → platform global default → first agent in the DB (last-resort fallback during bootstrap).

**Bootstrap:** When the setup wizard creates the default agent, it also inserts a `('platform', scope='global', scope_id=NULL, agent_id=<default>)` row for every enabled platform so first messages route cleanly.

**UI:** Admin → Platforms gains a routing table where the user can bind channels/users to specific agents. Out of scope for the initial migration but the schema supports it from day one.

### 4.2 Where do bot tokens live?

Today: env vars (`TELEGRAM_BOT_TOKEN`, `DISCORD_TOKEN`, etc.) or `~/.logos/.env`.

**Decision for this migration:** No change. Tokens stay in the gateway's env. The gateway is the only process that touches them. The sandbox never sees them because the outbound path goes through an MCP tool.

**Future:** A `platform_credentials` table could consolidate these, but that's a separate refactor and not on the critical path.

### 4.3 How does the sandbox know what platforms are available?

The sandbox's MCP tool list is managed by the MCP gateway. When the gateway starts and a platform adapter successfully connects, the MCP gateway registers a corresponding `platform.send_message` tool variant (or more likely, a single `platform_send` tool that takes a `platform` parameter). The sandbox sees it in its tool list automatically on next refresh.

If an adapter fails to connect, the tool is not registered, and the sandbox's attempts to call it will fail with "tool not available" — which is the right behaviour.

### 4.4 Voice, attachments, and rich replies

Today, `_process_message_background` handles a lot of rich-content logic after the agent returns: TTS generation, image extraction, media file routing, human-pacing delays between text and media chunks. This logic must remain in the adapter layer because it's platform-specific (Telegram voice notes, Discord embeds, Slack blocks).

**Decision:** `dispatch_platform_message` returns the same shape `_handle_message` returns today (`final_response` string, plus any media markers in the response). The adapter's post-processing (`extract_media`, `extract_images`, `play_tts`, `send_voice`, `send_image`, etc.) runs unchanged. The only thing that changes is HOW the `final_response` is generated (sandbox dispatch vs in-process).

### 4.5 Session and history

The session store currently lives on the runner (`runner.session_store`). It records transcripts per session_key and is read when building context. This stays in the gateway — the sandbox doesn't need its own session store because each dispatch is stateless from the sandbox's perspective (the full history is passed in the task payload every time).

Rationale: a sandbox that gets killed and re-spawned shouldn't lose conversation state. The gateway is the persistent side of the boundary.

### 4.6 Runner access to WorkerRegistry

The runner (`GatewayRunner`) is instantiated before the HTTP API starts, and `worker_registry` is created inside `start_http_api`. To let the runner dispatch, either:

**Option i:** Move `worker_registry` creation to `GatewayRunner.__init__` and have `start_http_api` read it from the runner. Cleanest dependency graph.

**Option ii:** Add `runner.worker_registry = None` field, have `start_http_api` set it after creating the registry. Minimal diff.

**Decision: Option i.** The runner is the longer-lived lifecycle and should own the registry. `start_http_api` becomes a pure "expose HTTP endpoints over this runner's state" operation.

### 4.7 Timeouts, interrupts, streaming

- **Timeout:** `HERMES_AGENT_TIMEOUT` becomes `LOGOS_AGENT_TIMEOUT` and is passed to `worker_registry.dispatch_task(..., timeout=...)` rather than wrapping `_run_agent` in `asyncio.wait_for`. Same enforcement, different code path.
- **Interrupts:** Today the platform adapter can interrupt a running agent (pending message injection). Worker-side interrupt support does not yet exist — adding a `cancel_task` WebSocket message type is a follow-up ticket.
- **Streaming:** Platforms don't need token-level streaming (Telegram doesn't show partial messages). They need the final response and any intermediate tool progress if we want "typing..." state updates. `on_stream_event` callback can forward `tool_start`/`tool_end` events to platform-specific progress indicators (already exists for Discord via `update_status`).

### 4.8 Hooks

`gateway:startup`, `agent:start`, `agent:end` hooks are emitted by the runner today. After the migration, `agent:start`/`agent:end` fire in `dispatch_platform_message` (before/after the sandbox dispatch) so hook consumers see the same events. Hook context payload is unchanged.

---

## 5. Migration Phases

Each phase is independently committable. No phase requires the next to be merged.

### Phase 5.1: Disable platforms in code, add design doc

**Goal:** Kill the dangerous in-process path immediately without waiting for the full migration.

- Comment out the platform-adapter startup loop in `GatewayRunner.start()`, with a clear TODO pointing at this doc.
- Delete the `self._run_agent` call in `_handle_message`. Replace with a `logger.warning("platforms disabled during OpenShell migration; see docs/migration/platforms-as-gateway-mediated.md")`.
- Gateway continues to start, HTTP + cron work normally, platform messages stop arriving.
- **No sandbox dispatch yet.** Platforms are simply off.

**Risk:** Users who depend on Telegram/Discord lose functionality until Phase 5.4 lands. Acceptable per user direction.

### Phase 5.2: `worker_registry` on runner, `dispatch_platform_message` skeleton

**Goal:** Wire the infrastructure without turning platforms back on yet.

- Move `WorkerRegistry()` instantiation from `start_http_api` to `GatewayRunner.__init__`.
- Update `start_http_api` to use `runner.worker_registry` instead of creating its own.
- Add `async def dispatch_platform_message(event: MessageEvent)` to `GatewayRunner`. Initially a stub that logs the event and returns an error string. This is the new `_message_handler` target.
- Do NOT re-enable platform startup yet.

### Phase 5.3: Implement `dispatch_platform_message` against WorkerRegistry

**Goal:** Real inbound dispatch, one platform at a time.

- Implement the resolution logic: look up `platform_routing` entry → fall back to first named agent.
- Build session, history, context prompt (reuse the existing `_handle_message` pre-processing verbatim — copy it into the new function so the old one can be deleted later).
- Build task payload matching the sandbox worker's expected schema.
- Call `self.worker_registry.dispatch_task(worker_id, task_payload, timeout=timeout, on_stream_event=progress_forwarder)`.
- Handle failure modes:
  - No sandbox worker connected → return a friendly error string, send via adapter, log.
  - Worker timeout → return timeout error, send, log.
  - Worker busy → queue (reuse existing `_pending_messages` mechanism from `BaseAdapter`).
- Return the same-shaped result as `_handle_message` did.

### Phase 5.4: Re-enable platform startup + add routing table + UI

**Goal:** Platforms work again, with routing configurable.

- Re-enable the adapter startup loop in `GatewayRunner.start()`.
- Bind `_message_handler = runner.dispatch_platform_message` instead of `_handle_message`.
- Create the `platform_routing` table + bootstrap a global row per enabled platform.
- Add Admin → Platforms tab in the web UI showing: enabled platforms, their connection status, their default agent, routing rules.
- At minimum: each row shows "Platform: Telegram, Default agent: Hermes [change]". Full per-channel routing comes later.

### Phase 5.5: Outbound MCP tool

**Goal:** Sandbox agents can initiate platform messages.

- Add `tools/platform_send_tool.py` that is exposed through the MCP gateway.
- Tool signature: `platform_send(platform: str, channel: str, text: str, media_urls: list[str] | None = None) -> dict`.
- Implementation: looks up the adapter in `runner.adapters` by platform name, calls `adapter.send(...)`, returns delivery receipt or error.
- Register the tool in the MCP gateway's tool catalogue with approval tier `LOW` (no confirmation needed for text replies — confirmation required for new channels the sandbox hasn't sent to before? open question).
- Update `gateway/policies/openshell_default.yaml` to allow the agent binary to reach the MCP gateway endpoint with `POST /mcp/*` scoped rules (already in place for other MCP tools).

### Phase 5.6: Delete `runner._run_agent` from the hot path

**Goal:** Kill the in-process function entirely.

- At this point `_run_agent` has zero external callers. Its only remaining call site is recursive from within itself (interrupt handling). That path is dead.
- Delete the function and all its supporting helpers (`_enrich_message_with_attachments` if nothing else uses it — check; `_running_agents` if nothing reads it — check).
- Clean up associated state: `runner._running_agents`, `runner._current_task_ids`, etc.
- Verify `gateway/worker.py` (the sandbox side) does not import any runner internals. It should be fully self-contained.

### Phase 5.7: Cron migration

**Goal:** Move scheduled jobs off `_run_agent` and onto `WorkerRegistry.dispatch_task`.

- Add `dispatch_cron_job(job: CronJob) -> dict` to `GatewayRunner`. Structural cousin of `dispatch_platform_message` but triggered by the cron ticker instead of an inbound message.
- Resolve the target agent from the cron job's config (jobs get an `agent_id` field). Default to the platform-global default agent if unset.
- Build a synthetic context prompt describing the scheduled trigger.
- Dispatch to the sandbox worker via `WorkerRegistry.dispatch_task`.
- Send the result to the user's configured home channel via the `logos.home_message` MCP tool (which calls the appropriate platform adapter server-side).
- **Depends on Phase 5.5 landed** because the home-channel delivery uses the Logos Capabilities MCP Server.

---

## 6. Decisions

Decisions made during the April 2026 design review. Revisit if real-world usage contradicts any of these.

- **[DECIDED] 6.1 — Outbound approval for new channels.** `platform_send` is `auto_approve` tier for any channel the sandbox has already received a message from (i.e. reply-in-thread is always free). Sending to a channel the sandbox has *not* received from moves to `user_approve` tier and needs confirmation. Rationale: reply-in-thread is a language act the user initiated; proactive outreach to a new channel deserves a prompt.

- **[DECIDED] 6.2 — Platforms boot before any sandbox is available.** Permanent-error state. Adapters connect normally so the bot presence is legitimate, but every inbound message gets a scripted reply: *"My sandbox isn't connected right now. Try again in a moment, or check the dashboard."* Rationale: silently dropping messages looks broken; refusing to start platforms means users have to restart the process to recover.

- **[DECIDED] 6.3 — Multi-agent on the same channel.** Routing entry wins; single dispatch; v1. Multi-dispatch has hard semantics (do the agents see each other's replies? do they step on each other?) and is out of scope.

- **[DECIDED] 6.4 — Cron.** Migrates **separately**, after platforms. Cron has a different trigger shape (timer, not MessageEvent) and a different response shape (result to home channel, not reply-thread). Cron gets its own thin function `dispatch_cron_job` that calls the same `WorkerRegistry.dispatch_task` path. Tracked as Phase 5.7 (see §5).

- **[DECIDED] 6.5 — Concurrent messages to a busy worker.** Queue via a per-`WorkerEntry` `asyncio.Lock`. While one turn is running, subsequent messages wait and the adapter shows a typing indicator. Spawning parallel workers-per-agent is a bigger architectural shift (conversation state has to converge) and is deferred.

- **[DECIDED] 6.6 — Interrupts (pending-message injection).** Ship without. The platform base's existing `_pending_messages` queue stays, but instead of interrupting the running worker it defers until the current turn completes. File a follow-up ticket to add `cancel_task` to the worker WebSocket protocol.

---

## 7. Code Changes Summary

| File | Phase | Change |
|---|---|---|
| `gateway/run.py` | 5.1 | Comment out adapter startup loop, delete `_run_agent` call in `_handle_message`, add deprecation log |
| `gateway/run.py` | 5.2 | Move `WorkerRegistry()` to `__init__`, add `dispatch_platform_message` stub |
| `gateway/run.py` | 5.3 | Implement `dispatch_platform_message` (real dispatch logic) |
| `gateway/run.py` | 5.4 | Re-enable adapter startup, bind `_message_handler = dispatch_platform_message` |
| `gateway/http_api.py` | 5.2 | Use `runner.worker_registry` instead of creating a new one |
| `gateway/auth/db.py` | 5.4 | Add `platform_routing` table + CRUD helpers |
| `gateway/setup_handlers.py` | 5.4 | Bootstrap `platform_routing` rows for the default agent during setup |
| `gateway/html/main_app.html` | 5.4 | Admin → Platforms tab (connection status + routing) |
| `tools/platform_send_tool.py` | 5.5 | NEW — MCP-exposed outbound send |
| `gateway/mcp_service.py` | 5.5 | Register `platform_send` in the tool catalogue |
| `gateway/policies/openshell_default.yaml` | 5.5 | Confirm the MCP-gateway network rule covers the new tool |
| `gateway/run.py` | 5.6 | Delete `_run_agent` + `_handle_message` + `_running_agents` + associated dead state |

---

## 8. Risks and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| **Adapters depend on runner internals besides `_handle_message`** | Medium — could block migration | Low | Grep `platforms/**` for `runner.` references. Platforms receive the runner via `set_message_handler` — the rest should be clean. Audit early. |
| **Sandbox worker interrupt API missing** | Medium — regression in UX | High (confirmed) | Accept regression for v1 migration. File a follow-up ticket to add `cancel_task` to the WebSocket protocol. |
| **Routing table design bakes in assumptions that don't hold for Slack threads / Discord threads** | Medium — schema churn | Medium | Make the `scope` column an open string, not an enum. Add new scope types (`thread`, `guild`) as real use cases emerge. |
| **Platform credentials in env vars is fragile for multi-tenant** | Low for solo use | Low | Defer `platform_credentials` table to a follow-up. Not on the critical path. |
| **Queue-on-busy (6.5) introduces head-of-line blocking under load** | Low for solo use | Medium | Document the limit. Horizontal-scale answer is "spawn multiple workers for the same named agent" but that needs work in the executor + worker registry. |

---

## 9. Success Criteria

- No file outside `gateway/worker.py` and `docker/sandbox_worker.py` calls into the AIAgent loop. Verified by grep.
- `runner._run_agent` does not exist after Phase 5.6.
- A Telegram message to the default-agent bot is received, dispatched to the sandbox worker, processed, and replied to — full round trip — with zero in-process agent code running.
- A sandbox agent can call `platform_send(platform="telegram", channel="home", text="hello")` and the message arrives in the configured home channel.
- The sandbox agent's environment contains zero platform bot tokens. Verified by `env` inside a running sandbox.
- If the default agent's sandbox is not connected, an inbound Telegram message triggers a friendly "agent sandbox not ready" reply, not a silent drop or in-process execution.
- The `platform_routing` table can bind different channels to different agents, and resolution obeys the precedence in §4.1.
- All platform adapters (Telegram, Discord, Slack, WhatsApp, Email, HomeAssistant) work through `dispatch_platform_message` without per-adapter code changes — the runner's `_message_handler` swap is the only wiring change.

---

## 10. What This Doc Is NOT

- **Not a platform feature expansion.** No new platform integrations, no new message types, no new UI affordances beyond the routing table.
- **Not a credential storage redesign.** Bot tokens stay in env vars. `platform_credentials` table is a follow-up.
- **Not a sandbox-to-sandbox communication protocol.** Agents talking to other agents is out of scope.
- **Not an Agent-as-a-Service multi-tenant design.** Single-user first; multi-user platform routing gets a separate doc when we need it.
- **Not a cron redesign.** §6.4 decides whether cron comes with this or later.

---

*DRAFT — Logos × OpenShell platform migration — April 2026*
