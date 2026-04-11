# Logos UI Inventory — Pass 1

**Date**: 2026-04-11
**Purpose**: Factual map of the current UI surface area. Reference artifact for pass 2 (architecture sketch) and pass 3 (UI audit).
**Tone**: Descriptive only. No "should" statements, no recommendations, no merge suggestions.
**Captured by**: exploration agent walking `gateway/templates/main_app.html`, `gateway/http_api.py`, `gateway/admin_handlers.py`. Minor tallies should be re-validated in pass 3 if load-bearing.

---

## 1. Top-level navbar

| Label | Target | Visibility | Defining file:line |
|-------|--------|------------|-------------------|
| Agents | `tab='agents'` | Always visible | `gateway/templates/main_app.html:272` |
| Chats | `tab='sessions'` | Always visible | `main_app.html:276` |
| Compare | `tab='compare'` | Always visible | `main_app.html:280` |
| Settings | `tab='infra'` | Auth-gated: `can('manage_machines') \|\| can('manage_profiles') \|\| can('view_routing_debug') \|\| can('view_evolution')` | `main_app.html:285` |
| Admin | `tab='admin'` (red badge if `pendingApprovalCount > 0`) | Auth-gated: `can('manage_users') \|\| can('view_audit_logs') \|\| can('manage_workflows') \|\| can('view_runs')` | `main_app.html:291` |
| Account menu | Dropdown: Change password, user info, role display, version | Authenticated user only | `main_app.html:307` |

---

## 2. Per-tab content

### Agents tab (`main_app.html:2431`)
Left side: 960px square Phaser.js canvas showing agent world visualization with fixed aspect ratio. Right side: collapsible "Create Agent" form panel (agent name input, character sprite picker 0–7, soul selector dropdown, model route selector dropdown). Agent cards list below form. World canvas auto-resizes based on form state (16rem width when form closed, 24rem when open or agent editing).

### Chats tab (`main_app.html:427`)
Top: Agent instance pill bar (one pill per named agent, sorted by active chat). Left sidebar (44rem fixed): "New Topic" button, platform filter pills (Web, Telegram, Discord), scrolling chat history list per platform. Right panel (fills remaining): Chat header with STAMP governance pills (S: Soul selector dropdown, T: Tool count, A: Agent name, M: Model route selector dropdown with ready routes, P: Policy badge). Messages area with ghost logo, chat bubbles (user/assistant/error cards), stats toggle per message, copy buttons. Bottom: chat input field with mic/submit buttons.

### Compare tab (`main_app.html:1106`)
Top: Horizontal agent palette (draggable pills, click to add to pane). Two-pane grid (50/50 split): each pane is a drop target (empty + hint, or filled with agent header + transcript area). Shared input row at bottom (send same prompt to both panes).

### Settings/Infra tab (`main_app.html:1289`)
Sub-tabs: Inference, Routing, Tools (`manage_machines` only), Channels (`manage_machines` only), Proposals (`view_evolution` only).

- **Inference sub-tab** (`main_app.html:1317`): Cloud Providers section (Anthropic/OpenRouter/Custom) with add form, provider list with test/activate/edit/delete buttons. Local Servers section (LM Studio, Ollama, etc.) with scan results, form to register machines, machine list with health probes and endpoint URLs.
- **Routing sub-tab** (`main_app.html:1773`): Model Routes table (model, provider, openshell_name, is_default, status), with reorder buttons. Fallback policy config. Debug info (`view_audit_logs` only) showing routing resolution logs.
- **Tools sub-tab** (`main_app.html:2732, 2869`): Toolsets (MCP server list with toggle enable/disable). Integration services (Anthropic, OpenRouter, Discord, Telegram) with API key management, test/validate buttons.
- **Channels sub-tab** (`main_app.html:2999`): Messaging platforms (Telegram, Discord) with routing rules editor. Platform session list. Routing form modal (`platformRoutingFormOpen`).
- **Proposals sub-tab** (`main_app.html:4954`): Evolution proposals list (if `can('view_evolution')`).

---

## 3. Secondary nav & drill-downs

### Agents tab
- Agent card click → agent detail view (inline on same card, no modal)
- "Manage routes →" link inside agent form → jumps to Admin/Model Routes (`tab='admin'; adminTab='model-routes'`)

