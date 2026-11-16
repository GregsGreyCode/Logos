# Self-Hosted Firecrawl + SearXNG

Free, private web search + scrape stack to replace the Firecrawl cloud API
(`fc-…` key) for agents that shouldn't send queries off-host.

---

## Why

Cloud Firecrawl works today (see `FIRECRAWL_API_KEY` in `~/.logos/.env`)
and gives ~500 free credits/month, but:

- Every search + scrape leaves the network.
- Rate/credit limit eventually kicks in on a multi-agent platform.
- Some agents (privacy-focused, `firecrawl-selfhosted` preset) explicitly
  want zero-egress.

A self-hosted Firecrawl + SearXNG stack solves all three.

---

## Current state (as of 2026-04-14)

- **Cloud Firecrawl**: wired. Key saved in `.env` and in the sandbox-env
  DB credentials (`platform_settings.feature_flags.credentials`). Adam
  confirmed working for `web_search` + `web_extract`.
- **`firecrawl-selfhosted` preset**: exists at
  `gateway/policies/presets/firecrawl-selfhosted.yaml` — opens egress
  to `firecrawl.internal` only.
- **`services.py` env entry**: `FIRECRAWL_API_URL` is already registered
  as the `alt_of` for `FIRECRAWL_API_KEY`; `compose_default` is
  `http://firecrawl-api:3002`.
- **`policies.py` readiness check**: now honours `presets_by_env`, so
  setting `FIRECRAWL_API_URL` auto-selects the `firecrawl-selfhosted`
  preset (fix landed this session, commit TBD).
- **What's missing**: the compose stack itself, a CLI to bring it up,
  a UI toggle, and a search backend (Firecrawl has no built-in search
  engine — it proxies to SERP backends).

---

## Three scoped tiers

### Tier 1 — minimal compose (~80 LOC, ~30 min)

- `docker/docker-compose.firecrawl.yaml` with four services:
  - `firecrawl-api` (Node.js Firecrawl backend)
  - `firecrawl-worker` (BullMQ scrape worker)
  - `firecrawl-playwright` (Chromium for JS rendering)
  - `firecrawl-redis` (BullMQ queue)
- Bring up with `docker compose -f docker/docker-compose.firecrawl.yaml up -d`.
- Expose `firecrawl-api` on host port 3002 (or via the `logos-net` docker
  network so the sandbox can reach it at `http://firecrawl-api:3002`).
- Auto-detect in the gateway: on boot, probe `FIRECRAWL_API_URL` —
  if reachable, apply the `firecrawl-selfhosted` preset to every agent
  that has the `web` toolset and clear any stale `FIRECRAWL_API_KEY`
  from the sandbox-env DB credentials.

**Trade-off:** no search backend → `web_search` returns nothing.
`web_extract` and `crawl` work fully. User wires search themselves later.

### Tier 2 — Tier 1 + SearXNG search (~150 LOC, ~1 hr) — **recommended**

- Adds a `searxng` service to the compose stack, configured for JSON
  output (`BASE_URL`, `INSTANCE_NAME`, `search.formats: [json]`).
- Adapter in `tools/web_tools.py`: when `SEARXNG_URL` is set, `web_search`
  queries SearXNG for result URLs, then uses Firecrawl's `/scrape` on
  each result to get AI-ready markdown. Keeps the existing Firecrawl
  SDK interface — agents don't need to know.
- SearXNG defaults to bang-shortcuts (`!g` = Google, `!ddg` = DuckDuckGo,
  `!w` = Wikipedia) which makes result quality as good as cloud Firecrawl
  for most queries.
- Egress preset `firecrawl-selfhosted` needs one addition:
  `searxng.internal` host allow. Or we keep both services behind a
  single `firecrawl-stack.internal` hostname.

**Trade-off:** two more containers; SearXNG's first boot needs a
generated `instance_id` in `searxng/settings.yml`.

### Tier 3 — Tier 2 + first-class UI (~400 LOC, half day)

- `logos firecrawl up|down|status|logs` CLI subcommand, mirrors the
  existing `logos gateway` verbs. Reads the compose file from the repo's
  `docker/` dir, runs against the `logos-net` network, stores state in
  `~/.logos/firecrawl-state.json`.
