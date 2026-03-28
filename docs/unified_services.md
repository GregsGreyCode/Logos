# Unified Services Architecture — MCP Servers + Tool Credentials

> **Status:** Design — ready for implementation

## Problem

Tool API keys (Firecrawl, FAL, Browserbase, etc.) are currently:
- Stored in `~/.logos/.env` as plaintext environment variables
- Not manageable through the web dashboard
- Not visible to users who deploy via Docker/Windows (no terminal access)
- Not subject to per-agent access control
- Invisible — users don't know which tools need keys until they fail

MCP servers are already centralized in the gateway with per-agent access control and a catalogue API. Tool credentials should follow the same pattern.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Gateway                                              │
│                                                       │
│  ┌─────────────────────────────────────────────────┐ │
│  │  Services Registry                               │ │
│  │  (unified view of all external integrations)     │ │
│  │                                                   │ │
│  │  ┌─────────────┐  ┌──────────────────────────┐  │ │
│  │  │ MCP Servers  │  │ Tool Credentials         │  │ │
│  │  │ (existing)   │  │ (new)                    │  │ │
│  │  │              │  │                          │  │ │
│  │  │ filesystem   │  │ FIRECRAWL_API_KEY    ✓   │  │ │
│  │  │ github       │  │ FAL_KEY              ✗   │  │ │
│  │  │ slack        │  │ BROWSERBASE_API_KEY  ✗   │  │ │
│  │  └─────────────┘  │ OPENROUTER_API_KEY   ✓   │  │ │
│  │                    │ ELEVENLABS_API_KEY   ✗   │  │ │
│  │                    └──────────────────────────┘  │ │
│  └─────────────────────────────────────────────────┘ │
│                                                       │
│  API:                                                 │
│    GET  /api/services           — unified catalogue   │
│    POST /api/services/keys      — set a credential    │
│    DELETE /api/services/keys    — remove a credential  │
│                                                       │
│  UI: Dashboard → Services tab                         │
│    Shows MCP servers + tool integrations side-by-side  │
│    Inline key entry for tools that need credentials    │
│    Status: connected / needs key / disabled            │
└──────────────────────────────────────────────────────┘
```

## Data Model

### Tool credential registry (built from tool registration data)

Each tool that registers with `requires_env=["FIRECRAWL_API_KEY"]` automatically appears in the services catalogue. The registry already has this data — we just need to surface it.

```python
# Already exists in tools/registry.py:
class ToolEntry:
    name: str
    toolset: str
    requires_env: list[str]  # ← the keys needed
    check_fn: Callable       # ← returns True if available
