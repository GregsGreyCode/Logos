# MCP Redesign: Centralized Gateway Service with Dynamic Access Approval

> **Status: Implemented** — shipped in v0.5.73. See `gateway/mcp_service.py`, `gateway/mcp_handlers.py`, `gateway/mcp_access.py`, and `tools/mcp_access_tool.py`.

## Problem with the Previous Architecture

MCP servers currently run as subprocess children of each agent process:

- N agents × M MCP servers = N×M processes
- In OpenShell/k8s, MCP servers must run *inside* the sandbox — but filesystem, GitHub, and database servers need host access, not sandbox access
- Credentials for MCP servers must exist inside every sandbox
- No shared state between agents using the same MCP server
- Config is silently absent in OpenShell mode (broken, not obvious)

---

## New Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Logos Gateway                                               │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  MCPGatewayService  (gateway/mcp_service.py)           │  │
│  │  • reads config → starts stdio/HTTP servers once       │  │
│  │  • exposes each via StreamableHTTP at /mcp/{name}      │  │
│  │  • catalogue: name, category, description per server   │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  MCPAccessRegistry  (gateway/mcp_access.py)            │  │
│  │  • per-session approved server set {session_id → set}  │  │
│  │  • thread-safe, mirrors approval.py pattern            │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  HTTP routes (gateway/mcp_handlers.py):                      │
│    GET  /api/mcp/catalogue                                   │
│    GET  /api/mcp/status                                      │
│    POST /api/mcp/grants/{session_id}/{server}                │
│    DELETE /api/mcp/grants/{session_id}/{server}              │
│    * /mcp/{server-name}   (StreamableHTTP proxy)             │
└──────────────────────────────┬───────────────────────────────┘
                               │ HTTP /mcp/{name}
              ┌────────────────┼────────────────┐
              │                │                │
        Local process     OpenShell        K8s pod
        127.0.0.1:8081    host.docker      logos-gateway.
                          .internal:8081   svc.cluster.local:8081
```

### Agent experience

1. **Boot** — receives MCP catalogue (names + categories, no tools yet)
2. **Needs a capability** — calls `request_mcp_access("filesystem", "need to read project files")`
3. **Gateway checks policy:**
   - `search` → auto-approve immediately
   - `filesystem` → send approval request to user (Telegram / web UI)
   - `code-exec` → require admin approval
4. **Approved** → gateway grants, tools injected on the next agent turn
5. **Agent has filesystem tools** alongside built-ins

---

## Config Format

Extends existing `~/.logos/config.yaml`:

```yaml
mcp_servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    category: filesystem               # used for policy lookup
    description: "Local file read/write"

  web-search:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: "sk-..."
    category: search
    description: "Web search via Brave"

  remote-api:
    url: "https://my-mcp-server.example.com/mcp"
    headers:
      Authorization: "Bearer sk-..."
    category: external-api
    description: "Company internal MCP API"

mcp_policy:
  auto_approve:  [search, read-only]       # granted immediately, no prompt
  user_approve:  [filesystem, external-api] # user prompted via Telegram/web
  admin_approve: [code-exec, database]     # requires admin account approval
```

---

## Cross-Platform URL Resolution

`MCPGatewayService.get_server_url(name, platform)` returns the correct URL per execution mode:

| Platform | URL |
|---|---|
| Local process | `http://127.0.0.1:8081/mcp/{name}` |
| OpenShell sandbox | `http://host.docker.internal:8081/mcp/{name}` |
| Kubernetes pod | `http://logos-gateway.{ns}.svc.cluster.local:8081/mcp/{name}` |

Port controlled by `HERMES_MCP_PORT` env var (default `8081`).

---

## Implementation Plan

### New Files

