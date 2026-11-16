# Logos Capabilities MCP Server — Design

**Version 0.1 | April 2026**
**Status:** DRAFT — for review
**Companion to:** `logos-openshell-migration.md`, `platforms-as-gateway-mediated.md`
**Target:** Logos v0.11.x

| | |
|---|---|
| **Scope** | A new in-process MCP server, registered under the name `logos`, that exposes gateway-held capabilities (platform send, home channel, session history, memory, cron, workflow, skills hub) as tools callable by sandboxed agents via the existing `/mcp/logos/*` HTTP interface. |
| **Depends on** | Phase 5.1 (`dc40e6d`) — platform adapters are disabled in the gateway during this work, so we can iterate without breaking inbound Telegram/Discord/etc. |
| **Blocks** | Phase 5.5, 5.7 of `platforms-as-gateway-mediated.md`, cron migration, any future sandbox-to-gateway capability call. |

---

## 1. Why This Doc Exists

After Phase 5.1 of the platforms migration disabled the in-process agent path, it became clear that sandboxed agents have **no way to call back into Logos for any gateway-held capability**. The existing `MCPGatewayService` in `gateway/mcp_service.py` is a proxy/manager for **external** MCP servers (third-party stdio subprocesses configured in `~/.logos/config.yaml` — filesystem-mcp, github-mcp, etc.) — it does not host any Logos-internal tools.

This is a hole that blocks every further phase of the migration. We can't dispatch inbound platform messages to a sandbox if the sandbox can't reply via a gateway-mediated tool (because outbound-through-sandbox-network is blocked by policy). We can't migrate cron because the cron result has to reach a home channel that only the gateway can speak to. We can't let agents call `memory.recall` or `session.read_transcript` without a gateway endpoint for them to hit. Every platform-layer feature that needs gateway state is in the same boat.

**The design insight:** the existing MCP gateway architecture can absorb this cleanly without inventing a new server type. An MCP server from the sandbox's point of view is *just an HTTP endpoint at `/mcp/{name}` that accepts JSON-RPC tool calls*. We don't need a stdio subprocess. We need an **in-process MCP server variant** — Python callables behind the same HTTP routing — registered under the name `logos`, which the sandbox can call the same way it calls any other MCP tool.

---

## 2. Architecture

### 2.1 Current state

```
                                    ┌────────────────────────────┐
                                    │ ~/.logos/config.yaml       │
                                    │   mcp_servers:             │
                                    │     filesystem: {cmd: ...} │
                                    │     github:     {cmd: ...} │
                                    └─────────────┬──────────────┘
                                                  │
                  ┌───────────────────────────────▼─────────────────┐
                  │ MCPGatewayService                               │
                  │  - reads config                                 │
                  │  - boots external stdio subprocesses            │
                  │  - tracks _servers[name] → MCPServerTask        │
                  │  - exposes /mcp/{name}/tools over HTTP          │
                  └──────────────────────┬──────────────────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
       ┌────────────┐            ┌────────────┐            ┌────────────┐
       │ filesystem │            │   github   │            │    ...     │
       │  (stdio    │            │  (stdio    │            │  (stdio    │
       │   proc)    │            │   proc)    │            │   proc)    │
       └────────────┘            └────────────┘            └────────────┘
```

The gateway itself exposes zero tools to sandboxes. All sandbox-callable tools are *third-party*.

### 2.2 Target state

