# Pass 3 — UI Audit & Punch List

**Date**: 2026-04-11
**Inputs**: [pass1_ui_inventory.md](pass1_ui_inventory.md), [pass1_db_inventory.md](pass1_db_inventory.md), [pass2_architecture.md](pass2_architecture.md), direction answers Q1–Q6 (below)
**Format**: Executable punch list. Grouped by scope. File:line refs where known.
**Companion**: [MISSING.md](../MISSING.md) for the large architectural gaps this audit surfaced.

---

## Direction established (Q1–Q6 answers)

1. **STAMP T and P are read-only because they're not built yet.** This is a gap, not a governance decision. Bubbled into [MISSING.md M1, M2](../MISSING.md).
2. **Duplicate surfaces (`model_routes`, `platform_routing`) are dev accidents.** They emerged while development was moving toward per-user inference routing and got interrupted. Consolidate now; build the real per-user routing feature as M3.
3. **Agents tab: world dominant, CRUD slides out.** Tamagotchi-aligned.
4. **Approvals are a user concern**, not admin-only. Must be accessible in-chat. Tied to the Policy (P) pill — bubbled into M2.
5. **Multi-user polish: HEAVY.** Era-4 residue is first-class audit work. Bubbled into M4.
6. **Pass 3 format: punch list Greg executes.** Each item is scoped to files with priority + risk.

---

## Top-line findings

**A) Multi-user + per-user routing is the biggest shape-change the audit recommends.**
`machine_users` is a fully functional CRUD layer with zero UI (M3). Era-4 single-operator defaults leak into every primary tab (M4). These are the heaviest interventions and should be planned as a coherent chunk, not sprinkled ad-hoc.

**B) Dupe surfaces are dev debt, not product intent.**
Consolidate `model_routes` and `platform_routing` into their Admin versions. Cheap, clears confusion, removes dead template code. Do this first — it clears the decks.

**C) The Chats STAMP pill is the product's governance spine and has two dead axes (T, P).**
Building these out is a big lift (M1, M2) but the roadmap matters more than the audit reshuffle. Pass 3 proposes ground-preparation (approval badges on chat rows) while the big builds are planned separately.

**D) The 5-tab navbar mostly works against the 8-domain model.**
No tab needs to split or merge right now. The shape is sound; the content inside each tab is where the work lives.

---

## Structural changes (S-series)

These touch multiple files, carry real risk, and change user-facing shape. Approach carefully. Ordered by recommended sequence.

### S1 — Consolidate dupe surfaces (model_routes + platform_routing)
**Change**:
- Delete Settings → Routing sub-tab entirely. Its routes table duplicates Admin → Model Routes.
- Delete Settings → Channels sub-tab entirely. Its platform-routing rules duplicate Admin → Platforms.
- Move the Routing debug log view (currently gated on `view_audit_logs` in Settings → Routing) to sit under Admin → Model Routes with the same permission gate.
- Re-target any intra-app links that currently jump to Settings → Routing / Settings → Channels.

**Files affected**:
- `gateway/templates/main_app.html:1773` — Settings → Routing block (delete)
- `gateway/templates/main_app.html:2999` — Settings → Channels block (delete)
- `gateway/templates/main_app.html:1289` — Settings sub-tab header row (remove Routing + Channels entries)
- `gateway/templates/main_app.html:285` — top-level Settings gate (`view_routing_debug` permission check — migrate to Admin → Model Routes or drop)
- Admin → Model Routes block at `main_app.html:3125` — append debug log view
- Grep for `tab='infra'` jumps that target `subtab='routing'` or `subtab='channels'` and re-point to the Admin equivalents

**Scope**: Medium. ~200 lines of template deletion, permission-flag audit, a few re-links.
**Risk**: Low. No backend changes. Pure UI consolidation.
**Priority**: **Do first.** Cheapest structural win and it clears the mental overhead for S2–S4.