| File | Purpose |
|---|---|
| `gateway/mcp_service.py` | `MCPGatewayService` — process lifecycle, StreamableHTTP bridge |
| `gateway/mcp_access.py` | Per-session grant registry (thread-safe) |
| `gateway/mcp_handlers.py` | aiohttp route handlers + `/mcp/{name}` proxy |
| `tools/mcp_access_tool.py` | `request_mcp_access` tool — self-registers at import |

### Modified Files

| File | Change |
|---|---|
| `gateway/http_api.py` | Register MCP routes; init `MCPGatewayService`; approval webhook |
| `gateway/run.py` | Inject approved MCP toolsets before each `AIAgent` construction |
| `gateway/auth/middleware.py` | `/mcp/` path uses bearer token auth (no login redirect) |
| `gateway/auth/policy.py` | Add `ACTION_MCP_ACCESS = "mcp_access"` constant |
| `core/model_tools.py` | Add `mcp_access_tool` to discovery; gate local MCP on `HERMES_GATEWAY_MCP` |
| `tools/mcp_tool.py` | Add `inject_mcp_server_for_session()` for post-grant tool hot-add |
| `gateway/policies/openshell_default.yaml` | Confirm port 8081 in `host.docker.internal` egress allowlist |

---

## Key Design Decisions

### stdio → HTTP Bridge

The MCP Python SDK's server-side transport is ASGI-based. Rather than pulling in an ASGI dependency, the bridge is implemented directly in aiohttp as a thin JSON-RPC forwarder:

- `POST /mcp/{name}` — receives a JSON-RPC request, calls the corresponding method on the upstream `ClientSession`, returns the response
- `GET /mcp/{name}` — SSE stream for server-sent events

~200 lines, no additional dependencies.

### Dynamic Tool Injection (Option A — safe for v1)

Tools are not injected mid-turn. When `request_mcp_access` approves (or the user approves), the grant is stored in `MCPAccessRegistry`. On the **next** agent turn, `_run_agent()` reads the grants and adds the MCP toolsets to `enabled_toolsets` before constructing `AIAgent`. The agent's response confirms: "Access granted — tools available from your next message."

Option B (true mid-turn injection into the live `AIAgent.tools` list) is deferred to a future iteration.

### Tool Registration Scope

Tools register globally in `ToolRegistry` but each tool's `check_fn` verifies `has_access(session_id, server_name)` before dispatching. Session ID is propagated via `contextvars.ContextVar` set by the agent loop.

### Windows `CREATE_NO_WINDOW`

The MCP SDK's `stdio_client()` doesn't expose subprocess flags. Gateway-managed stdio processes on Windows use a custom spawn path that passes `creationflags=0x08000000` (`CREATE_NO_WINDOW`) to `asyncio.create_subprocess_exec`. Gated on `sys.platform == "win32"`.

### Backward Compatibility

- Standalone CLI mode (`HERMES_GATEWAY_MCP` not set): `discover_mcp_tools()` runs exactly as today
- `request_mcp_access` in CLI mode returns a clear error: "Gateway MCP service not running"
- Existing `mcp_servers` config without `category`/`description` fields defaults to `category="general"`, `description=""`

---

## OpenShell Egress Policy

Port `8081` must be in the `host.docker.internal` allow rule in `gateway/policies/openshell_default.yaml`:

```yaml
- host: host.docker.internal
  ports: [11434, 1234, 8000, 8080, 8081]
```

---

## Kubernetes Notes

The gateway pod exposes port 8081 (or 8080 sub-path). Agent pods connect via the existing `logos-gateway` ClusterIP service. No new k8s objects required — only the service port mapping needs updating in the deployment manifests.

---

## Future Work

- **Option B tool injection**: hot-inject tools mid-turn into the running `AIAgent` instance
- **Grant persistence**: store `mcp_grants` in the auth DB so grants survive gateway restart
- **Per-server rate limiting**: prevent MCP abuse by sandboxed agents
- **MCP server health UI**: surface server status in the admin panel
- **Scoped grants**: approve access to a specific subset of tools within an MCP server, not the whole server