```
                                    ┌────────────────────────────┐
                                    │ ~/.logos/config.yaml       │
                                    │   mcp_servers:             │
                                    │     filesystem: {cmd: ...} │
                                    │     github:     {cmd: ...} │
                                    └─────────────┬──────────────┘
                                                  │
              ┌───────────────────────────────────▼──────────────┐
              │ MCPGatewayService                                │
              │  - reads config                                  │
              │  - boots external stdio subprocesses             │
              │  - also registers the built-in `logos` server    │
              │    (in-process, no subprocess)                   │
              │  - tracks _servers[name] → MCPServerTask | InProcessServer
              │  - exposes /mcp/{name}/tools over HTTP           │
              └──────────────────┬────────────────────────┬──────┘
                                 │                        │
              ┌──────────────────┼──────────────────┐     │
              ▼                  ▼                  ▼     ▼
       ┌────────────┐    ┌────────────┐    ┌────────────┐  ┌─────────────────────┐
       │ filesystem │    │   github   │    │    ...     │  │ logos (in-process)  │
       │  (stdio)   │    │   (stdio)  │    │  (stdio)   │  │ Python callables    │
       └────────────┘    └────────────┘    └────────────┘  │ bound to runner ref │
                                                            │                     │
                                                            │ tools:              │
                                                            │ - platform_send     │
                                                            │ - home_message      │
                                                            │ - session_read      │
                                                            │ - memory_recall     │
                                                            │ - memory_write      │
                                                            │ - cron_schedule     │
                                                            │ - cron_list         │
                                                            │ - workflow_start    │
                                                            │ - workflow_status   │
                                                            │ - agent_list        │
                                                            │ - agent_message     │
                                                            └─────────────────────┘
```

From the sandbox's point of view, calling `/mcp/logos/platform_send` looks identical to calling `/mcp/filesystem/read_file`. Same HTTP interface, same JSON-RPC shape, same approval tier machinery.

### 2.3 How the in-process server fits the existing abstraction

Looking at `gateway/mcp_service.py` and `tools/mcp_tool.py`, the external MCP servers are represented as `MCPServerTask` objects that have:

- a `name` and `description`
- a `_registered_tool_names` list
- a `session` (the stdio transport session)
- methods to call a tool by name and return a result

We need a parallel class — call it `InProcessMCPServer` — that has:

- the same `name`, `description`, `tool_count` surface (so `MCPGatewayService.get_catalogue()` works without special-casing)
- a dict of `name → async callable` for the tool implementations
- a `call_tool(name, args)` method that dispatches to the right callable
- a shared reference to the `GatewayRunner` so tools can reach `runner.adapters`, `runner.session_store`, `runner.cron`, etc.

The HTTP handler at `/mcp/{server-name}` (wherever that lives in `gateway/mcp_handlers.py`) checks the server type and either proxies to the stdio session or calls the in-process dispatcher. One branch on the server type; the rest is shared.

---

## 3. Initial Tool Set

These are the tools the `logos` server exposes in v1. Scoped to what's actually needed to migrate platforms + cron + minimal agent capabilities. Additional tools are added in later phases as needed; this is not the final list.

### 3.1 Platform tools

| Tool | Args | Returns | Approval tier | Notes |
|---|---|---|---|---|
| `platform_send` | `platform: str, channel: str, text: str, media_urls: list[str] = None, reply_to: str = None` | `{sent: bool, message_id: str, error: str}` | `auto` for reply-in-thread, `user` for new channel | Looks up `runner.adapters[platform]`, calls `adapter.send(...)` using the gateway-held bot token. |
| `home_message` | `text: str, media_urls: list[str] = None` | `{sent: bool, platform: str, channel: str}` | `auto` | Sends to the user's configured home channel across whichever platform owns it. Resolves via `platform_routing` + per-platform `HOME_CHANNEL` env. |

### 3.2 Session + memory tools

| Tool | Args | Returns | Approval tier | Notes |
|---|---|---|---|---|
| `session_read` | `session_id: str, limit: int = 50` | `{messages: [...]}` | `auto` | Reads transcript from `runner.session_store`. Scoped to sessions the calling agent owns. |
| `session_list` | `user_id: str = None, limit: int = 20` | `{sessions: [...]}` | `auto` | Lists recent sessions. Filtered by owning user. |
| `memory_recall` | `query: str, limit: int = 5` | `{memories: [...]}` | `auto` | FTS5 + embedding search against the agent's memory store. |
| `memory_write` | `content: str, tags: list[str] = None` | `{id: str}` | `auto` | Writes to the calling agent's memory store. |

### 3.3 Cron + workflow tools

