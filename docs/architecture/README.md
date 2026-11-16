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

- [Hermes API client](./hermes-client.md) — the upstream hermes v0.23.1 endpoints and
  SSE events `gateway/hermes_client.py` uses (with upstream file:line), and the open
  question of how Logos reaches port 8642 inside the sandbox.

Notes about the v1 architecture are in [../archive/v1/architecture/](../archive/v1/architecture/).

## Style

- Lead with the decision, not the history.
- **Why:** one or two lines naming the constraint that forced the choice.
- **Tradeoffs:** what we gave up, honestly. Don't sell the decision.
- **Confidence:** which claims we can cite vs which are inference. Future
  readers should be able to tell what to verify.
- **Triggers to revisit:** what would make us change the decision.
