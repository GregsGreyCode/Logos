# Network approvals: a blocked site asks the agent's person

Story E6. Every agent runs in an OpenShell sandbox whose network policy lets it reach Logos's own
services and nothing else (`infra/agent-image/policy.yaml`). When an agent tries a site that is not
open (a `curl` in its terminal, a Python download), the connection fails as before, and now its
person is asked:

> **Finn wants to reach shop.example.org**
> The site: Not a site Logos knows. `example.org`
> Who is asking: Your chat "Train times to Lyon" · using curl
> Logos says: **Unknown: ask the agent**. Ask the agent what it needs it for before allowing it.
> [Allow once] [Always allow] [Deny] · [Ask the agent why]

What the card says (28 Nov, after three agents asked about `models.dev` at once and the card did not
say enough to judge it):

- **The site**, in one plain line, from a small table of sites Logos knows
  (`network_approvals.SITES`, by registered name: `api.models.dev` is `models.dev`). Any other site
  is "Not a site Logos knows", with its registered name (`registrable()`: `cdn.shop.example.co.uk`
  shows `example.co.uk`) set apart, so a look-alike name is easy to spot.
- **Who is asking**: a chat (its title, on a private agent only; on the household agent just "a chat
  with one of you"), a scheduled job (its name and what it does, from the dashboard's job labels), or
  "the agent software in the background": hermes's own Python with no chat or job running, or a site
  only hermes itself fetches (`SOFTWARE_HOSTS`, e.g. `models.dev`) whatever was running.
- **Logos says**: Needed, Not needed, or Unknown: ask the agent, with one line of why. A "Not needed"
  site makes Deny the main button.
- **Ask the agent why** opens the agent's chat with a short question (which site, when, what for)
  already sent.
- **One question for one site asked by several agents** within 10 minutes (`GROUP_S`): one card
  ("Finn and Household agent want to reach models.dev") and one phone alert per person, not one per
  agent. Each person sees only their own agents in it, and an answer decides only those
  (`decide_group`): the household agent's question goes to both people as before, either can answer
  it, and Greg's answer on the card does not decide Béré's private agent. The audit log still has one
  line per agent.

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

## What hermes fetches by itself: the models.dev catalogue

The 28 Nov questions about `models.dev:443` (chunk binary: hermes's bundled `python3.14`) were not
the agents' doing. hermes's `agent/models_dev.py` downloads `https://models.dev/api.json` (a public
catalogue of AI models, context sizes and prices, about 7 MB) when its disk copy
(`$HERMES_HOME/models_dev_cache.json`) is missing or older than 4 hours, then every 4 hours, with an
ETag `GET`. Its config has a supported mirror setting, `models_dev.url`.

So Logos keeps a copy (`gateway/models_catalogue.py`):

- `scripts/logos-deploy` fetches it **on the host** on every deploy/update (by hand:
  `python3 gateway/models_catalogue.py refresh`), never from a sandbox. It is checked (valid JSON,
  1e5 to 6.4e7 bytes, at least 20 providers and 200 models) and written atomically to
  `~/.logos/models-dev.json`; a failed or odd download keeps the last good copy and never fails the
  deploy.
- The dashboard serves it read-only at `/api/v2/models-dev/api.json` (public data, no login), with
  an ETag and gzip, so hermes's refreshes are a 304.
- `agent_runtime` writes `models_dev.url` pointing there, and the sandbox rule `models_catalogue`
  opens only that path, `GET`, for hermes's Python.

Checked on the dev machine on 28 Nov (the bench agent, a throwaway eval agent, with a copy served on a
spare port via `LOGOS_PORT`): `config.yaml` had the local URL; hermes's own `fetch_models_dev()` in the
sandbox loaded 327 providers / 10,076 models (`GET ... 200`, then `304` on a forced refresh, OCSF
`ALLOWED ... [policy:models_catalogue engine:l7]`); `openshell rule get` showed no rules.

Other things hermes reaches by itself at start (OCSF log, 28 Nov): only Logos's own services. It probes
the model gateway to tell what kind of model server it is (`GET /v1/props` allowed; `/api/v1/models`,
`/api/tags`, `/version` refused at L7, which makes no question), and the noticeboard MCP server
(`HEAD /mcp` refused, then the MCP calls allowed). Update checks and hermes's own model catalogue are
off in its config (`updates.check: false`, `model_catalog.enabled: false`). Nothing reached the
internet and no other draft rule appeared. hermes's code names other sites (its makers' site, GitHub,
OpenRouter, paid AI services) for setup flows Logos does not use; they are in the card's table as
"Not needed" in case one shows up.

## Rolling out

1. Deploy as usual (`scripts/logos-deploy`): the dashboard starts looking at once. No agent needs
   recreating for the questions to appear.
2. Optional: list the household's own domains in `~/.config/logos/network-never.txt` on the server.
3. Recreate the agents when convenient (`scripts/logos-agent up <name>`) so their SOUL.md gets the
   "blocked sites ask" rule.
4. models.dev copy (28 Nov): the deploy fetches the copy and restarts the dashboard (new route); then
   recreate each agent (`scripts/logos-agent up <name>`) so its config points at the copy and its
   policy has the `models_catalogue` rule. Check: `openshell rule get <name>` stays empty after start,
   and `curl -sI http://127.0.0.1:8091/api/v2/models-dev/api.json` answers 200 with an ETag. Deny any
   models.dev question still open from before.