| Tool | Args | Returns | Approval tier | Notes |
|---|---|---|---|---|
| `cron_schedule` | `schedule: str, task: str, agent_id: str = None` | `{job_id: str}` | `user` | Creates a cron job (cron-format schedule) that dispatches `task` to the named agent. If `agent_id` is None, targets the calling agent. Approval tier is `user` because cron persistence survives restarts. |
| `cron_list` | `agent_id: str = None` | `{jobs: [...]}` | `auto` | Lists scheduled jobs. |
| `cron_cancel` | `job_id: str` | `{cancelled: bool}` | `user` | Cancels a scheduled job. |
| `workflow_start` | `workflow_id: str, input: dict` | `{run_id: str}` | `user` | Starts a workflow DAG run. |
| `workflow_status` | `run_id: str` | `{status: str, steps: [...]}` | `auto` | Polls workflow progress. |

### 3.4 Agent roster tools

| Tool | Args | Returns | Approval tier | Notes |
|---|---|---|---|---|
| `agent_list` | None | `{agents: [...]}` | `auto` | Lists the named agents on this Logos instance. |
| `agent_message` | `target_agent_id: str, message: str` | `{delivered: bool, response: str}` | `user` | Cross-agent message passing. The gateway dispatches the message to the target agent's sandbox worker and returns the response. This is the primitive that enables multi-agent collaboration. |

---

## 4. Design Decisions

### 4.1 In-process vs sidecar subprocess

Could we host the Logos capabilities as a *real* stdio MCP server subprocess (a Python module the gateway spawns at startup) instead of in-process callables?

**Decision: in-process.** Reasons:

- The capability tools need direct references to runner state (`runner.adapters`, `runner.session_store`, `runner.cron_manager`, `runner.memory_manager`). A subprocess would need IPC back to the gateway, which is exactly the complexity we're trying to avoid.
- The tools mutate gateway state (creating cron jobs, sending messages via adapters). Running them in a subprocess introduces race conditions with the gateway itself.
- The MCP "server" abstraction is really just "a thing that handles tool calls". We don't need a separate process to satisfy that contract; we need a dispatch table.
- The stdio MCP protocol overhead (JSON serialization round-trips) is pure waste when the caller is sitting in the same memory space.

### 4.2 How does a tool know which agent is calling?

