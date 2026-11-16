# Refactor: gateway/http_api.py

## Problem

`gateway/http_api.py` is 11,500+ lines and is a true monolith — it contains:

- All aiohttp route handlers (chat, sessions, admin, instances, setup, routing, etc.)
- The full HTML for three distinct SPAs (main app, login page, setup wizard) as inline Python strings
- Embedded CSS, Alpine.js JavaScript, Tailwind, and SVG assets — all as string literals
- App factory, middleware wiring, and startup logic

This makes the file hard to navigate, impossible to test in isolation, and a major merge-conflict surface area during active development.

## Goals

1. Split handlers into focused modules by domain
2. Extract HTML/JS/CSS templates to files (serve statically or via Jinja2)
3. Keep the public API surface identical — no breaking changes to routes or wire format

## Proposed Structure

```
gateway/
  app.py                     # App factory, middleware, route registration
  html/
    main_app.html            # The main Alpine.js SPA (~6,000 lines of HTML/JS/CSS)
    login.html               # Login page (~400 lines)
    setup.html               # Setup wizard (~2,500 lines)
  routes/
    __init__.py
    chat.py                  # /chat, /stream, SSE chat handler
    sessions.py              # /api/sessions/*, /api/web-sessions/*
    admin.py                 # /admin/* handlers (users, machines, audit, settings)
    instances.py             # /instances, /spawn-templates
    setup.py                 # /setup, /api/setup/* (thin wrapper around setup_handlers.py)
    routing.py               # /routing/*, /profiles/*
    auth_routes.py           # /auth/login, /auth/logout, /auth/refresh, /auth/me
    health.py                # /health, /health/ready, /metrics
    static.py                # /static/*, /favicon.ico
    souls.py                 # /souls, /souls/{slug}
    workflows.py             # /workflows, /api/workflows/*
    runs.py                  # /runs, /api/runs/*
  setup_handlers.py          # (already exists — keep as-is)
  executors/                 # (already exists)
  auth/                      # (already exists)
```

## Migration Strategy

### Phase 1 — Extract HTML templates (lowest risk, highest reward)
Move the three inline HTML strings (`_LOGIN_HTML`, `_SETUP_HTML`, `_ADMIN_HTML`) to files under `gateway/html/`. Load them at startup with `Path.read_text()`. No route logic changes.

**Why first**: The HTML blocks account for ~9,000 of the 11,500 lines. This alone makes the Python file manageable.

**Risk**: Low. The only change is load-time file reads instead of string literals. Template tokens (`__VERSION_LABEL__`, `__SETUP_TS__`) stay the same.

### Phase 2 — Split route handlers by domain
Move handler functions and their helpers into `gateway/routes/` modules. Import and register them in `gateway/app.py`.

**Suggested split order** (lowest to highest coupling):
1. `health.py` — zero dependencies
2. `souls.py` — reads registry, no session state
3. `runs.py` — thin wrappers around existing run logic
4. `auth_routes.py` — already has `gateway/auth/` backing it
5. `instances.py` — depends on executor but otherwise self-contained
6. `routing.py` + `workflows.py` — moderate coupling
7. `admin.py` — high coupling (user management, machine management)
8. `sessions.py` + `chat.py` — highest coupling (SSE streaming, session state)

### Phase 3 — App factory cleanup
Move middleware setup, lifespan hooks, and route registration to `gateway/app.py`. `http_api.py` becomes a thin entry point or is removed entirely.

## Other Files That Could Use the Same Treatment

| File | Lines | Issue |
|---|---|---|
| `gateway/setup_handlers.py` | ~2,000 | Growing handler file, could split into sub-handlers |
| `agents/hermes/agent.py` | ~1,500 | Tool dispatch + agent loop in one file |
| `gateway/auth/db.py` | ~1,600 | DB schema + all CRUD in one file, could split into domain files |
| `gateway/executors/kubernetes.py` | ~400 | Manageable, lower priority |

## Key Constraints

- `_ADMIN_HTML`, `_LOGIN_HTML`, `_SETUP_HTML` contain `__VERSION_LABEL__` and `__SETUP_TS__` tokens substituted at request time. The HTML files would need the same substitution on load (or per-request).
- Several handlers share module-level state (`_instance_queue`, `_hermes_home`, `_start_time`, `_IS_CANARY`). These would move to a shared `gateway/state.py` or be passed via `app["..."]` storage.
- The Alpine.js JavaScript in the HTML is ~5,000 lines. It is NOT a separate bundling step — it's rendered inline. Extracting to `.js` files would require a static file serving setup and cache busting. Acceptable trade-off.

## Estimated Effort

| Phase | Effort | Risk |
|---|---|---|
| Phase 1 (extract HTML) | 2–3h | Low |
| Phase 2 (split handlers) | 1–2 days | Medium (test coverage needed) |
| Phase 3 (app factory) | 2–4h | Low |

## Notes

- No change to wire format, route paths, or Alpine.js behavior
- After Phase 1, `http_api.py` drops from ~11,500 to ~2,500 lines
- After Phase 2, each handler module is independently readable and testable
- The existing `gateway/setup_handlers.py` extract is the proof-of-concept that this pattern works