### Chats tab
- Soul (S) pill → dropdown selector (1 click to open, agent's soul is switchable)
- Model (M) pill → dropdown selector with "Provision one →" link to Admin/Model Routes (1 click)
- Chat history item click → switch to that chat (1 click)
- Message "stats" toggle → expands stats card inline (1 click per message)
- Error message "Details" toggle → expands error details inline (1 click)
- Error message "Go to Settings" button → jumps to Settings/Inference (1 click)

### Compare tab
- Agent pill drag → drop into pane (1–2 clicks)
- Agent pill click → auto-add to next empty pane (1 click)
- Pane X button → remove agent from pane (1 click)
- Message "Show reasoning" detail → expand thinking inline (1 click)

### Settings/Infra tab
- Cloud Provider form toggle → opens add/edit form inline (1 click)
- Cloud Provider "Test" button → validates connection, shows model count (1 click, async)
- Cloud Provider "Activate" button → makes active (1 click, async)
- Machine "Add" button → opens registration form inline (1 click)
- Machine row → probe health check icon updates async (no explicit click, auto-probes)
- Machines "Reorder" mode → drag rows to reorder (1 click to enter mode)
- Routing debug info → shows routing logs in collapsible section (`view_audit_logs` gated, visible below routing table)
- Platform routing "Add" button → opens `platformRoutingFormOpen` modal (1 click)
- Toolset toggle → enable/disable MCP server (1 click per row, async)
- Services key input → text field with validate/save buttons (1 click)
- Channels "Add routing" button → opens `platformRoutingFormOpen` modal (1 click)

---

## 4. Admin-only pages

All nested under `tab='admin'` with per-sub-tab permission gates:

| Sub-tab | Trigger | Permission | Defining file:line | Content |
|---|---|---|---|---|
| Users | `adminTab='users'` | `manage_users` | `main_app.html:3105` | User list table (email, username, display_name, role). Create/edit user forms, delete and reset-password buttons. |
| Security | `adminTab='action-policies'` | `manage_action_policies` | `main_app.html:3109` | Action policies list table. Create form (name, description, network_policy, filesystem_policy, exec_policy, write_policy, provider_policy, secret_policy). Edit inline. Delete button. |
| Workflows | `adminTab='workflows'` | `view_workflows \|\| manage_workflows` | `main_app.html:3113` | Workflow definitions table. "Build" button (opens `wfBuilderOpen` modal) and "Import JSON" button (opens `wfNewFormOpen` modal). Workflow list with trigger/edit/delete buttons. |
| Runs | `adminTab='runs'` | `view_runs` | `main_app.html:3117` | Agent runs table (job_id, started_at, status, duration). Clone run button. |
| Sandboxes | `adminTab='sandboxes'` | `view_runs` | `main_app.html:3121` | Sandbox list table (name, status, uptime). Restart button. Logs viewer modal. Warns if OpenShell CLI not installed. |
| Model Routes | `adminTab='model-routes'` | `manage_machines` | `main_app.html:3125` | Model Routes table (model, provider, openshell_name, is_default, status, actions). Reorder buttons. Set default. Refresh. Provision modal (`provisionModalOpen`) to add new route. |
| Platforms | `adminTab='platforms'` | `manage_users` | `main_app.html:3129` | Platform list (web, telegram, discord). Routing config section with table of platform→agent mappings. Add routing button (opens `platformRoutingFormOpen` modal). |
| Audit Log | `adminTab='audit'` | `view_audit_logs` | `main_app.html:3133` | Audit log entries table (timestamp, user, action, resource, status). Filters/search. |
| Approvals | `adminTab='approvals'` (with badge count) | `view_approvals` | `main_app.html:3137` | Pending approvals list (workflow approval or user action approval). Approve/Reject buttons. Details expandable. |

---

## 5. Routes that exist but aren't linked from navbar or modals

Routes with handlers but no direct UI entry point (no `fetch()`, `href`, or button triggers in templates):

- `/` (serves `main_app.html`)
- `/login` (login page, pre-auth)
- `/setup` (setup wizard, first-time onboarding)
- `/auth/login`, `/auth/logout`, `/auth/refresh`, `/auth/me` (called by JS during session init)
- `/health`, `/healthz`, `/health/ready` (liveness probes, infrastructure)
- `/status` (JSON status, used internally by JS)
- `/chat_logo.png`, `/favicon.ico` (asset routes)
- `/api/hue` (tray icon color cycling, called by JS)
- `/api/model-catalog` (model listing, called during setup)
- `/canary/status` (canary deployment status, polled by JS)
- `/api/workers` (worker registry, read-only JSON)
- `/ws/worker` (WebSocket for worker connections)
- `/spawn-templates` (legacy instance templates)
- `/routing/preview` (admin route to preview routing resolution, may be unreachable from UI)
- `/admin/routing/resolve` (called by JS fetch, not via button)
- `/admin/routing/log` (called during routing debug view, `view_audit_logs` gated)
- `/update-status`, `/update-trigger` (called by JS, no UI button)
- `/instances`, `/instances/{name}/...` (legacy instance CRUD, may be superseded by named agents)
- `/souls`, `/souls/{slug}` (soul detail view; fetched by JS, not directly navigated)
- `/api/model` (PATCH endpoint to update active model, called by model route switching)
- `/internal/routing/claims`, `/internal/routing/apply` (internal routing, CSRF-protected)
- `/machines/{id}/claim` (claim/unclaim machine, internal ops)
- `/admin/setup` (setup wizard POST, called by form submission)
- `/admin/agents`, `/admin/policies`, `/admin/users/{id}/...` (admin resource endpoints)
- `/evolution/settings` (fetched during proposals tab load, no dedicated settings UI in proposals tab)
- `/evolution/proposals/{id}/answer`, `/evolution/proposals/{id}/consult`, `/evolution/proposals/{id}/apply` (called by proposal buttons)

---

## 6. Raw counts

- **Top-level navbar tabs**: 5 (Agents, Chats, Compare, Settings, Admin)
- **Settings/Infra sub-tabs**: 5 (Inference, Routing, Tools, Channels, Proposals)
- **Admin sub-tabs**: 9 (Users, Security, Workflows, Runs, Sandboxes, Model Routes, Platforms, Audit Log, Approvals)
- **Total secondary nav entries** (collapsible forms, modals, dropdowns): ~35
- **Major modal drill-downs**: 6 (Provision modal, Workflow Builder, Workflow Import JSON, Platform Routing, Sandbox Logs, Setup Reset confirmation)
- **Unlinked routes**: ~30+ (mostly infrastructure, legacy, or internal endpoints)