When a sandbox worker makes a request to `/mcp/logos/<tool>`, the request includes the `worker_id` (via the worker's WebSocket association). The MCP handler can pass that `worker_id` into the in-process tool dispatcher, which resolves it to `named_agents[agent_id]` via `_sanitize_sandbox_name` reverse lookup.

The dispatcher passes the resolved agent record to the tool as a hidden first argument (`calling_agent: dict`). Tools can use that to:

- Scope access (e.g. `session_read` only reads sessions owned by the calling agent)
- Bill usage (e.g. per-agent API call counts)
- Audit (log "agent `hermes` called `platform_send`" for the dashboard)

### 4.3 Tool argument validation

Tools use **Pydantic v2 models** for argument validation. Each tool gets a pair:

```python
class PlatformSendArgs(BaseModel):
    platform: Literal["telegram", "discord", "slack", "whatsapp", "email", "homeassistant"]
    channel: str
    text: str
    media_urls: list[str] | None = None
    reply_to: str | None = None

async def platform_send_tool(calling_agent: dict, args: PlatformSendArgs) -> dict:
    ...
```

The dispatcher parses the incoming JSON against the model before calling the tool function. Invalid arguments return a structured error to the sandbox instead of crashing.

### 4.4 Approval gating

The existing `MCPGatewayService` has policy tiers (`auto_approve`, `user_approve`, `admin_approve`, `deny`) based on the tool's *category*. For the in-process `logos` server, each tool declares its tier in a decorator or metadata dict. When the sandbox calls a `user_approve` tool, the dispatcher surfaces an approval request through the existing approval UI before executing.

**Special case — contextual approval for `platform_send`:** The tier depends on whether the target channel has been seen before (reply-in-thread = auto; new outbound channel = user). This requires looking at a per-agent-per-channel "seen channels" set, which lives in `runner.platform_seen_channels` (new state). The dispatcher consults this before picking a tier.

### 4.5 Return shape

All tool results follow a consistent envelope:

```json
{
  "ok": true,
  "data": {...},
  "error": null,
  "tool": "platform_send",
  "duration_ms": 42
}
```

or on failure:

```json
{
  "ok": false,
  "data": null,
  "error": {"type": "AdapterError", "message": "Telegram connection closed", "recoverable": true},
  "tool": "platform_send",
  "duration_ms": 12
}
```

This mirrors the existing MCP tool response shape so the sandbox's MCP client code doesn't need to handle a special case for the `logos` server.

### 4.6 Where the code lives

```
gateway/mcp_logos/
├── __init__.py           # public register_logos_server(runner, service) entry point
├── server.py             # InProcessMCPServer class + tool registry + dispatcher
├── tools/
│   ├── __init__.py
│   ├── platform.py       # platform_send, home_message
│   ├── session.py        # session_read, session_list
│   ├── memory.py         # memory_recall, memory_write
│   ├── cron.py           # cron_schedule, cron_list, cron_cancel
│   ├── workflow.py       # workflow_start, workflow_status
│   └── agents.py         # agent_list, agent_message
└── schemas.py            # Pydantic models for tool args
```

The `gateway/mcp_service.py` boot sequence calls `register_logos_server(runner, self)` after the external servers are started. The HTTP handler in `gateway/mcp_handlers.py` gains one branch for in-process dispatch.

### 4.7 Network policy for the sandbox

Sandbox workers already have `host.openshell.internal:{mcp_port}` in their default network policy (`gateway/policies/openshell_default.yaml`) for calling the existing external MCP servers. The `logos` server lives at the same host + port under a different path, so **no policy change needed**. The sandbox just sees a new `/mcp/logos/*` route appear in its tool catalogue.

### 4.8 Error handling and timeouts

Each tool has a per-call timeout (default 30s, overridable in the tool metadata). The dispatcher wraps the tool call in `asyncio.wait_for` and returns a structured timeout error on breach. Tool functions are responsible for their own cleanup on cancellation — e.g. `platform_send` must not leave the adapter in an inconsistent state if it's cancelled mid-send.

---

## 5. Implementation Phases

### Phase L.1: In-process server primitive

- Create `gateway/mcp_logos/server.py` with `InProcessMCPServer` class.
- Define the protocol surface (`name`, `description`, `tool_count`, `call_tool`, `list_tools`).
- Implement the dispatcher: arg validation, tier resolution, approval gating, timeout, error envelope.
- Wire it into `MCPGatewayService.start()` so the `logos` server appears in the catalogue at boot.
- Update the HTTP handler in `gateway/mcp_handlers.py` to branch on server type and call the in-process dispatcher.
- Zero tools yet — just an empty `logos` server that can be listed.

### Phase L.2: Platform tools

- `platform_send` (with reply-in-thread vs new-channel tier resolution)
- `home_message`
- Unit tests that assert the bot token never leaves the gateway (inject a fake adapter, assert `calling_agent` cannot read env vars).

### Phase L.3: Session + memory tools

- `session_read`, `session_list` against `runner.session_store`
- `memory_recall`, `memory_write` against the existing memory manager
- Scope access by `calling_agent`

### Phase L.4: Cron + workflow tools

- `cron_schedule`, `cron_list`, `cron_cancel`
- `workflow_start`, `workflow_status`
- These depend on the cron manager and workflow engine already in `gateway/run.py` / `gateway/workflows/`

### Phase L.5: Agent roster tools

- `agent_list`
- `agent_message` — dispatches via the same `WorkerRegistry.dispatch_task` that platforms will use in Phase 5.3. This is the cross-agent primitive.

### Phase L.6: Dashboard surface

- Admin → MCP tab gains a "Logos capabilities" section showing each tool, its tier, its call count, and recent errors.
- This is read-only for v1; editing tool tiers comes later.

---

## 6. Interaction with the Platform Migration

The platform migration (`platforms-as-gateway-mediated.md`) depends on this work as follows:

| Platform migration phase | Requires |
|---|---|
| 5.1 — Disable platforms | **Independent.** Already shipped in `dc40e6d`. |
| 5.2 — `worker_registry` on runner, `dispatch_platform_message` stub | **Independent.** Pure plumbing in the runner. |
| 5.3 — Real inbound dispatch | **Independent.** Uses existing `WorkerRegistry.dispatch_task`. |
| 5.4 — Re-enable adapters + routing table | **Depends on L.1** (so the sandbox can reply via `logos.platform_send` when the agent's response contains no text but a tool call result). Actually — strictly speaking 5.4 only needs L.1 if the agent is expected to reply via a tool. For a pure "text response" flow 5.4 is independent. Probably ship L.2 before 5.4 so the UX is sane. |
| 5.5 — Outbound `platform_send` | **Depends on L.2** directly. This is the core coupling. |
| 5.6 — Delete `_run_agent` | **Independent.** Just bookkeeping. |
| 5.7 — Cron migration | **Depends on L.4.** Cron needs `cron_schedule` + `home_message` to work. |

**Recommended build order:**

1. L.1 (primitive)
2. L.2 (platform tools)
3. 5.2 → 5.3 → 5.4 → 5.5 (platforms end-to-end)
4. L.3 (session + memory, nice-to-have but not blocking)
5. L.4 (cron tools)
6. 5.7 (cron migration)
7. L.5 (agent roster, unblocks multi-agent)
8. 5.6 (delete `_run_agent`)
9. L.6 (dashboard surface)

---

## 7. Open Questions (for review)

- **[DECIDE] 7.1** — Naming. Is `logos` the right server name, or should it be `gateway`, `platform`, `system`, or something else? Affects the URL (`/mcp/logos/*`) and the tool catalogue UI. `logos` is consistent with the product name but collides visually with the gateway itself. `gateway` is more technical but clearer.

- **[DECIDE] 7.2** — Per-user vs per-agent scoping for `memory_recall`. If two users share the same named agent "Hermes", should their memories be isolated? Proposal: memory is scoped by agent, not by user, because an agent's persona is shared across users — but `calling_agent` can carry a `user_id` hint so agents *can* write per-user-memory if they want.

- **[DECIDE] 7.3** — Should `agent_message` (cross-agent) wait for the target agent's response synchronously, or fire-and-forget with a separate polling tool? Sync is simpler for the caller but head-of-line blocks. Proposal: sync with a short timeout (30s); for longer-running cross-agent work, agents use `workflow_start` instead.

- **[DECIDE] 7.4** — Pydantic vs JSON Schema. Pydantic gives us free validation + type hints, but the MCP protocol natively uses JSON Schema for tool argument descriptions. Solution: generate JSON Schema from the Pydantic models via `.model_json_schema()` when publishing the tool catalogue. No double-definition. (Not really an open question — just a note.)

- **[DECIDE] 7.5** — Should tool calls be logged to the existing `agent_runs` table for traceability, or a new `mcp_tool_calls` table? Proposal: a new table, because `agent_runs` is per-turn and tool calls can happen at any granularity within a turn. Both tables cross-reference each other by `run_id`.

- **[DECIDE] 7.6** — Rate limiting. Should the dispatcher enforce per-agent rate limits on tool calls to prevent runaway loops burning platform quota? Proposal: yes, but make the limits generous by default (100 calls/min per tool per agent) and tunable per-tool in the metadata.

---

## 8. Code Changes Summary

| File | Phase | Change |
|---|---|---|
| `gateway/mcp_logos/__init__.py` | L.1 | NEW — public entry point |
| `gateway/mcp_logos/server.py` | L.1 | NEW — `InProcessMCPServer` + dispatcher |
| `gateway/mcp_logos/schemas.py` | L.1 | NEW — Pydantic arg models |
| `gateway/mcp_logos/tools/platform.py` | L.2 | NEW — `platform_send`, `home_message` |
| `gateway/mcp_logos/tools/session.py` | L.3 | NEW — `session_read`, `session_list` |
| `gateway/mcp_logos/tools/memory.py` | L.3 | NEW — `memory_recall`, `memory_write` |
| `gateway/mcp_logos/tools/cron.py` | L.4 | NEW — `cron_schedule`, `cron_list`, `cron_cancel` |
| `gateway/mcp_logos/tools/workflow.py` | L.4 | NEW — `workflow_start`, `workflow_status` |
| `gateway/mcp_logos/tools/agents.py` | L.5 | NEW — `agent_list`, `agent_message` |
| `gateway/mcp_service.py` | L.1 | Register logos server after external servers boot |
| `gateway/mcp_handlers.py` | L.1 | Branch HTTP handler on server type |
| `gateway/run.py` | L.1 | Hold reference to in-process server for later tool injection |
| `gateway/html/main_app.html` | L.6 | Admin → MCP tab shows Logos capabilities |
| `gateway/auth/db.py` | L.5 | `mcp_tool_calls` audit table |

---

## 9. Risks and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| **Tool calls deadlock on runner state** | High — freezes the gateway | Low | All tools use `asyncio.wait_for` with a timeout. Long-running ops get a `job_id` and poll pattern (see `workflow_start`). |
| **`calling_agent` spoofing** | High — agent A masquerades as B | Low | `calling_agent` is resolved server-side from the WebSocket association, never accepted from the tool args. |
| **Cross-agent `agent_message` creates infinite loops** | Medium — agents chatter forever | Medium | Per-agent rate limit + max recursion depth tracked in a call stack threaded through the dispatcher. |
| **Tool arg validation bypass** | Medium — crashes or security issue | Low | Pydantic v2 validates before dispatch. Failed validation returns a structured error, never calls the tool function. |
| **Schema drift between JSON Schema and Pydantic** | Low | Low | JSON Schema is generated from Pydantic, not maintained by hand. |
| **`memory_write` blows up the DB** | Medium — storage exhaustion | Medium | Per-agent write quota. Reject writes over a size threshold. |

---

## 10. Success Criteria

- A sandboxed agent can call `/mcp/logos/platform_send` with `platform="telegram", channel="home", text="hi"` and the message arrives in the configured home channel, with the bot token never leaving the gateway process.
- A sandboxed agent can call `/mcp/logos/memory_write` and see the content in `/mcp/logos/memory_recall` on the next turn.
- A sandboxed agent can call `/mcp/logos/cron_schedule` with a valid cron expression and see the job in `runner.cron_manager.jobs` on the next tick.
- `agent_message` from agent A to agent B dispatches a task to B's sandbox worker and returns B's response within the 30s timeout.
- The `logos` server appears in the MCP catalogue (`/admin/mcp`) with its tool list, tier, and call count.
- An agent that invokes `platform_send` to a never-seen channel triggers the `user_approve` flow in the existing approval UI before the message is sent.
- Tool calls are audited in `mcp_tool_calls` with `(run_id, tool_name, agent_id, duration_ms, ok, error)` per call.
- The `sandbox_worker.py` code does not need to change — the new `logos` server is discoverable via the normal MCP catalogue refresh.

---

## 11. What This Doc Is NOT

- **Not a rewrite of the existing external MCP server support.** External stdio MCP servers continue to work exactly as they do today.
- **Not a new transport layer.** HTTP + JSON-RPC stays; we're adding a new backend behind the existing route.
- **Not a multi-language tool system.** All tools are Python functions running in the gateway process.
- **Not a permission system redesign.** We reuse the existing tier-based approval machinery.
- **Not an agent-to-agent protocol.** `agent_message` is the primitive; higher-level protocols (broadcasting, group chats, agent quorum) are out of scope.

---

*DRAFT — Logos Capabilities MCP Server — April 2026*
