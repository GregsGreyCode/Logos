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
  handshake, the tool list and `search_web` through.

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
