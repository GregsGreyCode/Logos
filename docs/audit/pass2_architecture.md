# Logos Architecture Sketch — Pass 2

**Date**: 2026-04-11
**Purpose**: Concept map that pass 3 (UI audit) will measure against. Opinionated synthesis grounded in the two pass-1 inventories. Not exhaustive, not prescriptive — just enough structure to answer "what IS Logos today" so we can judge whether the UI reflects that.
**Inputs**: [pass1_ui_inventory.md](pass1_ui_inventory.md), [pass1_db_inventory.md](pass1_db_inventory.md)
**Output used by**: pass 3 (UI audit)

---

## Core entities

**User** — Authenticated human. Has a role (admin/operator/user/viewer), a permission set, and optionally an `action_policy` that constrains what their agents may do. Multi-user is real, not a toy hack — `n_parallel=4` on the inference side is a concurrency feature, and `machine_users` exists as a claims table even if it has no UI yet.

**Named agent** — Persistent entity with a unique name, a soul, a sprite index, and a model-route binding. *Not* a disposable session. Shared between users by default (`agents.shared=True`). Agents survive gateway restarts, model changes, and sandbox recreation. They are the product — conceptually, "Hermes" is more durable than any particular runtime process.

**Sandbox** — OpenShell runtime instance that an agent lives inside. Bound by name (`hermes-{agent_name}`). Sandboxes die and respawn; the agent identity survives. An agent without a sandbox is "not running"; an agent with a fresh sandbox is still the same agent.

**Model route** — A `(provider, model)` pair that resolves to a dedicated OpenShell sub-gateway, itself pinned to exactly that model. Each route owns its own `openshell_name` and port. Agents bind to routes; routes proxy to inference backends. This layer is what makes multi-model multi-agent concurrency possible.

**Session / chat** — A transcript bound to a `(named agent, platform, chat_id)` triple. Not an entity — just the conversation history. Lives in messages/history tables on the agent side, not in `auth.db`.

**Soul** — A personality / system-prompt definition. Exists as a **file on disk** (not a table row), referenced by `agents.soul_slug` and `user_settings.default_soul`. The UI has soul pickers in two places (Agents tab create form, Chats STAMP pill) but no admin surface for managing souls themselves.

---

## Domains

Eight domains emerge from the data model. Each owns a set of tables and corresponds to a coherent concept in the system.

| # | Domain | Tables | What it does |
|---|---|---|---|
| 1 | **Identity & access** | `users`, `refresh_tokens`, `user_settings` | Who can log in, what role they have, what their defaults are |
| 2 | **Governance** | `action_policies`, `approval_requests`, `audit_logs` | What agents are allowed to do, what gets gated, what happened |
| 3 | **Agents as entities** | `agents`, `platform_routing` | The named-entity roster and where each one listens |
| 4 | **Inference infrastructure** | `machines`, `cloud_providers`, `machine_capabilities`, `machine_users`, `model_routes`, `routing_log`, ~~`routing_policies`~~, ~~`policy_rules`~~ | Where inference happens and how requests route to it |
| 5 | **Workflows & runs** | `workflow_definitions`, `workflow_runs`, `workflow_step_runs`, `agent_runs` | Execution state — both scripted (workflows) and ad-hoc (agent runs) |
| 6 | **Tooling** | `mcp_servers` | MCP integrations and service credentials |
| 7 | **Evolution** | `evolution_proposals`, `evolution_settings` | Self-modification loop (agents propose their own code changes) |
| 8 | **Platform config** | `platform_settings` | Global feature flags, allowed souls, registration policy |

Strikethrough tables are legacy / vestigial but still live in code (see "Architectural eras" below).

### Domain coupling

Domains are not islands. The important cross-cuts:

- **Governance touches almost everything.** `action_policies` binds into `users`, `agent_runs`, `approval_requests`, and indirectly into `workflow_step_runs` via the approval chain. `audit_logs` is written from nearly every admin mutation.
- **Identity & access feeds agents, workflows, and proposals.** `users` is the FK root for `creator_id`, `created_by`, `decided_by` across three domains.
- **Inference infrastructure is the spine.** `machines` → `machine_capabilities` → `model_routes` → `agents` is the resolution path from a chat request down to a live inference call.
- **Runs cross workflows and agents.** `agent_runs` has an optional FK into `workflow_runs` — an agent run can be standalone or a step inside a workflow. This is why Admin → Runs exists at all.

