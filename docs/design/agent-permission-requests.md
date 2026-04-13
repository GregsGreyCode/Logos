# Agent-requested permission grants

**Status**: Draft (design, not implementation)
**Depends on**: capability system (`gateway/capabilities.py`, `gateway/policies/capabilities.yaml`)
**Related**: existing approval pipeline (`approvals` DB table + Activity → Approvals UI)

## Problem

Today, capability changes are made by the human toggling the P dropdown.
When an agent needs a capability it doesn't have, the failure mode is a
silent tool error — model calls `web_search`, sandbox returns "tool not
available", agent narrates "I don't have web access" and stops. User then
has to realise what's missing, navigate to the right UI, toggle, retry.

A better flow: the agent **asks for the permission** mid-task. User sees
the request in Activity → Approvals, approves/denies with context, the
grant is applied atomically, the agent retries automatically.

## Why this fits the existing architecture

Three things are already in place:

1. **`approvals` DB table** + Activity → Approvals UI — queue, display,
   one-click approve/deny.
2. **Capability system** — `capabilities.yaml` is the whitelist; each
   capability is a well-bounded scope (toolsets + presets). No room for
   the agent to smuggle a bespoke network rule.
3. **`capabilities.apply(agent_id, cap_id, enabled)`** — the atomic
   backend that a granted approval would call.

Net new work: ~100 lines. A single new tool the agent can call, an
entry in the approvals UI, and one line of glue to apply on approve.

## Threat model

The fundamental risk is **prompt injection laundered into permission
requests**. If the agent reads a malicious web page / Slack message / git
commit with hidden instructions, those instructions can reach the model
as apparent user intent. Without guardrails, the attacker gets the model
to request privileges the user would never grant deliberately, with a
plausible-sounding reason the user is likely to approve.

Secondary risks:

- **Approval fatigue** — users desensitize to prompts they see often.
- **Scope creep** — small requests each look reasonable; aggregate
  grants become over-permissioned agent.
- **Hallucinated need** — model mis-reasons about the task and requests
  access it doesn't actually need.
- **Social engineering by the model** — plausible justifications the
  user can't easily verify.

## Non-goals

- **Credential entry**: agent cannot request API keys. Credentials are
  "provide a secret value", not "toggle a capability" — different risk
  surface (the user could be tricked into pasting a key). Credentials
  stay manual-only in Config → Tools.
- **Arbitrary hosts/ports**: agent can only request capabilities from
  the published YAML. No `"grant me 1.2.3.4:8080"`.
- **Power tools**: `terminal`, `pypi`, anything in `power_tools` cannot
  be requested by agents. Mis-approval there is catastrophic, so the
  approval path stays closed and manual.

## Required guardrails (all non-negotiable)

1. **Whitelist source = capabilities.yaml only**.
   Agent-callable tool validates `capability_id` against
   `capabilities.capabilities` (the mid-tier list), rejects anything
   else — `always_on` is trivially useless (already granted), `power_tools`
   is out of scope.

2. **Time-box by default**.
   Granted capabilities auto-expire after `grant_ttl_min` (default: 60
   minutes, configurable). Approver can tick "keep on" to make
   permanent. Scheduler task periodically looks for expired grants and
   removes them (same mechanism as session expiry).

3. **Per-agent rate limit**.
   Max N permission requests per agent per hour (default: 3). Stops a
   compromised agent from scope-creeping rapidly, and surfaces
   runaway-request bugs in new agents / new souls.

4. **Provenance display**.
   The approval card shows *where the request came from*:
   - the originating chat turn (last user message before the request)
   - the last external content the agent read before requesting (if any
     — web page URL, Slack message, git commit ref)
   - flag clearly if provenance includes any non-user content

   Perfect provenance is hard (the model is a function of all prior
   context), but "here's what the agent saw right before asking" is a
   useful heuristic and catches the obvious case of untrusted content
   → immediate request.

5. **Trust-tier consent friction**.
   - `trust: local` → one-click approve (low-stakes, data stays local)
   - `trust: third_party` → require typing the capability name
     (match the factory-reset modal pattern; prevents muscle-memory
     approves)

