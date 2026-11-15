# Pass 4 — Deferred Audit Scope

**Date**: 2026-04-11
**Status**: Scope placeholder — not yet executed.
**Purpose**: Capture the audit dimensions pass 3 explicitly deferred, so they don't get lost. When pass 4 actually runs, this doc frames what each sub-audit covers.

---

## Items deferred from pass 3

### P4.1 — Rendering / performance audit
**Scope**: Render churn (over-polling, over-rendering on state change), DOM size, Alpine reactivity hotspots, asset loading, Phaser scene efficiency.
**Known concerns carried over from pass 1/3**:
- Chats agent pill bar re-renders on every poll (suspected, not profiled)
- `/admin/sandboxes` polls every 3s without caching — blank flash on tab click ([TASKS.md #17](../../TASKS.md))
- Phaser canvas resize on every form open/close (removed by S2 — one source of churn already gone by pass 4 time)
**Depth**: Medium. Single-session profiling + targeted fixes for top 3 hotspots.

### P4.2 — Accessibility audit
**Scope**: ARIA labels, keyboard navigation, focus management, screen reader flow, color contrast, visible focus indicators.
**Known concerns**: None explicitly flagged. The app uses a lot of custom Alpine components that likely lack ARIA coverage, but this is unconfirmed.
**Depth**: Medium. Full walk-through with an a11y checker + targeted fixes.

### P4.3 — Mobile / responsive audit
**Scope**: Breakpoints, touch targets, mobile Chats flow, mobile world view viability.
**Known concerns**: 960px fixed Phaser canvas rules out mobile-first by design. First question is a product decision: is mobile support wanted at all, or is Logos explicitly desktop-only?
**Depth**: Gated on the product decision. If yes, this becomes a large redesign pass. If no, it's a one-sentence doc update saying so.

### P4.4 — First-run auth flow / setup UX
**Scope**: `/login`, `/setup`, onboarding ergonomics, error recovery, the setup wizard's multi-step flow.
**Known concerns**:
- `/setup` has unbuilt IANA tz dropdown (punted, low priority — `TASKS.md`)
- `platform_settings` feature flags are only writable by the setup wizard — no post-setup editor (MISSING.md territory, not first-run territory)
- Error paths and retry flows through the wizard are unaudited
**Depth**: Medium.

### P4.5 — Individual modal UX audit
**Scope**: Workflow Builder, Provision modal, Platform Routing modal, Sandbox Logs modal, Setup Reset confirmation, plus whatever slide-out S2 adds.
**Known concerns**: None from inventory — just "we didn't look inside them". Pass 3 focused on the navbar and main content areas, not modal internals.
**Depth**: Small per modal; Medium total.

---

## Dependency check

**Does any P4 item need to happen before S1–S4?** No.

| Item | Dependency on S-series | Best timing |
|---|---|---|
| P4.1 perf | None (but easier after S1+S2 remove churn) | After S2 |
| P4.2 a11y | None | Any time |
| P4.3 mobile | Needs product decision first | Independent |
| P4.4 first-run | None | Any time |
| P4.5 modals | Should include S2's new slide-out | After S2 |

**Recommended timing**: After S1 + S2 land, before S3 implementation. That clears the easy wins first and lets perf profiling get a clean baseline on a nav that's no longer confusing itself.

---

## Out of scope for pass 4 (could become pass 5+)

- Content / copy review (labels, error messages, tooltips)
- Design system consistency (typography, spacing, color tokens)
- Illustration / branding refresh
- Documentation / help content
- i18n / localization