```

### Credential storage (new)

Credentials stored in `platform_settings.feature_flags` JSON under a `credentials` key. This keeps them in the existing auth DB (already file-locked, already backed up by PVC on k8s).

```json
{
  "setup_completed": true,
  "credentials": {
    "FIRECRAWL_API_KEY": "fc-...",
    "FAL_KEY": "fal_...",
    "BROWSERBASE_API_KEY": "bb_..."
  }
}
```

At gateway startup and before each agent run, credentials from the DB are injected into `os.environ` (same as `.env` reload). This means **zero changes to existing tool code** — tools still read from `os.environ`.

### Priority order (credentials resolution)

```
1. os.environ (set by Docker env, k8s secrets, or /model command)
2. ~/.logos/.env (reloaded before each chat)
3. DB credentials (injected at startup + before each chat)
```

Higher priority wins. This means k8s secrets and `.env` still work — the DB is a fallback for users who only have the web UI.

## API Endpoints

### GET /api/services

Returns the unified catalogue of all external services:

```json
{
  "mcp_servers": [
    {"name": "filesystem", "category": "local", "connected": true, "approval_tier": "auto_approve"}
  ],
  "tool_integrations": [
    {
      "env_var": "FIRECRAWL_API_KEY",
      "label": "Firecrawl (Web Search)",
      "tools": ["web_search", "web_extract"],
      "toolset": "web",
      "has_key": true,
      "available": true,
      "help_url": "https://firecrawl.dev/"
    },
    {
      "env_var": "FAL_KEY",
      "label": "fal.ai (Image Generation)",
      "tools": ["image_generate"],
      "toolset": "image",
      "has_key": false,
      "available": false,
      "help_url": "https://fal.ai/"
    }
  ]
}
```

### POST /api/services/keys

Set a credential. Requires admin permission.

```json
{"env_var": "FIRECRAWL_API_KEY", "value": "fc-..."}
```

The value is stored in the DB and immediately injected into `os.environ`. Response confirms whether the tool is now available.

### DELETE /api/services/keys

Remove a credential.

```json
{"env_var": "FIRECRAWL_API_KEY"}
```

## UI Design

A new **Services** tab in the dashboard (or a section within the existing Agents tab):

```
┌─ Services ──────────────────────────────────────────┐
│                                                      │
│  MCP Servers                                         │
│  ┌──────────────────────────────────────────────┐   │
│  │ filesystem    local     ● connected           │   │
│  │ github        external  ○ needs approval      │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  Tool Integrations                                   │
│  ┌──────────────────────────────────────────────┐   │
│  │ ● Web Search (Firecrawl)      key set ✓      │   │
│  │   web_search, web_extract                     │   │
│  │                                               │   │
│  │ ○ Image Generation (fal.ai)   needs key       │   │
│  │   image_generate                              │   │
│  │   [Enter FAL_KEY...] [Save]                   │   │
│  │                                               │   │
│  │ ○ Browser (Browserbase)       needs key       │   │
│  │   browser_navigate, browser_click, ...        │   │
│  │   [Enter BROWSERBASE_API_KEY...] [Save]       │   │
│  │                                               │   │
│  │ ● Mixture of Agents            key set ✓      │   │
│  │   mixture_of_agents                           │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ℹ Keys set here are stored in the gateway database  │
│  and available to all agents. Keys from .env or k8s  │
│  secrets take priority.                              │
└──────────────────────────────────────────────────────┘
```

## Implementation Plan

### Phase 1 — Backend (do now)

1. **`gateway/services.py`** (new): Credential storage + injection
   - `get_credentials() -> dict` — read from DB
   - `set_credential(env_var, value)` — write to DB + os.environ
   - `delete_credential(env_var)` — remove from DB + os.environ
   - `inject_credentials()` — load all DB credentials into os.environ
   - `get_tool_integrations()` — build catalogue from tool registry

2. **`gateway/http_api.py`**: Register API routes
   - `GET /api/services` — unified catalogue
   - `POST /api/services/keys` — set credential (admin only)
   - `DELETE /api/services/keys` — remove credential (admin only)

3. **`gateway/run.py`**: Call `inject_credentials()` before each agent run
   (same location as `.env` reload)

### Phase 2 — Frontend (do now)

4. **`gateway/html/main_app.html`**: Services tab/section
   - List MCP servers (from existing `/api/mcp/catalogue`)
   - List tool integrations (from new `/api/services`)
   - Inline key entry fields with save/delete
   - Status indicators

### Phase 3 — Polish (follow-up)

5. Per-agent tool access control (like MCP `request_mcp_access`)
6. Credential encryption at rest
7. Audit logging for credential changes

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Store in DB vs config.yaml | DB (`platform_settings`) | Already has auth, file locking, PVC backup. Config.yaml is read-only at runtime. |
| Inject via os.environ | Yes | Zero changes to existing tool code. Tools still call `os.getenv()`. |
| Priority: env > .env > DB | Yes | k8s secrets and .env must still work. DB is the web-UI-only path. |
| Plaintext in DB | Yes (phase 1) | Same as .env today. Encryption is phase 3. |
| Admin-only key management | Yes | Credentials are platform-wide, not per-user. |

## Files Changed

| File | Change |
|------|--------|
| `gateway/services.py` | New — credential storage, catalogue builder |
| `gateway/http_api.py` | New routes: `/api/services`, `/api/services/keys` |
| `gateway/auth/db.py` | Helper: `get_credentials()`, `set_credential()`, `delete_credential()` |
| `gateway/run.py` | Call `inject_credentials()` before agent creation |
| `gateway/html/main_app.html` | Services section in dashboard |
| No tool files changed | Tools continue reading os.environ unchanged |