---

## The five axes of a chat: STAMP

The Chats tab surfaces a five-pill governance summary — **S**oul, **T**ools, **A**gent, **M**odel, **P**olicy. This isn't just UI decoration; it's an architecture statement. Every chat is bound to exactly one value on each of five orthogonal axes:

| Pill | Axis | Data-model anchor |
|---|---|---|
| **S** | Personality / system prompt | `agents.soul_slug` → soul file on disk |
| **T** | Available tools | `mcp_servers` + `agents.toolsets` |
| **A** | Identity | `agents` row — the entity itself |
| **M** | Inference backend | `model_routes` → sub-gateway → `machines`/`cloud_providers` |
| **P** | Governance | `users.action_policy_id` → `action_policies` |

STAMP is the load-bearing mental model for how an agent is *configured* at the moment of a chat. Any new surface area should map cleanly onto one of these five, or it's a sixth axis that needs explicit promotion. Pass 3 should use STAMP as the rubric when judging whether a UI element has a home or is homeless.

---

## How a chat request flows

```
user types in Chats tab
    │
    ▼
gateway resolves (platform, chat_id, user) → named agent
    │  (via platform_routing table)
    ▼
looks up agent.model_route_id → model_routes row → openshell_name
    │
    ▼
dispatches task over WS to worker in sandbox hermes-{agent_name}
    │
    ▼
sandbox worker calls inference.local → OpenShell router
    │
    ▼
router proxies to sub-gateway for that model_route
    │
    ▼
sub-gateway proxies to the machine (LM Studio / cloud provider)
    │
    ▼
streaming response bubbles back up:
    provider → sub-gateway → router → worker → gateway → SSE → browser
    │
    ▼
messages persist under (agent, platform, chat_id)
```

Every arrow is a place something can go wrong, and the TASKS.md fix history is essentially a map of which arrows have been hardened. `#19` (stale sub-gateway credentials), `#21` (empty reasoning replies), `#22` (60s router timeout), `#23` (persistence of the fix), all live on the fourth and fifth arrows down.

---

## Architectural eras (scar tissue)

Four rewrites have left visible fingerprints in the data model and UI. Pass 3 will need to decide what to do about each.

### Era 1 → 2: `routing_policies` → `action_policies`
- **Old**: `routing_policies` + `policy_rules` described *how to route* inference requests. Users had a `policy_id`.
- **New**: `action_policies` describe *what an agent is allowed to do* (network/fs/exec/write/provider/secret). Users got a second FK: `action_policy_id`.
- **Scar**: Both tables still live. `routing_policies` has no UI but still has writer code paths (`db.create_policy`, `db.set_policy_rules`). `users` has both FKs. The v1 concept of "policy as routing" was replaced but never removed.

### Era 2 → 3: Single primordial OpenShell → per-model sub-gateway
- **Old**: One OpenShell "primordial" gateway, all models funnelled through it, model switch meant reloading LM Studio.
- **New**: Each `model_routes` row owns its own OpenShell sub-gateway on its own port. Concurrent multi-model inference is real. The primordial is now *one of* the gateways, flagged `is_primordial=True`.
- **Scar**: `is_primordial` singleton is enforced by app code, not schema. `BOOTSTRAP_PRIMORDIAL_NAME = "logos-openshell"` still exists as a discovery candidate on first `/setup`, aliased away after adoption (this is what the `#18` gateway-naming cleanup fixed).

### Era 3 → 4: Disposable sandboxes → named agents
- **Old**: Sandboxes were ephemeral. Users spun up "instances", named however, disposable by design.
- **New**: `agents` table, name is `UNIQUE`, sandbox is bound by name, agent survives sandbox recreation. Agents are tamagotchi-grade entities, not sessions.
- **Scar**: `agent_runs.agent_id` is still a soft-ref `TEXT` column, not a FK — intentional for post-delete audit persistence, but means orphans are a normal state. The `/instances` and `/spawn-templates` routes still exist in `http_api.py` but aren't linked from the navbar (vestigial endpoints from era 3).

