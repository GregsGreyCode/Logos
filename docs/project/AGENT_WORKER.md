# Agent Worker Architecture

**Status:** Planning  
**Created:** 2026-04-03

## Problem

When Logos "spawns an agent instance", it launches a **complete gateway** — web
UI, auth system, session management, platform adapters, HTTP server, cron ticker,
the whole stack. Each pod is ~580MB, takes 20+ seconds to start, and runs
services the user never interacts with. The main gateway already provides the UI;
spawned instances duplicate it needlessly.

This is like spawning a new Kubernetes cluster every time you want to run a
container.

## Current Architecture

```
┌─────────────────────────┐     ┌─────────────────────────┐
│   Main Logos Gateway    │     │   Spawned Instance #1   │
│                         │     │   (FULL GATEWAY COPY)   │
│  Web UI                 │     │  Web UI (unused)        │
│  Auth / Sessions        │     │  Auth / Sessions        │
│  Platform Adapters      │     │  Platform Adapters      │
│  HTTP API + SSE         │     │  HTTP API + SSE         │
│  Cron Ticker            │     │  Cron Ticker            │
│  AIAgent loop           │     │  AIAgent loop           │
│  Tools                  │     │  Tools                  │
│  Memory / Knowledge     │     │  Memory / Knowledge     │
└─────────────────────────┘     └─────────────────────────┘
         ~580MB                          ~580MB
```

Each spawned instance gets its own NodePort, runs its own web UI, manages its own
sessions independently. Users interact with them by clicking "Chat →" which opens
a connection to that instance's `/chat` SSE endpoint. The main gateway has no
visibility into what the spawned agents are doing.

## Proposed Architecture

```
┌─────────────────────────────────────────────────┐
│              Main Logos Gateway                   │
│                                                   │
│  Web UI (World tab, Chats, Inspector)            │
│  Auth / Sessions / Platform Adapters              │
│  Agent Orchestrator (routes messages to workers)  │
│  HTTP API + SSE (unified /chat endpoint)          │
│  Worker Registry (track worker health/state)      │
│  Cron Ticker                                      │
└──────────┬──────────┬──────────┬─────────────────┘
           │ gRPC/WS  │ gRPC/WS  │ gRPC/WS
    ┌──────▼──┐ ┌─────▼───┐ ┌────▼─────┐
    │ Worker  │ │ Worker  │ │ Worker   │
    │ "hermes"│ │ "coder" │ │ "analyst"│
    │         │ │         │ │          │
    │ AIAgent │ │ AIAgent │ │ AIAgent  │
    │ Tools   │ │ Tools   │ │ Tools    │
    │ Memory  │ │ Memory  │ │ Memory   │
    │ PVC     │ │ PVC     │ │ PVC      │
    └─────────┘ └─────────┘ └──────────┘
      ~150MB      ~150MB      ~150MB
```

### What changes

| Component | Current (full gateway) | Proposed (worker) |
|-----------|----------------------|-------------------|
| **Entrypoint** | `logos gateway run` | `logos worker run --connect ws://gateway:8080/ws/worker` |
| **Web UI** | Full UI on its own port | None — all UI through main gateway |
| **Auth** | Own auth DB | None — gateway handles auth |
| **Sessions** | Own session store | Receives session context from gateway |
| **Platform adapters** | Full Telegram/Discord/etc. | None — gateway routes messages |
| **HTTP server** | aiohttp on port 8080 | None (or minimal health endpoint) |
| **AIAgent** | Full agent loop | Full agent loop (this stays) |
| **Tools** | Full toolset | Full toolset (this stays) |
| **Memory/Knowledge** | Per-instance HERMES_HOME | Per-instance HERMES_HOME (this stays) |
| **Image size** | ~580MB | ~150MB (no web assets, no platform deps) |
| **Startup time** | ~20s | ~3s |

### What stays the same

- AIAgent loop — untouched
- Tool execution — untouched
- Memory/knowledge stores — untouched, still per-instance PVC
- Per-agent HERMES_HOME isolation — untouched
- Soul system — untouched

## Worker Protocol

### Gateway → Worker (task dispatch)

The gateway sends a task when a user sends a chat message:

```json
{
  "type": "run_conversation",
  "task_id": "uuid",
  "session_id": "abc123",
  "session_key": "agent:main:web:dm",
  "message": "Write a Python script to...",
  "history": [...],
  "context_prompt": "You are Hermes, an AI agent...",
  "model": "qwen3:9b",
  "model_kwargs": { "api_key": "...", "base_url": "http://..." },
  "toolsets": ["hermes-cli"],
  "reasoning_config": { "enabled": true },
  "max_iterations": 90
}
```

### Worker → Gateway (results + streaming)

Results stream back as the agent works:

```json
{"type": "tool_progress", "task_id": "uuid", "tool": "web_search", "status": "running"}
{"type": "tool_progress", "task_id": "uuid", "tool": "web_search", "status": "done"}
{"type": "thinking", "task_id": "uuid", "content": "<reasoning>...</reasoning>"}
{"type": "token", "task_id": "uuid", "content": "Here is"}
{"type": "token", "task_id": "uuid", "content": " the script"}
{"type": "done", "task_id": "uuid", "final_response": "...", "api_calls": 3, "tools_used": ["web_search"]}
```

### Worker → Gateway (heartbeat)

```json
{"type": "heartbeat", "worker_id": "hermes-greg-researcher", "status": "idle|busy", "uptime_s": 3600}
```

