# Network approvals: a blocked site asks the agent's person

Story E6. Every agent runs in an OpenShell sandbox whose network policy lets it reach Logos's own
services and nothing else (`infra/agent-image/policy.yaml`). When an agent tries a site that is not
open (a `curl` in its terminal, a Python download), the connection fails as before, and now its
person is asked:

> **Finn wants to reach example.org**
> For: Train times to Lyon · Using curl
> [Allow once] [Always allow] [Deny]

The question is under **Needs you** in Logos Notifications and comes as a phone alert. Only the
agent's own people (`owners` in its definition) can answer; the household agent's questions go to
both people, and either can answer.

| Answer | What happens |
|---|---|
| Allow once | Opened in the running sandbox at once, for an hour, then closed again. Not kept for the next start. |
| Always allow | Opened at once and kept: `logos-agent up` puts it in the agent's policy from then on. |
| Deny | Stays closed. Not asked again for a week; a try after that asks again. |

Answers show in everyone's Notifications (as updates) for the agents they share, and are in the
audit log (`network_once`, `network_always`, `network_deny`, `network_removed`, `network_expired`,
`network_refused`) with the agent, site and port: never what was fetched. The People page (admins)
lists what each agent may reach beyond Logos, with **Remove**; an agent's own people see their
sites on its Agents page under "Can reach" and can remove them too (`POST
/api/v2/network-sites/{agent}/remove`).

## How OpenShell 0.1.8 surfaces a blocked connection

Found on the dev machine on 27 Nov (`openshell sandbox exec dev -- curl https://example.org`):

1. The sandbox proxy refuses it and logs OCSF events (`openshell logs dev`):

   ```
   NET:REFUSE [MED] DENIED example.org [reason:policy_dns_ineligible]
   CONFIG:OBSERVATION Policy DNS staged unapproved name example.org ... for TCP policy review
   NET:OPEN [MED] DENIED /usr/bin/curl(0) -> example.org:443 [reason:transparent_tcp_policy_denied]
   ```

2. The sandbox's denial aggregator (OpenShell's "policy advisor", flushing every 10 s) turns each
   denied `(host, port, binary)` into a **draft rule chunk** on the gateway. The CLI command for it,
   `openshell rule`, is not in `openshell --help` but is there:

   ```
   $ openshell rule get dev
     Chunk: 2bee005c-…
     Status: pending
     Rule: allow_example_org_443
     Binary: /usr/bin/curl
     Endpoints: example.org:443 [L4]
     Hits: 2 (first seen …, last seen …)      # printed once it was refused more than once
   ```

   `rule approve|reject --chunk-id`, `approve-all`, `clear` and `history` go with it. A rejected
   chunk stays rejected; refusing it again only bumps its hit count. 0.1.8 prints text only (no
   `-o json`), so Logos reads the text (`network_approvals.parse_rules`, tested against the real
   output). OpenShell's design note is `architecture/policy-advisor.md` in its source.

3. A running sandbox's policy can be changed without recreating it:
   `openshell policy update <name> --rule-name R --add-endpoint host:port --binary … --wait`
   (and `--remove-rule R`). The sandbox loads the new revision within seconds.

So no log scraping is needed. The OCSF log is the fallback if a later OpenShell drops `rule`.

## How Logos uses it

`gateway/network_approvals.py` (standard library only; `logos-agent up` loads it too) and
`gateway/network_api.py` (the dashboard's routes, the background look and the phone alert).

- Every 15 s the dashboard runs `openshell rule get` for each running agent. A new pending chunk,
  or a known one whose hit count went up, for a site that is not allowed or denied now, becomes a
  question (one per agent and site, whatever the program). What the agent was doing is the title of
  the conversation it was in (on the household agent only a scheduled job's name, never one
  person's conversation).
- Logos **never uses `rule approve`**: that merges OpenShell's proposed rule as it stands, and for a
  name that resolves to a private address the proposal carries an `allowed_ips` override. Logos
  writes its own rule, `logos_net_<host>_<port>`, with no `allowed_ips`, for the hermes Python
  interpreter (whose children match through their ancestors) and the exact program that was
  refused. Every chunk Logos has dealt with is then marked `rejected` in OpenShell with the reason
  ("decided in Logos: always"), so OpenShell's own inbox stays empty and a later try shows as a
  higher hit count.
- Decisions are kept in `~/.config/logos/network-approvals.json` (0600, never in git):
  `always`, `once` (with its end), `deny` (with its end), the open questions and a 30-day log.
  `logos-agent up` merges the `always` sites into the rendered policy (`.rendered/<name>.policy.yaml`).

### What is never let through this way

Checked when the question would be asked and again when it is answered (and by name at `up`):

- addresses (only names), wildcards, single-label names;
- names inside a home or tailnet (`.local`, `.lan`, `.home.arpa`, `.internal`, `.ts.net`, …);
- names that resolve to anything not public: RFC 1918, loopback, link-local, ULA, and 100.64/10
  (Tailscale's range);
- the agent's model server, the household's Matrix server and its people's domains, and anything
  in `~/.config/logos/network-never.txt` (one name per line; put the household's own domains here).

Such a try is not offered: its chunk is rejected and the audit log says why. The sandbox's own proxy
is a second line: a rule without `allowed_ips` never reaches a private address, even if a name is
pointed somewhere private later.

## The agent's side

`souls/_shared/network.md` (every agent with a sandbox): a refused site asks the person; do not
retry in a loop or try another way round; say in one line which site and what for; carry on without
it and try once more later. An agent can be left out with `network_approvals: false` in its
definition (it then neither asks nor gets the rule).

## Notes

- An agent's always-allowed sites take its policy beyond `infra/agent-image/boundary.yaml`; that is
  the point. `openshell-prover check --boundary …` on a rendered policy reports `exceeds_boundary`
  for them; the tracked policy stays within.
- OpenShell 0.1.8 also has `agent_policy_proposals_enabled` / `proposal_approval_mode` sandbox
  settings (agent-written proposals); they are unset and not used.
- Recreating a sandbox gives it a fresh draft inbox; the `always` list is what carries over.

## Rolling out

1. Deploy as usual (`scripts/logos-deploy`): the dashboard starts looking at once. No agent needs
   recreating for the questions to appear.
2. Optional: list the household's own domains in `~/.config/logos/network-never.txt` on the server.
3. Recreate the agents when convenient (`scripts/logos-agent up <name>`) so their SOUL.md gets the
   "blocked sites ask" rule.
