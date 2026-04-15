# Architecture notes

A living index of architectural decisions, constraints, and open questions.
Each entry captures the **why** behind a design choice, the tradeoffs we
accepted, and what we're still uncertain about — so the reasoning survives
context compactions and onboarding.

When you make a non-trivial design decision, add an entry here. When a
constraint or assumption is invalidated (e.g. OpenShell ships a feature
that changes the tradeoff calculus), update the entry rather than
deleting it.

## Entries

- [Dispatch worker model (Plan A-prime)](./dispatch-worker-model.md) — per-task
  subprocess spawn instead of persistent worker, why it's forced on us by
  `openshell sandbox exec` semantics, and what newer OpenShell versions
  might change.
- [MCP server reachability from sandboxes](./mcp-sandbox-reachability.md) —
  why `http://host.openshell.internal:8091/mcp/<name>` is blocked by
  OpenShell's SSRF layer, and what the OpenShell-aligned options are.

## Style

- Lead with the decision, not the history.
- **Why:** one or two lines naming the constraint that forced the choice.
- **Tradeoffs:** what we gave up, honestly. Don't sell the decision.
- **Confidence:** which claims we can cite vs which are inference. Future
  readers should be able to tell what to verify.
- **Triggers to revisit:** what would make us change the decision.
