# The web reader

How the person agents search the web without the web reaching them (story W1; reading pages
follows in W2). Designed in the spec doc's Advisor tab.

```
agent (sandbox) --MCP, own token--> logos-reader --private network--> logos-searxng --> Bing, Brave, Wikipedia, (Google)
                                     privacy filter
```

- **Who:** person agents with `web: {enabled: true}` (greg, bere, dev). Never the household
  agent, whatever its definition says.
- **logos-reader** (`gateway/web_service.py`): an MCP server on the Docker bridge, port 8096,
  one tool, `search_web(query, kind)`, `kind` = general, code or news. Read-only container,
  no capabilities, 128 MB. It returns up to 8 results (title, address, snippet), marked as
  information, not instructions.
- **logos-searxng:** SearXNG `2026.11.5` pinned by digest, on the private network `logos-web`
  only (no port on the host), read-only, as its own user. Engines: Google, Bing, Brave and
  Wikipedia for general searches; GitHub and Stack Overflow for code; Bing News for news.
  Google often refuses automated searches ("access denied"); SearXNG then pauses it and the
  others answer.
- **Sandbox rule:** the `web` rule in `infra/agent-image/policy.yaml` lets only the MCP
  handshake, the tool list, `search_web` and `add_private_name` through.
- **Weather:** the reader also serves `GET /weather` on port 8097 for the morning briefing
  (one line from Open-Meteo, cached for an hour; see `docs/briefing.md`). The `weather` rule
  allows only that request.

- **Daily changes (story DC1):** `GET /changes/<repo>/<days>` on the same port 8097, `repo` =
  `logos` or `homelab`, `days` = 1 to 14. Logos: the merged PRs into `v2`, each marked `live`
  (true, false, or null when unknown). The reader reads the commit this machine runs from the
  checkout's `.git` (mounted read-only) and asks GitHub which commits of `v2` are not in it.
  Homelab: the commits on `main` plus merged PRs (Renovate), without listing a PR twice. One
  day carries each PR description (cut to 600 characters); longer periods carry titles only.
  Answers are cached for ten minutes.
  - Only agents with `changes: {enabled: true}` (the household agent) get it, with their own
    token (`LOGOS_CHANGES_TOKEN`) that opens `/changes` and nothing else. Their `changes`
    sandbox rule allows only `GET /changes/**`. Web tokens do not open it.
  - GitHub token: `~/.config/logos/github-changes-token` on the machine (mode 0600), a
    fine-grained token with read access to Contents and Pull requests of `Logos` and
    `homelab-infra` only. `up` copies it into the reader's volume; it never enters a sandbox.
    Without it `/changes` answers 503 and the rest of the reader works as before.

- **Feature requests (story FR1):** the `file_request` tool (kind `feature` | `issue`, area
  `logos` | `homelab` | `other`, title, details) adds a dated entry at the bottom of the
  "Requests from agents" page in Docmost (HomeLab → Logos), as the reader's own Docmost user
  `Logos`. A Claude session moves each into the backlog or Outstanding Fixes and Changes.
  - Every agent of the reader has it. The household agent reaches the reader's MCP port with
    its changes token, and both the reader and its sandbox rule let only `file_request` through.
  - Login and page: `~/.config/logos/docmost` on the machine (mode 0600): `DOCMOST_EMAIL`,
    `DOCMOST_PASSWORD`, `DOCMOST_URL`, `DOCMOST_REQUESTS_PAGE`. `up` copies the login into the
    reader's volume; it never enters a sandbox. Without the file the tool is not offered.
  - At most 20 requests per agent a day; a request that looks like it holds a key is refused.
    The agents' instructions are in `souls/_shared/requests.md`.

- **Homelab docs (story DM1):** `search_docs` (key words), `read_doc` (page id or exact title,
  long pages in parts of about 6,000 characters, part 1 with the headings) and `list_docs` read
  the Docmost pages under `DOCMOST_READ_ROOTS` (page ids in `~/.config/logos/docmost`,
  comma separated: on prod the HomeLab page and Béré's guide), with the same `Logos` login.
  - Read-only: the reader calls only Docmost's page info, page list and search, and drops
    search hits outside those pages, so the rest of the space (to-do lists, plans) stays out.
    Known key formats and `password: …`-style values are replaced before an agent sees them.
  - Every agent of the reader has them; the household agent's sandbox rule lets them through
    with `file_request` and `recent_changes`. The page list is cached for ten minutes.
    Without `DOCMOST_READ_ROOTS` the tools and their instructions (`souls/_shared/docs.md`) are
    not offered. Docs stay in the house: they go only to the agents' local model.

## The privacy filter

Every query is checked before it leaves. Refused, with the reason, so the agent can rephrase:

- names on the private list (below), matched as whole words, ignoring case and accents;
- email addresses, phone numbers, postcodes (in capitals), card and account numbers, sort
  codes, IBANs, National Insurance numbers, Matrix ids, keys and tokens;
- private network addresses and our own addresses (`gregandbere`, the tailnet).

Queries are not logged; the reader logs only which agent searched and how many results came
back, or which rule refused a search.

### The private list

Gathered on every `up` of the web reader:

1. The agents' and people's own names.
2. The title of every page in each brain's `people/` folder.
3. `~/.config/logos/home-names.txt` on the machine (created with instructions on first `up`):
   one name per line for anyone else: family, friends, children, pets, employers, schools,
   street and town. A line starting with `=` matches only exactly as written, for names that
   are also words (`=Rose`).

4. Names the agents added themselves with `add_private_name`, when their person mentions someone
   new who should stay private. Agents can only add (never remove or read the list); the names
   are kept in the `logos-reader-names` volume (`added.txt`: name, agent, when), at most 500.
   To remove one, edit that file:
   `docker run --rm -it -v logos-reader-names:/v alpine:3 vi /v/added.txt`, then restart
   `logos-reader`.

After editing the file, run `scripts/logos-agent up <agent>` for one agent (or redeploy) to
reload it.

## Commands

```shell
venv/bin/python -c "import sys; sys.path.insert(0,'.'); from gateway.agent_runtime import AgentRuntime; from gateway.web_host import WebHost; print(WebHost(AgentRuntime()).status())"
docker logs logos-reader      # who searched, how many results, what was refused (never the words)
docker logs logos-searxng     # engine errors, e.g. Google refusing
```