### S2 — Agents tab: slide out CRUD, let world breathe
**Change**:
- Remove the Create-Agent form from the right column of the Agents tab.
- Promote the form to a slide-out drawer (side sheet, right-anchored) triggered by a prominent "+" button on the Agents tab header.
- Agent detail view (currently inline on card click) also moves into the slide-out.
- World canvas now occupies the full Agents tab width and height — no resize logic tracking form state.

**Files affected**:
- `gateway/templates/main_app.html:2431` — Agents tab main block (layout rewrite)
- Phaser canvas resize logic (currently switches between 16rem / 24rem based on `form_open` state) — simplify to full-width
- New slide-out drawer component (look at existing modals like `provisionModalOpen`, `wfBuilderOpen` for scaffold pattern)
- Agent card click handler — swap from "reveal inline panel" to "open slide-out"

**Scope**: Medium. Layout rewrite + drawer scaffold + handler swap.
**Risk**: Low. The Phaser canvas already handles dynamic resize, and slide-out/modal scaffolding is already present elsewhere in the codebase.
**Priority**: **High.** Biggest tamagotchi-alignment for the smallest effort. Can land in parallel with S1 since they touch different blocks.
**Next step beyond this**: [MISSING.md M5](../MISSING.md#m5--world-view-as-a-first-class-persistent-surface) — world view as first-class surface.

### S3 — Per-user inference routing MVP (executes MISSING.md M3)
**Change**: Minimum viable version of the M3 feature. Not the full build — just enough surface to start using `machine_users` from the UI.
- New Settings sub-tab: **My Inference** (positioned between Inference and Tools)
- Lists machines claimed by the current user, reading from `machine_users` via a new endpoint (`/api/my-machines`, or extend `/api/machines?scope=me`)
- "Claim" button on each global machine in Settings → Inference (admins still see everything; regular users see only what's claimable)
- Routing resolver in `openshell_routes.py` / `run.py` honors `machine_users` when resolving a chat: prefer user's claimed machines first, fall back to shared pool
- Admin view of per-user claims as a new column (or sub-view) in Admin → Platforms or Admin → Users

**Files affected**:
- `gateway/templates/main_app.html` — new sub-tab block, reorder existing sub-tabs, Inference sub-tab gets a "Claim" button column
- `gateway/http_api.py` — new `/api/my-machines` route or filter existing
- `gateway/admin_handlers.py` — `handle_machine_claim` / `handle_machine_unclaim` handlers (data-layer exists: `gateway/auth/db.py:1244` for `claim_machine`, `:1264` for `unclaim_machine`)
- `gateway/openshell_routes.py` — routing resolution reads `machine_users` during dispatch and applies the fallback order
- `gateway/run.py` — cascade into any inference path that bypasses `openshell_routes`

**Scope**: Large. First feature that changes the inference dispatch path.
**Risk**: **Medium-to-high.** Touches the routing-resolver — the same code path responsible for the `#19` stale-credential bug in TASKS.md. Stage rollout: land the UI + claims CRUD first (resolver still falls through to shared pool), then change the resolver in a second pass once claim data is being populated.
**Priority**: High (Q5 said HEAVY multi-user), but **S1 and S2 must land first**. Don't mix structural cleanup with new feature code in one branch.
**Fallback order to decide before writing resolver code**:
1. My claimed machines (highest `machine_users.priority` first)
2. Unclaimed shared machines (ordered by existing `machines.sort_order`)
3. Claimed-by-others shared machines (if `agents.shared=True` and policy permits)
4. Error — no route

### S4 — Multi-user UX polish cluster (executes MISSING.md M4)
**Change**: Cluster of related fixes. Best executed as a single coherent pass — not piecemeal — because the changes are small per-file but the surface area is large.

- **Remove `/chats` first-agent auto-select.** Find via grep for the chat-mount handler that sets `currentAgent` on page load (likely in `main_app.html` around the `x-init` on the Chats tab). Replace with "show empty state / agent picker" if no agent chosen.
- **Owner badge on agent cards.** Agents tab card → small "by username" text or avatar corner. Data: `agents.creator_id` → `users.display_name`.
- **Owner distinction in Chats agent pill bar.** Subtle visual difference (border color, icon) between "mine" and "shared".
- **Per-agent shared/private toggle.** Create Agent slide-out (from S2) → bind to `agents.shared`.
- **Per-user chat history filter.** New filter pill in Chats sidebar next to existing platform filters (Web / Telegram / Discord).
- **"Created by" column in Admin → Runs** (both workflow_runs and agent_runs detail views).
- **Settings → My Account sub-tab** — writes to `user_settings`. Fields: default soul, default model, UI theme, notification_telegram toggle. `user_settings` table already exists at `gateway/auth/db.py` with the columns ready.

**Files affected**: `main_app.html` throughout; minor changes in `http_api.py` (new `/api/my-settings` PATCH route) and `admin_handlers.py` (owner column data).

**Scope**: Large as a cluster, small per-change.
**Risk**: Low per change, but as a cluster it's broad surface. No backend reshapes, no data migrations.
**Priority**: High. Land **after S1 + S2** for cleaner diffs, and **after S3 MVP** so per-user routing is in place before per-user everything else.

---

## Cheap wins (C-series)

Single-file, low-risk, high-signal. Can land any time.

### C1 — Approval badge on chat row when chat has pending approval
**Change**: Show a red dot on any chat in the Chats sidebar whose agent has an unresolved approval request for that session.
**Files**: `gateway/templates/main_app.html` Chats sidebar block; poll against existing `pendingApprovalCount` feed (extend to return per-session counts).
**Scope**: ~30 lines.
**Priority**: Medium. Lays groundwork for M2's full in-chat approvals experience.

### C2 — Rename internal tab state `tab='infra'` → `tab='settings'`
**Change**: After S1 removes Routing and Channels from the Settings sub-tab list, the remaining content (Inference, Tools, Proposals) isn't "infra"-shaped. Rename the internal Alpine state variable. Label on the navbar stays "Settings".
**Files**: `main_app.html` — search/replace `tab='infra'` → `tab='settings'`. Cosmetic.
**Scope**: Trivial.
**Priority**: Low. Do it when touching that block for S1.

### C3 — Ownership distinction in Chats agent pill bar
**Change**: Subtle visual distinction between agents the current user created vs. shared-with-me agents. Border color, small icon, whatever matches the design vocabulary.
**Files**: `main_app.html` Chats agent pill bar block.
**Scope**: ~20 lines.
**Priority**: Medium. Part of S4 but cheap enough to land independently if S4 slips.

### C4 — Audit and delete dead routes
**Change**: Verify these still have JS callers, delete the ones that don't:
- `/api/hue` (pass-1 inventory flagged as "color cycling for tray icon" — is the tray icon still a thing?)
- `/spawn-templates` (era-3 legacy)
- `/instances`, `/instances/{name}/...` (era-3 legacy, superseded by `agents`)
- `/souls/{slug}` (fetched by JS — verify soul picker still uses it)

**Method**: For each, grep `main_app.html` and `gateway/static/` for the URL literal. Zero callers → delete. Live callers → leave.
**Files**: `gateway/http_api.py` handler definitions + grep results in templates.
**Scope**: Small investigation + small edit per route.
**Priority**: Low. Dev-debt cleanup.

---

## Cleanup (X-series — archaeology)

Dev debt from prior architectural eras. Delete or document explicitly as legacy.

### X1 — Delete `routing_policies` table and code paths
**Change**: The v1 routing-policies concept was superseded by `action_policies` in schema v2 but the table still exists with writer code paths and a dead FK (`users.policy_id`). Full cleanup:
- Drop `routing_policies` CREATE TABLE at `gateway/auth/db.py:110-126`
- Drop `policy_rules` CREATE TABLE (depends on `routing_policies`)
- Delete `db.create_policy()`, `db.update_policy()`, `db.delete_policy()`, `db.get_policy()`, `db.list_policies()`, `db.set_policy_rules()`, `db.get_policy_rules()`, `db.resolve_policy_machines()`
- Add v10 migration to drop `users.policy_id` column
- Delete `assign_user_policy` call sites
- Grep the codebase for `routing_policies` or `policy_id` (careful — `action_policy_id` is different!) to verify nothing still reads it

**Risk**: Medium. Data migration + must be sure nothing in the routing resolver reads `policy_rules`. S3's new routing logic may supersede what `policy_rules` was trying to do.
**Priority**: Low. Pure cleanup. Wait until S3 has stabilized so the new routing behavior is settled before removing the old plumbing.

### X2 — `agent_runs.agent_id` soft-ref: document or convert
**Change**: Today `agent_runs.agent_id` is a TEXT soft-ref to `agents.id`, intentionally not a FK so runs persist after agent deletion. Two options:
- **(a)** Leave as-is, add an explanatory comment at `gateway/auth/db.py:459` (the migration that added it)
- **(b)** Convert to a nullable FK with ON DELETE SET NULL — same persistence semantics, enforces integrity where the agent still exists

**Recommendation**: (b), but only after S3/S4 land in case they add other soft-refs needing the same treatment.
**Priority**: Very low.

---

## Questions pass 3 answers from evidence (no Greg input needed)

### Does the 5-tab navbar cleanly map to the 8 domains + STAMP?
Mostly yes, with one forced fit:
- **Agents tab** mixes "entity CRUD" with "live world visualization" — **S2 addresses this**.
- **Settings vs. Admin** had blurry boundaries pre-S1. Post-S1 the split is cleaner: Settings = user-facing preferences + inference setup; Admin = platform-wide governance + audit.
- **Tools, Proposals, Evolution** sit under Settings but aren't really "settings" in the preferences sense — they're subsystems. Post-S1 they dominate Settings, which argues for the `tab='infra'` → `tab='settings'` rename (C2) since the preferences framing is what's left.

No tab needs to split or merge.

### Admin → Runs folds `workflow_runs` and `agent_runs`. Useful lens or category error?
Useful lens ("everything that ran") **but the UI must visibly distinguish the two kinds**. Pass 1 didn't capture whether a row-type badge exists. Before doing anything: spot-check the Runs table at `main_app.html:3117` and see if rows indicate their type. If no, add a small type column. If yes, leave it.

### Vestigial routes — delete, hide, or document?
C4 covers this as cheap wins. Delete the ones with zero callers.

### Dupe surfaces — deliberate mirror or accident?
Accident (Q2 confirmed). S1 executes the consolidation.

---

## "Do first" ordering (your pick for this week)

1. **S1** — consolidate dupe surfaces. Cheapest structural win. Clears the nav's self-confusion before any other changes land.
2. **S2** — slide out Agents CRUD, world breathes. Parallel with S1, independent files.
3. **C1** — approval badges on chat rows. Cheap signal that M2 is coming; unblocks user-visibility-of-approvals immediately.
4. **S3 scoping** (not yet coding) — decide the routing-resolver fallback order (see S3 item). Sketch the `/api/my-machines` contract. Write a short design note.
5. **S3 implementation** — stage rollout: UI + claims CRUD first, resolver change second.
6. **S4** — multi-user polish cluster, after S1+S2+S3 have cleared the ground.
7. **C2, C3, C4** — cheap wins, land whenever you're touching adjacent code.
8. **X2** — formalize soft-ref, after M3/M4 land.
9. **X1** — `routing_policies` deletion. Hold until S3 has stabilized.

---

## What pass 3 does NOT cover

- **Rendering / performance audit**: Not in scope. Pass 3 is purely structural. Issues like "agent pill bar re-renders on every poll" aren't considered here. Ask for a pass 4 if you want this.
- **Accessibility**: Not examined. ARIA, focus management, keyboard nav all out of scope.
- **Mobile / responsive**: The 960px Phaser canvas alone rules this out as mobile-first. Explicitly not audited.
- **Auth flow / first-run UX**: `/login` and `/setup` were inventoried but not audited. If first-run polish matters, it's another pass.
- **Content of individual modals**: Workflow Builder, Provision modal, etc. — inventoried but their internal UX wasn't examined.
- **The MISSING.md items themselves**: Pass 3 only proposes the ground-preparation work. M1–M5 each need their own design pass before execution.