### Gateway → Worker (interrupt)

```json
{"type": "interrupt", "task_id": "uuid", "new_message": "Actually, use TypeScript instead"}
```

## Transport Options

| Option | Pros | Cons |
|--------|------|------|
| **WebSocket** | Bidirectional, low latency, native browser support | Need reconnection logic |
| **gRPC** | Strong typing, streaming, efficient binary | Heavier dependency, no browser interop |
| **HTTP + SSE** | Simple, reuses existing patterns | Unidirectional (need separate POST for gateway→worker) |
| **Redis pub/sub** | Decoupled, scalable, persistent queues | New dependency, more infrastructure |

**Recommendation**: WebSocket. It's bidirectional (gateway pushes tasks, worker
streams results), lightweight, and the gateway already runs aiohttp which has
excellent WebSocket support. Workers connect to `ws://gateway:8080/ws/worker`
on startup and maintain the connection.

Fallback: If the worker loses connection, it reconnects with exponential backoff.
In-flight tasks are lost (gateway retries or reports error to user).

## Worker Lifecycle

### Startup

1. Worker starts with `logos worker run --connect ws://gateway:8080/ws/worker --name hermes-greg-researcher`
2. Connects to gateway WebSocket
3. Sends registration: `{"type": "register", "worker_id": "...", "soul": "researcher", "toolsets": [...]}`
4. Gateway adds to worker registry
5. Worker appears in World tab

### Task Execution

1. User sends message in Chats tab (routed to specific agent)
2. Gateway looks up worker for that agent name
3. Gateway sends `run_conversation` task via WebSocket
4. Worker creates ephemeral AIAgent, runs conversation
5. Worker streams tokens/tool progress back to gateway
6. Gateway forwards to client via SSE (existing `/chat` endpoint)
7. Gateway persists transcript to session store

### Idle / Heartbeat

- Workers send heartbeat every 30s
- Gateway marks workers as unhealthy after 90s without heartbeat
- Unhealthy workers shown with error state in World tab

### Shutdown

1. Gateway sends `{"type": "shutdown"}` (graceful)
2. Worker finishes current task (or interrupts after timeout)
3. Worker closes WebSocket
4. Gateway removes from registry
5. K8s pod terminates

## Implementation Phases

### Phase 1 — Worker entrypoint + WebSocket connection

- [ ] New CLI command: `logos worker run --connect URL --name NAME`
- [ ] Worker connects to gateway via WebSocket, sends registration
- [ ] Gateway accepts worker connections at `/ws/worker`
- [ ] Worker registry in gateway (track connected workers + health)
- [ ] Heartbeat ping/pong

### Phase 2 — Task dispatch + execution

- [ ] Gateway routes chat messages to workers instead of running AIAgent locally
- [ ] Worker receives task, creates AIAgent, runs conversation
- [ ] Worker streams results back via WebSocket
- [ ] Gateway forwards to client SSE stream
- [ ] Gateway persists transcript

### Phase 3 — Token streaming + tool progress

- [ ] Real-time token streaming from worker → gateway → client
- [ ] Tool progress events (tool name, status, duration)
- [ ] Interrupt support (gateway sends interrupt, worker stops agent)

### Phase 4 — K8s integration

- [ ] New `worker` container image (slim, no web UI)
- [ ] KubernetesExecutor spawns workers instead of full gateways
- [ ] Worker pods connect back to gateway service URL
- [ ] PVC mounting unchanged

### Phase 5 — Migration

- [ ] Primary gateway's own AIAgent runs as an in-process "worker"
  (same interface, no WebSocket — just direct function call)
- [ ] Remove full-gateway spawning path
- [ ] Update World tab to show worker state from registry

## Docker Image

### Current: `logos` (full gateway)

```dockerfile
FROM python:3.11-slim
# Install ALL dependencies (web, platforms, tools, UI assets)
# ~580MB
CMD ["logos", "gateway", "run"]
```

### Proposed: `logos-worker` (headless agent)

```dockerfile
FROM python:3.11-slim
# Install ONLY: openai, anthropic, tools, agent core
# Skip: aiohttp (web), platform adapters, UI assets, auth
# ~150MB
CMD ["logos", "worker", "run"]
```

Or better: single image with two entrypoints. The `worker` entrypoint skips
web UI initialization, platform adapter loading, and auth setup.

## Key Decisions

1. **Single image or separate images?** Single image with two entrypoints is
   simpler for CI/CD. Two images saves ~400MB per worker pod but doubles the
   build pipeline. Recommendation: start with single image, split later if
   needed.

2. **WebSocket vs gRPC?** WebSocket for simplicity. gRPC if we need strong
   typing and efficient binary serialization later.

3. **Where does session state live?** Gateway owns sessions. Workers receive
   history as part of the task payload and don't access the session store
   directly. This keeps workers stateless (except for their PVC).

4. **How does the primary agent work?** The gateway's own agent becomes an
   in-process worker — same interface, but calls AIAgent directly instead of
   going through WebSocket. This is Phase 5.

5. **Can workers serve multiple users?** Yes, but one task at a time. The
   gateway queues tasks if the worker is busy. Workers are single-threaded
   (AIAgent is synchronous).

6. **What about Honcho?** Honcho managers are currently per-session in the
   gateway. With workers, the gateway passes Honcho config and the worker
   manages its own Honcho state. Or Honcho becomes a shared service.