- New panel in **Config → Tools → Web search** showing one of three
  states: `Cloud (fc-…)` · `Self-hosted (localhost:3002 ✓)` · `Off`.
  Radio-button switch + "Start the stack" / "Stop the stack" buttons
  that shell into the CLI.
- Setup wizard step 5 ("Tools & egress") gets a **Privacy mode**
  checkbox: "Self-host all web tools (no traffic leaves this host)."
  Checking it runs the compose up at setup time instead of prompting
  for a cloud key.
- Health badges per container (poll `/health` every 15 s, colour-code).
- Gateway-side auto-detect on boot: if the compose stack is already up,
  mark self-hosted in sandbox-env credentials automatically. No manual
  sync needed.

**Trade-off:** biggest surface, best UX. Best built after Tier 2 has
been used for a week so we know which controls actually matter.

---

## Files that need touching (Tier 2 plan)

| File | Change |
| --- | --- |
| `docker/docker-compose.firecrawl.yaml` (new) | 5-service stack (api, worker, playwright, redis, searxng) |
| `docker/searxng-settings.yml` (new) | SearXNG JSON-output config |
| `docker/firecrawl.env.example` (new) | Documented env template |
| `tools/web_tools.py` | `SEARXNG_URL` branch in `web_search_tool`; fetch-and-scrape chain |
| `gateway/services.py` | Add `SEARXNG_URL` to `SERVICE_ENVS` (new entry) |
| `gateway/policies/presets/firecrawl-selfhosted.yaml` | Add `searxng.internal` to egress allow-list |
| `gateway/policies.py` | Register `SEARXNG_URL` in the `presets_by_env` block for `web_search` |
| `docs/user-guide/self_hosted_firecrawl.md` (new) | User-facing setup guide |

---

## Known gotchas to plan for

1. **Playwright RAM** — ~500 MB idle, 1 GB+ under crawl load. On the
   current homelab host (where you already run 10 OpenShell clusters +
   Immich + the \*arr stack) this is a real constraint, not theoretical.
2. **Firecrawl AGPL-3.0** — if the host ever exposes the Firecrawl
   frontend publicly, modifications become public. Internal-only use
   is unaffected. Worth a note in the setup page.
3. **SearXNG rate limits** — some of its upstream search engines
   (notably Google) will 429 SearXNG if queried too fast. SearXNG has
   a rotation of ~30 engines by default so the blast radius is small.
4. **`allowed_hosts` list in instance-config** — the sandbox's
   `allowed_hosts` today includes `api.firecrawl.dev` (cloud). For
   self-hosted it needs `firecrawl.internal` / `firecrawl-api` /
   `searxng.internal` instead. Done via the
   `firecrawl-selfhosted` preset, not a separate code change.
5. **LLM extraction endpoint** — Firecrawl's `/extract` uses an LLM to
   fit scraped content to a schema. Self-hosted needs `OPENAI_BASE_URL`
   pointing at a Logos OpenShell route; we already have these so reuse
   one (e.g. bind to the `claude-sonnet-4-6` route if one's provisioned,
   otherwise `qwen/qwen3-coder-30b`).

---

## Recommendation

Do **Tier 2** first, skip straight to **Tier 3** after two weeks of
real use. Don't stall on the UI design — the underlying system being
fully scriptable (Tier 2) is the thing that unblocks everything else.

---

## TODO when we come back to this

- [ ] Pick a location for the compose file (`docker/` vs top-level `selfhosted/`)
- [ ] Decide: single compose or split (firecrawl + searxng separate)
- [ ] Clone Firecrawl repo to `knowledge-repos/firecrawl` to pin the
      container versions we use
- [ ] Confirm with Greg: which LLM route should `/extract` use by
      default?
- [ ] Write the compose file; test up/down locally
- [ ] Wire the `SEARXNG_URL` env through services.py + policies.py
- [ ] Update `firecrawl-selfhosted` preset to include SearXNG egress
- [ ] Write the `logos firecrawl` CLI subcommand (Tier 3 pre-work —
      the scriptability matters even without the UI)
- [ ] Write the user guide page