### Era 4 → 5: Single-user → multi-user
- **Old**: One user, one machine claim. UX assumed a single operator.
- **New**: `users` is a real table with roles and permissions. `machine_users` claims table exists for M:M assignment. `n_parallel=4` is deliberate concurrency.
- **Scar**: `machine_users` has full CRUD methods but **no UI** — claims are populated only by backend setup flows. The single-user mental model is still baked into some UX defaults (e.g., `/chats` auto-selects the first agent on land).

---

## Cross-cutting concerns

Things that don't live in a single domain but thread through several. Pass 3 should check that each of these has a coherent UI story.

- **Approvals** — `approval_requests` binds `action_policies` → `workflow_step_runs` → `agent_runs`. When an agent tries to do something gated, an approval row is written, a pending badge shows up in Admin → Approvals, and the workflow step blocks until resolved. Notice that this touches domains 2, 5, 6, and the badge is on the top-level Admin tab.
- **Routing debug** — `routing_log` is an audit trail for which machine handled which request. It's consumed in Settings → Routing but conceptually belongs to Governance.
- **Audit** — `audit_logs` is the general-purpose admin mutation log. Nearly every domain writes into it; the UI reads it only from Admin → Audit Log.
- **Setup wizard** — the only writer to `platform_settings.feature_flags`. Not part of any domain per se, more of a bootstrap ritual. No post-setup surface to modify what it wrote.

---

## Factually absent from the UI

Things that exist in the data model but have no current nav entry point:

1. No post-setup surface for `platform_settings` feature flags.
2. No direct UI for `machine_users` M:M claims (backend-populated only).
3. No UI for `routing_policies` at all — the v1 routing concept is kept alive in code but hidden.
4. No admin surface for managing souls themselves (only picking them). Souls are files on disk.
5. `mcp_servers` is categorized under Settings → Tools in the UI inventory, but the DB inventory flagged it as ambiguous ("indirect" and "none" both). Needs a direct spot-check.
6. `evolution_settings` is displayed in Settings → Proposals but not editable from that nav location.

---

## Open questions (handoff to pass 3)

These are the questions pass 3 — the actual UI audit — needs to answer, grounded in the above.

1. **Does the 5-tab navbar cleanly map to the 8 domains + STAMP mental model, or are there forced fits?**
   Candidates for forced fits: Agents tab (mixing entity management and world visualization); Settings vs. Admin split (several admin sub-tabs look like Settings concerns and vice versa).

2. **Duplicate surfaces.** `model_routes` appears in both Settings → Routing and Admin → Model Routes. `platform_routing` appears in both Settings → Channels and Admin → Platforms. Is this by-design mirroring (two audiences) or accidental duplication (one superseded the other and the original never got deleted)?

3. **Mixed lenses.** Admin → Runs folds two different entity types (`workflow_runs` and `agent_runs`) into one tab. Is that a useful lens ("everything that ran") or a category error?

4. **Vestigial tables and routes.** `routing_policies`, `/instances`, `/spawn-templates`, `policy_rules` — delete, hide, or document as archaeology? The answer likely depends on whether anything still writes to them from a code path pass 1 missed.

5. **STAMP completeness.** The Chats pill surfaces S/T/A/M/P, but three of those (T, P) aren't actually drill-downs from the pill — you have to leave Chats to edit them. Should all five be adjustable from the pill, or is the current read-only-for-T-and-P a deliberate governance choice?

6. **Single-user residue.** `/chats` auto-selecting the first agent, absence of per-user chat filtering, the shape of the agent pill bar — are these intentional affordances or era-4 residue that leaks the old mental model?

7. **Governance visibility.** Approvals are badged on the Admin tab. Should they be visible from anywhere else (e.g., the Chats STAMP P pill, or the agent card on the Agents tab)?

8. **The Agents-tab world view.** The 960px Phaser canvas and the agent-as-tamagotchi vision is load-bearing to the product identity, but it shares the tab with entity-CRUD. Is the split `[world] | [form]` the right shape, or should "manage agents" and "watch agents live" be separate concerns?

---

## Pass 3 ground rules (reminder)

Pass 3 is the actual UI audit. It is allowed to say "should". It should answer the open questions above using the rubric of: the 8 domains, the STAMP axes, the architectural eras, and "how a chat flows". It should propose concrete consolidations, not abstract observations. And it should distinguish between cheap wins (rename a tab) and structural changes (re-split the navbar), because those have very different costs.