6. **Audit log prominence**.
   Every capability request + grant gets an audit log entry at warn
   level. Admin → Audit Log gets a filter chip for "permission grants"
   so spikes are visible.

## Tool surface (agent side)

One new tool, registered in hermes's tool registry:

```python
@tool
def request_permission(capability: str, reason: str) -> str:
    """Request a capability grant from the user. Returns "pending",
    "approved", or "denied" once the user responds.

    capability: one of the published capability ids
                (see Permissions panel). Whitelisted — requests for
                non-published capabilities error immediately.
    reason:     1-2 sentences on why you need this for the current
                task. Shown to the user verbatim.
    """
```

Blocking call semantics: the tool returns when the approval row is
resolved, not when it's queued. If the user ignores the request for
> `request_timeout_min` (default: 15), tool returns `"timeout"` and
the agent proceeds without the grant.

## Approvals UI surface

New approval row shape:

```
Agent "Atlas" requested: 🌐 Use the web
Reason: "To look up the next UK eclipse date"
Impact: +1 capability (web)
          toolsets: [browser, web]
          presets: (none — local only)
Provenance:
  triggered by: user turn
  last external content: (none)
  (safe: request appears to come from direct user instruction)

[Approve — one click]   [Approve and keep permanent]   [Deny]
```

For `third_party` trust:
```
Agent "Atlas" requested: 💬 Send messages on Slack
Reason: "To post the daily stand-up summary"
Impact: +1 capability (slack)
          data will be sent to Slack's servers
Provenance:
  triggered by: user turn
  last external content: wikipedia.org/wiki/Stand-up_meeting
  ⚠ non-user content recently consumed — verify the request
    reflects your intent, not instructions smuggled via the page

Type "slack" to approve:  [_____]    [Deny]
```

## Revocation

"My grants" sub-page (Activity → My grants? or part of agent detail):
a list of active grants per agent with expiry time, origin request,
approver, and a revoke button. Revoke = `capabilities.apply(agent_id,
cap_id, enabled=False)` + audit log entry.

## Implementation sketch

Backend:

1. `gateway/capabilities.py` → add `request_grant(agent_id, cap_id,
   reason, trigger_context)` that:
   - validates cap_id against published capabilities (no always_on, no
     power_tools)
   - enforces rate limit
   - writes an approvals row with `action="capability_grant"`, metadata
     including cap_id, reason, trigger_context, optional ttl
   - blocks waiting for resolution (same pattern as existing approval
     tools)

2. `gateway/admin_handlers.py` → approval resolution handler dispatches
   on `action`; for `capability_grant` calls
   `capabilities.apply(agent_id, cap_id, enabled=approved)` then
   refreshes instance-config.

3. Scheduled sweep (existing cron infra): prune expired grants,
   emitting audit entries.

Hermes-side:

4. New tool `request_permission` registered in hermes's tool registry,
   gated by a `permission_request` toolset that's **always_on** (so
   agents can always ask — asking is the whole point). Tool body hits
   an HTTP endpoint on the gateway that wraps `request_grant` and
   blocks on the response.

5. Soul-level defaults: a well-tuned soul can include prompt guidance
   like "if you need a capability you don't have, say
   `request_permission(...)` rather than telling the user to toggle
   it themselves". Encourages the better UX flow.

## What I'd build first (MVP)

1. Tool + approval row + resolution dispatch, covering `web`,
   `slack`, `discord`, `telegram`, `github` only. Excludes cloud AI
   (user configures those intentionally) and media gen (too niche).
2. Time-box + rate limit from day one (non-negotiable guardrails).
3. `third_party` type-to-confirm.
4. Provenance display — the simple "last user turn + last external
   content" version, not the full lineage.
5. Audit log entries + Admin filter chip.

Post-MVP:
- Soul-tunable rate limits
- "My grants" revocation page
- Provenance for chained tool calls (beyond just "last external
  content")
- Agent-side: when a tool call fails with "missing capability",
  the agent's next inference turn gets a hint that `request_permission`
  is available for this exact scope. Reduces need for the agent to
  infer the right capability name.
